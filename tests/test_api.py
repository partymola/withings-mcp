"""Tests for API layer: status-in-body error handling, PII non-leakage."""

import json
import unittest
from unittest.mock import patch, MagicMock
from io import BytesIO

from withings_mcp.api import (
    WithingsAPIError,
    WithingsAuthError,
    WithingsRateLimitError,
    post,
)

from tests.fixtures import fake_api_response


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
        mock_urlopen.return_value = _mock_urlopen(
            fake_api_response(status=0, body=body_data)
        )
        result = post("https://example.com", {"action": "getmeas"})
        self.assertEqual(result, body_data)

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_401_raises_auth_error(self, mock_urlopen, mock_refresh):
        mock_urlopen.return_value = _mock_urlopen(
            fake_api_response(status=401)
        )
        with self.assertRaises(WithingsAuthError) as ctx:
            post("https://example.com", {"action": "getmeas"})
        self.assertIn("withings-mcp auth", str(ctx.exception))

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_601_raises_rate_limit(self, mock_urlopen, mock_refresh):
        mock_urlopen.return_value = _mock_urlopen(
            fake_api_response(status=601)
        )
        with self.assertRaises(WithingsRateLimitError) as ctx:
            post("https://example.com", {"action": "getmeas"}, retries=1)
        self.assertIn("60 seconds", str(ctx.exception))

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_602_raises_rate_limit(self, mock_urlopen, mock_refresh):
        mock_urlopen.return_value = _mock_urlopen(
            fake_api_response(status=602)
        )
        with self.assertRaises(WithingsRateLimitError):
            post("https://example.com", {"action": "getmeas"}, retries=1)

    @patch("withings_mcp.api.refresh_token", return_value="fake_token")
    @patch("withings_mcp.api.urllib.request.urlopen")
    def test_unknown_status_raises_api_error(self, mock_urlopen, mock_refresh):
        mock_urlopen.return_value = _mock_urlopen(
            fake_api_response(status=2555)
        )
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
        mock_urlopen.return_value = _mock_urlopen(
            {"status": 0}
        )
        result = post("https://example.com", {"action": "getdevice"})
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
