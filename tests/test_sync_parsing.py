"""Tests for the sync parsers (_sync_body, _sync_sleep).

These exercise the response-parsing and upsert logic that turns a Withings
API payload into database rows - measure-group flattening, measure-type
resolution, value decoding, the temperature fallback/precedence, pagination
(including offset threading), sparse/unknown/missing fields, and the commit -
against an in-memory database with the API mocked. All input comes from the
fictional tests.fixtures factories.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tests.fixtures import fake_measure_group, fake_sleep_summary
from withings_mcp.db import SCHEMA
from withings_mcp.tools import sync_tools

# The date/isoformat the parsers derive from the fixtures' default timestamp.
_TS = 1736899200
_EXPECTED_DATE = datetime.fromtimestamp(_TS, tz=timezone.utc).strftime("%Y-%m-%d")
_EXPECTED_ISO = datetime.fromtimestamp(_TS, tz=timezone.utc).isoformat()


class _CountingConn(sqlite3.Connection):
    """Connection that records how many times commit() was called."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commit_count = 0

    def commit(self):
        self.commit_count += 1
        super().commit()


def make_conn(factory=None):
    conn = sqlite3.connect(":memory:", factory=factory or sqlite3.Connection)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def body_rows(conn):
    return conn.execute("SELECT * FROM body_measurements ORDER BY grpid").fetchall()


def sleep_rows(conn):
    return conn.execute("SELECT * FROM sleep_summaries ORDER BY date").fetchall()


def temp_group(grpid, temperature=None, body_temperature=None):
    """A measure group carrying only temperature measures."""
    return fake_measure_group(
        grpid=grpid,
        timestamp=_TS,
        weight=None,
        fat_pct=None,
        fat_mass=None,
        muscle_mass=None,
        hydration=None,
        bone_mass=None,
        temperature=temperature,
        body_temperature=body_temperature,
    )


class TestSyncBody(unittest.TestCase):
    def test_single_group_decoded(self):
        resp = {"measuregrps": [fake_measure_group(grpid=1001, timestamp=_TS)], "more": 0}
        conn = make_conn()
        with patch.object(sync_tools.api, "post", return_value=resp):
            count = sync_tools._sync_body(conn, 0, _TS)
        rows = body_rows(conn)
        self.assertEqual(count, 1)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["grpid"], 1001)
        self.assertEqual(r["date"], _EXPECTED_DATE)
        self.assertEqual(r["weight_kg"], 70.0)
        self.assertEqual(r["fat_pct"], 20.0)
        self.assertEqual(r["muscle_mass_kg"], 30.0)
        self.assertEqual(r["bone_mass_kg"], 3.0)

    def test_pagination_threads_offset(self):
        page1 = {
            "measuregrps": [fake_measure_group(grpid=1001, timestamp=_TS)],
            "more": 1,
            "offset": 5,
        }
        page2 = {"measuregrps": [fake_measure_group(grpid=1002, timestamp=_TS)], "more": 0}
        conn = make_conn()
        with patch.object(sync_tools.api, "post", side_effect=[page1, page2]) as mock_post:
            count = sync_tools._sync_body(conn, 0, _TS)
        self.assertEqual(count, 2)
        self.assertEqual(mock_post.call_count, 2)
        # The offset returned by page 1 must be threaded into the page-2 request.
        self.assertEqual(mock_post.call_args_list[1].args[1]["offset"], 5)
        self.assertEqual([r["grpid"] for r in body_rows(conn)], [1001, 1002])

    def test_more_accepts_bool_true(self):
        # The body loop guard is `more in (1, True)` - the bool variant must page too.
        page1 = {"measuregrps": [fake_measure_group(grpid=1001, timestamp=_TS)], "more": True}
        page2 = {"measuregrps": [fake_measure_group(grpid=1002, timestamp=_TS)], "more": 0}
        conn = make_conn()
        with patch.object(sync_tools.api, "post", side_effect=[page1, page2]) as mock_post:
            count = sync_tools._sync_body(conn, 0, _TS)
        self.assertEqual(count, 2)
        self.assertEqual(mock_post.call_count, 2)

    def test_unknown_measure_type_skipped(self):
        grp = fake_measure_group(grpid=1003, timestamp=_TS)
        grp["measures"].append({"type": 9999, "value": 123, "unit": 0})  # not in MEASURE_TYPES
        conn = make_conn()
        with patch.object(sync_tools.api, "post", return_value={"measuregrps": [grp], "more": 0}):
            count = sync_tools._sync_body(conn, 0, _TS)
        rows = body_rows(conn)
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]["weight_kg"], 70.0)  # known types still parsed, unknown ignored

    def test_temperature_from_type_12(self):
        conn = make_conn()
        resp = {"measuregrps": [temp_group(2001, temperature=36.8)], "more": 0}
        with patch.object(sync_tools.api, "post", return_value=resp):
            sync_tools._sync_body(conn, 0, _TS)
        self.assertEqual(body_rows(conn)[0]["temperature_c"], 36.8)

    def test_temperature_falls_back_to_body_temperature(self):
        conn = make_conn()
        resp = {"measuregrps": [temp_group(2002, body_temperature=36.5)], "more": 0}
        with patch.object(sync_tools.api, "post", return_value=resp):
            sync_tools._sync_body(conn, 0, _TS)
        self.assertEqual(body_rows(conn)[0]["temperature_c"], 36.5)

    def test_temperature_type_12_takes_precedence(self):
        # When both are present, the direct temperature_c (type 12) wins.
        conn = make_conn()
        grp = temp_group(2003, temperature=36.8, body_temperature=99.9)
        with patch.object(sync_tools.api, "post", return_value={"measuregrps": [grp], "more": 0}):
            sync_tools._sync_body(conn, 0, _TS)
        self.assertEqual(body_rows(conn)[0]["temperature_c"], 36.8)

    def test_empty_response_stores_nothing(self):
        conn = make_conn()
        with patch.object(sync_tools.api, "post", return_value={"measuregrps": [], "more": 0}):
            count = sync_tools._sync_body(conn, 0, _TS)
        self.assertEqual(count, 0)
        self.assertEqual(body_rows(conn), [])

    def test_commit_is_called(self):
        conn = make_conn(factory=_CountingConn)
        resp = {"measuregrps": [fake_measure_group(grpid=1001, timestamp=_TS)], "more": 0}
        with patch.object(sync_tools.api, "post", return_value=resp):
            sync_tools._sync_body(conn, 0, _TS)
        self.assertGreaterEqual(conn.commit_count, 1)


class TestSyncSleep(unittest.TestCase):
    def test_single_entry_decoded(self):
        resp = {"series": [fake_sleep_summary(date="2026-01-15", startdate=_TS)], "more": False}
        conn = make_conn()
        with patch.object(sync_tools.api, "post", return_value=resp):
            count = sync_tools._sync_sleep(conn, "2026-01-01", "2026-01-31")
        rows = sleep_rows(conn)
        self.assertEqual(count, 1)
        r = rows[0]
        self.assertEqual(r["date"], "2026-01-15")
        self.assertEqual(r["total_sleep_sec"], 28800)
        self.assertEqual(r["deep_sleep_sec"], 3600)
        self.assertEqual(r["rem_sleep_sec"], 7200)
        self.assertEqual(r["wakeup_count"], 2)
        self.assertEqual(r["sleep_score"], 78)
        self.assertEqual(r["hr_average"], 58)
        self.assertEqual(r["device_model"], "32")  # model int -> str
        self.assertEqual(r["startdate"], _EXPECTED_ISO)

    def test_pagination_threads_offset(self):
        page1 = {"series": [fake_sleep_summary(date="2026-01-15")], "more": True, "offset": 1}
        page2 = {"series": [fake_sleep_summary(date="2026-01-16")], "more": False}
        conn = make_conn()
        with patch.object(sync_tools.api, "post", side_effect=[page1, page2]) as mock_post:
            count = sync_tools._sync_sleep(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(count, 2)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_post.call_args_list[1].args[1]["offset"], 1)
        self.assertEqual([r["date"] for r in sleep_rows(conn)], ["2026-01-15", "2026-01-16"])

    def test_sparse_data_fields_become_null(self):
        # A summary carrying only total sleep time stores, with NULLs for the gaps.
        entry = fake_sleep_summary(
            date="2026-02-01",
            deep_sleep=None,
            light_sleep=None,
            rem_sleep=None,
            awake=None,
            wakeup_count=None,
            hr_average=None,
            hr_min=None,
            hr_max=None,
            rr_average=None,
            rr_min=None,
            rr_max=None,
            sleep_score=None,
            snoring=None,
        )
        conn = make_conn()
        with patch.object(sync_tools.api, "post", return_value={"series": [entry], "more": False}):
            count = sync_tools._sync_sleep(conn, "2026-02-01", "2026-02-28")
        r = sleep_rows(conn)[0]
        self.assertEqual(count, 1)
        self.assertEqual(r["total_sleep_sec"], 28800)
        self.assertIsNone(r["deep_sleep_sec"])
        self.assertIsNone(r["sleep_score"])

    def test_missing_startdate_stored_as_null(self):
        # No startdate/enddate -> the guard stores NULL rather than raising.
        entry = fake_sleep_summary(date="2026-03-01", startdate=None, enddate=None)
        conn = make_conn()
        with patch.object(sync_tools.api, "post", return_value={"series": [entry], "more": False}):
            sync_tools._sync_sleep(conn, "2026-03-01", "2026-03-31")
        r = sleep_rows(conn)[0]
        self.assertIsNone(r["startdate"])
        self.assertIsNone(r["enddate"])

    def test_empty_response_stores_nothing(self):
        conn = make_conn()
        with patch.object(sync_tools.api, "post", return_value={"series": [], "more": False}):
            count = sync_tools._sync_sleep(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(count, 0)
        self.assertEqual(sleep_rows(conn), [])


class TestSyncLogRecordsFailures(unittest.TestCase):
    """Every failure a run_sync call swallows must leave a row in sync_log.

    Nothing re-reports a failed sync afterwards: queries keep serving the
    cache, so an auth failure that is not logged leaves no record anywhere
    that syncing stopped.
    """

    def _run_sync_raising(self, exc):
        """run_sync closes its connection, so the log is read back from a file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "withings.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            with (
                patch.object(sync_tools.db, "get_db", return_value=conn),
                patch.object(sync_tools, "_sync_body", side_effect=exc),
            ):
                results = sync_tools.run_sync(["body"])

            reopened = sqlite3.connect(path)
            reopened.row_factory = sqlite3.Row
            rows = reopened.execute("SELECT data_type, status FROM sync_log").fetchall()
            reopened.close()
        return results, rows

    def test_auth_failure_is_logged(self):
        results, rows = self._run_sync_raising(sync_tools.api.WithingsAuthError("denied"))
        self.assertEqual(results["body"]["status"], "auth_error")
        self.assertEqual([(r["data_type"], r["status"]) for r in rows], [("body", "auth_error")])

    def test_api_failure_is_logged(self):
        _, rows = self._run_sync_raising(sync_tools.api.WithingsAPIError("boom"))
        self.assertEqual([(r["data_type"], r["status"]) for r in rows], [("body", "error")])

    def test_a_rejected_refresh_is_not_logged_and_propagates(self):
        """The boundary of what this class covers, pinned rather than assumed.

        A revoked or missing refresh token raises RuntimeError from auth, which
        run_sync does not catch, so no row is written and the caller sees the
        exception. Widening the catch here without deciding what to log would
        silently change that.
        """
        with self.assertRaises(RuntimeError):
            self._run_sync_raising(RuntimeError("Token refresh failed"))

    def test_rate_limit_is_logged(self):
        _, rows = self._run_sync_raising(sync_tools.api.WithingsRateLimitError("slow down"))
        self.assertEqual([(r["data_type"], r["status"]) for r in rows], [("body", "partial")])

    def test_a_logged_failure_does_not_advance_the_resume_cursor(self):
        """Logging failures must not move the backfill start date.

        run_sync resumes from get_last_sync, so if a failure row counted as
        progress, the days it failed on would never be fetched again.
        """
        conn = make_conn()
        sync_tools.db.log_sync(conn, "body", "ok", 1)
        good = conn.execute("SELECT synced_at FROM sync_log").fetchone()["synced_at"]
        for status in ("auth_error", "error", "partial"):
            sync_tools.db.log_sync(conn, "body", status, notes="later but not progress")
        self.assertEqual(sync_tools.db.get_last_sync(conn, "body"), good)


if __name__ == "__main__":
    unittest.main()
