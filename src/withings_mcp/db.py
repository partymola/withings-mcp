"""SQLite database schema and helpers for the Withings local cache."""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS body_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    grpid INTEGER,
    weight_kg REAL,
    fat_pct REAL,
    fat_mass_kg REAL,
    lean_mass_kg REAL,
    muscle_mass_kg REAL,
    hydration_kg REAL,
    bone_mass_kg REAL,
    heart_rate INTEGER,
    systolic_bp INTEGER,
    diastolic_bp INTEGER,
    spo2_pct REAL,
    temperature_c REAL,
    visceral_fat_index REAL,
    basal_metabolic_rate REAL,
    UNIQUE(grpid)
);

CREATE INDEX IF NOT EXISTS idx_body_date ON body_measurements(date);

CREATE TABLE IF NOT EXISTS sleep_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    startdate TEXT,
    enddate TEXT,
    total_sleep_sec INTEGER,
    deep_sleep_sec INTEGER,
    light_sleep_sec INTEGER,
    rem_sleep_sec INTEGER,
    awake_sec INTEGER,
    wakeup_count INTEGER,
    hr_average INTEGER,
    hr_min INTEGER,
    hr_max INTEGER,
    rr_average INTEGER,
    rr_min INTEGER,
    rr_max INTEGER,
    sleep_score INTEGER,
    snoring_sec INTEGER,
    apnea_hypopnea_index REAL,
    device_model TEXT,
    UNIQUE(date, device_model)
);

CREATE INDEX IF NOT EXISTS idx_sleep_date ON sleep_summaries(date);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    steps INTEGER,
    distance_m REAL,
    active_calories REAL,
    total_calories REAL,
    soft_sec INTEGER,
    moderate_sec INTEGER,
    intense_sec INTEGER,
    hr_average INTEGER,
    hr_min INTEGER,
    hr_max INTEGER,
    hr_zone_0_sec INTEGER,
    hr_zone_1_sec INTEGER,
    hr_zone_2_sec INTEGER,
    hr_zone_3_sec INTEGER
);

CREATE INDEX IF NOT EXISTS idx_activity_date ON activities(date);

CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    startdate TEXT NOT NULL,
    enddate TEXT NOT NULL,
    category INTEGER,
    category_name TEXT,
    duration_sec INTEGER,
    calories REAL,
    distance_m REAL,
    steps INTEGER,
    hr_average INTEGER,
    hr_min INTEGER,
    hr_max INTEGER,
    UNIQUE(startdate, category)
);

CREATE INDEX IF NOT EXISTS idx_workout_date ON workouts(date);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at TEXT NOT NULL,
    data_type TEXT NOT NULL,
    status TEXT NOT NULL,
    records_added INTEGER,
    notes TEXT
);
"""


def get_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a database connection and ensure the schema exists."""
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_body_measurement(conn: sqlite3.Connection, row: dict):
    """Insert or replace a body measurement row."""
    conn.execute(
        """INSERT OR REPLACE INTO body_measurements
        (date, measured_at, grpid, weight_kg, fat_pct, fat_mass_kg, lean_mass_kg,
         muscle_mass_kg, hydration_kg, bone_mass_kg, heart_rate, systolic_bp,
         diastolic_bp, spo2_pct, temperature_c, visceral_fat_index, basal_metabolic_rate)
        VALUES (:date, :measured_at, :grpid, :weight_kg, :fat_pct, :fat_mass_kg,
                :lean_mass_kg, :muscle_mass_kg, :hydration_kg, :bone_mass_kg,
                :heart_rate, :systolic_bp, :diastolic_bp, :spo2_pct, :temperature_c,
                :visceral_fat_index, :basal_metabolic_rate)""",
        row,
    )


def save_sleep_summary(conn: sqlite3.Connection, row: dict):
    """Insert or replace a sleep summary row."""
    conn.execute(
        """INSERT OR REPLACE INTO sleep_summaries
        (date, startdate, enddate, total_sleep_sec, deep_sleep_sec, light_sleep_sec,
         rem_sleep_sec, awake_sec, wakeup_count, hr_average, hr_min, hr_max,
         rr_average, rr_min, rr_max, sleep_score, snoring_sec,
         apnea_hypopnea_index, device_model)
        VALUES (:date, :startdate, :enddate, :total_sleep_sec, :deep_sleep_sec,
                :light_sleep_sec, :rem_sleep_sec, :awake_sec, :wakeup_count,
                :hr_average, :hr_min, :hr_max, :rr_average, :rr_min, :rr_max,
                :sleep_score, :snoring_sec, :apnea_hypopnea_index, :device_model)""",
        row,
    )


def save_activity(conn: sqlite3.Connection, row: dict):
    """Insert or replace a daily activity row."""
    conn.execute(
        """INSERT OR REPLACE INTO activities
        (date, steps, distance_m, active_calories, total_calories,
         soft_sec, moderate_sec, intense_sec, hr_average, hr_min, hr_max,
         hr_zone_0_sec, hr_zone_1_sec, hr_zone_2_sec, hr_zone_3_sec)
        VALUES (:date, :steps, :distance_m, :active_calories, :total_calories,
                :soft_sec, :moderate_sec, :intense_sec, :hr_average, :hr_min,
                :hr_max, :hr_zone_0_sec, :hr_zone_1_sec, :hr_zone_2_sec,
                :hr_zone_3_sec)""",
        row,
    )


def save_workout(conn: sqlite3.Connection, row: dict):
    """Insert or replace a workout row."""
    conn.execute(
        """INSERT OR REPLACE INTO workouts
        (date, startdate, enddate, category, category_name, duration_sec,
         calories, distance_m, steps, hr_average, hr_min, hr_max)
        VALUES (:date, :startdate, :enddate, :category, :category_name,
                :duration_sec, :calories, :distance_m, :steps, :hr_average,
                :hr_min, :hr_max)""",
        row,
    )


def log_sync(conn: sqlite3.Connection, data_type: str, status: str,
             records_added: int = 0, notes: str = ""):
    """Record a sync event."""
    conn.execute(
        """INSERT INTO sync_log (synced_at, data_type, status, records_added, notes)
        VALUES (?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), data_type, status, records_added, notes),
    )
    conn.commit()


def get_last_sync(conn: sqlite3.Connection, data_type: str) -> str | None:
    """Return the ISO timestamp of the last successful sync for a data type."""
    row = conn.execute(
        """SELECT synced_at FROM sync_log
        WHERE data_type = ? AND status = 'ok'
        ORDER BY synced_at DESC LIMIT 1""",
        (data_type,),
    ).fetchone()
    return row["synced_at"] if row else None


def query_body(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[dict]:
    """Query body measurements within a date range."""
    rows = conn.execute(
        """SELECT * FROM body_measurements
        WHERE date >= ? AND date <= ?
        ORDER BY date""",
        (start_date, end_date),
    ).fetchall()
    return [dict(r) for r in rows]


def query_sleep(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[dict]:
    """Query sleep summaries within a date range."""
    rows = conn.execute(
        """SELECT * FROM sleep_summaries
        WHERE date >= ? AND date <= ?
        ORDER BY date""",
        (start_date, end_date),
    ).fetchall()
    return [dict(r) for r in rows]


def query_activities(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[dict]:
    """Query daily activities within a date range."""
    rows = conn.execute(
        """SELECT * FROM activities
        WHERE date >= ? AND date <= ?
        ORDER BY date""",
        (start_date, end_date),
    ).fetchall()
    return [dict(r) for r in rows]


def query_workouts(conn: sqlite3.Connection, start_date: str, end_date: str,
                   category: str | None = None) -> list[dict]:
    """Query workouts within a date range, optionally filtered by category."""
    if category:
        rows = conn.execute(
            """SELECT * FROM workouts
            WHERE date >= ? AND date <= ? AND LOWER(category_name) LIKE ?
            ORDER BY date""",
            (start_date, end_date, f"%{category.lower()}%"),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM workouts
            WHERE date >= ? AND date <= ?
            ORDER BY date""",
            (start_date, end_date),
        ).fetchall()
    return [dict(r) for r in rows]
