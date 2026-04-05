"""Activity and workout query tools."""

import logging
from datetime import datetime, timedelta, timezone

import anyio

from ..mcp_instance import mcp
from ..helpers import (
    format_response, require_auth, parse_date,
    format_duration, format_distance, resolve_workout_category,
)
from .. import api, db
from ..config import WITHINGS_MEASURE_V2_URL

logger = logging.getLogger(__name__)

_ACTIVITY_FIELDS = (
    "steps,distance,calories,totalcalories,soft,moderate,intense,"
    "hr_average,hr_min,hr_max,hr_zone_0,hr_zone_1,hr_zone_2,hr_zone_3"
)

_WORKOUT_FIELDS = "calories,distance,steps,hr_average,hr_min,hr_max"


def _fetch_activity_live(start_date, end_date):
    """Fetch daily activity summaries from the API."""
    results = []
    offset = 0

    while True:
        body = api.post(WITHINGS_MEASURE_V2_URL, {
            "action": "getactivity",
            "startdateymd": start_date.isoformat(),
            "enddateymd": end_date.isoformat(),
            "data_fields": _ACTIVITY_FIELDS,
            "offset": offset,
        })

        activities = body.get("activities", [])
        if isinstance(activities, dict):
            activities = [activities]

        for entry in activities:
            results.append({
                "date": entry.get("date", ""),
                "steps": entry.get("steps"),
                "distance": format_distance(entry.get("distance")),
                "active_calories": entry.get("calories"),
                "total_calories": entry.get("totalcalories"),
                "light_activity": format_duration(entry.get("soft")),
                "moderate_activity": format_duration(entry.get("moderate")),
                "intense_activity": format_duration(entry.get("intense")),
                "hr_average": entry.get("hr_average"),
                "hr_min": entry.get("hr_min"),
                "hr_max": entry.get("hr_max"),
            })

        if body.get("more", False):
            offset = body.get("offset", 0)
        else:
            break

    return results


def _fetch_workouts_live(start_date, end_date, category=None):
    """Fetch workout sessions from the API."""
    results = []
    offset = 0

    while True:
        body = api.post(WITHINGS_MEASURE_V2_URL, {
            "action": "getworkouts",
            "startdateymd": start_date.isoformat(),
            "enddateymd": end_date.isoformat(),
            "data_fields": _WORKOUT_FIELDS,
            "offset": offset,
        })

        for entry in body.get("series", []):
            data = entry.get("data", {})
            cat = entry.get("category", 36)
            cat_name = resolve_workout_category(cat)

            if category and category.lower() not in cat_name:
                continue

            start_ts = entry.get("startdate", 0)
            end_ts = entry.get("enddate", 0)
            results.append({
                "date": datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                "type": cat_name,
                "duration": format_duration(end_ts - start_ts if end_ts and start_ts else 0),
                "calories": data.get("calories"),
                "distance": format_distance(data.get("distance")),
                "steps": data.get("steps"),
                "hr_average": data.get("hr_average"),
                "hr_min": data.get("hr_min"),
                "hr_max": data.get("hr_max"),
            })

        if body.get("more", False):
            offset = body.get("offset", 0)
        else:
            break

    return results


@mcp.tool()
@require_auth
async def withings_get_activity(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get daily activity summaries (steps, distance, calories, active time).

    Returns one entry per day from the local cache by default.
    Run withings_sync first to populate the cache.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d".
            Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, fetch from Withings API instead of cache.

    Returns daily activity data sorted by date, with steps, distance
    in km, calories, and active minutes by intensity level.
    Not for workout sessions -- use withings_get_workouts instead.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    if live:
        entries = await anyio.to_thread.run_sync(lambda: _fetch_activity_live(start, end))
    else:
        def _query():
            conn = db.get_db()
            rows = db.query_activities(conn, start.isoformat(), end.isoformat())
            conn.close()
            for r in rows:
                r["distance"] = format_distance(r.get("distance_m"))
                r["light_activity"] = format_duration(r.get("soft_sec"))
                r["moderate_activity"] = format_duration(r.get("moderate_sec"))
                r["intense_activity"] = format_duration(r.get("intense_sec"))
            return rows
        entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response({
            "message": "No activity data found for this period.",
            "hint": "Run withings_sync first, or try live=True.",
        })

    return format_response({"days": entries, "count": len(entries)})


@mcp.tool()
@require_auth
async def withings_get_workouts(
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    live: bool = False,
) -> str:
    """Get workout sessions (type, duration, HR, calories).

    Returns individual workout sessions from the local cache by default.
    Run withings_sync first to populate the cache.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "90d".
            Default: last 90 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        category: Filter by workout type, e.g. "cycling", "walk", "run".
            Case-insensitive partial match.
        live: If true, fetch from Withings API instead of cache.

    Returns workout sessions sorted by date with type, duration,
    calories, distance, and heart rate data.
    Not for daily step/activity totals -- use withings_get_activity.
    """
    start, end = parse_date(start_date, end_date, default_days=90)

    if live:
        entries = await anyio.to_thread.run_sync(
            lambda: _fetch_workouts_live(start, end, category)
        )
    else:
        def _query():
            conn = db.get_db()
            rows = db.query_workouts(conn, start.isoformat(), end.isoformat(), category)
            conn.close()
            for r in rows:
                r["type"] = r.get("category_name", "other")
                r["duration"] = format_duration(r.get("duration_sec"))
                r["distance"] = format_distance(r.get("distance_m"))
            return rows
        entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response({
            "message": "No workouts found for this period.",
            "hint": "Run withings_sync first, or try live=True.",
        })

    return format_response({"workouts": entries, "count": len(entries)})
