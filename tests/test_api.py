"""Tests for API layer: status-in-body error handling, PII non-leakage."""

import io
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
    """api.post maps the two types refresh_token guarantees, and nothing else.

    It used to catch a tuple of builtins, so the classification depended on
    remembering every type auth could raise - which is how a bare OSError, an
    http.client exception and a decode failure were each graded a dead
    credential in turn.
    """

    def _post_with_refresh_raising(self, exc):
        with patch.object(api, "refresh_token", side_effect=exc):
            return api.post("https://example.invalid/measure", {})

    def test_a_refusal_becomes_an_auth_error(self):
        with self.assertRaises(api.WithingsAuthError):
            self._post_with_refresh_raising(auth.TokenRefused("revoked"))

    def test_the_auth_message_is_fixed_text(self):
        """sync_log stores this string, so nothing may be interpolated."""
        with self.assertRaises(api.WithingsAuthError) as caught:
            self._post_with_refresh_raising(auth.TokenRefused("/etc/secret/path is missing"))
        self.assertEqual(
            str(caught.exception), "Could not obtain an access token. Run: withings-mcp auth"
        )


class TestTheRefreshBoundary(unittest.TestCase):
    """Every exit from refresh_token is one of two types, by construction."""

    def _refresh_with_worker_raising(self, exc):
        with patch.object(auth, "_refresh_token", side_effect=exc):
            return auth.refresh_token()

    def test_an_unclassified_failure_becomes_a_network_error(self):
        import http.client

        for exc in (
            TimeoutError("bare timeout"),
            ConnectionResetError("reset"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            KeyError("access_token"),
            ValueError("unparseable"),
            RuntimeError("something nobody classified"),
            http.client.BadStatusLine("garbage"),
        ):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(auth.RefreshNetworkError):
                    self._refresh_with_worker_raising(exc)

    def test_a_refusal_is_passed_through_unchanged(self):
        with self.assertRaises(auth.TokenRefused):
            self._refresh_with_worker_raising(auth.TokenRefused("revoked"))

    def test_the_boundary_does_not_swallow_a_successful_refresh(self):
        with patch.object(auth, "_refresh_token", return_value="a-token"):
            self.assertEqual(auth.refresh_token(), "a-token")


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

    def test_a_read_timeout_is_a_network_failure(self):
        """urlopen wraps only connect-phase errors, so this arrives bare."""
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(auth.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_a_reset_connection_is_a_network_failure(self):
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(
            auth.urllib.request, "urlopen", side_effect=ConnectionResetError("reset")
        ):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def _http_error(self, code):
        return urllib.error.HTTPError("https://example.invalid", code, "boom", {}, io.BytesIO(b""))

    def test_a_bad_request_from_the_server_is_an_auth_failure(self):
        """400 is RFC 6749's invalid_grant, the standard refusal."""
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(auth.urllib.request, "urlopen", side_effect=self._http_error(400)):
            with self.assertRaises(api.WithingsAuthError):
                self._post_with_token_file(tokens)

    def test_a_refusal_from_the_server_is_an_auth_failure(self):
        """The headline case: a revoked refresh token, answered with 4xx."""
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(auth.urllib.request, "urlopen", side_effect=self._http_error(401)):
            with self.assertRaises(api.WithingsAuthError):
                self._post_with_token_file(tokens)

    def test_a_server_side_failure_is_not_an_auth_failure(self):
        """A 5xx is the server unable to answer, not a judgement on credentials."""
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(auth.urllib.request, "urlopen", side_effect=self._http_error(503)):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def _urlopen_returning(self, payload):
        response = MagicMock()
        response.read.return_value = payload
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: None
        return patch.object(auth.urllib.request, "urlopen", return_value=response)

    def test_a_response_that_is_not_json_is_a_network_failure(self):
        """A captive portal answering 200 with HTML is not a bad credential."""
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with self._urlopen_returning(b"<html>sign in to the wifi</html>"):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_a_response_of_the_wrong_shape_is_not_a_refusal(self):
        """An intermediary that replaced the body says nothing about the token.

        Whether what it substituted happens to parse as JSON is not a fact
        about the credentials, so this and the non-JSON case must agree.
        """
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with self._urlopen_returning(b'["not", "an", "object"]'):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_a_response_missing_its_body_is_reported_not_raised(self):
        """Without the guard a KeyError reaches the same exception type.

        auth is asserted directly so the test discriminates the guard from the
        accident, rather than passing either way.
        """
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with self._urlopen_returning(b'{"status": 0}'):
            with self.assertRaises(RuntimeError) as caught:
                with (
                    patch.object(auth, "WITHINGS_TOKENS_PATH", self.dir / "withings_tokens.json"),
                    patch.object(auth, "WITHINGS_CLIENT_PATH", self.dir / "withings_client.json"),
                ):
                    (self.dir / "withings_tokens.json").write_text(tokens)
                    (self.dir / "withings_client.json").write_text(
                        '{"client_id": "i", "client_secret": "s"}'
                    )
                    auth._cached_tokens = None
                    auth._cached_creds = None
                    auth.refresh_token()
        self.assertNotIsInstance(caught.exception, auth.RefreshNetworkError)
        self.assertIn("Token refresh failed", str(caught.exception))

    def test_a_rate_limit_over_http_is_not_an_auth_failure(self):
        """429 is a 4xx that carries no judgement on the credentials.

        Answering it with re-authorisation turns a condition that clears
        itself into a rotated token file on every host sharing it.
        """
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(auth.urllib.request, "urlopen", side_effect=self._http_error(429)):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_a_request_timeout_over_http_is_not_an_auth_failure(self):
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(auth.urllib.request, "urlopen", side_effect=self._http_error(408)):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_a_rate_limit_in_the_body_is_not_an_auth_failure(self):
        """The live path: Withings answers 200 with the outcome in the body.

        api.post already calls 601/602 rate limiting for data requests; the
        token endpoint is the same host and means the same thing by them.
        """
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with self._urlopen_returning(b'{"status": 601}'):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_a_refusal_in_the_body_is_still_an_auth_failure(self):
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with self._urlopen_returning(b'{"status": 401}'):
            with self.assertRaises(api.WithingsAuthError):
                self._post_with_token_file(tokens)

    def test_a_truncated_response_is_a_network_failure(self):
        """http.client exceptions are not OSError, so they escaped everything."""
        import http.client

        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(
            auth.urllib.request, "urlopen", side_effect=http.client.IncompleteRead(b"partial")
        ):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_an_undecodable_response_is_a_network_failure(self):
        """A body that will not decode raises ValueError, which reads as auth."""
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with self._urlopen_returning(b"\xff\xfe not utf-8 at all"):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_an_unknown_body_status_is_not_treated_as_a_refusal(self):
        """The default has to be the non-destructive direction.

        api.post takes the same view of an unknown status for a data request.
        """
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with self._urlopen_returning(b'{"status": 2555}'):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_a_bad_signature_is_treated_as_a_refusal(self):
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with self._urlopen_returning(b'{"status": 342}'):
            with self.assertRaises(api.WithingsAuthError):
                self._post_with_token_file(tokens)

    def test_a_forbidden_response_is_not_treated_as_a_refusal(self):
        """403 is what a WAF returns; it says nothing about the grant."""
        tokens = '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
        with patch.object(auth.urllib.request, "urlopen", side_effect=self._http_error(403)):
            with self.assertRaises(api.WithingsAPIError) as caught:
                self._post_with_token_file(tokens)
        self.assertNotIsInstance(caught.exception, api.WithingsAuthError)


class TestTheDataRequestTransport(unittest.TestCase):
    """api.post's own request has the same exposure as the refresh.

    Every other test in this file fails inside refresh_token, so nothing
    reached this urlopen at all - widening its catch could have been reverted
    with the suite still green.
    """

    def _post_with_a_working_token(self, urlopen_side_effect):
        with (
            patch.object(api, "refresh_token", return_value="token"),
            patch.object(api.urllib.request, "urlopen", side_effect=urlopen_side_effect),
        ):
            return api.post("https://example.invalid/measure", {})

    def test_a_read_timeout_is_reported_not_escaped(self):
        with self.assertRaises(api.WithingsAPIError):
            self._post_with_a_working_token(TimeoutError("timed out"))

    def test_a_truncated_response_is_reported_not_escaped(self):
        import http.client

        with self.assertRaises(api.WithingsAPIError):
            self._post_with_a_working_token(http.client.IncompleteRead(b"partial"))

    def test_a_reset_connection_is_reported_not_escaped(self):
        with self.assertRaises(api.WithingsAPIError):
            self._post_with_a_working_token(ConnectionResetError("reset"))


class TestTheDataRequestParses(unittest.TestCase):
    """The data request has its own read and parse, with its own failures.

    Every earlier round enumerated exception types arriving at the refresh
    boundary, so this site - which reads and parses separately - was never in
    scope, and a captive portal answering 200 with HTML escaped run_sync
    entirely: no sync_log row, and doctor reporting a clean log.
    """

    def _post_returning(self, payload):
        response = MagicMock()
        response.read.return_value = payload
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: None
        with (
            patch.object(api, "refresh_token", return_value="token"),
            patch.object(api.urllib.request, "urlopen", return_value=response),
        ):
            return api.post("https://example.invalid/measure", {})

    def test_a_body_that_is_not_json_is_reported(self):
        with self.assertRaises(api.WithingsAPIError):
            self._post_returning(b"<html>captive portal</html>")

    def test_a_body_of_the_wrong_shape_is_reported(self):
        with self.assertRaises(api.WithingsAPIError):
            self._post_returning(b'["not", "an", "object"]')

    def test_an_undecodable_body_is_reported(self):
        with self.assertRaises(api.WithingsAPIError):
            self._post_returning(b"\xff\xfe not utf-8")

    def test_neither_is_reported_as_an_auth_failure(self):
        for payload in (b"<html>x</html>", b'["a"]', b"\xff\xfe"):
            with self.subTest(payload=payload[:8]):
                with self.assertRaises(api.WithingsAPIError) as caught:
                    self._post_returning(payload)
                self.assertNotIsInstance(caught.exception, api.WithingsAuthError)

    def test_a_null_inner_body_is_not_returned_as_none(self):
        """An explicit null used to fail at the caller's .get, past run_sync."""
        self.assertEqual(self._post_returning(b'{"status": 0, "body": null}'), {})

    def test_an_inner_body_of_the_wrong_shape_is_reported(self):
        with self.assertRaises(api.WithingsAPIError):
            self._post_returning(b'{"status": 0, "body": ["not", "an", "object"]}')


class TestTheSyncConnectionIsAlwaysClosed(unittest.TestCase):
    """An exception nobody classified must not leak the connection."""

    def test_the_connection_closes_when_a_sync_raises(self):
        from withings_mcp.tools import sync_tools

        conn = MagicMock()
        with (
            patch.object(sync_tools.db, "get_db", return_value=conn),
            patch.object(sync_tools, "_run_sync", side_effect=RuntimeError("unclassified")),
        ):
            with self.assertRaises(RuntimeError):
                sync_tools.run_sync(["body"])
        conn.close.assert_called_once()


class TestTheStatusIsNotQuotedBackUnlessItIsACode(unittest.TestCase):
    """The status comes from the response body, and this message is kept.

    run_sync writes it into sync_log and returns it to the model, so a
    hostile or broken intermediary could put anything there. The Data
    Safety Rules say status codes and operation names only.
    """

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_a_real_status_code_is_still_reported(self, mock_urlopen, _refresh):
        mock_urlopen.return_value = _mock_urlopen({"status": 214})
        with self.assertRaises(WithingsAPIError) as caught:
            post("https://example.com", {"action": "getmeas"})
        self.assertIn("214", str(caught.exception))

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_a_string_status_is_not_quoted_back(self, mock_urlopen, _refresh):
        secret = "SESSION=abcdef-token-shaped-value; weight=72.5kg"
        mock_urlopen.return_value = _mock_urlopen({"status": secret})
        with self.assertRaises(WithingsAPIError) as caught:
            post("https://example.com", {"action": "getmeas"})
        self.assertNotIn("SESSION", str(caught.exception))
        self.assertNotIn("72.5", str(caught.exception))

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_a_structured_status_is_not_quoted_back(self, mock_urlopen, _refresh):
        mock_urlopen.return_value = _mock_urlopen({"status": {"leak": "user@example.invalid"}})
        with self.assertRaises(WithingsAPIError) as caught:
            post("https://example.com", {"action": "getmeas"})
        self.assertNotIn("example.invalid", str(caught.exception))

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_a_boolean_status_is_not_reported_as_a_code(self, mock_urlopen, _refresh):
        """bool is a subclass of int, so the carve-out is load-bearing.

        Without it, True renders as "status True", which is not a code.
        """
        mock_urlopen.return_value = _mock_urlopen({"status": True})
        with self.assertRaises(WithingsAPIError) as caught:
            post("https://example.com", {"action": "getmeas"})
        self.assertIn("unrecognised", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
