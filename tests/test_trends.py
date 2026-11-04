"""Tests for trend analysis: aggregation, comparison, sparse data."""

import unittest
from datetime import date, timedelta

from withings_mcp.db import (
    get_db,
    save_activity,
    save_body_measurement,
    save_sleep_summary,
)
from withings_mcp.errors import InvalidDateError
from withings_mcp.tools.analysis_tools import (
    _compare_periods,
    _compare_ranges,
    _get_period_key,
    _trend_activity,
    _trend_body,
    _trend_sleep,
)


def _get_test_db():
    return get_db(":memory:")


def _body_row(date, grpid, weight=70.0, fat_pct=20.0, muscle=30.0):
    return {
        "date": date,
        "measured_at": f"{date}T08:00:00+00:00",
        "grpid": grpid,
        "weight_kg": weight,
        "fat_pct": fat_pct,
        "fat_mass_kg": weight * fat_pct / 100,
        "lean_mass_kg": None,
        "muscle_mass_kg": muscle,
        "hydration_kg": None,
        "bone_mass_kg": None,
        "heart_rate": None,
        "systolic_bp": None,
        "diastolic_bp": None,
        "spo2_pct": None,
        "temperature_c": None,
        "visceral_fat_index": None,
        "basal_metabolic_rate": None,
    }


def _sleep_row(date, device="32", total=28800, score=78):
    return {
        "date": date,
        "startdate": f"{date}T23:00:00+00:00",
        "enddate": f"{date}T07:00:00+00:00",
        "total_sleep_sec": total,
        "deep_sleep_sec": 3600,
        "light_sleep_sec": 14400,
        "rem_sleep_sec": 7200,
        "awake_sec": 3600,
        "wakeup_count": 2,
        "hr_average": 58,
        "hr_min": 48,
        "hr_max": 72,
        "rr_average": 15,
        "rr_min": 12,
        "rr_max": 19,
        "sleep_score": score,
        "snoring_sec": 600,
        "apnea_hypopnea_index": None,
        "device_model": device,
    }


def _activity_row(date, steps=8000, distance=6000):
    return {
        "date": date,
        "steps": steps,
        "distance_m": distance,
        "active_calories": 250,
        "total_calories": 1850,
        "soft_sec": 5400,
        "moderate_sec": 1800,
        "intense_sec": 600,
        "hr_average": 72,
        "hr_min": 55,
        "hr_max": 145,
        "hr_zone_0_sec": 3600,
        "hr_zone_1_sec": 1800,
        "hr_zone_2_sec": 600,
        "hr_zone_3_sec": 120,
    }


class TestPeriodKey(unittest.TestCase):
    def test_monthly(self):
        self.assertEqual(_get_period_key("2026-03-15", "monthly"), "2026-03")

    def test_quarterly(self):
        self.assertEqual(_get_period_key("2026-01-15", "quarterly"), "2026-Q1")
        self.assertEqual(_get_period_key("2026-04-15", "quarterly"), "2026-Q2")
        self.assertEqual(_get_period_key("2026-12-15", "quarterly"), "2026-Q4")

    def test_weekly(self):
        key = _get_period_key("2026-01-15", "weekly")
        self.assertRegex(key, r"^\d{4}-W\d{2}$")


class TestBodyTrends(unittest.TestCase):
    def test_monthly_averages(self):
        conn = _get_test_db()
        # Jan: 70, 71 -> avg 70.5. Feb: 69, 70 -> avg 69.5. Mar: 68.
        for i, (d, w) in enumerate(
            [
                ("2026-01-05", 70.0),
                ("2026-01-20", 71.0),
                ("2026-02-05", 69.0),
                ("2026-02-20", 70.0),
                ("2026-03-10", 68.0),
            ]
        ):
            save_body_measurement(conn, _body_row(d, grpid=2000 + i, weight=w))
        conn.commit()

        result = _trend_body(conn, "2026-01-01", "2026-03-31", "monthly")
        periods = result["periods"]
        self.assertEqual(len(periods), 3)
        self.assertAlmostEqual(periods[0]["weight_kg"], 70.5, places=1)
        self.assertAlmostEqual(periods[1]["weight_kg"], 69.5, places=1)
        self.assertAlmostEqual(periods[2]["weight_kg"], 68.0, places=1)

    def test_quarterly_aggregation(self):
        conn = _get_test_db()
        save_body_measurement(conn, _body_row("2026-01-15", 3001, weight=70.0))
        save_body_measurement(conn, _body_row("2026-04-15", 3002, weight=68.0))
        conn.commit()

        result = _trend_body(conn, "2026-01-01", "2026-06-30", "quarterly")
        periods = result["periods"]
        self.assertEqual(len(periods), 2)
        self.assertEqual(periods[0]["period"], "2026-Q1")
        self.assertEqual(periods[1]["period"], "2026-Q2")

    def test_empty_cache(self):
        conn = _get_test_db()
        result = _trend_body(conn, "2026-01-01", "2026-03-31", "monthly")
        self.assertIn("message", result)

    def test_sparse_data(self):
        conn = _get_test_db()
        # Only Jan and Mar, Feb is missing
        save_body_measurement(conn, _body_row("2026-01-15", 4001, weight=70.0))
        save_body_measurement(conn, _body_row("2026-03-15", 4002, weight=69.0))
        conn.commit()

        result = _trend_body(conn, "2026-01-01", "2026-03-31", "monthly")
        periods = result["periods"]
        # Should have 2 periods, not 3 (Feb is skipped, not zero-averaged)
        self.assertEqual(len(periods), 2)


class TestSleepTrends(unittest.TestCase):
    def test_monthly_sleep(self):
        conn = _get_test_db()
        save_sleep_summary(conn, _sleep_row("2026-01-10", score=75))
        save_sleep_summary(conn, _sleep_row("2026-01-20", score=80))
        save_sleep_summary(conn, _sleep_row("2026-02-10", score=85))
        conn.commit()

        result = _trend_sleep(conn, "2026-01-01", "2026-02-28", "monthly")
        periods = result["periods"]
        self.assertEqual(len(periods), 2)
        self.assertAlmostEqual(periods[0]["avg_sleep_score"], 77.5, places=1)
        self.assertAlmostEqual(periods[1]["avg_sleep_score"], 85.0, places=1)


class TestActivityTrends(unittest.TestCase):
    def test_monthly_steps(self):
        conn = _get_test_db()
        save_activity(conn, _activity_row("2026-01-10", steps=8000))
        save_activity(conn, _activity_row("2026-01-20", steps=12000))
        save_activity(conn, _activity_row("2026-02-10", steps=5000))
        conn.commit()

        result = _trend_activity(conn, "2026-01-01", "2026-02-28", "monthly")
        periods = result["periods"]
        self.assertEqual(len(periods), 2)
        self.assertAlmostEqual(periods[0]["avg_steps"], 10000, places=0)
        self.assertAlmostEqual(periods[1]["avg_steps"], 5000, places=0)

    def test_total_distance(self):
        conn = _get_test_db()
        save_activity(conn, _activity_row("2026-01-10", distance=5000))
        save_activity(conn, _activity_row("2026-01-20", distance=7000))
        conn.commit()

        result = _trend_activity(conn, "2026-01-01", "2026-01-31", "monthly")
        # Total distance = 12000m = 12.0 km
        self.assertAlmostEqual(result["periods"][0]["total_distance_km"], 12.0, places=1)


class TestComparePeriods(unittest.TestCase):
    def test_compare_months(self):
        conn = _get_test_db()
        save_body_measurement(conn, _body_row("2026-02-15", 5001, weight=71.0))
        save_body_measurement(conn, _body_row("2026-03-15", 5002, weight=69.0))
        conn.commit()

        result = _compare_periods(conn, "body", _compare_ranges("2026-03 vs 2026-02"))
        self.assertEqual(result["period_1"]["period"], "2026-03-01 to 2026-03-31")
        self.assertEqual(result["period_2"]["period"], "2026-02-01 to 2026-02-28")
        self.assertAlmostEqual(result["period_1"]["avg_weight"], 69.0)
        self.assertAlmostEqual(result["period_2"]["avg_weight"], 71.0)

    def test_invalid_format(self):
        with self.assertRaises(InvalidDateError) as caught:
            _compare_ranges("invalid string")
        self.assertIn("last_30d vs previous_30d", str(caught.exception))

    def test_each_form_covers_its_whole_span(self):
        today = date.today()
        days = timedelta(days=1)
        for compare, spans in (
            (
                "2026-03 vs 2026-02",
                [(date(2026, 3, 1), date(2026, 3, 31)), (date(2026, 2, 1), date(2026, 2, 28))],
            ),
            (
                "2026-Q4 vs 2026-Q1",
                [(date(2026, 10, 1), date(2026, 12, 31)), (date(2026, 1, 1), date(2026, 3, 31))],
            ),
            (
                "last_30d vs previous_30d",
                [(today - 29 * days, today), (today - 59 * days, today - 30 * days)],
            ),
            ("last_1d vs previous_1d", [(today, today), (today - days, today - days)]),
        ):
            with self.subTest(compare=compare):
                self.assertEqual(_compare_ranges(compare), spans)

    def test_a_period_that_names_no_dates_is_refused_by_name(self):
        # A month of 13 or a count past year 1 raised, which reaches the
        # caller masked; a zero or a trailing suffix answered as if valid,
        # last_0d with a range ending before it starts.
        for compare, bad in (
            ("2026-13 vs 2026-02", "2026-13"),
            ("2026-02 vs 2026-00", "2026-00"),
            ("last_99999999d vs previous_30d", "last_99999999d"),
            ("previous_99999999d vs last_30d", "previous_99999999d"),
            ("last_0d vs previous_30d", "last_0d"),
            ("last_30d vs previous_0d", "previous_0d"),
            ("last_30dx vs previous_30d", "last_30dx"),
            ("2026-02x vs 2026-03", "2026-02x"),
            ("last_30d vs 2026-Q1x", "2026-Q1x"),
        ):
            with self.subTest(compare=compare):
                with self.assertRaises(InvalidDateError) as caught:
                    _compare_ranges(compare)
                self.assertEqual(
                    str(caught.exception),
                    f"Cannot parse period '{bad}'. "
                    "Use: last_30d, previous_30d, 2026-03, or 2026-Q1",
                )


if __name__ == "__main__":
    unittest.main()
