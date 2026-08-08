"""Tests for API layer: status-in-body error handling, PII non-leakage."""

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures import fake_api_response
from withings_mcp import api, auth
from withings_mcp.api import (
    WithingsAPIError,
    WithingsAuthError,
    WithingsRateLimitError,
    post,
)


def _mock_urlopen(response_dict):
    """Create a mock urlopen context manager returning a JSON response."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_dict).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestApiPost(unittest.TestCase):
    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_success_returns_body(self, mock_urlopen, mock_refresh):
        body_data = {"measuregrps": []}
        mock_urlopen.return_value = _mock_urlopen(fake_api_response(status=0, body=body_data))
        result = post("https://example.com", {"action": "getmeas"})
        self.assertEqual(result, body_data)

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_401_raises_auth_error(self, mock_urlopen, mock_refresh):
        mock_urlopen.return_value = _mock_urlopen(fake_api_response(status=401))
        with self.assertRaises(WithingsAuthError) as ctx:
            post("https://example.com", {"action": "getmeas"})
        self.assertIn("withings-mcp auth", str(ctx.exception))

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_601_raises_rate_limit(self, mock_urlopen, mock_refresh):
        mock_urlopen.return_value = _mock_urlopen(fake_api_response(status=601))
        with self.assertRaises(WithingsRateLimitError) as ctx:
            post("https://example.com", {"action": "getmeas"}, retries=1)
        self.assertIn("60 seconds", str(ctx.exception))

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_602_raises_rate_limit(self, mock_urlopen, mock_refresh):
        mock_urlopen.return_value = _mock_urlopen(fake_api_response(status=602))
        with self.assertRaises(WithingsRateLimitError):
            post("https://example.com", {"action": "getmeas"}, retries=1)

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_unknown_status_raises_api_error(self, mock_urlopen, mock_refresh):
        mock_urlopen.return_value = _mock_urlopen(fake_api_response(status=2555))
        with self.assertRaises(WithingsAPIError) as ctx:
            post("https://example.com", {"action": "getmeas"}, retries=1)
        self.assertIn("2555", str(ctx.exception))

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_network_error_raises_api_error(self, mock_urlopen, mock_refresh):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        with self.assertRaises(WithingsAPIError):
            post("https://example.com", {"action": "getmeas"})

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_error_message_no_health_data(self, mock_urlopen, mock_refresh):
        """Verify that error messages never contain health data from response body."""
        # Simulate a response with health data in the body alongside an error status
        response_with_data = {
            "status": 2555,
            "body": {"measuregrps": [{"measures": [{"value": 72500, "type": 1, "unit": -3}]}]},
            "error": "some_error_with_weight_72.5kg",
        }
        mock_urlopen.return_value = _mock_urlopen(response_with_data)
        with self.assertRaises(WithingsAPIError) as ctx:
            post("https://example.com", {"action": "getmeas"}, retries=1)
        error_msg = str(ctx.exception)
        # Error should contain status code but NOT health data
        self.assertIn("2555", error_msg)
        self.assertNotIn("72500", error_msg)
        self.assertNotIn("72.5", error_msg)
        self.assertNotIn("measuregrps", error_msg)
        self.assertNotIn("weight", error_msg)

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_success_returns_empty_body(self, mock_urlopen, mock_refresh):
        mock_urlopen.return_value = _mock_urlopen({"status": 0})
        result = post("https://example.com", {"action": "getdevice"})
        self.assertEqual(result, {})


class TestRefreshFailuresBecomeAuthErrors(unittest.TestCase):
    """Failing to obtain a token is an auth failure, not an escaping error.

    auth.refresh_token signals every one of its failures with a plain builtin,
    so without this translation they pass through run_sync's handlers
    untouched: nothing is written to sync_log, the connection is never closed,
    and doctor reports a healthy log while syncing has been dead for weeks.
    """

    def _post_with_refresh_raising(self, exc):
        with patch.object(api, "refresh_token", side_effect=exc):
            return api.post("https://example.invalid/measure", {})

    def test_a_failed_refresh_becomes_an_auth_error(self):
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_refresh_raising(RuntimeError("Token refresh failed"))

    def test_a_missing_token_file_becomes_an_auth_error(self):
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_refresh_raising(FileNotFoundError("withings_tokens.json"))

    def test_a_malformed_token_file_becomes_an_auth_error(self):
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_refresh_raising(json.JSONDecodeError("bad", "{", 0))

    def test_a_token_file_missing_its_keys_becomes_an_auth_error(self):
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_refresh_raising(KeyError("access_token"))

    def test_an_unrelated_bug_is_not_disguised_as_an_auth_failure(self):
        """Only the documented failure modes translate; a bug must stay a bug."""
        with self.assertRaises(TypeError):
            self._post_with_refresh_raising(TypeError("someone changed a signature"))

    def test_the_reported_message_carries_no_file_content(self):
        """sync_log stores this string, so it must not echo what was read."""
        with patch.object(api, "refresh_token", side_effect=FileNotFoundError("/etc/secret/path")):
            with self.assertRaises(api.WithingsAuthError) as caught:
                api.post("https://example.invalid/measure", {})
        assert "/etc/secret/path" not in str(caught.exception)


class TestNetworkFailureIsNotAnAuthFailure(unittest.TestCase):
    """An unreachable server says nothing about the credentials.

    Classified as auth, it would reach sync_log as auth_error, which doctor
    grades FAIL and answers with "run withings-mcp auth" - re-authorising over
    a dropped connection, and rotating a token file another host may own.
    """

    def test_a_refresh_that_cannot_reach_the_server_is_an_api_error(self):
        with patch.object(api, "refresh_token", side_effect=auth.RefreshNetworkError("no route")):
            with self.assertRaises(api.WithingsAPIError):
                api.post("https://example.invalid/measure", {})

    def test_it_is_not_reported_as_an_auth_error(self):
        with patch.object(api, "refresh_token", side_effect=auth.RefreshNetworkError("no route")):
            try:
                api.post("https://example.invalid/measure", {})
            except api.WithingsAuthError:
                self.fail("a network failure was classified as an auth failure")
            except api.WithingsAPIError:
                pass


class TestAgainstTheRealRefresh(unittest.TestCase):
    """Drive the real auth.refresh_token, so the catch tuple is a fact.

    Every other test here injects a side effect, which pins what api.post does
    with an exception but not that auth actually raises that kind. A broken
    token file exercised end to end is what proves the two agree.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        auth._cached_tokens = None
        auth._cached_creds = None

    def tearDown(self):
        self._tmp.cleanup()
        auth._cached_tokens = None
        auth._cached_creds = None

    def _post_with_token_file(self, contents):
        tokens = self.dir / "withings_tokens.json"
        client = self.dir / "withings_client.json"
        if contents is not None:
            tokens.write_text(contents)
        client.write_text('{"client_id": "fake-id", "client_secret": "fake-secret"}')
        with (
            patch.object(auth, "WITHINGS_TOKENS_PATH", tokens),
            patch.object(auth, "WITHINGS_CLIENT_PATH", client),
        ):
            return api.post("https://example.invalid/measure", {})

    def test_a_missing_token_file(self):
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_token_file(None)

    def test_a_token_file_that_is_not_json(self):
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_token_file("{not json")

    def test_a_token_file_that_is_not_an_object(self):
        """Valid JSON of the wrong shape used to fail at an attribute access."""
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_token_file('["not", "an", "object"]')

    def test_a_token_file_with_a_non_numeric_expiry(self):
        """A hand-edited expires_at used to raise TypeError on the comparison."""
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_token_file('{"access_token": "a", "expires_at": "soon"}')

    def test_a_token_file_with_no_refresh_token(self):
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_token_file('{"access_token": "a", "expires_at": 0}')

    def test_a_refresh_that_cannot_reach_the_server_is_not_an_auth_failure(self):
        """Driven through the real auth code, not an injected exception type.

        The classification lives at auth's raise site, so injecting the error
        here would pin api.post's handling and leave that raise unpinned.
        """
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(
            auth.urllib.request, "urlopen", side_effect=urllib.error.URLError("no route")
        ):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)


if __name__ == "__main__":
    unittest.main()
