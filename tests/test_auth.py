"""Tests for OAuth token management (auth.py).

Exercises the real refresh_token / _exchange_code logic with the HTTP layer
mocked and time frozen - the 5-minute expiry buffer and its boundary, the
refresh-token grant, the lazy load of cached credentials, refresh-token
preservation, and every error branch. No network, no real OAuth, no secrets.
"""

import json
import urllib.error
from unittest.mock import MagicMock, Mock, call, patch
from urllib.parse import parse_qs

import pytest

from withings_mcp import auth


def _sent_form(opener):
    """Decode the urlencoded POST body from a mocked urlopen call."""
    request = opener.call_args.args[0]
    return {k: v[0] for k, v in parse_qs(request.data.decode()).items()}


_NOW = 1_700_000_000.0  # fixed clock for deterministic buffer maths


def _urlopen_returning(payload):
    """A urlopen replacement whose context manager yields the given JSON body."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return Mock(return_value=cm)


def _frozen_time():
    return patch.object(auth.time, "time", return_value=_NOW)


def _tokens(access="tok-old", refresh="ref-old", expires_at=_NOW + 10_000, userid=7):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "userid": userid,
    }


_CREDS = {"client_id": "client-xyz", "client_secret": "secret-xyz"}


@pytest.fixture(autouse=True)
def _isolate_auth(tmp_path):
    """Reset the module token caches and redirect the token file off the real one.

    _save_json is mocked in every refresh test, but pointing the paths at a temp
    dir as well guarantees no test can ever touch the production credential files.
    """
    with (
        patch.object(auth, "_cached_tokens", None),
        patch.object(auth, "_cached_creds", None),
        patch.object(auth, "WITHINGS_TOKENS_PATH", tmp_path / "withings_tokens.json"),
        patch.object(auth, "WITHINGS_CLIENT_PATH", tmp_path / "withings_client.json"),
    ):
        yield


class TestRefreshToken:
    def test_returns_cached_token_when_fresh(self):
        opener = Mock()
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(expires_at=_NOW + 10_000)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            assert auth.refresh_token() == "tok-old"
        opener.assert_not_called()  # no HTTP when the token is comfortably valid

    def test_returns_cached_just_outside_buffer(self):
        # expires_at is 301s away: time.time() < expires_at - 300 holds -> cached.
        opener = Mock()
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(expires_at=_NOW + 301)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            assert auth.refresh_token() == "tok-old"
        opener.assert_not_called()

    def test_refreshes_at_buffer_boundary(self):
        # expires_at exactly 300s away: the < comparison fails -> refresh fires.
        body = {"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 10800}
        opener = _urlopen_returning({"status": 0, "body": body})
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(expires_at=_NOW + 300)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch.object(auth, "_save_json"),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            assert auth.refresh_token() == "tok-new"
        opener.assert_called_once()

    def test_refresh_updates_cache_and_persists(self):
        body = {
            "access_token": "tok-new",
            "refresh_token": "ref-new",
            "userid": 99,
            "expires_in": 10800,
        }
        opener = _urlopen_returning({"status": 0, "body": body})
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(expires_at=_NOW - 1)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch.object(auth, "_save_json") as save,
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            auth.refresh_token()
            assert auth._cached_tokens["access_token"] == "tok-new"
            assert auth._cached_tokens["refresh_token"] == "ref-new"
            assert auth._cached_tokens["userid"] == 99
            # expires_at is stamped from the frozen clock plus expires_in.
            assert auth._cached_tokens["expires_at"] == _NOW + 10800
            # The outgoing grant carries the refresh_token flow with the old token.
            sent = _sent_form(opener)
            assert sent["grant_type"] == "refresh_token"
            assert sent["refresh_token"] == "ref-old"
            assert sent["client_id"] == "client-xyz"
            # The updated cache is what gets persisted, to the token path.
            save.assert_called_once_with(auth.WITHINGS_TOKENS_PATH, auth._cached_tokens)

    def test_refresh_writes_token_file_to_disk(self):
        # Exercise the real _save_json (write + chmod 600); paths point at tmp_path.
        body = {"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 10800}
        opener = _urlopen_returning({"status": 0, "body": body})
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(expires_at=_NOW - 1)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            auth.refresh_token()
            written = json.loads(auth.WITHINGS_TOKENS_PATH.read_text())
            mode = auth.WITHINGS_TOKENS_PATH.stat().st_mode & 0o777
        assert written["access_token"] == "tok-new"
        assert written["refresh_token"] == "ref-new"
        assert written["expires_at"] == _NOW + 10800
        assert mode == 0o600  # owner-only, no group/other access

    def test_refresh_preserves_old_refresh_token_when_omitted(self):
        # A refresh response without a new refresh_token keeps the old one.
        body = {"access_token": "tok-new", "expires_in": 10800}
        opener = _urlopen_returning({"status": 0, "body": body})
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(refresh="ref-keep", expires_at=_NOW - 1)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch.object(auth, "_save_json"),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            auth.refresh_token()
            assert auth._cached_tokens["refresh_token"] == "ref-keep"
            assert auth._cached_tokens["userid"] == 7  # old userid preserved too

    def test_lazy_loads_cached_credentials_from_disk(self):
        # With both caches None, refresh_token loads tokens then creds via _load_json.
        opener = Mock()
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", None),
            patch.object(auth, "_cached_creds", None),
            patch.object(
                auth, "_load_json", side_effect=[_tokens(expires_at=_NOW + 10_000), dict(_CREDS)]
            ) as load,
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            assert auth.refresh_token() == "tok-old"
        # tokens are loaded from the token path, creds from the client path, in order.
        assert load.call_args_list == [
            call(auth.WITHINGS_TOKENS_PATH),
            call(auth.WITHINGS_CLIENT_PATH),
        ]
        opener.assert_not_called()

    def test_expired_without_refresh_token_raises(self):
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(refresh="", expires_at=_NOW - 1)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
        ):
            with pytest.raises(RuntimeError):
                auth.refresh_token()

    def test_refresh_nonzero_status_raises(self):
        opener = _urlopen_returning({"status": 401})
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(expires_at=_NOW - 1)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            with pytest.raises(RuntimeError):
                auth.refresh_token()

    def test_refresh_network_error_raises(self):
        opener = Mock(side_effect=urllib.error.URLError("boom"))
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(expires_at=_NOW - 1)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            with pytest.raises(RuntimeError):
                auth.refresh_token()


class TestExchangeCode:
    def test_success_returns_body_and_no_error(self):
        token_body = {"access_token": "tok-x", "refresh_token": "ref-x", "userid": 3}
        opener = _urlopen_returning({"status": 0, "body": token_body})
        with patch("withings_mcp.auth.urllib.request.urlopen", opener):
            body, err = auth._exchange_code("the-code", "cid", "csecret")
        assert err is None
        assert body == token_body
        # The exchange sends the authorization_code grant with the code + redirect.
        sent = _sent_form(opener)
        assert sent["grant_type"] == "authorization_code"
        assert sent["code"] == "the-code"
        assert sent["client_id"] == "cid"
        assert sent["redirect_uri"] == auth.WITHINGS_REDIRECT_URI

    def test_nonzero_status_returns_error(self):
        opener = _urlopen_returning({"status": 214})
        with patch("withings_mcp.auth.urllib.request.urlopen", opener):
            body, err = auth._exchange_code("the-code", "cid", "csecret")
        assert body is None
        assert "214" in err

    def test_network_error_returns_message(self):
        opener = Mock(side_effect=urllib.error.URLError("down"))
        with patch("withings_mcp.auth.urllib.request.urlopen", opener):
            body, err = auth._exchange_code("the-code", "cid", "csecret")
        assert body is None
        assert err.startswith("Network error")
