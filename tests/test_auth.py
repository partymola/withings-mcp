"""Tests for OAuth token management (auth.py).

Exercises the real auth logic - no network, no real OAuth, no secrets.
refresh_token runs against a mocked HTTP layer and a frozen clock: the
5-minute expiry buffer and its boundary, the refresh-token grant, the lazy
load of cached credentials, refresh-token preservation, and every error
branch. _exchange_code covers the same transport failures plus the guards
that keep one from escaping into the callback handler thread. The rest is
offline: how the token file is written and replaced, and the client-file
shapes that must reach the setup prompts rather than the browser.
"""

import json
import os
import sys
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
        # Exercise the real _save_json (mkstemp at 0600, then atomic replace);
        # paths point at tmp_path.
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
        # Only the mode assertion is POSIX-only; the write above holds anywhere,
        # so guarding it here rather than skipping the whole test.
        if sys.platform != "win32":
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

    def test_a_transport_failure_reports_its_type_not_its_text(self):
        """This reaches stderr and the local HTML page.

        A TLS failure's own text is a filesystem path; a decode failure's is
        response bytes.
        """

        def urlopen(req, timeout=None):
            raise OSError(2, "No such file", "/home/someone/certs/ca.pem")

        body, error = self._exchange(urlopen)
        assert body is None
        assert "/home/someone" not in error
        # OSError(2, ...) constructs a FileNotFoundError.
        assert "FileNotFoundError" in error

    def test_a_status_that_is_not_a_code_is_not_quoted_back(self):
        """Same rule as api.py: the status came from the response body."""

        def urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {"status": "SESSION=abcdef; weight=72.5kg"}
            ).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        body, error = self._exchange(urlopen)
        assert body is None
        assert "SESSION" not in error
        assert "72.5" not in error

    def test_a_real_status_code_is_still_reported(self):
        def urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps({"status": 214}).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            return resp

        body, error = self._exchange(urlopen)
        assert body is None
        assert "214" in error

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

    def test_a_null_inner_body_is_returned_not_raised(self):
        """The envelope parsed and the payload did not - previously unguarded."""
        body, error = self._exchange(_urlopen_returning_raw(b'{"status": 0, "body": null}'))
        assert body is None
        assert error

    def test_an_inner_body_of_the_wrong_shape_is_returned_not_raised(self):
        body, error = self._exchange(_urlopen_returning_raw(b'{"status": 0, "body": []}'))
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

    The mode is asserted at the moment of writing, not only on the final
    file, which is 0600 whether or not there was a readable window. Only the
    mode-bit test is POSIX-only; the rest hold anywhere.
    """

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; Windows uses ACLs")
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

    def test_the_last_write_wins_intact(self, tmp_path):
        """Not an atomicity test: a single-threaded run cannot observe the gap.

        Atomicity here rests on os.replace's semantics, not on this. What it
        does pin is that replacing does not corrupt or truncate the result.
        """
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


class TestSetupAuthSurvivesABrokenClientFile:
    """Any client file that is not usable must reach the prompts.

    Four shapes depend on this guard, each stopping somewhere different
    without it. A file with no client_id at all dies at the lookup in the
    re-use branch; one whose client_id is the wrong type dies at the slice on
    the same line. Both surface as an uncaught traceback. A file with an id
    but no secret reaches the browser and the callback server, and raises
    inside the handler thread if the callback carries an authorisation code.
    One whose values are strings but empty reaches them too and raises
    nothing. Those last two end at the 120-second join, which is what
    surfaces as "timed out or denied".

    The other four shapes in the list below reach the prompts without it: an
    absent, unparseable or non-object file leaves no credentials to re-use,
    and an empty object is falsy.
    """

    def _run_with_client_file(self, contents, tmp_path, monkeypatch):
        client = tmp_path / "withings_client.json"
        if contents is not None:
            client.write_text(contents)
        monkeypatch.setattr(auth, "WITHINGS_CLIENT_PATH", client)
        monkeypatch.setattr(auth, "WITHINGS_TOKENS_PATH", tmp_path / "withings_tokens.json")
        monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)

        # Blocked outright rather than relied on being unreachable: the path
        # under test is precisely the one that skips the prompts, so a
        # regression here would otherwise open a browser and hold the socket
        # for the full two-minute timeout.
        def no_browser(url):
            raise AssertionError("setup_auth reached the browser")

        def no_server(*args, **kwargs):
            raise AssertionError("setup_auth reached the callback socket")

        monkeypatch.setattr(auth.webbrowser, "open", no_browser)
        monkeypatch.setattr(auth, "HTTPServer", no_server)

        prompts = []

        def fake_input(prompt):
            prompts.append(prompt)
            raise KeyboardInterrupt  # Stop before anything else runs.

        monkeypatch.setattr("builtins.input", fake_input)
        with pytest.raises(KeyboardInterrupt):
            auth.setup_auth()
        return prompts

    @pytest.mark.parametrize(
        "contents",
        [
            None,
            "{not json",
            '["not", "an", "object"]',
            "{}",
            '{"note": "hi"}',
            '{"client_id": "abc"}',
            '{"client_id": 12345, "client_secret": "x"}',
            '{"client_id": "", "client_secret": "x"}',
        ],
        ids=[
            "absent",
            "unparseable",
            "wrong-shape",
            "empty",
            "no-id",
            "id-without-secret",
            "id-not-a-string",
            "id-empty-string",
        ],
    )
    def test_it_reaches_the_prompts_rather_than_raising(self, contents, tmp_path, monkeypatch):
        prompts = self._run_with_client_file(contents, tmp_path, monkeypatch)
        assert prompts, "setup_auth never reached a prompt"
        assert "Client ID" in prompts[0]

    def test_a_usable_client_file_still_offers_re_use(self, tmp_path, monkeypatch):
        prompts = self._run_with_client_file(
            '{"client_id": "abc", "client_secret": "def"}', tmp_path, monkeypatch
        )
        assert "Re-use" in prompts[0]


class TestTheCallbackPageEscapesWhatItShows:
    """One of the four callers builds its message from a query parameter.

    Escaped at the sink rather than at that caller, so a fifth caller does
    not have to remember. The state check runs first, so whoever supplies
    the parameter knows the per-run token - which does not rule out
    Withings itself, or an intermediary in the redirect chain, since a
    denial legitimately comes back with the correct state.
    """

    def test_a_script_tag_is_escaped(self):
        page = auth._callback_page('Error: <script>alert("x")</script>')
        assert "<script>" not in page
        assert "&lt;script&gt;" in page

    def test_an_ordinary_message_still_reads_normally(self):
        assert "Authorised! You can close this tab." in auth._callback_page(
            "Authorised! You can close this tab."
        )

    def test_the_handler_builds_its_page_through_the_helper(self):
        """The helper being correct is no use if _respond stops calling it."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth.setup_auth))
        writes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "wfile"
        ]
        assert writes, "no wfile.write found - has the callback handler moved?"
        for write in writes:
            source = ast.unparse(write)
            assert "_callback_page(" in source, f"unescaped page built at: {source}"
