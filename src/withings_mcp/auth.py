"""Withings OAuth setup and token management.

Withings auth codes expire in 30 seconds, so the code exchange MUST happen
inside the HTTP callback handler, not after server shutdown.
"""

import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

from .config import (
    CONFIG_DIR, WITHINGS_CLIENT_PATH, WITHINGS_TOKENS_PATH,
    WITHINGS_AUTH_URL, WITHINGS_TOKEN_URL, WITHINGS_SCOPES,
    WITHINGS_CALLBACK_PORT, WITHINGS_REDIRECT_URI,
)

logger = logging.getLogger(__name__)

# In-memory token cache to avoid re-reading JSON files on every API call
_cached_tokens = None
_cached_creds = None


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)


def _load_json(path):
    return json.loads(path.read_text())


def _exchange_code(code, client_id, client_secret):
    """Exchange auth code for tokens. Must complete within 30 seconds."""
    data = urlencode({
        "action": "requesttoken",
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": WITHINGS_REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(WITHINGS_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        return None, f"Network error: {e}"

    if body.get("status") != 0:
        return None, f"Token exchange failed (status {body.get('status')})"

    return body.get("body"), None


def refresh_token() -> str:
    """Return a valid access token, refreshing if expired.

    Checks expiry with a 5-minute buffer. If expired, uses the refresh_token
    grant to obtain new tokens and updates the token file.
    """
    global _cached_tokens, _cached_creds

    if _cached_tokens is None:
        _cached_tokens = _load_json(WITHINGS_TOKENS_PATH)
    if _cached_creds is None:
        _cached_creds = _load_json(WITHINGS_CLIENT_PATH)

    expires_at = _cached_tokens.get("expires_at", 0)
    if time.time() < expires_at - 300:
        return _cached_tokens["access_token"]

    if not _cached_tokens.get("refresh_token"):
        logger.error("Token expired and no refresh token. Run: withings-mcp auth")
        raise RuntimeError("Token expired and no refresh token. Run: withings-mcp auth")

    data = urlencode({
        "action": "requesttoken",
        "grant_type": "refresh_token",
        "client_id": _cached_creds["client_id"],
        "client_secret": _cached_creds["client_secret"],
        "refresh_token": _cached_tokens["refresh_token"],
    }).encode()

    req = urllib.request.Request(WITHINGS_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        logger.error("Token refresh failed: %s", e)
        raise RuntimeError(f"Token refresh failed. Run: withings-mcp auth") from e

    if body.get("status") != 0:
        logger.error("Token refresh returned status %s", body.get("status"))
        raise RuntimeError("Token refresh failed. Run: withings-mcp auth")

    new_tokens = body["body"]
    _cached_tokens = {
        "access_token": new_tokens["access_token"],
        "refresh_token": new_tokens.get("refresh_token", _cached_tokens["refresh_token"]),
        "userid": new_tokens.get("userid", _cached_tokens.get("userid")),
        "expires_at": time.time() + new_tokens.get("expires_in", 10800),
    }
    _save_json(WITHINGS_TOKENS_PATH, _cached_tokens)
    return _cached_tokens["access_token"]


def setup_auth():
    """Interactive OAuth setup. Prompts for credentials, opens browser, exchanges code."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    creds = None
    if WITHINGS_CLIENT_PATH.exists():
        creds = _load_json(WITHINGS_CLIENT_PATH)
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
            tokens, err = _exchange_code(
                code, creds["client_id"], creds["client_secret"]
            )
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
            self.wfile.write(
                f"<html><body><h2>{message}</h2></body></html>".encode()
            )

        def log_message(self, format, *args):
            pass

    auth_url = WITHINGS_AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": creds["client_id"],
        "scope": WITHINGS_SCOPES,
        "redirect_uri": WITHINGS_REDIRECT_URI,
        "state": state,
    })

    print(f"\nOpening browser for Withings auth...")
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
    print(f"  claude mcp add -s user withings -- {sys.executable.replace('/bin/python', '/bin/withings-mcp')}")
