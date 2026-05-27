"""Withings MCP server entry point.

Usage:
    withings-mcp              Start the MCP server (stdio transport)
    withings-mcp --version    Print the installed package version
    withings-mcp auth         Interactive OAuth setup
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

    args = parser.parse_args()

    if args.cmd == "auth":
        from withings_mcp.auth import setup_auth

        setup_auth()
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
