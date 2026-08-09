"""Withings OAuth setup and token management.

Withings auth codes expire in 30 seconds, so the code exchange MUST happen
inside the HTTP callback handler, not after server shutdown.
"""

import http.client
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from .config import (
    CONFIG_DIR,
    WITHINGS_AUTH_URL,
    WITHINGS_CALLBACK_PORT,
    WITHINGS_CLIENT_PATH,
    WITHINGS_REDIRECT_URI,
    WITHINGS_SCOPES,
    WITHINGS_TOKEN_URL,
    WITHINGS_TOKENS_PATH,
)

logger = logging.getLogger(__name__)


# RFC 6749 defines the token endpoint's refusals as 400, with 401 for a bad
# client. 403 is deliberately absent: a WAF or bot-protection block returns it
# with no opinion about the grant.
_REFUSAL_CODES = frozenset({400, 401})

# In-body statuses that mean the credentials themselves were refused. Kept as
# an allow-list because the default has to be the other way round: grading a
# transient server condition as an auth failure tells the user to re-authorise,
# which rewrites a token file other hosts share, while the opposite mistake
# only under-advises. api.post takes the same view of an unknown status for a
# data request, defaulting it to WithingsAPIError.
_REFUSAL_STATUSES = frozenset({342, 401})


class TokenRefused(RuntimeError):
    """The server judged the credentials and rejected them.

    The only failure that warrants telling the user to re-authorise, which
    rewrites a token file other hosts may share.
    """


class RefreshNetworkError(RuntimeError):
    """The refresh request never got an answer.

    Subclasses RuntimeError so existing callers are unaffected, but is
    distinguishable: an unreachable server says nothing about whether the
    credentials are still good, and telling the user to re-authorise would
    rotate a token file another host may own.
    """


# In-memory token cache to avoid re-reading JSON files on every API call
_cached_tokens = None
_cached_creds = None


def _save_json(path, data):
    """Replace the file in one step, and never leave it readable in between.

    `os.replace` is atomic within a directory, so a reader sees either the
    old file or the new one - a property of the filesystem, not of whatever
    syncs the directory to another host. mkstemp rather than a named sibling
    because it creates the file 0600 before anything is written and its name
    is unique, so concurrent writers cannot interleave through a shared path.
    A kill between the write and the rename can still leave a temp file, at
    0600.

    The 0600 applies on POSIX; Windows ignores the mode and governs access by
    inherited ACLs. The atomic replace holds on both.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _load_json(path):
    """Read a credential file as a dict, or say why the credentials are unusable.

    Classified here rather than left to the caller: a file that is absent,
    unreadable or not a JSON object means there are no usable credentials, and
    re-authorising is the remedy. _save_json writes atomically, so a reader
    cannot see a half-written file and mistake it for a malformed one.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise TokenRefused(f"{path.name} is missing or unreadable. Run: withings-mcp auth") from e
    if not isinstance(data, dict):
        raise TokenRefused(f"{path.name} is malformed. Run: withings-mcp auth")
    return data


def _exchange_code(code, client_id, client_secret):
    """Exchange auth code for tokens. Must complete within 30 seconds."""
    data = urlencode(
        {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": WITHINGS_REDIRECT_URI,
        }
    ).encode()

    req = urllib.request.Request(WITHINGS_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
    except (OSError, http.client.HTTPException, UnicodeDecodeError) as e:
        # Wider than URLError: the body read happens after urlopen returns, so
        # a stalled connection, a truncated response or an undecodable body all
        # arrive unwrapped and would otherwise escape into the callback handler
        # thread as a traceback.
        return None, f"Network error ({type(e).__name__})"

    try:
        body = json.loads(raw)
    except ValueError:
        return None, "Token exchange failed (unreadable response)"

    if not isinstance(body, dict):
        return None, "Token exchange failed (unexpected response shape)"

    if body.get("status") != 0:
        # Same rule as api.py: the status came from the response body, so only
        # a recognisable code is repeated back.
        status = body.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            return None, f"Token exchange failed (status {status})"
        return None, "Token exchange failed (unrecognised status)"

    payload = body.get("body")
    if not isinstance(payload, dict):
        return None, "Token exchange failed (unexpected response shape)"

    return payload, None


def _refresh_token() -> str:
    """Return a valid access token, refreshing if expired.

    Checks expiry with a 5-minute buffer. If expired, uses the refresh_token
    grant to obtain new tokens and updates the token file.
    """
    global _cached_tokens, _cached_creds

    if _cached_tokens is None:
        _cached_tokens = _load_json(WITHINGS_TOKENS_PATH)
    if _cached_creds is None:
        _cached_creds = _load_json(WITHINGS_CLIENT_PATH)

    # A non-numeric expiry - hand-edited, or half-repaired - counts as expired
    # rather than raising on the comparison, so the refresh below decides.
    expires_at = _cached_tokens.get("expires_at", 0)
    if not isinstance(expires_at, (int, float)):
        expires_at = 0
    if time.time() < expires_at - 300:
        return _cached_tokens["access_token"]

    if not _cached_tokens.get("refresh_token"):
        logger.error("Token expired and no refresh token. Run: withings-mcp auth")
        raise TokenRefused("Token expired and no refresh token. Run: withings-mcp auth")

    data = urlencode(
        {
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": _cached_creds["client_id"],
            "client_secret": _cached_creds["client_secret"],
            "refresh_token": _cached_tokens["refresh_token"],
        }
    ).encode()

    req = urllib.request.Request(WITHINGS_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        # Checked before OSError, which it subclasses. Listed by what the code
        # says about the credentials rather than by range: RFC 6749 refuses a
        # bad grant with 400/401/403, while 408, 425 and 429 are 4xx that carry
        # no judgement at all. Answering a rate limit with "re-authorise" turns
        # a condition that clears itself into a rotated token file.
        if e.code in _REFUSAL_CODES:
            logger.error("Token refresh refused with HTTP %s", e.code)
            raise TokenRefused("Token refresh failed. Run: withings-mcp auth") from e
        logger.error("Token refresh got HTTP %s from the server", e.code)
        raise RefreshNetworkError("Withings could not answer the refresh request.") from e
    except (OSError, http.client.HTTPException, UnicodeDecodeError) as e:
        # Wider than URLError on three counts: urlopen wraps only connect-phase
        # failures in it, so a read timeout or reset arrives bare; a truncated
        # or malformed response raises from http.client, which is not an
        # OSError at all; and a body that will not decode raises ValueError,
        # which the caller would otherwise read as a bad credential.
        logger.error("Token refresh could not reach the server")
        raise RefreshNetworkError("Could not reach Withings to refresh the token.") from e

    try:
        body = json.loads(raw)
    except ValueError as e:
        # A proxy or captive portal answering 200 with HTML is a network
        # condition, not a rejected credential.
        logger.error("Token refresh got a response that is not JSON")
        raise RefreshNetworkError("Withings returned an unreadable response.") from e

    if not isinstance(body, dict):
        # An intermediary that replaced the body says nothing about the
        # credentials, whether what it substituted parses as JSON or not.
        logger.error("Token refresh returned an unexpected response shape")
        raise RefreshNetworkError("Withings returned an unexpected response shape.")

    # Withings answers over HTTP 200 with the real outcome in the body, so this
    # is the live path and the HTTP codes above are the rare one.
    status = body.get("status")
    if status != 0:
        if status in _REFUSAL_STATUSES:
            logger.error("Token refresh refused with status %s", status)
            raise TokenRefused("Token refresh failed. Run: withings-mcp auth")
        logger.error("Token refresh returned status %s", status)
        raise RefreshNetworkError("Withings could not complete the refresh.")

    new_tokens = body.get("body")
    if not isinstance(new_tokens, dict):
        logger.error("Token refresh returned no token payload")
        raise TokenRefused("Token refresh failed. Run: withings-mcp auth")
    _cached_tokens = {
        "access_token": new_tokens["access_token"],
        "refresh_token": new_tokens.get("refresh_token", _cached_tokens["refresh_token"]),
        "userid": new_tokens.get("userid", _cached_tokens.get("userid")),
        "expires_at": time.time() + new_tokens.get("expires_in", 10800),
    }
    _save_json(WITHINGS_TOKENS_PATH, _cached_tokens)
    return _cached_tokens["access_token"]


def refresh_token() -> str:
    """Return a valid access token, refreshing if expired.

    The boundary that classifies every way obtaining a token can fail. Only
    TokenRefused means the credentials were rejected; everything else becomes
    RefreshNetworkError, by construction rather than by listing the exception
    types that happen to occur. Enumerating them is what went wrong before:
    each round of fixes found another type nobody had thought of - a bare
    OSError, an http.client exception that is not an OSError at all, a decode
    failure that is a ValueError - and each was graded a dead credential.

    A bug inside the refresh therefore reports as a network failure rather
    than a refusal. That is the safe direction: it is still recorded and still
    visible, and it does not tell anyone to rotate a shared token file.
    """
    try:
        return _refresh_token()
    except (TokenRefused, RefreshNetworkError):
        raise
    except Exception as e:
        logger.error("Token refresh failed: %s", type(e).__name__)
        raise RefreshNetworkError("Could not obtain a token from Withings.") from e


def setup_auth():
    """Interactive OAuth setup. Prompts for credentials, opens browser, exchanges code."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    creds = None
    if WITHINGS_CLIENT_PATH.exists():
        try:
            creds = _load_json(WITHINGS_CLIENT_PATH)
        except TokenRefused:
            # _load_json's advice is "run withings-mcp auth", which is what is
            # running. Say the thing that actually helps instead, and fall
            # through to the prompts rather than reading from the empty result.
            print(f"{WITHINGS_CLIENT_PATH} is unreadable; setting up from scratch.")

    def _usable(value):
        return isinstance(value, str) and value

    if creds and not (_usable(creds.get("client_id")) and _usable(creds.get("client_secret"))):
        print(f"{WITHINGS_CLIENT_PATH} is incomplete; setting up from scratch.")
        creds = None

    if creds:
        print(f"Existing client_id: {creds['client_id'][:12]}...")
        resp = input("Re-use existing credentials? [Y/n] ").strip().lower()
        if resp in ("n", "no"):
            creds = None

    if not creds:
        print("Register an app at https://developer.withings.com/dashboard")
        print(f"Set callback URL to: {WITHINGS_REDIRECT_URI}")
        print(f"Set scopes to: {WITHINGS_SCOPES}")
        client_id = input("Client ID: ").strip()
        client_secret = input("Client secret: ").strip()
        if not client_id or not client_secret:
            print("Error: both client_id and client_secret required.", file=sys.stderr)
            sys.exit(1)
        creds = {"client_id": client_id, "client_secret": client_secret}
        _save_json(WITHINGS_CLIENT_PATH, creds)
        print("Credentials saved.")

    state = secrets.token_urlsafe(32)

    # Withings auth codes expire in 30 seconds. The code exchange MUST happen
    # inside the HTTP callback handler, not after the server shuts down.
    auth_result = {"tokens": None, "error": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)

            recv_state = qs.get("state", [None])[0]
            if recv_state != state:
                self._respond(400, "State mismatch - possible CSRF attack.")
                auth_result["error"] = "State mismatch"
                return

            code = qs.get("code", [None])[0]
            if not code:
                error = qs.get("error", ["unknown"])[0]
                self._respond(400, f"Error: {error}")
                auth_result["error"] = error
                return

            # Exchange immediately (30-second window)
            tokens, err = _exchange_code(code, creds["client_id"], creds["client_secret"])
            if err:
                self._respond(500, f"Token exchange failed: {err}")
                auth_result["error"] = err
            else:
                self._respond(200, "Authorised! You can close this tab.")
                auth_result["tokens"] = tokens

        def _respond(self, status_code, message):
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><body><h2>{message}</h2></body></html>".encode())

        def log_message(self, format, *args):
            pass

    auth_url = (
        WITHINGS_AUTH_URL
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": creds["client_id"],
                "scope": WITHINGS_SCOPES,
                "redirect_uri": WITHINGS_REDIRECT_URI,
                "state": state,
            }
        )
    )

    print("\nOpening browser for Withings auth...")
    print(f"If it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", WITHINGS_CALLBACK_PORT), CallbackHandler)
    # Use a thread with timeout so we don't hang forever
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=120)
    server.server_close()

    if auth_result["error"]:
        print(f"Authorisation failed: {auth_result['error']}", file=sys.stderr)
        sys.exit(1)

    if not auth_result["tokens"]:
        print("No response received. Timed out or denied.", file=sys.stderr)
        sys.exit(1)

    tokens = auth_result["tokens"]
    token_store = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "userid": tokens.get("userid"),
        "expires_at": time.time() + tokens.get("expires_in", 10800),
    }
    _save_json(WITHINGS_TOKENS_PATH, token_store)
    print(f"Tokens saved. User ID: {tokens.get('userid')}")
    print("\nSetup complete. Register with Claude Code:")
    command = sys.executable.replace("/bin/python", "/bin/withings-mcp")
    print(f"  claude mcp add -s user withings -- {command}")
