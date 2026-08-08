"""Withings API client with automatic token refresh.

Withings API quirk: HTTP status is always 200. Errors are in the JSON
response body under the 'status' field (0 = success).
"""

import http.client
import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .auth import RefreshNetworkError, refresh_token

logger = logging.getLogger(__name__)


class WithingsAuthError(Exception):
    """Token expired or invalid, re-auth needed."""


class WithingsRateLimitError(Exception):
    """Rate limited (status 601/602). Retry after delay."""


class WithingsAPIError(Exception):
    """General API error."""


def post(url: str, params: dict, retries: int = 2) -> dict:
    """Make an authenticated POST to the Withings API.

    Handles:
    - Automatic token refresh before each call (5-min buffer)
    - Status-in-body error detection (HTTP always returns 200)
    - 401: refresh token and retry once
    - 601/602: raise WithingsRateLimitError with recovery guidance
    - Other non-zero status: raise WithingsAPIError

    Returns the 'body' field from the response.
    """
    for attempt in range(retries):
        # refresh_token reports its documented failures with builtins, which
        # run_sync does not classify; translated here, at its only call site.
        # The messages are fixed rather than built from the original, which can
        # carry a path or file content into the sync log.
        try:
            token = refresh_token()
        except RefreshNetworkError as e:
            # Not an auth failure: an unreachable server says nothing about the
            # credentials, and advising re-auth would rotate a token file
            # another host may own.
            raise WithingsAPIError("Network error. Check your connection.") from e
        except (RuntimeError, OSError, ValueError, KeyError) as e:
            raise WithingsAuthError(
                "Could not obtain an access token. Run: withings-mcp auth"
            ) from e

        data = urlencode(params).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "withings-mcp/0.1",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
        except (OSError, http.client.HTTPException, UnicodeDecodeError) as e:
            # Wider than URLError: a read timeout or reset arrives bare, a
            # truncated response raises from http.client (not an OSError at
            # all), and an undecodable body raises ValueError. Any of them
            # escaping here would leave run_sync with no log row and an
            # unclosed connection.
            raise WithingsAPIError("Network error. Check your connection.") from e

        status = body.get("status")

        if status == 0:
            return body.get("body", {})

        if status == 401:
            # Token expired - force refresh and retry
            logger.info("Token expired (401), refreshing")
            from . import auth

            auth._cached_tokens = None
            continue

        if status in (601, 602):
            raise WithingsRateLimitError(
                "Rate limited by Withings API. Retry in 60 seconds or reduce request frequency."
            )

        raise WithingsAPIError(f"Withings API error (status {status}).")

    raise WithingsAuthError("Authentication failed after retry. Run: withings-mcp auth")
