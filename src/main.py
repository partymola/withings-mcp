"""Withings MCP server entry point.

Usage:
    withings-mcp          Start the MCP server (stdio transport)
    withings-mcp auth     Interactive OAuth setup
"""

import logging
import sys

# Configure logging to stderr (stdout is reserved for JSON-RPC on stdio)
logging.basicConfig(
    level=logging.INFO,
    format="%(name)s: %(message)s",
    stream=sys.stderr,
)

# Import MCP instance and register all tools
from withings_mcp.mcp_instance import mcp  # noqa: E402
from withings_mcp.tools import sync_tools  # noqa: E402, F401
from withings_mcp.tools import body_tools  # noqa: E402, F401
from withings_mcp.tools import sleep_tools  # noqa: E402, F401
from withings_mcp.tools import activity_tools  # noqa: E402, F401
from withings_mcp.tools import heart_tools  # noqa: E402, F401
from withings_mcp.tools import device_tools  # noqa: E402, F401
from withings_mcp.tools import analysis_tools  # noqa: E402, F401


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        from withings_mcp.auth import setup_auth
        setup_auth()
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
