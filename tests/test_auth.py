"""Tests for OAuth token management (auth.py).

Exercises the real refresh_token / _exchange_code logic with the HTTP layer
mocked and time frozen - the 5-minute expiry buffer and its boundary, the
refresh-token grant, the lazy load of cached credentials, refresh-token
preservation, and every error branch. No network, no real OAuth, no secrets.
"""

import json
import os
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
        """A refusal, which must stay distinguishable from a network fault.

        RefreshNetworkError subclasses RuntimeError, so asserting the base
        class alone would pass whichever of the two was raised - and the two
        lead to opposite advice downstream.
        """
        opener = _urlopen_returning({"status": 401})
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(expires_at=_NOW - 1)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            with pytest.raises(RuntimeError) as caught:
                auth.refresh_token()
        assert not isinstance(caught.value, auth.RefreshNetworkError)

    def test_refresh_network_error_raises(self):
        opener = Mock(side_effect=urllib.error.URLError("boom"))
        with (
            _frozen_time(),
            patch.object(auth, "_cached_tokens", _tokens(expires_at=_NOW - 1)),
            patch.object(auth, "_cached_creds", dict(_CREDS)),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            with pytest.raises(auth.RefreshNetworkError):
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


class TestExchangeCodeGuards:
    """The auth code exchange runs inside the callback handler thread.

    Anything escaping it surfaces there as a traceback while the CLI reports
    only a timeout, so every failure has to come back as (None, message).
    """

    def _exchange(self, urlopen):
        with patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            return auth._exchange_code("code", "id", "secret")

    def test_a_read_failure_is_returned_not_raised(self):
        def urlopen(req, timeout=None):
            raise TimeoutError("timed out")

        body, error = self._exchange(urlopen)
        assert body is None
        assert error

    def test_a_truncated_response_is_returned_not_raised(self):
        import http.client

        def urlopen(req, timeout=None):
            raise http.client.IncompleteRead(b"partial")

        body, error = self._exchange(urlopen)
        assert body is None
        assert error

    def test_a_response_that_is_not_json_is_returned_not_raised(self):
        body, error = self._exchange(_urlopen_returning_raw(b"<html>portal</html>"))
        assert body is None
        assert "unreadable" in error

    def test_a_response_of_the_wrong_shape_is_returned_not_raised(self):
        body, error = self._exchange(_urlopen_returning_raw(b'["not", "an", "object"]'))
        assert body is None
        assert error


def _urlopen_returning_raw(payload):
    resp = MagicMock()
    resp.read.return_value = payload
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return Mock(return_value=cm)


class TestTheTokenFileIsNeverExposed:
    """The refresh token must not sit in a readable file, even briefly.

    Writing a temp file at the umask default and chmodding afterwards opened a
    world-readable window on every refresh, and left the temp behind on
    failure. Asserted on the mode at the moment of writing, not just the final
    file, because the final file was 0600 either way.
    """

    def test_the_file_is_never_written_at_a_readable_mode(self, tmp_path, monkeypatch):
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(oct(os.stat(src).st_mode & 0o777))
            return real_replace(src, dst)

        monkeypatch.setattr(auth.os, "replace", spy)
        target = tmp_path / "withings_tokens.json"
        auth._save_json(target, {"refresh_token": "fictional"})

        assert seen == ["0o600"]
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_nothing_is_left_behind_when_the_write_fails(self, tmp_path, monkeypatch):
        def boom(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr(auth.os, "replace", boom)
        target = tmp_path / "withings_tokens.json"
        with pytest.raises(OSError):
            auth._save_json(target, {"refresh_token": "fictional"})

        assert list(tmp_path.iterdir()) == []

    def test_a_reader_never_sees_a_partial_file(self, tmp_path):
        """The point of replacing rather than truncating."""
        target = tmp_path / "withings_tokens.json"
        auth._save_json(target, {"refresh_token": "first"})
        auth._save_json(target, {"refresh_token": "second", "padding": "x" * 10000})

        assert json.loads(target.read_text())["refresh_token"] == "second"

    def test_concurrent_writers_do_not_share_a_temp_path(self, tmp_path, monkeypatch):
        """A fixed temp name lets one writer publish another's half-written file."""
        names = []
        real_mkstemp = auth.tempfile.mkstemp

        def spy(**kwargs):
            fd, name = real_mkstemp(**kwargs)
            names.append(name)
            return fd, name

        monkeypatch.setattr(auth.tempfile, "mkstemp", spy)
        target = tmp_path / "withings_tokens.json"
        auth._save_json(target, {"refresh_token": "a"})
        auth._save_json(target, {"refresh_token": "b"})

        assert len(set(names)) == 2
