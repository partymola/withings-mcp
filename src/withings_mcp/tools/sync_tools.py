"""Sync tool: fetch data from Withings API and store in local SQLite cache."""

import logging
import time
from datetime import datetime, date, timedelta, timezone

import anyio

from ..mcp_instance import mcp
from ..helpers import (
    format_response, require_auth, parse_value,
    resolve_measure_type, resolve_workout_category,
    MEASURE_TYPES,
)
from .. import api, db
from ..config import (
    WITHINGS_MEASURE_URL, WITHINGS_MEASURE_V2_URL,
    WITHINGS_SLEEP_V2_URL,
)

logger = logging.getLogger(__name__)

# All cacheable measure type IDs
_BODY_MEASTYPES = ",".join(str(k) for k in MEASURE_TYPES.keys())


def run_sync(types: list[str], days: int = 30) -> dict:
    """Sync one or more data types from the Withings API to the local cache.

    Args:
        types: List of data types to sync. Valid values: "body", "sleep",
            "activity", "workouts".
        days: Days of history to fetch on first sync (default: 30).
            Subsequent syncs are incremental from last sync timestamp.

    Returns a dict mapping each data type to its sync result.
    """
    conn = db.get_db()
    results = {}
    today = date.today()

    for dtype in types:
        try:
            # Determine start date: last sync or N days ago
            last = db.get_last_sync(conn, dtype)
            if last:
                start_date = datetime.fromisoformat(last).date()
            else:
                start_date = today - timedelta(days=days)

            end_date = today
            start_ymd = start_date.isoformat()
            end_ymd = end_date.isoformat()
            start_ts = int(datetime.combine(start_date, datetime.min.time(),
                                             tzinfo=timezone.utc).timestamp())
            end_ts = int(datetime.combine(end_date + timedelta(days=1),
                                           datetime.min.time(),
                                           tzinfo=timezone.utc).timestamp())

            if dtype == "body":
                count = _sync_body(conn, start_ts, end_ts)
            elif dtype == "sleep":
                count = _sync_sleep(conn, start_ymd, end_ymd)
            elif dtype == "activity":
                count = _sync_activity(conn, start_ymd, end_ymd)
            elif dtype == "workouts":
                count = _sync_workouts(conn, start_ymd, end_ymd)
            else:
                results[dtype] = {"status": "error", "message": f"Unknown type: {dtype}"}
                continue

            db.log_sync(conn, dtype, "ok", count)
            results[dtype] = {"status": "ok", "records": count, "range": f"{start_ymd} to {end_ymd}"}

        except api.WithingsRateLimitError:
            db.log_sync(conn, dtype, "partial", notes="rate limited")
            results[dtype] = {"status": "rate_limited", "message": "Retry in 60 seconds."}
        except api.WithingsAuthError as e:
            results[dtype] = {"status": "auth_error", "message": str(e)}
        except api.WithingsAPIError as e:
            db.log_sync(conn, dtype, "error", notes=str(e))
            results[dtype] = {"status": "error", "message": str(e)}

    conn.close()
    return results


def auto_sync_if_stale(data_type: str) -> None:
    """Trigger an incremental sync if the cache for data_type is not current.

    Checks whether the last successful sync for data_type was before today.
    If so, runs run_sync([data_type]) to bring the cache up to date.
    All exceptions are silently swallowed - this is best-effort only.
    """
    try:
        conn = db.get_db()
        last = db.get_last_sync(conn, data_type)
        conn.close()

        today = date.today().isoformat()
        if last is None or last[:10] < today:
            run_sync([data_type])
    except Exception:
        pass

# Sleep summary fields to request
_SLEEP_DATA_FIELDS = (
    "total_sleep_time,deepsleepduration,lightsleepduration,remsleepduration,"
    "wakeupduration,wakeupcount,hr_average,hr_min,hr_max,rr_average,rr_min,"
    "rr_max,sleep_score,snoring,apnea_hypopnea_index"
)

# Activity fields to request
_ACTIVITY_DATA_FIELDS = (
    "steps,distance,calories,totalcalories,soft,moderate,intense,"
    "hr_average,hr_min,hr_max,hr_zone_0,hr_zone_1,hr_zone_2,hr_zone_3"
)

# Workout fields to request
_WORKOUT_DATA_FIELDS = (
    "calories,distance,steps,hr_average,hr_min,hr_max"
)


def _sync_body(conn, start_ts: int, end_ts: int) -> int:
    """Sync body composition measurements. Returns count of records added."""
    count = 0
    offset = 0

    while True:
        body = api.post(WITHINGS_MEASURE_URL, {
            "action": "getmeas",
            "meastypes": _BODY_MEASTYPES,
            "category": 1,
            "startdate": start_ts,
            "enddate": end_ts,
            "offset": offset,
        })

        for grp in body.get("measuregrps", []):
            ts = grp.get("date", 0)
            measured_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            ds = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

            # Parse all measures in this group
            values = {}
            for m in grp.get("measures", []):
                name = resolve_measure_type(m.get("type", 0))
                if not name.startswith("type_"):
                    values[name] = round(parse_value(m), 3)

            row = {
                "date": ds,
                "measured_at": measured_at,
                "grpid": grp.get("grpid"),
                "weight_kg": values.get("weight_kg"),
                "fat_pct": values.get("fat_pct"),
                "fat_mass_kg": values.get("fat_mass_kg"),
                "lean_mass_kg": values.get("lean_mass_kg"),
                "muscle_mass_kg": values.get("muscle_mass_kg"),
                "hydration_kg": values.get("hydration_kg"),
                "bone_mass_kg": values.get("bone_mass_kg"),
                "heart_rate": values.get("heart_rate"),
                "systolic_bp": values.get("systolic_bp"),
                "diastolic_bp": values.get("diastolic_bp"),
                "spo2_pct": values.get("spo2_pct"),
                "temperature_c": values.get("temperature_c") or values.get("body_temperature_c"),
                "visceral_fat_index": values.get("visceral_fat_index"),
                "basal_metabolic_rate": values.get("basal_metabolic_rate"),
            }
            db.save_body_measurement(conn, row)
            count += 1

        if body.get("more", 0) in (1, True):
            offset = body.get("offset", 0)
        else:
            break

    conn.commit()
    return count


def _sync_sleep(conn, start_ymd: str, end_ymd: str) -> int:
    """Sync sleep summaries. Returns count of records added."""
    count = 0
    offset = 0

    while True:
        body = api.post(WITHINGS_SLEEP_V2_URL, {
            "action": "getsummary",
            "startdateymd": start_ymd,
            "enddateymd": end_ymd,
            "data_fields": _SLEEP_DATA_FIELDS,
            "offset": offset,
        })

        for entry in body.get("series", []):
            data = entry.get("data", {})
            start_ts = entry.get("startdate")
            end_ts = entry.get("enddate")
            startdate = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat() if start_ts else None
            enddate = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat() if end_ts else None

            row = {
                "date": entry.get("date", ""),
                "startdate": startdate,
                "enddate": enddate,
                "total_sleep_sec": data.get("total_sleep_time"),
                "deep_sleep_sec": data.get("deepsleepduration"),
                "light_sleep_sec": data.get("lightsleepduration"),
                "rem_sleep_sec": data.get("remsleepduration"),
                "awake_sec": data.get("wakeupduration"),
                "wakeup_count": data.get("wakeupcount"),
                "hr_average": data.get("hr_average"),
                "hr_min": data.get("hr_min"),
                "hr_max": data.get("hr_max"),
                "rr_average": data.get("rr_average"),
                "rr_min": data.get("rr_min"),
                "rr_max": data.get("rr_max"),
                "sleep_score": data.get("sleep_score"),
                "snoring_sec": data.get("snoring"),
                "apnea_hypopnea_index": data.get("apnea_hypopnea_index"),
                "device_model": str(entry.get("model", "")),
            }
            db.save_sleep_summary(conn, row)
            count += 1

        if body.get("more", False):
            offset = body.get("offset", 0)
        else:
            break

    conn.commit()
    return count


def _sync_activity(conn, start_ymd: str, end_ymd: str) -> int:
    """Sync daily activity summaries. Returns count of records added."""
    count = 0
    offset = 0

    while True:
        body = api.post(WITHINGS_MEASURE_V2_URL, {
            "action": "getactivity",
            "startdateymd": start_ymd,
            "enddateymd": end_ymd,
            "data_fields": _ACTIVITY_DATA_FIELDS,
            "offset": offset,
        })

        activities = body.get("activities", [])
        # Single-day quirk: might return an object instead of array
        if isinstance(activities, dict):
            activities = [activities]

        for entry in activities:
            row = {
                "date": entry.get("date", ""),
                "steps": entry.get("steps"),
                "distance_m": entry.get("distance"),
                "active_calories": entry.get("calories"),
                "total_calories": entry.get("totalcalories"),
                "soft_sec": entry.get("soft"),
                "moderate_sec": entry.get("moderate"),
                "intense_sec": entry.get("intense"),
                "hr_average": entry.get("hr_average"),
                "hr_min": entry.get("hr_min"),
                "hr_max": entry.get("hr_max"),
                "hr_zone_0_sec": entry.get("hr_zone_0"),
                "hr_zone_1_sec": entry.get("hr_zone_1"),
                "hr_zone_2_sec": entry.get("hr_zone_2"),
                "hr_zone_3_sec": entry.get("hr_zone_3"),
            }
            db.save_activity(conn, row)
            count += 1

        if body.get("more", False):
            offset = body.get("offset", 0)
        else:
            break

    conn.commit()
    return count


def _sync_workouts(conn, start_ymd: str, end_ymd: str) -> int:
    """Sync workout sessions. Returns count of records added."""
    count = 0
    offset = 0

    while True:
        body = api.post(WITHINGS_MEASURE_V2_URL, {
            "action": "getworkouts",
            "startdateymd": start_ymd,
            "enddateymd": end_ymd,
            "data_fields": _WORKOUT_DATA_FIELDS,
            "offset": offset,
        })

        for entry in body.get("series", []):
            data = entry.get("data", {})
            start_ts = entry.get("startdate", 0)
            end_ts = entry.get("enddate", 0)
            cat = entry.get("category", 36)

            startdate = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
            enddate = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()
            ds = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")

            row = {
                "date": ds,
                "startdate": startdate,
                "enddate": enddate,
                "category": cat,
                "category_name": resolve_workout_category(cat),
                "duration_sec": end_ts - start_ts if end_ts and start_ts else None,
                "calories": data.get("calories"),
                "distance_m": data.get("distance"),
                "steps": data.get("steps"),
                "hr_average": data.get("hr_average"),
                "hr_min": data.get("hr_min"),
                "hr_max": data.get("hr_max"),
            }
            db.save_workout(conn, row)
            count += 1

        if body.get("more", False):
            offset = body.get("offset", 0)
        else:
            break

    conn.commit()
    return count


@mcp.tool()
@require_auth
async def withings_sync(
    data_types: str = "all",
    days: int = 30,
) -> str:
    """Sync Withings health data to the local cache.

    Fetches data from the Withings API and stores it in SQLite for fast
    offline queries. Run this before using other withings_get_* tools.

    Syncs incrementally: only fetches data newer than the last sync.
    First sync fetches the specified number of days of history.

    Args:
        data_types: What to sync. Options: "all", "body", "sleep",
            "activity", "workouts". Comma-separated for multiple,
            e.g. "body,sleep". Default: "all".
        days: Days of history for first sync (default: 30). Ignored
            on subsequent syncs (uses last sync timestamp).

    Returns summary of records synced per data type.
    Not for querying data - use withings_get_body, withings_get_sleep,
    withings_get_activity, or withings_get_workouts instead.
    """
    types = [t.strip() for t in data_types.split(",")]
    if "all" in types:
        types = ["body", "sleep", "activity", "workouts"]

    results = await anyio.to_thread.run_sync(lambda: run_sync(types, days))
    return format_response(results)
