"""Configuration paths and constants, and how a stored credential is read.

`auth` and `doctor` both judge the token file, and `doctor` may not import `auth`.
"""

import math
import os
from pathlib import Path

# Package root: three levels up from this file (src/withings_mcp/config.py -> withings-mcp/)
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

# Config and data paths (overridable via environment variables)
CONFIG_DIR = Path(os.environ.get("WITHINGS_MCP_CONFIG_DIR", _PACKAGE_ROOT / "config"))
DB_PATH = Path(os.environ.get("WITHINGS_MCP_DB_PATH", _PACKAGE_ROOT / "withings.db"))

# Credential files
WITHINGS_CLIENT_PATH = CONFIG_DIR / "withings_client.json"
WITHINGS_TOKENS_PATH = CONFIG_DIR / "withings_tokens.json"

# Withings API endpoints
WITHINGS_AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
WITHINGS_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
WITHINGS_MEASURE_URL = "https://wbsapi.withings.net/measure"
WITHINGS_MEASURE_V2_URL = "https://wbsapi.withings.net/v2/measure"
WITHINGS_SLEEP_V2_URL = "https://wbsapi.withings.net/v2/sleep"
WITHINGS_HEART_V2_URL = "https://wbsapi.withings.net/v2/heart"
WITHINGS_USER_V2_URL = "https://wbsapi.withings.net/v2/user"

# OAuth
WITHINGS_SCOPES = "user.info,user.metrics,user.activity"
WITHINGS_CALLBACK_PORT = 8585
WITHINGS_REDIRECT_URI = f"http://localhost:{WITHINGS_CALLBACK_PORT}"


def is_token(value) -> bool:
    """Whether this is a string of RFC 6749's VSCHAR characters, printable ASCII, and not blank."""
    return (
        isinstance(value, str) and value.isascii() and value.isprintable() and bool(value.strip())
    )


def token_expiry(tokens) -> float:
    """The stored expiry, or 0 when it is unusable.

    A hand-edited or half-repaired expiry that is not a finite number counts as
    expired, so a refresh decides.
    """
    value = tokens.get("expires_at", 0)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0
    try:
        return value if math.isfinite(value) else 0
    except OverflowError:
        return 0
