"""Tests for database schema and helper functions.

All tests use in-memory SQLite - no on-disk files created.
"""

import sqlite3
import unittest

from withings_mcp.db import (
    get_db,
    save_body_measurement,
    save_sleep_summary,
    save_activity,
    save_workout,
    log_sync,
    get_last_sync,
    query_body,
    query_sleep,
    query_activities,
    query_workouts,
)


def _get_test_db():
    """Create an in-memory database with the schema applied."""
    return get_db(":memory:")


class TestSchema(unittest.TestCase):
    def test_schema_creates_all_tables(self):
        conn = _get_test_db()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [t["name"] for t in tables]
        self.assertIn("body_measurements", names)
        self.assertIn("sleep_summaries", names)
        self.assertIn("activities", names)
        self.assertIn("workouts", names)
        self.assertIn("sync_log", names)

    def test_schema_creates_indexes(self):
        conn = _get_test_db()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        names = [i["name"] for i in indexes]
        self.assertIn("idx_body_date", names)
        self.assertIn("idx_sleep_date", names)
        self.assertIn("idx_activity_date", names)
        self.assertIn("idx_workout_date", names)


class TestBodyMeasurements(unittest.TestCase):
    def _make_row(self, grpid=1001, date="2026-01-15", weight=70.0, fat_pct=20.0):
        return {
            "date": date, "measured_at": "2026-01-15T08:00:00+00:00",
            "grpid": grpid, "weight_kg": weight, "fat_pct": fat_pct,
            "fat_mass_kg": 14.0, "lean_mass_kg": 56.0, "muscle_mass_kg": 30.0,
            "hydration_kg": 38.0, "bone_mass_kg": 3.0, "heart_rate": None,
            "systolic_bp": None, "diastolic_bp": None, "spo2_pct": None,
            "temperature_c": None, "visceral_fat_index": None,
            "basal_metabolic_rate": None,
        }

    def test_insert_and_retrieve(self):
        conn = _get_test_db()
        save_body_measurement(conn, self._make_row())
        conn.commit()
        rows = query_body(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["weight_kg"], 70.0)
        self.assertAlmostEqual(rows[0]["fat_pct"], 20.0)

    def test_dedup_same_grpid(self):
        conn = _get_test_db()
        save_body_measurement(conn, self._make_row(grpid=1001, weight=70.0))
        save_body_measurement(conn, self._make_row(grpid=1001, weight=71.0))
        conn.commit()
        rows = query_body(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["weight_kg"], 71.0)  # replaced

    def test_different_grpid_kept(self):
        conn = _get_test_db()
        save_body_measurement(conn, self._make_row(grpid=1001))
        save_body_measurement(conn, self._make_row(grpid=1002, date="2026-01-16"))
        conn.commit()
        rows = query_body(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 2)


class TestSleepSummaries(unittest.TestCase):
    def _make_row(self, date="2026-01-15", device="32"):
        return {
            "date": date, "startdate": "2026-01-14T23:00:00+00:00",
            "enddate": "2026-01-15T07:00:00+00:00",
            "total_sleep_sec": 28800, "deep_sleep_sec": 3600,
            "light_sleep_sec": 14400, "rem_sleep_sec": 7200,
            "awake_sec": 3600, "wakeup_count": 2,
            "hr_average": 58, "hr_min": 48, "hr_max": 72,
            "rr_average": 15, "rr_min": 12, "rr_max": 19,
            "sleep_score": 78, "snoring_sec": 600,
            "apnea_hypopnea_index": None, "device_model": device,
        }

    def test_insert_and_retrieve(self):
        conn = _get_test_db()
        save_sleep_summary(conn, self._make_row())
        conn.commit()
        rows = query_sleep(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sleep_score"], 78)

    def test_dedup_same_date_device(self):
        conn = _get_test_db()
        save_sleep_summary(conn, self._make_row(date="2026-01-15", device="32"))
        save_sleep_summary(conn, self._make_row(date="2026-01-15", device="32"))
        conn.commit()
        rows = query_sleep(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 1)

    def test_different_devices_same_night(self):
        conn = _get_test_db()
        save_sleep_summary(conn, self._make_row(date="2026-01-15", device="32"))
        save_sleep_summary(conn, self._make_row(date="2026-01-15", device="93"))
        conn.commit()
        rows = query_sleep(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 2)


class TestActivities(unittest.TestCase):
    def _make_row(self, date="2026-01-15"):
        return {
            "date": date, "steps": 8000, "distance_m": 6000,
            "active_calories": 250, "total_calories": 1850,
            "soft_sec": 5400, "moderate_sec": 1800, "intense_sec": 600,
            "hr_average": 72, "hr_min": 55, "hr_max": 145,
            "hr_zone_0_sec": 3600, "hr_zone_1_sec": 1800,
            "hr_zone_2_sec": 600, "hr_zone_3_sec": 120,
        }

    def test_insert_and_retrieve(self):
        conn = _get_test_db()
        save_activity(conn, self._make_row())
        conn.commit()
        rows = query_activities(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["steps"], 8000)

    def test_dedup_same_date(self):
        conn = _get_test_db()
        save_activity(conn, self._make_row(date="2026-01-15"))
        save_activity(conn, self._make_row(date="2026-01-15"))
        conn.commit()
        rows = query_activities(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 1)


class TestWorkouts(unittest.TestCase):
    def _make_row(self, startdate="2026-01-15T10:00:00+00:00", category=6):
        return {
            "date": "2026-01-15", "startdate": startdate,
            "enddate": "2026-01-15T11:00:00+00:00",
            "category": category, "category_name": "cycling",
            "duration_sec": 3600, "calories": 350,
            "distance_m": 15000, "steps": 0,
            "hr_average": 135, "hr_min": 95, "hr_max": 170,
        }

    def test_insert_and_retrieve(self):
        conn = _get_test_db()
        save_workout(conn, self._make_row())
        conn.commit()
        rows = query_workouts(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category_name"], "cycling")

    def test_dedup_same_start_category(self):
        conn = _get_test_db()
        save_workout(conn, self._make_row())
        save_workout(conn, self._make_row())
        conn.commit()
        rows = query_workouts(conn, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 1)

    def test_category_filter(self):
        conn = _get_test_db()
        save_workout(conn, self._make_row(category=6))
        save_workout(conn, self._make_row(
            startdate="2026-01-16T10:00:00+00:00", category=2
        ))
        conn.commit()
        # Fix: second workout needs different category_name
        conn.execute(
            "UPDATE workouts SET category_name='run' WHERE category=2"
        )
        conn.commit()
        rows = query_workouts(conn, "2026-01-01", "2026-01-31", category="cycling")
        self.assertEqual(len(rows), 1)


class TestSyncLog(unittest.TestCase):
    def test_log_sync(self):
        conn = _get_test_db()
        log_sync(conn, "body", "ok", 15)
        rows = conn.execute("SELECT * FROM sync_log").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data_type"], "body")
        self.assertEqual(rows[0]["records_added"], 15)

    def test_get_last_sync_none(self):
        conn = _get_test_db()
        result = get_last_sync(conn, "body")
        self.assertIsNone(result)

    def test_get_last_sync(self):
        conn = _get_test_db()
        log_sync(conn, "body", "ok", 10)
        log_sync(conn, "body", "ok", 5)
        result = get_last_sync(conn, "body")
        self.assertIsNotNone(result)

    def test_get_last_sync_ignores_errors(self):
        conn = _get_test_db()
        log_sync(conn, "body", "error", 0, "failed")
        result = get_last_sync(conn, "body")
        self.assertIsNone(result)


class TestEmptyQueries(unittest.TestCase):
    def test_empty_body(self):
        conn = _get_test_db()
        self.assertEqual(query_body(conn, "2026-01-01", "2026-01-31"), [])

    def test_empty_sleep(self):
        conn = _get_test_db()
        self.assertEqual(query_sleep(conn, "2026-01-01", "2026-01-31"), [])

    def test_empty_activities(self):
        conn = _get_test_db()
        self.assertEqual(query_activities(conn, "2026-01-01", "2026-01-31"), [])

    def test_empty_workouts(self):
        conn = _get_test_db()
        self.assertEqual(query_workouts(conn, "2026-01-01", "2026-01-31"), [])


if __name__ == "__main__":
    unittest.main()
