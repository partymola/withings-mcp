"""Withings MCP server entry point.

Usage:
    withings-mcp              Start the MCP server (stdio transport)
    withings-mcp --version    Print the installed package version
    withings-mcp auth         Interactive OAuth setup
    withings-mcp sync         Sync data to local cache
    withings-mcp doctor       Check the setup and report what needs fixing
"""

import argparse
import logging
import sys
from importlib.metadata import version

# Configure logging to stderr (stdout is reserved for JSON-RPC on stdio)
logging.basicConfig(
    level=logging.INFO,
    format="%(name)s: %(message)s",
    stream=sys.stderr,
)

# Import MCP instance and register all tools
from withings_mcp.mcp_instance import mcp  # noqa: E402
from withings_mcp.tools import (
    activity_tools,  # noqa: E402, F401
    analysis_tools,  # noqa: E402, F401
    body_tools,  # noqa: E402, F401
    device_tools,  # noqa: E402, F401
    heart_tools,  # noqa: E402, F401
    sleep_tools,  # noqa: E402, F401
    sync_tools,  # noqa: E402, F401
)


def _version_text():
    return f"withings-mcp {version('withings-mcp')}"


def _add_version_argument(parser):
    parser.add_argument("--version", action="version", version=_version_text())


def main():
    if len(sys.argv) == 1:
        mcp.run(transport="stdio")
        return

    parser = argparse.ArgumentParser(
        prog="withings-mcp",
        description="Withings MCP server - serves Withings health data via MCP.",
    )
    _add_version_argument(parser)
    subparsers = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    auth_parser = subparsers.add_parser("auth", help="Interactive OAuth setup")
    _add_version_argument(auth_parser)

    doctor_parser = subparsers.add_parser(
        "doctor", help="Check the setup and report what needs fixing"
    )
    _add_version_argument(doctor_parser)

    sync_parser = subparsers.add_parser("sync", help="Sync Withings data to local SQLite cache")
    _add_version_argument(sync_parser)
    sync_parser.add_argument(
        "--days", type=int, default=30, help="Days of history for first sync (default: 30)"
    )
    sync_parser.add_argument(
        "--types",
        default="all",
        help="Comma-separated data types: all, body, sleep, activity, workouts",
    )

    args = parser.parse_args()

    if args.cmd == "auth":
        from withings_mcp.auth import setup_auth

        setup_auth()
    elif args.cmd == "doctor":
        from withings_mcp.doctor import run_doctor

        sys.exit(run_doctor())
    elif args.cmd == "sync":
        types = [t.strip() for t in args.types.split(",")]
        if "all" in types:
            types = ["body", "sleep", "activity", "workouts"]

        print(f"Syncing: {', '.join(types)}")
        results = sync_tools.run_sync(types, args.days)
        for dtype, result in results.items():
            status = result.get("status", "?")
            if status == "ok":
                print(f"  {dtype}: {result.get('records', 0)} records ({result.get('range', '')})")
            else:
                print(f"  {dtype}: {status} - {result.get('message', '')}")

        # Exit non-zero if any type failed in a way that needs attention, so a
        # cron/systemd wrapper marks the run failed. rate_limited is transient
        # and self-heals on the next run, so it is not treated as a failure.
        failed = [d for d, r in results.items() if r.get("status") in ("auth_error", "error")]
        if failed:
            print(f"Sync failed for: {', '.join(failed)}", file=sys.stderr)
            sys.exit(1)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
