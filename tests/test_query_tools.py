"""Tests for the withings_get_* query tools.

Each tool is driven end-to-end (the real async coroutine, run under asyncio)
with auth bypassed, the API mocked, and either an in-memory-style temp cache
seeded via the db helpers or a mocked ``api.post`` for the live/always-live
paths. Covers cache-hit, the ``live=True`` branch, the always-live tools
(heart, devices), detailed sleep, metric/category filters, pagination, the
7-day detail clamp, and the empty/no-data branches. All payloads come from the
fictional tests.fixtures factories.
"""

import ast
import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fixtures import (
    fake_activity,
    fake_activity_db_row,
    fake_body_db_row,
    fake_device,
    fake_ecg_recording,
    fake_measure_group,
    fake_sleep_db_row,
    fake_sleep_phase,
    fake_sleep_summary,
    fake_workout,
    fake_workout_db_row,
)
from withings_mcp import db, helpers
from withings_mcp.tools import activity_tools, body_tools, device_tools, heart_tools, sleep_tools

# The fixtures' default timestamp resolves to this UTC instant. Hard-coded (not
# derived with the SUT's own datetime call) so the assertions catch a regression
# to naive/local time. See test_tz_shift_does_not_move_the_date for the tz guard.
_TS = 1736899200
_TS_DATE = "2025-01-15"
_TS_DATETIME = "2025-01-15 00:00"
_TS_ISO = "2025-01-15T00:00:00+00:00"


def _run(coro):
    """Drive a tool coroutine to completion (anyio runs on the asyncio backend)."""
    return asyncio.run(coro)


def _payload(coro):
    """Run a tool and return its parsed JSON response."""
    return json.loads(_run(coro))


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Make require_auth pass without real credential files."""
    ok = Mock()
    ok.exists.return_value = True
    with (
        patch.object(helpers, "WITHINGS_CLIENT_PATH", ok),
        patch.object(helpers, "WITHINGS_TOKENS_PATH", ok),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_autosync():
    """Stop cache reads from triggering a real incremental sync."""
    with (
        patch.object(body_tools, "auto_sync_if_stale"),
        patch.object(sleep_tools, "auto_sync_if_stale"),
        patch.object(activity_tools, "auto_sync_if_stale"),
    ):
        yield


# Windows has no time.tzset and does not re-read TZ after start, so the zone
# cannot be forced there and the module runs under the runner's own.
_TZ_CAN_BE_FORCED = hasattr(time, "tzset")
needs_forced_tz = pytest.mark.skipif(
    not _TZ_CAN_BE_FORCED, reason="tz guard needs time.tzset, absent on Windows"
)


@pytest.fixture(autouse=True)
def _force_non_utc_tz():
    """Run every test under a non-UTC zone.

    All timestamp encode/decode in the tools must pin UTC explicitly. CI runners
    are UTC, so a dropped tz=timezone.utc is invisible there; forcing a western
    zone makes every date/epoch assertion in this module a real tz regression
    guard (a naive decode of a UTC-midnight timestamp rolls back to the prior
    day).

    Where the zone cannot be forced the module still runs, but under UTC, and
    the date/epoch assertions below stop being tz guards - a naive decode or a
    naive encode passes most of them. The two tests written for the shift alone
    carry `needs_forced_tz` so they skip rather than passing vacuously, and
    TestEveryConversionPinsTheZone carries the guarantee itself on every
    platform by reading the source.
    """
    if not _TZ_CAN_BE_FORCED:
        yield
        return

    prev = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


@pytest.fixture
def cache(tmp_path):
    """A temp SQLite cache wired into db.get_db(); yields a seed() helper."""
    path = tmp_path / "withings.db"

    def seed(save_fn, *rows):
        conn = db.get_db(str(path))
        for row in rows:
            save_fn(conn, row)
        conn.commit()
        conn.close()

    with patch.object(db, "DB_PATH", path):
        yield seed


_SRC = Path(__file__).resolve().parents[1] / "src" / "withings_mcp"

# The one conversion that is naive on purpose: doctor prints a token expiry for
# whoever is reading the terminal, so their own zone is the useful one. Named by
# the function holding it rather than by file, so moving or renaming it fails
# here instead of quietly widening the exemption to the rest of doctor.py.
_NAIVE_BY_DESIGN = ("doctor.py", "_timestamp_or_none")


def _calls(attr):
    """Every `<x>.<attr>(...)` in the package, as (site, enclosing def, node)."""
    found = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        # Walk is outermost-first, so a nested def overwrites its parent and the
        # innermost enclosing name is the one that survives.
        enclosing = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    enclosing[id(child)] = node.name
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr
            ):
                site = f"{path.name}:{node.lineno}"
                found.append((site, (path.name, enclosing.get(id(node))), node))
    return found


def _names_a_zone(node, keyword):
    """True when a real zone is passed, by keyword or position.

    An explicit `None` is rejected rather than counted: it is what the argument
    already defaults to, so accepting it would let the check pass over a
    conversion that still reads the machine's zone.
    """
    for kw in node.keywords:
        if kw.arg == keyword:
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    position = {"tz": 1, "tzinfo": 2}[keyword]
    if len(node.args) > position:
        arg = node.args[position]
        return not (isinstance(arg, ast.Constant) and arg.value is None)
    return False


class TestEveryConversionPinsTheZone:
    """The half of the fixture's guarantee that no assertion can see without tzset.

    Forcing a western zone turns a dropped `tz=timezone.utc` into a failed
    assertion, but only where `time.tzset` exists. Reading the source holds
    identically on every platform, and it covers the encode direction, which on
    a runner without tzset nothing in this module can see at all.

    Scoped to the whole package with one named exemption rather than to
    `tools/`: a directory boundary would have let a conversion helper added to
    `helpers.py` - the module every tool imports - pass without naming a zone.
    """

    def test_every_fromtimestamp_names_a_zone(self):
        calls = _calls("fromtimestamp")
        assert calls, "no fromtimestamp found - has the decode moved?"
        assert [c for c in calls if c[1] == _NAIVE_BY_DESIGN], (
            f"{_NAIVE_BY_DESIGN} no longer holds a naive decode - move the exemption or drop it"
        )
        naive = [
            site
            for site, where, node in calls
            if where != _NAIVE_BY_DESIGN and not _names_a_zone(node, "tz")
        ]
        assert not naive, f"decodes in local time: {naive}"

    def test_every_combine_names_a_zone(self):
        calls = _calls("combine")
        assert calls, "no datetime.combine found - has the encode moved?"
        naive = [site for site, _where, node in calls if not _names_a_zone(node, "tzinfo")]
        assert not naive, f"encodes from local time: {naive}"

    def test_the_naive_conversion_helpers_are_not_used(self):
        """Both sidestep the checks above by name while returning local time."""
        assert not _calls("utcfromtimestamp")
        assert not _calls("mktime")

    def test_no_epoch_is_taken_from_a_naive_parse(self):
        """`strptime` and `fromisoformat` yield naive datetimes unless the text
        carries an offset, so `.timestamp()` on one reads the machine's zone.
        It is the natural way to write date-to-epoch, and the checks above see
        straight past it."""
        offenders = [
            site
            for site, _where, node in _calls("timestamp")
            if isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Attribute)
            and node.func.value.func.attr in {"strptime", "fromisoformat"}
        ]
        assert not offenders, f"encodes from a naive parse: {offenders}"


# --- withings_get_body ---


class TestGetBody:
    def test_cache_hit_returns_seeded_rows(self, cache):
        cache(
            db.save_body_measurement,
            fake_body_db_row(date="2026-01-10", grpid=1001, weight_kg=70.0),
            fake_body_db_row(date="2026-01-20", grpid=1002, weight_kg=71.5),
        )
        out = _payload(body_tools.withings_get_body("2026-01-01", "2026-01-31"))
        assert out["count"] == 2
        # query_body orders by date ascending.
        assert [m["date"] for m in out["measurements"]] == ["2026-01-10", "2026-01-20"]
        assert out["measurements"][0]["weight_kg"] == 70.0
        assert out["measurements"][1]["weight_kg"] == 71.5

    def test_cache_hit_respects_date_range(self, cache):
        cache(
            db.save_body_measurement,
            fake_body_db_row(date="2026-01-05", grpid=1001),
            fake_body_db_row(date="2026-03-05", grpid=1002),
        )
        out = _payload(body_tools.withings_get_body("2026-01-01", "2026-01-31"))
        assert out["count"] == 1
        assert out["measurements"][0]["date"] == "2026-01-05"

    def test_metrics_filter_keeps_only_requested_plus_date(self, cache):
        cache(db.save_body_measurement, fake_body_db_row(date="2026-01-10", grpid=1001))
        out = _payload(
            body_tools.withings_get_body("2026-01-01", "2026-01-31", metrics="weight_kg,fat_pct")
        )
        assert set(out["measurements"][0].keys()) == {"date", "weight_kg", "fat_pct"}

    def test_empty_cache_returns_hint_not_data(self, cache):
        out = _payload(body_tools.withings_get_body("2026-01-01", "2026-01-31"))
        assert "measurements" not in out
        assert "message" in out and "hint" in out

    def test_live_decodes_api_measures(self):
        resp = {"measuregrps": [fake_measure_group(grpid=1001, timestamp=_TS)], "more": 0}
        with patch.object(body_tools.api, "post", return_value=resp) as post:
            out = _payload(body_tools.withings_get_body(live=True))
        post.assert_called_once()
        assert out["count"] == 1
        m = out["measurements"][0]
        assert m["date"] == _TS_DATE  # derived from the group timestamp, not the fixture string
        assert m["weight_kg"] == 70.0
        assert m["muscle_mass_kg"] == 30.0

    def test_live_does_not_touch_cache(self, cache):
        # A seeded cache must be ignored on the live path.
        cache(db.save_body_measurement, fake_body_db_row(date="2026-01-10", grpid=9999))
        resp = {"measuregrps": [fake_measure_group(grpid=1001, timestamp=_TS)], "more": 0}
        with patch.object(body_tools.api, "post", return_value=resp):
            out = _payload(body_tools.withings_get_body(live=True))
        assert [m["date"] for m in out["measurements"]] == [_TS_DATE]

    def test_live_paginates_and_threads_offset(self):
        page1 = {
            "measuregrps": [fake_measure_group(grpid=1001, timestamp=_TS)],
            "more": 1,  # body's loop guard is `more in (1, True)`
            "offset": 7,
        }
        page2 = {"measuregrps": [fake_measure_group(grpid=1002, timestamp=_TS)], "more": 0}
        with patch.object(body_tools.api, "post", side_effect=[page1, page2]) as post:
            out = _payload(body_tools.withings_get_body(live=True))
        assert out["count"] == 2
        assert post.call_count == 2
        assert post.call_args_list[0].args[1]["offset"] == 0  # first page starts at 0
        # The nonzero offset from page 1 must be threaded into the page-2 request.
        assert post.call_args_list[1].args[1]["offset"] == 7

    def test_live_skips_unknown_measure_types(self):
        grp = fake_measure_group(grpid=1001, timestamp=_TS)
        grp["measures"].append({"type": 999, "value": 123, "unit": 0})  # not in MEASURE_TYPES
        with patch.object(body_tools.api, "post", return_value={"measuregrps": [grp], "more": 0}):
            out = _payload(body_tools.withings_get_body(live=True))
        entry = out["measurements"][0]
        assert entry["weight_kg"] == 70.0  # known types still decoded
        assert "type_999" not in entry  # the unknown measure is dropped

    def test_empty_live_still_shows_cache_hint(self):
        # Known quirk: even on the live path the empty response advertises live=True.
        with patch.object(body_tools.api, "post", return_value={"measuregrps": [], "more": 0}):
            out = _payload(body_tools.withings_get_body(live=True))
        assert "measurements" not in out
        assert out["hint"] == "Try live=True to fetch directly from the API."

    def test_unknown_metric_filter_is_refused(self, cache):
        # It used to strip every value and answer with bare dates, which reads
        # as measurements that exist and hold nothing. What the refusal says,
        # and the same class on withings_get_workouts, is in
        # tests/test_filter_arguments.py.
        cache(db.save_body_measurement, fake_body_db_row(date="2026-01-10", grpid=1001))
        # require_auth has already converted it by the time it escapes the tool.
        with pytest.raises(ToolError, match="no_such_metric"):
            _run(body_tools.withings_get_body("2026-01-01", "2026-01-31", metrics="no_such_metric"))

    @needs_forced_tz
    def test_live_date_decode_is_utc_not_local(self):
        # Under the module's non-UTC TZ, a naive decode of this UTC-midnight
        # timestamp would roll the date back to 2025-01-14.
        resp = {"measuregrps": [fake_measure_group(grpid=1001, timestamp=_TS)], "more": 0}
        with patch.object(body_tools.api, "post", return_value=resp):
            out = _payload(body_tools.withings_get_body(live=True))
        assert out["measurements"][0]["date"] == _TS_DATE

    @needs_forced_tz
    def test_live_request_encodes_utc_window(self):
        # The request window is encoded as UTC-midnight epochs regardless of TZ.
        with patch.object(body_tools.api, "post", return_value={"measuregrps": [], "more": 0}) as p:
            _run(body_tools.withings_get_body("2025-01-01", "2025-01-01", live=True))
        params = p.call_args.args[1]
        assert params["startdate"] == 1735689600  # 2025-01-01 00:00 UTC
        assert params["enddate"] == 1735776000  # 2025-01-02 00:00 UTC (end + 1 exclusive day)


# --- withings_get_sleep ---


class TestGetSleep:
    def test_cache_hit_formats_durations(self, cache):
        cache(db.save_sleep_summary, fake_sleep_db_row(date="2026-01-15", total_sleep_sec=28800))
        out = _payload(sleep_tools.withings_get_sleep("2026-01-01", "2026-01-31"))
        assert out["count"] == 1
        night = out["nights"][0]
        assert night["date"] == "2026-01-15"
        assert night["total_sleep"] == "8h 0m"  # 28800s
        assert night["deep_sleep"] == "1h 0m"  # 3600s
        assert night["sleep_score"] == 78

    def test_empty_cache_returns_hint(self, cache):
        out = _payload(sleep_tools.withings_get_sleep("2026-01-01", "2026-01-31"))
        assert "nights" not in out
        assert "message" in out and "hint" in out

    def test_live_summary_formats_durations(self):
        resp = {"series": [fake_sleep_summary(date="2026-01-15")], "more": False}
        with patch.object(sleep_tools.api, "post", return_value=resp) as post:
            out = _payload(sleep_tools.withings_get_sleep(live=True))
        post.assert_called_once()
        night = out["nights"][0]
        assert night["total_sleep"] == "8h 0m"
        assert night["rem_sleep"] == "2h 0m"  # 7200s
        assert night["snoring"] == "10m"  # 600s

    def test_live_summary_paginates_on_truthy_more(self):
        # Summary/activity/workouts/heart page on `body.get("more")`, unlike body.
        page1 = {"series": [fake_sleep_summary(date="2026-01-15")], "more": True, "offset": 4}
        page2 = {"series": [fake_sleep_summary(date="2026-01-16")], "more": False}
        with patch.object(sleep_tools.api, "post", side_effect=[page1, page2]) as post:
            out = _payload(sleep_tools.withings_get_sleep(live=True))
        assert out["count"] == 2
        assert post.call_count == 2
        assert post.call_args_list[1].args[1]["offset"] == 4

    def test_detail_returns_phases_with_state_names(self):
        resp = {
            "series": [
                fake_sleep_phase(startdate=_TS, enddate=_TS + 3600, state=2, hr=55, rr=14),
                fake_sleep_phase(startdate=_TS + 3600, enddate=_TS + 7200, state=3, hr=60, rr=15),
            ]
        }
        with patch.object(sleep_tools.api, "post", return_value=resp) as post:
            out = _payload(sleep_tools.withings_get_sleep(detail=True))
        # action "get" hits the sleep endpoint once for the detail path.
        assert post.call_args.args[1]["action"] == "get"
        assert out["count"] == 2
        assert [p["state"] for p in out["phases"]] == ["deep", "rem"]
        assert out["phases"][0]["hr"] == 55
        assert out["phases"][0]["start"] == _TS_ISO

    def test_detail_ignores_live_flag_and_stays_live(self, cache):
        # detail=True is always live even with live=False; the cache is never read.
        cache(db.save_sleep_summary, fake_sleep_db_row(date="2026-01-15"))
        resp = {"series": [fake_sleep_phase(startdate=_TS, enddate=_TS + 3600)]}
        with patch.object(sleep_tools.api, "post", return_value=resp) as post:
            out = _payload(sleep_tools.withings_get_sleep(detail=True, live=False))
        post.assert_called_once()
        assert "phases" in out

    def test_detail_defaults_and_resolves_sleep_states(self):
        resp = {
            "series": [
                fake_sleep_phase(startdate=_TS, enddate=_TS + 60, state=0),  # awake
                {"startdate": _TS + 60, "enddate": _TS + 120},  # no state -> defaults to 5
                fake_sleep_phase(startdate=_TS + 120, enddate=_TS + 180, state=99),  # unknown id
            ]
        }
        with patch.object(sleep_tools.api, "post", return_value=resp):
            out = _payload(sleep_tools.withings_get_sleep(detail=True))
        assert [p["state"] for p in out["phases"]] == ["awake", "unspecified", "state_99"]

    def test_detail_clamps_range_to_seven_days(self):
        # A 30-day request must be clamped to start .. start+7 (end-exclusive +1 day);
        # unclamped it would span 31 days, so 8*86400 proves the clamp fired.
        start_ts = 1735689600  # 2025-01-01 00:00 UTC
        resp = {"series": []}
        with patch.object(sleep_tools.api, "post", return_value=resp) as post:
            _run(sleep_tools.withings_get_sleep("2025-01-01", "2025-01-31", detail=True))
        params = post.call_args.args[1]
        assert params["startdate"] == start_ts  # start is never moved
        assert params["enddate"] == start_ts + 8 * 86400  # clamped end + 1 exclusive day

    def test_detail_clamp_boundary_at_eight_days(self):
        # Exactly 8 days apart trips the `> 7` guard; a 7-day span would not.
        start_ts = 1735689600  # 2025-01-01
        with patch.object(sleep_tools.api, "post", return_value={"series": []}) as post:
            _run(sleep_tools.withings_get_sleep("2025-01-01", "2025-01-09", detail=True))
        assert post.call_args.args[1]["enddate"] == start_ts + 8 * 86400  # clamped from 9

    def test_detail_within_seven_days_is_not_clamped(self):
        start_ts = 1735689600  # 2025-01-01
        with patch.object(sleep_tools.api, "post", return_value={"series": []}) as post:
            _run(sleep_tools.withings_get_sleep("2025-01-01", "2025-01-06", detail=True))
        # 5-day span stays 5 days + 1 exclusive day; the clamp does not shrink it.
        assert post.call_args.args[1]["enddate"] == start_ts + 6 * 86400

    def test_detail_empty_returns_message_without_hint(self):
        with patch.object(sleep_tools.api, "post", return_value={"series": []}):
            out = _payload(sleep_tools.withings_get_sleep(detail=True))
        assert "phases" not in out
        assert out["message"] and "hint" not in out


# --- withings_get_activity ---


class TestGetActivity:
    def test_cache_hit_formats_distance_and_duration(self, cache):
        cache(
            db.save_activity,
            fake_activity_db_row(date="2026-01-15", steps=8000, distance_m=6000),
        )
        out = _payload(activity_tools.withings_get_activity("2026-01-01", "2026-01-31"))
        assert out["count"] == 1
        day = out["days"][0]
        assert day["steps"] == 8000
        assert day["distance"] == "6.0 km"  # 6000m
        assert day["light_activity"] == "1h 30m"  # 5400s

    def test_empty_cache_returns_hint(self, cache):
        out = _payload(activity_tools.withings_get_activity("2026-01-01", "2026-01-31"))
        assert "days" not in out
        assert "message" in out and "hint" in out

    def test_live_returns_api_days(self):
        resp = {"activities": [fake_activity(date="2026-01-15", steps=8000)], "more": False}
        with patch.object(activity_tools.api, "post", return_value=resp) as post:
            out = _payload(activity_tools.withings_get_activity(live=True))
        post.assert_called_once()
        assert out["days"][0]["steps"] == 8000
        assert out["days"][0]["distance"] == "6.0 km"

    def test_live_normalizes_single_day_object(self):
        # Withings returns a bare object (not a list) for a single-day range.
        resp = {"activities": fake_activity(date="2026-01-15", steps=8000), "more": False}
        with patch.object(activity_tools.api, "post", return_value=resp):
            out = _payload(activity_tools.withings_get_activity(live=True))
        assert out["count"] == 1
        assert out["days"][0]["steps"] == 8000

    def test_live_paginates_on_truthy_more(self):
        page1 = {"activities": [fake_activity(date="2026-01-15")], "more": True, "offset": 3}
        page2 = {"activities": [fake_activity(date="2026-01-16")], "more": False}
        with patch.object(activity_tools.api, "post", side_effect=[page1, page2]) as post:
            out = _payload(activity_tools.withings_get_activity(live=True))
        assert out["count"] == 2
        assert post.call_count == 2
        assert post.call_args_list[1].args[1]["offset"] == 3


# --- withings_get_workouts ---


class TestGetWorkouts:
    def test_cache_hit_maps_type_and_duration(self, cache):
        cache(
            db.save_workout,
            fake_workout_db_row(date="2026-01-15", category_name="cycling", duration_sec=3600),
        )
        out = _payload(activity_tools.withings_get_workouts("2026-01-01", "2026-01-31"))
        assert out["count"] == 1
        w = out["workouts"][0]
        assert w["type"] == "cycling"
        assert w["duration"] == "1h 0m"
        assert w["distance"] == "15.0 km"  # 15000m

    def test_cache_category_filter_narrows_results(self, cache):
        cache(
            db.save_workout,
            fake_workout_db_row(
                startdate="2026-01-15T10:00:00+00:00", category=6, category_name="cycling"
            ),
            fake_workout_db_row(
                date="2026-01-16",
                startdate="2026-01-16T10:00:00+00:00",
                category=1,
                category_name="walk",
            ),
        )
        out = _payload(
            activity_tools.withings_get_workouts("2026-01-01", "2026-01-31", category="cycling")
        )
        assert out["count"] == 1
        assert out["workouts"][0]["type"] == "cycling"

    def test_empty_cache_returns_hint(self, cache):
        out = _payload(activity_tools.withings_get_workouts("2026-01-01", "2026-01-31"))
        assert "workouts" not in out
        assert "message" in out and "hint" in out

    def test_live_category_filter_drops_non_matches(self):
        resp = {
            "series": [
                fake_workout(category=6),  # cycling
                fake_workout(category=1),  # walk
            ],
            "more": False,
        }
        with patch.object(activity_tools.api, "post", return_value=resp):
            out = _payload(activity_tools.withings_get_workouts(live=True, category="cycling"))
        assert out["count"] == 1
        assert out["workouts"][0]["type"] == "cycling"

    def test_live_missing_timestamps_give_zero_duration(self):
        w = fake_workout(category=6)
        del w["enddate"]  # no end -> duration falls back to 0
        with patch.object(activity_tools.api, "post", return_value={"series": [w], "more": False}):
            out = _payload(activity_tools.withings_get_workouts(live=True))
        assert out["workouts"][0]["duration"] == "0m"

    def test_live_paginates_on_truthy_more(self):
        page1 = {
            "series": [fake_workout(startdate=1736935200, category=6)],
            "more": True,
            "offset": 2,
        }
        page2 = {"series": [fake_workout(startdate=1737021600, category=6)], "more": False}
        with patch.object(activity_tools.api, "post", side_effect=[page1, page2]) as post:
            out = _payload(activity_tools.withings_get_workouts(live=True))
        assert out["count"] == 2
        assert post.call_count == 2
        assert post.call_args_list[1].args[1]["offset"] == 2


# --- withings_get_heart (always live) ---


class TestGetHeart:
    def test_maps_afib_states(self):
        resp = {
            "series": [
                fake_ecg_recording(timestamp=_TS, heart_rate=60, afib=0, signalid=5001),
                fake_ecg_recording(timestamp=_TS, heart_rate=61, afib=1, signalid=5002),
                fake_ecg_recording(timestamp=_TS, heart_rate=62, afib=2, signalid=5003),
                fake_ecg_recording(timestamp=_TS, heart_rate=63, afib=9, signalid=5004),
            ],
            "more": False,
        }
        with patch.object(heart_tools.api, "post", return_value=resp) as post:
            out = _payload(heart_tools.withings_get_heart())
        post.assert_called_once()
        assert out["count"] == 4
        assert [r["afib"] for r in out["recordings"]] == [
            "negative",
            "positive",
            "inconclusive",
            "unknown",
        ]
        assert out["recordings"][0]["date"] == _TS_DATETIME
        assert out["recordings"][0]["signal_id"] == 5001

    def test_paginates_on_truthy_more(self):
        page1 = {"series": [fake_ecg_recording(signalid=5001)], "more": True, "offset": 6}
        page2 = {"series": [fake_ecg_recording(signalid=5002)], "more": False}
        with patch.object(heart_tools.api, "post", side_effect=[page1, page2]) as post:
            out = _payload(heart_tools.withings_get_heart())
        assert out["count"] == 2
        assert post.call_count == 2
        assert post.call_args_list[1].args[1]["offset"] == 6

    def test_missing_ecg_block_maps_to_unknown(self):
        entry = fake_ecg_recording(timestamp=_TS, afib=0)
        del entry["ecg"]  # no ecg block at all -> afib unknown
        with patch.object(heart_tools.api, "post", return_value={"series": [entry], "more": False}):
            out = _payload(heart_tools.withings_get_heart())
        assert out["recordings"][0]["afib"] == "unknown"

    def test_empty_returns_message_and_hint(self):
        with patch.object(heart_tools.api, "post", return_value={"series": [], "more": False}):
            out = _payload(heart_tools.withings_get_heart())
        assert "recordings" not in out
        assert "message" in out and "hint" in out


# --- withings_get_devices (always live) ---


class TestGetDevices:
    def test_formats_last_session(self):
        resp = {"devices": [fake_device(model="Body Comp", battery="high", last_session_date=_TS)]}
        with patch.object(device_tools.api, "post", return_value=resp) as post:
            out = _payload(device_tools.withings_get_devices())
        post.assert_called_once()
        assert out["count"] == 1
        d = out["devices"][0]
        assert d["model"] == "Body Comp"
        assert d["battery"] == "high"
        assert d["last_session"] == _TS_DATETIME

    def test_missing_last_session_stays_falsy(self):
        resp = {"devices": [fake_device(last_session_date=None)]}
        with patch.object(device_tools.api, "post", return_value=resp):
            out = _payload(device_tools.withings_get_devices())
        assert out["devices"][0]["last_session"] is None

    def test_empty_returns_message(self):
        with patch.object(device_tools.api, "post", return_value={"devices": []}):
            out = _payload(device_tools.withings_get_devices())
        assert "devices" not in out
        assert "message" in out and "hint" in out


# --- require_auth gate ---


class TestRequireAuth:
    def test_missing_credentials_short_circuits_before_api(self):
        missing = Mock()
        missing.exists.return_value = False
        with (
            patch.object(helpers, "WITHINGS_CLIENT_PATH", missing),
            patch.object(helpers, "WITHINGS_TOKENS_PATH", missing),
            patch.object(heart_tools.api, "post") as post,
        ):
            out = _payload(heart_tools.withings_get_heart())
        post.assert_not_called()
        assert "error" in out and "auth" in out["error"].lower()

    def test_missing_only_tokens_still_blocks(self):
        # require_auth needs BOTH files; a missing token file alone must gate.
        present = Mock()
        present.exists.return_value = True
        missing = Mock()
        missing.exists.return_value = False
        with (
            patch.object(helpers, "WITHINGS_CLIENT_PATH", present),
            patch.object(helpers, "WITHINGS_TOKENS_PATH", missing),
            patch.object(heart_tools.api, "post") as post,
        ):
            out = _payload(heart_tools.withings_get_heart())
        post.assert_not_called()
        assert "error" in out
