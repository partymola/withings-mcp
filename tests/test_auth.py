"""Tests for OAuth token management (auth.py).

Exercises the real auth logic - no network, no real OAuth, no secrets.
refresh_token runs against a mocked HTTP layer, a frozen clock and real
credential files in a temp dir: the 5-minute expiry buffer and its boundary,
the refresh-token grant, refresh-token preservation, a token file another
process replaces, and every error branch. _exchange_code covers the same
transport failures plus the guards that keep one from escaping into the
callback handler thread. The rest is offline: how the token file is written
and replaced, and the client-file shapes that must reach the setup prompts
rather than the browser.
"""

import json
import logging
import os
import socket
import sys
import threading
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
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


#: The directory `_isolate_auth` created for the running test; empty otherwise.
_ISOLATED = {}


@pytest.fixture(autouse=True)
def _isolate_auth(tmp_path):
    """Point the credential files at a temp dir, off the production ones.

    Also clears a grant held after a failed save, which outlives the test that
    failed the save.
    """
    _ISOLATED["root"] = tmp_path
    try:
        with (
            patch.object(auth, "WITHINGS_TOKENS_PATH", tmp_path / "withings_tokens.json"),
            patch.object(auth, "WITHINGS_CLIENT_PATH", tmp_path / "withings_client.json"),
            patch.object(auth, "_unsaved", None),
        ):
            yield
    finally:
        _ISOLATED.clear()


def _refuse_unless_isolated(path):
    """Fail the test rather than touch a path outside its temp dir.

    The refresh tests write real files at the paths `auth` reads, so if the
    fixture's patch slips those are the live credentials. Checked against the
    directory the fixture created, not against the patched paths.
    """
    root = _ISOLATED.get("root")
    if root is None or not path.resolve().is_relative_to(root.resolve()):
        pytest.fail(f"refusing to write {path.name} outside the isolated directory")


class TestRefreshToken:
    def test_returns_the_stored_token_when_fresh(self):
        _write(_tokens(expires_at=_NOW + 10_000))
        opener = Mock()
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", opener):
            assert auth.refresh_token() == "tok-old"
        opener.assert_not_called()  # no HTTP when the token is comfortably valid

    def test_returns_the_stored_token_just_outside_buffer(self):
        # expires_at is 301s away: time.time() < expires_at - 300 holds -> stored.
        _write(_tokens(expires_at=_NOW + 301))
        opener = Mock()
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", opener):
            assert auth.refresh_token() == "tok-old"
        opener.assert_not_called()

    def test_refreshes_at_buffer_boundary(self):
        # expires_at exactly 300s away: the < comparison fails -> refresh fires.
        _write(_tokens(expires_at=_NOW + 300))
        body = {"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 10800}
        opener = _urlopen_returning({"status": 0, "body": body})
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", opener):
            assert auth.refresh_token() == "tok-new"
        opener.assert_called_once()

    def test_refresh_sends_the_stored_grant_and_persists_the_result(self):
        _write(_tokens(expires_at=_NOW - 1))
        body = {
            "access_token": "tok-new",
            "refresh_token": "ref-new",
            "userid": 99,
            "expires_in": 10800,
        }
        opener = _urlopen_returning({"status": 0, "body": body})
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", opener):
            auth.refresh_token()
        stored = _stored()
        assert stored["access_token"] == "tok-new"
        assert stored["refresh_token"] == "ref-new"
        assert stored["userid"] == 99
        # expires_at is stamped from the frozen clock plus expires_in.
        assert stored["expires_at"] == _NOW + 10800
        # The outgoing grant carries the refresh_token flow with the old token.
        sent = _sent_form(opener)
        assert sent["grant_type"] == "refresh_token"
        assert sent["refresh_token"] == "ref-old"
        assert sent["client_id"] == "client-xyz"

    def test_refresh_writes_token_file_to_disk(self):
        # Exercise the real _save_json (mkstemp at 0600, then atomic replace);
        # paths point at tmp_path.
        _write(_tokens(expires_at=_NOW - 1))
        body = {"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 10800}
        opener = _urlopen_returning({"status": 0, "body": body})
        with (
            _frozen_time(),
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
        _write(_tokens(refresh="ref-keep", expires_at=_NOW - 1))
        body = {"access_token": "tok-new", "expires_in": 10800}
        opener = _urlopen_returning({"status": 0, "body": body})
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", opener):
            auth.refresh_token()
        assert _stored()["refresh_token"] == "ref-keep"
        assert _stored()["userid"] == 7  # old userid preserved too

    def test_a_valid_token_does_not_need_the_client_file(self):
        """Only a refresh uses it, so a moment when it cannot be read must not
        refuse a token that needs no refresh."""
        _write(_tokens(expires_at=_NOW + 10_000), creds=None)
        with _frozen_time():
            assert auth.refresh_token() == "tok-old"

    def test_a_stored_token_with_no_access_token_is_refreshed_rather_than_sent(self):
        _write(_tokens(access="", expires_at=_NOW + 10_000))
        body = {"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 10800}
        opener = _urlopen_returning({"status": 0, "body": body})
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", opener):
            assert auth.refresh_token() == "tok-new"

    def test_expired_without_refresh_token_raises(self):
        _write(_tokens(refresh="", expires_at=_NOW - 1))
        with _frozen_time():
            with pytest.raises(RuntimeError):
                auth.refresh_token()

    def test_refresh_nonzero_status_raises(self):
        """A refusal, which must stay distinguishable from a network fault.

        RefreshNetworkError subclasses RuntimeError, so asserting the base
        class alone would pass whichever of the two was raised - and the two
        lead to opposite advice downstream.
        """
        _write(_tokens(expires_at=_NOW - 1))
        opener = _urlopen_returning({"status": 401})
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", opener):
            with pytest.raises(RuntimeError) as caught:
                auth.refresh_token()
        assert not isinstance(caught.value, auth.RefreshNetworkError)

    def test_refresh_network_error_raises(self):
        _write(_tokens(expires_at=_NOW - 1))
        opener = Mock(side_effect=urllib.error.URLError("boom"))
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", opener):
            with pytest.raises(auth.RefreshNetworkError):
                auth.refresh_token()


def _write(tokens=None, creds=_CREDS):
    """Write the credential files at the paths `_isolate_auth` points `auth` at."""
    if tokens is not None:
        _refuse_unless_isolated(auth.WITHINGS_TOKENS_PATH)
        auth.WITHINGS_TOKENS_PATH.write_text(json.dumps(tokens))
    if creds is not None:
        _refuse_unless_isolated(auth.WITHINGS_CLIENT_PATH)
        auth.WITHINGS_CLIENT_PATH.write_text(json.dumps(creds))


def test_the_credential_writer_refuses_outside_the_isolated_directory(monkeypatch):
    """The guard is what stands between a slipped patch and the live files.
    A sibling of the isolated directory catches a check made against anything
    wider."""
    with pytest.raises(pytest.fail.Exception, match="refusing to write"):
        _refuse_unless_isolated(_ISOLATED["root"].parent / "elsewhere" / "withings_tokens.json")
    monkeypatch.setitem(_ISOLATED, "root", None)
    with pytest.raises(pytest.fail.Exception, match="refusing to write"):
        _refuse_unless_isolated(auth.WITHINGS_TOKENS_PATH)


def _stored():
    return json.loads(auth.WITHINGS_TOKENS_PATH.read_text())


class TestTheTokenFileIsTheOnlyCopy:
    """`withings-mcp auth` runs in another process while the server runs.

    Each test writes real files at the paths `_isolate_auth` patches, because
    the property is about what is on disk, not about anything the module holds.
    Withings rotates the refresh token on every refresh, so a refreshed token
    left unsaved is a grant lost.
    """

    _REFRESHED = {"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 10800}

    @staticmethod
    def _rewrite_unnoticeably(path, text):
        """Same length and mtime as before, so only the content says it changed.

        A cache keyed on size or modification time would serve the old copy;
        mtime resolution is coarse on some filesystems, so it can miss a real
        rewrite.
        """
        before = path.stat()
        assert len(path.read_text()) == len(text)
        _refuse_unless_isolated(path)
        path.write_text(text)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    @staticmethod
    def _urlopen_writing(then, tokens=None, delete=False):
        """An urlopen that changes the token file mid-request, then answers."""
        answer = _urlopen_returning(then) if isinstance(then, dict) else None

        def urlopen(request, timeout=None):
            if delete:
                _refuse_unless_isolated(auth.WITHINGS_TOKENS_PATH)
                auth.WITHINGS_TOKENS_PATH.unlink()
            else:
                _write(tokens, creds=None)
            if answer is None:
                raise then
            return answer(request, timeout=timeout)

        return urlopen

    def test_a_token_written_by_auth_is_used_without_a_restart(self):
        """Held in memory, the old token stays in use after `auth` until a restart."""
        _write(_tokens(access="tok-aaaa"))
        with _frozen_time():
            assert auth.refresh_token() == "tok-aaaa"
            self._rewrite_unnoticeably(
                auth.WITHINGS_TOKENS_PATH,
                auth.WITHINGS_TOKENS_PATH.read_text().replace("tok-aaaa", "tok-bbbb"),
            )
            assert auth.refresh_token() == "tok-bbbb"

    def test_a_new_client_file_is_used_without_a_restart(self):
        """`auth` replaces the client file too, when reuse is declined."""
        _write(_tokens(expires_at=_NOW - 1))
        sent = []

        def urlopen(request, timeout=None):
            sent.append(parse_qs(request.data.decode())["client_id"][0])
            return _urlopen_returning({"status": 0, "body": self._REFRESHED})()

        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            auth.refresh_token()
            self._rewrite_unnoticeably(
                auth.WITHINGS_CLIENT_PATH,
                auth.WITHINGS_CLIENT_PATH.read_text().replace("client-xyz", "client-abc"),
            )
            _write(_tokens(expires_at=_NOW - 1), creds=None)
            auth.refresh_token()
        assert sent == ["client-xyz", "client-abc"]

    def test_a_refresh_does_not_overwrite_a_grant_saved_while_it_ran(self):
        """Saving over it loses the user's new grant.

        The competing grant expires before the refreshed one would, as one
        issued moments earlier does, so a comparison against the refreshed
        expiry instead of the starting one fails here.
        """
        _write(_tokens(expires_at=_NOW - 1))
        urlopen = self._urlopen_writing(
            {"status": 0, "body": self._REFRESHED},
            _tokens(access="tok-auth", refresh="ref-auth", expires_at=_NOW + 5000),
        )
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            assert auth.refresh_token() == "tok-auth"
        assert (_stored()["access_token"], _stored()["refresh_token"]) == ("tok-auth", "ref-auth")

    def test_a_refresh_saves_over_an_older_copy(self):
        """The refresh token just issued is the only live one, since Withings
        rotates it; an older copy holds one that no longer works."""
        _write(_tokens(expires_at=_NOW - 1))
        urlopen = self._urlopen_writing(
            {"status": 0, "body": self._REFRESHED},
            _tokens(access="tok-restored", refresh="ref-restored", expires_at=_NOW - 100),
        )
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            assert auth.refresh_token() == "tok-new"
        assert _stored()["refresh_token"] == "ref-new"

    def test_a_refresh_saves_over_a_later_copy_whose_token_has_expired(self):
        """Later is not enough: yielding to an expired copy returns a token
        that is refused, and drops the rotated refresh token."""
        _write(_tokens(expires_at=_NOW - 1000))
        urlopen = self._urlopen_writing(
            {"status": 0, "body": self._REFRESHED},
            _tokens(access="tok-stale", refresh="ref-stale", expires_at=_NOW - 500),
        )
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            assert auth.refresh_token() == "tok-new"
        assert _stored()["refresh_token"] == "ref-new"

    def test_a_refresh_saves_when_the_file_has_gone(self):
        """Writes are atomic, so an unreadable file is gone or corrupt rather
        than half-written, and the rotated refresh token must still land."""
        _write(_tokens(expires_at=_NOW - 1))
        urlopen = self._urlopen_writing({"status": 0, "body": self._REFRESHED}, delete=True)
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            assert auth.refresh_token() == "tok-new"
        assert _stored()["refresh_token"] == "ref-new"

    @pytest.mark.parametrize(
        "refusal",
        [
            urllib.error.HTTPError("https://example.invalid", 400, "no", {}, None),
            urllib.error.HTTPError("https://example.invalid", 401, "no", {}, None),
            {"status": 401},
            {"status": 342},
        ],
        ids=["http-400", "http-401", "status-401", "status-342"],
    )
    def test_a_refusal_after_the_file_was_replaced_uses_the_new_file(self, refusal):
        """The refusal judged a refresh token `auth` had already replaced.
        Reporting it as a refusal tells the user to re-authorise over a working
        grant."""
        _write(_tokens(expires_at=_NOW - 1))
        urlopen = self._urlopen_writing(
            refusal, _tokens(access="tok-auth", refresh="ref-auth", expires_at=_NOW + 5000)
        )
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            assert auth.refresh_token() == "tok-auth"

    @pytest.mark.parametrize(
        "copy_expires_at",
        [_NOW - 100, _NOW + 100, _NOW + 5000],
        ids=["older", "later-inside-the-buffer", "usable"],
    )
    def test_a_refusal_with_the_same_refresh_token_in_the_file_is_a_refusal(self, copy_expires_at):
        """The file changed in other fields, but still holds the refresh token
        that was refused, so the refusal is a verdict on it, even where its
        access token is still valid."""
        _write(_tokens(expires_at=_NOW - 1))
        urlopen = self._urlopen_writing(
            {"status": 401}, _tokens(access="tok-copy", expires_at=copy_expires_at)
        )
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            with pytest.raises(auth.TokenRefused):
                auth.refresh_token()

    @pytest.mark.parametrize(
        "copy_expires_at", [_NOW - 100, _NOW + 100], ids=["older", "later-inside-the-buffer"]
    )
    def test_a_refusal_after_the_file_changed_to_an_unusable_grant_is_not_a_refusal(
        self, copy_expires_at
    ):
        """The refusal judged the refresh token sent, not the one now in the
        file, so the next call refreshes with that one rather than this call
        telling the user to re-authorise."""
        _write(_tokens(expires_at=_NOW - 1))
        urlopen = self._urlopen_writing(
            {"status": 401},
            _tokens(access="tok-copy", refresh="ref-copy", expires_at=copy_expires_at),
        )
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            with pytest.raises(auth.RefreshNetworkError):
                auth.refresh_token()

    def test_a_refusal_while_the_file_has_gone_is_not_a_refusal(self):
        """The refusal judged the refresh token sent; with no file to compare,
        the next call is what says whether the credentials are gone."""
        _write(_tokens(expires_at=_NOW - 1))
        urlopen = self._urlopen_writing({"status": 401}, delete=True)
        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            with pytest.raises(auth.RefreshNetworkError):
                auth.refresh_token()

    def test_a_refresh_saves_when_the_clock_steps_back_during_it(self):
        """The file is untouched and its token still looks valid by the stepped
        clock; only the later-expiry test tells it from a grant someone saved,
        and yielding to it leaves the file on a refresh token Withings has
        retired."""
        clock = {"now": _NOW}
        _write(_tokens(expires_at=_NOW + 200))

        def urlopen(request, timeout=None):
            clock["now"] = _NOW - 1000
            return _urlopen_returning({"status": 0, "body": self._REFRESHED})()

        with (
            patch.object(auth.time, "time", lambda: clock["now"]),
            patch("withings_mcp.auth.urllib.request.urlopen", urlopen),
        ):
            assert auth.refresh_token() == "tok-new"
        assert _stored()["refresh_token"] == "ref-new"

    @staticmethod
    def _fail_saves(times):
        """A `_save_json` that raises OSError for its first `times` calls."""
        real = auth._save_json
        calls = {"n": 0}

        def save(path, data):
            calls["n"] += 1
            if calls["n"] <= times:
                raise OSError(28, "No space left on device")
            real(path, data)

        return patch.object(auth, "_save_json", save)

    def test_a_grant_whose_save_failed_is_kept_and_saved_later(self):
        """Withings has already rotated the refresh token in the file, so the
        refreshed grant is the only live copy until a save succeeds."""
        _write(_tokens(expires_at=_NOW - 1))
        opener = _urlopen_returning({"status": 0, "body": self._REFRESHED})
        with (
            _frozen_time(),
            self._fail_saves(1),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            assert auth.refresh_token() == "tok-new"
            assert _stored()["refresh_token"] == "ref-old"
            assert auth.refresh_token() == "tok-new"
        opener.assert_called_once()
        assert _stored()["refresh_token"] == "ref-new"
        assert auth._unsaved is None

    def test_a_held_grant_is_used_while_the_file_has_gone(self):
        """Nothing newer can be seen, and the held grant is the only live copy:
        dropping it leaves nothing to refresh from."""
        _write(_tokens(expires_at=_NOW - 1))
        opener = _urlopen_returning({"status": 0, "body": self._REFRESHED})
        with (
            _frozen_time(),
            self._fail_saves(1),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            auth.refresh_token()
            _refuse_unless_isolated(auth.WITHINGS_TOKENS_PATH)
            auth.WITHINGS_TOKENS_PATH.unlink()
            assert auth.refresh_token() == "tok-new"
        opener.assert_called_once()
        assert _stored()["refresh_token"] == "ref-new"

    def test_a_held_grant_is_keyed_to_the_file_as_it_was_when_the_save_failed(self):
        """The file can change during the request. Keyed to the token the call
        started from, the held grant is dropped on the next call though it is
        the only live copy."""
        _write(_tokens(expires_at=_NOW - 1))
        urlopen = Mock(
            side_effect=self._urlopen_writing(
                {"status": 0, "body": self._REFRESHED},
                _tokens(access="tok-copy", refresh="ref-copy", expires_at=_NOW - 100),
            )
        )
        with (
            _frozen_time(),
            self._fail_saves(1),
            patch("withings_mcp.auth.urllib.request.urlopen", urlopen),
        ):
            auth.refresh_token()
            assert auth.refresh_token() == "tok-new"
        # The held grant answered the second call; a dropped one would have
        # been refreshed again from the copy.
        assert urlopen.call_count == 1
        assert _stored()["refresh_token"] == "ref-new"

    def test_a_refused_held_grant_with_the_file_gone_is_settled_by_the_next_call(self):
        """Gone may be another writer about to put a grant there, so the
        refusal is not a verdict; the held grant is dead either way, and the
        next call reads the file without sending anything."""
        _write(_tokens(expires_at=_NOW - 1))
        answers = [
            {"status": 0, "body": dict(self._REFRESHED, expires_in=0)},
            {"status": 401},
        ]
        posts = []

        def urlopen(request, timeout=None):
            posts.append(request)
            return _urlopen_returning(answers.pop(0))()

        with (
            _frozen_time(),
            self._fail_saves(9),
            patch("withings_mcp.auth.urllib.request.urlopen", urlopen),
        ):
            auth.refresh_token()
            _refuse_unless_isolated(auth.WITHINGS_TOKENS_PATH)
            auth.WITHINGS_TOKENS_PATH.unlink()
            with pytest.raises(auth.RefreshNetworkError):
                auth.refresh_token()
            assert auth._unsaved is None
            with pytest.raises(auth.TokenRefused):
                auth.refresh_token()
        assert len(posts) == 2

    def test_a_failed_save_is_reported_once_while_it_is_retried(self, caplog):
        """The retry runs on every request, and a sync makes many."""
        _write(_tokens(expires_at=_NOW - 1))
        opener = _urlopen_returning({"status": 0, "body": self._REFRESHED})
        with (
            _frozen_time(),
            self._fail_saves(9),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
            caplog.at_level(logging.WARNING, logger="withings_mcp.auth"),
        ):
            for _ in range(4):
                auth.refresh_token()
        assert len([r for r in caplog.records if "save" in r.getMessage()]) == 1

    def test_a_held_grant_that_expires_is_refreshed_with_its_own_refresh_token(self):
        """The file's refresh token is the one the held grant replaced, and
        Withings has retired it; sending it is refused, and reads as the file
        unchanged, so the user is told to re-authorise over a live grant."""
        _write(_tokens(expires_at=_NOW - 1))
        sent = []
        answers = [
            dict(self._REFRESHED, expires_in=0),
            dict(self._REFRESHED, access_token="tok-long", refresh_token="ref-long"),
        ]

        def urlopen(request, timeout=None):
            sent.append(parse_qs(request.data.decode())["refresh_token"][0])
            return _urlopen_returning({"status": 0, "body": answers.pop(0)})()

        # Two failures, so the retry before the second refresh fails too and
        # the file still holds ref-old when it runs.
        with (
            _frozen_time(),
            self._fail_saves(2),
            patch("withings_mcp.auth.urllib.request.urlopen", urlopen),
        ):
            auth.refresh_token()
            assert auth.refresh_token() == "tok-long"
        assert sent == ["ref-old", "ref-new"]

    def test_a_later_grant_that_wins_also_ends_a_held_one(self):
        """Left held, a later moment with the file gone saves the held grant,
        whose refresh token this refresh has just spent."""
        _write(_tokens(expires_at=_NOW - 1))
        posts = []

        def urlopen(request, timeout=None):
            posts.append(request)
            if len(posts) == 1:
                body = dict(self._REFRESHED, expires_in=0)
            else:
                _write(_tokens(access="tok-auth", refresh="ref-auth", expires_at=_NOW + 5000))
                body = self._REFRESHED
            return _urlopen_returning({"status": 0, "body": body})()

        # Two failures: the first refresh's save, and the retry before the
        # second, so the grant is still held when the second refresh runs.
        with (
            _frozen_time(),
            self._fail_saves(2),
            patch("withings_mcp.auth.urllib.request.urlopen", urlopen),
        ):
            auth.refresh_token()
            assert auth.refresh_token() == "tok-auth"
        assert len(posts) == 2
        assert auth._unsaved is None

    def test_a_held_grant_gives_way_to_a_file_written_since(self):
        _write(_tokens(expires_at=_NOW - 1))
        opener = _urlopen_returning({"status": 0, "body": self._REFRESHED})
        with (
            _frozen_time(),
            self._fail_saves(1),
            patch("withings_mcp.auth.urllib.request.urlopen", opener),
        ):
            auth.refresh_token()
            _write(_tokens(access="tok-auth", refresh="ref-auth", expires_at=_NOW + 5000))
            assert auth.refresh_token() == "tok-auth"
        assert auth._unsaved is None

    def test_a_refused_held_grant_is_a_refusal(self):
        """Otherwise the held grant's refusal reads as a change to the file,
        which still holds the token it replaced, and never converges."""
        _write(_tokens(expires_at=_NOW - 1))
        answers = [
            {"status": 0, "body": dict(self._REFRESHED, expires_in=0)},
            {"status": 401},
        ]

        def urlopen(request, timeout=None):
            return _urlopen_returning(answers.pop(0))()

        with (
            _frozen_time(),
            self._fail_saves(9),
            patch("withings_mcp.auth.urllib.request.urlopen", urlopen),
        ):
            auth.refresh_token()
            with pytest.raises(auth.TokenRefused):
                auth.refresh_token()
        assert auth._unsaved is None

    def test_concurrent_calls_refresh_once(self):
        """Tool calls run in worker threads. Unserialised, two spend the same
        refresh token, and Withings rotates it on use."""
        import threading

        _write(_tokens(expires_at=_NOW - 1))
        posts = []
        second = {}

        def call_from_another_thread():
            second["token"] = auth.refresh_token()

        other = threading.Thread(target=call_from_another_thread)

        def urlopen(request, timeout=None):
            posts.append(request)
            if len(posts) == 1:
                other.start()
                # Long enough for an unserialised second call to reach the
                # network; with the lock it is still waiting when this returns.
                other.join(timeout=0.5)
            body = dict(self._REFRESHED, access_token=f"tok-{len(posts)}")
            return _urlopen_returning({"status": 0, "body": body})()

        with _frozen_time(), patch("withings_mcp.auth.urllib.request.urlopen", urlopen):
            first = auth.refresh_token()
            other.join(timeout=5)

        assert len(posts) == 1
        assert second["token"] == first


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


def _writes_that(action):
    """An `os.fdopen` whose handle calls `action(fd)` before each write."""
    real_fdopen = os.fdopen

    class _Handle:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

        def write(self, text):
            action(self._handle.fileno())
            return self._handle.write(text)

        def __getattr__(self, name):
            return getattr(self._handle, name)

    return patch.object(auth.os, "fdopen", lambda fd, *a, **k: _Handle(real_fdopen(fd, *a, **k)))


class TestTheTokenFileIsNeverExposed:
    """The refresh token must not sit in a readable file, even briefly.

    The mode is asserted at the moment of writing, not only on the final
    file, which is 0600 whether or not there was a readable window. Only the
    mode-bit and symlink tests are POSIX-only; the rest hold anywhere. Paths
    are resolved because the save follows symlinks, and on macOS the temp
    directory sits behind one.
    """

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; Windows uses ACLs")
    def test_the_new_file_is_owner_only_before_the_token_is_written(self, tmp_path):
        seen = []
        target = tmp_path / "withings_tokens.json"
        with _writes_that(lambda fd: seen.append(oct(os.fstat(fd).st_mode & 0o777))):
            auth._save_json(target, {"refresh_token": "fictional"})

        assert seen == ["0o600"]
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    @pytest.mark.parametrize(
        "failure",
        [OSError(28, "No space left on device"), KeyboardInterrupt()],
        ids=["disk-full", "interrupted"],
    )
    def test_a_failed_write_leaves_the_old_file_and_nothing_else(self, tmp_path, failure):
        """The temp file holds the new refresh token, so one left behind is a
        live credential lying in the config directory."""
        target = tmp_path / "withings_tokens.json"
        target.write_text('{"refresh_token": "old"}')

        def fail(fd):
            raise failure

        with _writes_that(fail), pytest.raises(type(failure)):
            auth._save_json(target, {"refresh_token": "new"})

        assert target.read_text() == '{"refresh_token": "old"}'
        assert list(tmp_path.iterdir()) == [target]

    def test_a_failed_replace_leaves_the_old_file_and_nothing_else(self, tmp_path, monkeypatch):
        def boom(src, dst):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(auth.os, "replace", boom)
        target = tmp_path / "withings_tokens.json"
        with pytest.raises(OSError):
            auth._save_json(target, {"refresh_token": "fictional"})

        assert list(tmp_path.iterdir()) == []

    def test_a_failed_cleanup_does_not_hide_why_the_save_failed(self, tmp_path):
        """A scanner can block the unlink as well as the replace on Windows."""

        def refuse_replace(src, dst):
            raise OSError(28, "No space left on device")

        def refuse_unlink(path):
            raise PermissionError(13, "Access is denied")

        with (
            patch.object(auth.os, "replace", refuse_replace),
            patch.object(auth.os, "unlink", refuse_unlink),
            pytest.raises(OSError) as raised,
        ):
            auth._save_json(tmp_path / "withings_tokens.json", {"refresh_token": "new"})
        assert raised.value.errno == 28

    def test_the_new_file_is_synced_before_it_replaces_the_old_one(self, tmp_path):
        """Unsynced, a crash soon after the rename can leave an empty file on
        some filesystems, after the refresh token it replaced was spent."""
        data = {"refresh_token": "new"}
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            # The whole file has to be in it by now, or the sync covers less.
            events.append(("fsync", os.fstat(fd).st_size))
            return real_fsync(fd)

        def replace(src, dst):
            events.append(("replace", None))
            return real_replace(src, dst)

        target = tmp_path / "withings_tokens.json"
        with patch.object(auth.os, "fsync", fsync), patch.object(auth.os, "replace", replace):
            auth._save_json(target, data)

        # Against the saved file rather than the JSON: Windows writes `\r\n`.
        assert events == [("fsync", target.stat().st_size), ("replace", None)]
        assert target.stat().st_size > 0

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_a_symlinked_token_file_is_saved_where_the_link_points(self, tmp_path):
        """Replacing the link would leave its target, which anything else using
        the real path reads, on a refresh token already spent."""
        folder = tmp_path.resolve()
        real = folder / "secrets" / "withings_tokens.json"
        real.parent.mkdir()
        real.write_text('{"refresh_token": "old"}')
        link = folder / "withings_tokens.json"
        link.symlink_to(real)
        published = []
        real_replace = os.replace

        def spy(src, dst):
            published.append(Path(src).parent)
            return real_replace(src, dst)

        with patch.object(auth.os, "replace", spy):
            auth._save_json(link, {"refresh_token": "new"})

        assert link.is_symlink()
        assert json.loads(real.read_text()) == {"refresh_token": "new"}
        # Beside the real file, since a rename cannot cross filesystems.
        assert published == [real.parent]

    def test_windows_retries_a_replace_another_reader_is_blocking(self, tmp_path):
        """Windows refuses to replace a file another process has open, and a
        server sharing the file opens it on every request; `auth` cannot redo
        the save once the authorisation code is spent."""
        attempts = []
        real_replace = os.replace

        def busy_twice(src, dst):
            attempts.append(src)
            if len(attempts) <= 2:
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)

        target = tmp_path / "withings_tokens.json"
        with (
            patch.object(auth.sys, "platform", "win32"),
            patch.object(auth.os, "replace", busy_twice),
            patch.object(auth.time, "sleep") as sleep,
        ):
            auth._save_json(target, {"refresh_token": "new"})

        assert len(attempts) == 3
        assert json.loads(target.read_text()) == {"refresh_token": "new"}
        # Back-to-back retries would be over before a reader let go.
        delays = [call.args[0] for call in sleep.call_args_list]
        assert len(delays) == 2 and 0 < delays[0] < delays[1], delays

    @pytest.mark.parametrize("platform,expected", [("linux", 1), ("win32", 5)])
    def test_a_refused_replace_is_not_retried_elsewhere_or_forever(
        self, tmp_path, platform, expected
    ):
        """Off Windows the refusal is a real permission fault, and on Windows a
        file that stays locked has to fail rather than hang the server."""
        attempts = []

        def refuse(src, dst):
            attempts.append(src)
            raise PermissionError(13, "Access is denied")

        with (
            patch.object(auth.sys, "platform", platform),
            patch.object(auth.os, "replace", refuse),
            patch.object(auth.time, "sleep"),
            pytest.raises(PermissionError),
        ):
            auth._save_json(tmp_path / "withings_tokens.json", {"refresh_token": "new"})
        assert len(attempts) == expected
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.skipif(sys.platform != "win32", reason="the sharing rule retried is Windows'")
    def test_a_save_waits_out_a_reader_holding_the_file_open(self, tmp_path):
        """The real refusal, not a stand-in for it: a reader holds the file
        while the save runs, and lets go within the retries."""
        refused = []
        real_replace = os.replace

        def watch(src, dst):
            try:
                return real_replace(src, dst)
            except PermissionError:
                refused.append(src)
                raise

        target = tmp_path / "withings_tokens.json"
        target.write_text('{"refresh_token": "old"}')
        reader = open(target)
        timer = threading.Timer(0.1, reader.close)
        timer.start()
        try:
            with patch.object(auth.os, "replace", watch):
                auth._save_json(target, {"refresh_token": "new"})
        finally:
            timer.join()
            reader.close()
        assert json.loads(target.read_text()) == {"refresh_token": "new"}
        # Without a refusal this proves nothing about the retry.
        assert refused, "Windows replaced a file another process had open"

    def test_the_last_write_wins_intact(self, tmp_path):
        """Not an atomicity test: a single-threaded run cannot observe the gap.

        Atomicity here rests on os.replace's semantics, not on this. What it
        does pin is that replacing does not corrupt or truncate the result.
        """
        target = tmp_path / "withings_tokens.json"
        auth._save_json(target, {"refresh_token": "first"})
        auth._save_json(target, {"refresh_token": "second", "padding": "x" * 10000})

        assert json.loads(target.read_text())["refresh_token"] == "second"

    def test_each_save_publishes_its_own_temp_file_from_the_same_directory(
        self, tmp_path, monkeypatch
    ):
        """A fixed temp name lets one writer publish another's half-written
        file, and a rename from another directory is not atomic."""
        published = []
        real_replace = os.replace

        def spy(src, dst):
            published.append(Path(src))
            return real_replace(src, dst)

        monkeypatch.setattr(auth.os, "replace", spy)
        folder = tmp_path.resolve()
        target = folder / "withings_tokens.json"
        auth._save_json(target, {"refresh_token": "a"})
        auth._save_json(target, {"refresh_token": "b"})

        assert len(set(published)) == 2
        assert all(p.parent == folder and p != target for p in published)


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
        # The name setup_auth actually constructs. Patching HTTPServer instead
        # blocks nothing, and the guard reads as working - which is why
        # test_no_bare_httpserver_is_constructed_anywhere pins the name too.
        monkeypatch.setattr(auth, "_CallbackServer", no_server)

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


class _RecordingSocket:
    """Stands in for the real socket so server_bind's Windows branch can run."""

    def __init__(self):
        self.calls = []

    def setsockopt(self, level, option, value):
        self.calls.append(("setsockopt", level, option, value))

    def bind(self, address):
        self.calls.append(("bind", address))

    def getsockname(self):
        return ("127.0.0.1", 8585)


class TestTheCallbackPortIsNotShared:
    """The listener carries the authorisation code, so it must not be displaceable.

    `server_bind` chooses on `sys.platform` at call time, so the Windows branch
    is reachable from a POSIX runner against a recording socket. That is worth
    more than reading the source for it: source assertions here let the option
    be set after the bind, at the wrong level, on the wrong socket, or with a
    value of 0, all of which pass a check that only looks for the name.
    """

    def _bind_as(self, platform, monkeypatch):
        monkeypatch.setattr(auth.sys, "platform", platform)
        # Absent on POSIX, so it has to be created rather than replaced.
        monkeypatch.setattr(auth.socket, "SO_EXCLUSIVEADDRUSE", 0xFFFFFFFF, raising=False)
        monkeypatch.setattr(auth.socket, "getfqdn", lambda host: host)

        server = auth._CallbackServer.__new__(auth._CallbackServer)
        server.socket = _RecordingSocket()
        server.server_address = ("localhost", 8585)
        # The class attribute is fixed at import against the real platform, so
        # the value the other test pins is supplied here rather than read.
        server.allow_reuse_address = platform != "win32"
        server.allow_reuse_port = False
        auth._CallbackServer.server_bind(server)
        return server.socket.calls

    def test_windows_asks_for_exclusive_use_before_binding(self, monkeypatch):
        calls = self._bind_as("win32", monkeypatch)
        assert calls[0] == (
            "setsockopt",
            socket.SOL_SOCKET,
            auth.socket.SO_EXCLUSIVEADDRUSE,
            1,
        ), calls
        assert ("bind", ("localhost", 8585)) in calls
        # Never both: asking to share after asking not to is the configuration
        # Microsoft documents as insecure.
        assert not any(
            call[:3] == ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR) for call in calls
        ), calls

    def test_posix_asks_for_nothing_exclusive(self, monkeypatch):
        calls = self._bind_as("linux", monkeypatch)
        assert not any(
            call[0] == "setsockopt" and call[2] == auth.socket.SO_EXCLUSIVEADDRUSE for call in calls
        ), calls

    def test_reuse_is_allowed_off_windows_and_refused_on_it(self):
        """Not asking to share is the fix; the exclusive option is the belt.

        The runtime value alone cannot see a revert to a bare `True`, because
        that is what this platform is supposed to have, so the source form is
        pinned with it.
        """
        import ast
        import inspect

        assert auth._CallbackServer.allow_reuse_address == (sys.platform != "win32")

        tree = ast.parse(inspect.getsource(auth._CallbackServer))
        assigned = [
            ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "allow_reuse_address" for t in node.targets)
        ]
        assert assigned, "allow_reuse_address is no longer set here"
        assert all("platform" in value for value in assigned), assigned

    def test_only_win32_is_named(self):
        """`sys.platform` is `win32` on 64-bit Windows too, so a `win64` test
        is a branch that never runs."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth._CallbackServer))
        compared = {
            const.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == "sys.platform"
            for const in node.comparators
            if isinstance(const, ast.Constant)
        }
        assert compared == {"win32"}, compared

    @pytest.mark.skipif(sys.platform == "win32", reason="binds a real POSIX socket")
    def test_it_still_binds(self):
        """server_bind is overridden, so a mistake there breaks `auth` outright."""
        server = auth._CallbackServer(("localhost", 0), auth.BaseHTTPRequestHandler)
        try:
            assert server.server_address[0] == "127.0.0.1"
            assert server.server_address[1] != 0
        finally:
            server.server_close()

    def test_no_bare_httpserver_is_constructed_anywhere(self):
        """Scoped to the module, not to setup_auth: a second listener added
        elsewhere would carry the default this class exists to refuse."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth))
        bare = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HTTPServer"
        ]
        assert not bare, bare

    def test_a_busy_port_is_reported_before_the_browser_opens(self, monkeypatch, capsys):
        """A port that is already held is a failure this flow could not have
        before, so it has to end in advice rather than a traceback - and end
        there before an authorisation page is opened onto a redirect with
        nowhere to land."""
        opened = []
        # _isolate_auth already points the client path at a temp file that does
        # not exist, so the flow goes to the prompts rather than the re-use path.
        monkeypatch.setattr(auth.webbrowser, "open", lambda url: opened.append(url))
        monkeypatch.setattr("builtins.input", lambda prompt: "supplied")
        monkeypatch.setattr(auth, "_save_json", lambda path, data: None)

        def refuse_to_bind(*args, **kwargs):
            raise OSError(98, "Address already in use")

        monkeypatch.setattr(auth, "_CallbackServer", refuse_to_bind)

        with pytest.raises(SystemExit) as exit_info:
            auth.setup_auth()

        assert exit_info.value.code == 1
        assert opened == [], opened
        assert str(auth.WITHINGS_CALLBACK_PORT) in capsys.readouterr().err

    def test_a_save_that_fails_reports_its_type_not_its_paths(self, monkeypatch, capsys):
        """The save needs write access to the directory and, on Windows, a file
        nobody holds open, and an OSError's text names the install's paths."""
        monkeypatch.setattr("builtins.input", lambda prompt: "supplied")

        def refuse(path, data):
            raise PermissionError(13, "Permission denied", "/home/someone/config/.tmpfile")

        monkeypatch.setattr(auth, "_save_json", refuse)
        with pytest.raises(SystemExit) as exit_info:
            auth.setup_auth()

        captured = capsys.readouterr()
        assert exit_info.value.code == 1
        expected = "Error: the credentials could not be saved (PermissionError)."
        assert captured.err.strip() == expected
        assert "/home/someone" not in captured.out

    def test_setup_auth_saves_only_through_the_reporting_helper(self):
        """The token save comes after the code is spent, where no test drives
        the flow; a direct `_save_json` there would end in a traceback."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth.setup_auth))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_save_json" not in called
        assert "_save_or_exit" in called


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
