"""Trend analysis tool - works from local cache only."""

import logging
import re
from collections import defaultdict
from datetime import date, timedelta

import anyio

from ..mcp_instance import mcp
from ..helpers import format_response, require_auth, parse_date, format_duration
from .. import db

logger = logging.getLogger(__name__)


def _get_period_key(ds: str, period: str) -> str:
    """Map a YYYY-MM-DD date string to a period bucket key."""
    year = ds[:4]
    month = int(ds[5:7])
    if period == "weekly":
        d = date.fromisoformat(ds)
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    elif period == "quarterly":
        q = (month - 1) // 3 + 1
        return f"{year}-Q{q}"
    else:  # monthly
        return ds[:7]


def _avg(values: list) -> float | None:
    """Compute average, returning None for empty lists."""
    return round(sum(values) / len(values), 1) if values else None


def _trend_body(conn, start_date: str, end_date: str, period: str) -> dict:
    """Compute body composition trends."""
    rows = db.query_body(conn, start_date, end_date)
    if not rows:
        return {"message": "No body data in cache for this period. Run withings_sync first."}

    fields = ["weight_kg", "fat_pct", "fat_mass_kg", "muscle_mass_kg",
              "bone_mass_kg", "hydration_kg"]
    buckets = defaultdict(lambda: defaultdict(list))

    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in fields:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        entry = {"period": key, "count": len(buckets[key].get("weight_kg", []))}
        for f in fields:
            entry[f] = _avg(buckets[key].get(f, []))
        periods.append(entry)

    return {"periods": periods, "data_type": "body", "aggregation": period}


def _trend_sleep(conn, start_date: str, end_date: str, period: str) -> dict:
    """Compute sleep trends."""
    rows = db.query_sleep(conn, start_date, end_date)
    if not rows:
        return {"message": "No sleep data in cache for this period. Run withings_sync first."}

    fields = ["total_sleep_sec", "deep_sleep_sec", "light_sleep_sec",
              "rem_sleep_sec", "sleep_score", "hr_average", "rr_average"]
    buckets = defaultdict(lambda: defaultdict(list))

    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in fields:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        entry = {
            "period": key,
            "nights": len(b.get("total_sleep_sec", [])),
            "avg_total_sleep": format_duration(int(_avg(b.get("total_sleep_sec", [])) or 0)),
            "avg_deep_sleep": format_duration(int(_avg(b.get("deep_sleep_sec", [])) or 0)),
            "avg_rem_sleep": format_duration(int(_avg(b.get("rem_sleep_sec", [])) or 0)),
            "avg_sleep_score": _avg(b.get("sleep_score", [])),
            "avg_hr": _avg(b.get("hr_average", [])),
            "avg_rr": _avg(b.get("rr_average", [])),
        }
        periods.append(entry)

    return {"periods": periods, "data_type": "sleep", "aggregation": period}


def _trend_activity(conn, start_date: str, end_date: str, period: str) -> dict:
    """Compute activity trends."""
    rows = db.query_activities(conn, start_date, end_date)
    if not rows:
        return {"message": "No activity data in cache for this period. Run withings_sync first."}

    fields = ["steps", "distance_m", "active_calories", "total_calories"]
    buckets = defaultdict(lambda: defaultdict(list))

    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in fields:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        entry = {
            "period": key,
            "days": len(b.get("steps", [])),
            "avg_steps": _avg(b.get("steps", [])),
            "total_distance_km": round(sum(b.get("distance_m", [])) / 1000, 1) if b.get("distance_m") else None,
            "avg_active_calories": _avg(b.get("active_calories", [])),
        }
        periods.append(entry)

    return {"periods": periods, "data_type": "activity", "aggregation": period}


def _compare_periods(conn, data_type: str, compare_str: str) -> dict:
    """Compare two time periods. Format: 'last_30d vs previous_30d'."""
    # Parse the comparison string
    parts = re.split(r"\s+vs\s+", compare_str.strip(), maxsplit=1)
    if len(parts) != 2:
        return {"error": "Invalid compare format. Use: 'last_30d vs previous_30d' or '2026-03 vs 2026-02'"}

    today = date.today()
    ranges = []
    for part in parts:
        part = part.strip()
        m = re.match(r"last_(\d+)d", part)
        if m:
            days = int(m.group(1))
            ranges.append((today - timedelta(days=days), today))
            continue
        m = re.match(r"previous_(\d+)d", part)
        if m:
            days = int(m.group(1))
            ranges.append((today - timedelta(days=days * 2), today - timedelta(days=days)))
            continue
        if re.match(r"^\d{4}-\d{2}$", part):
            year, month = int(part[:4]), int(part[5:7])
            start = date(year, month, 1)
            if month == 12:
                end = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(year, month + 1, 1) - timedelta(days=1)
            ranges.append((start, end))
            continue
        m = re.match(r"^(\d{4})-Q([1-4])$", part)
        if m:
            year, q = int(m.group(1)), int(m.group(2))
            start = date(year, (q - 1) * 3 + 1, 1)
            end_month = q * 3
            if end_month == 12:
                end = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(year, end_month + 1, 1) - timedelta(days=1)
            ranges.append((start, end))
            continue
        return {"error": f"Cannot parse period '{part}'. Use: last_30d, previous_30d, 2026-03, or 2026-Q1"}

    if len(ranges) != 2:
        return {"error": "Need exactly two periods to compare."}

    query_fn = {"body": db.query_body, "sleep": db.query_sleep, "activity": db.query_activities}.get(data_type)
    if not query_fn:
        return {"error": f"Cannot compare data_type '{data_type}'. Use: body, sleep, or activity."}

    period_a = query_fn(conn, ranges[0][0].isoformat(), ranges[0][1].isoformat())
    period_b = query_fn(conn, ranges[1][0].isoformat(), ranges[1][1].isoformat())

    def summarize(rows, dtype):
        if not rows:
            return {"count": 0}
        if dtype == "body":
            weights = [r["weight_kg"] for r in rows if r.get("weight_kg")]
            fats = [r["fat_pct"] for r in rows if r.get("fat_pct")]
            return {"count": len(rows), "avg_weight": _avg(weights), "avg_fat_pct": _avg(fats)}
        elif dtype == "sleep":
            scores = [r["sleep_score"] for r in rows if r.get("sleep_score")]
            durations = [r["total_sleep_sec"] for r in rows if r.get("total_sleep_sec")]
            return {
                "count": len(rows),
                "avg_sleep_score": _avg(scores),
                "avg_total_sleep": format_duration(int(_avg(durations) or 0)),
            }
        elif dtype == "activity":
            steps = [r["steps"] for r in rows if r.get("steps")]
            return {"count": len(rows), "avg_steps": _avg(steps)}
        return {"count": len(rows)}

    result_a = summarize(period_a, data_type)
    result_b = summarize(period_b, data_type)
    result_a["period"] = f"{ranges[0][0]} to {ranges[0][1]}"
    result_b["period"] = f"{ranges[1][0]} to {ranges[1][1]}"

    return {"period_1": result_a, "period_2": result_b, "data_type": data_type}


@mcp.tool()
@require_auth
async def withings_trends(
    data_type: str = "body",
    period: str = "monthly",
    start_date: str | None = None,
    end_date: str | None = None,
    compare: str | None = None,
) -> str:
    """Analyse trends in cached health data.

    Computes averages, min/max, and changes over time from the local
    cache. Run withings_sync first to populate data.

    Args:
        data_type: What to analyse. Options: "body", "sleep", "activity".
        period: Aggregation period. Options: "weekly", "monthly",
            "quarterly". Default: "monthly".
        start_date: Start date as "YYYY-MM-DD" or "12m" for relative.
            Default: last 12 months.
        end_date: End date as "YYYY-MM-DD". Default: today.
        compare: Compare two periods. Format: "last_30d vs previous_30d",
            "2026-03 vs 2026-02", "2026-Q1 vs 2025-Q4".
            When set, period/start_date/end_date are ignored.

    Returns aggregated averages with change indicators. For body data:
    weight, fat%, muscle trends. For sleep: duration, score, HR trends.
    For activity: steps, distance, calorie trends.
    Not for raw data -- use withings_get_body/sleep/activity instead.
    """
    def _analyse():
        conn = db.get_db()

        if compare:
            result = _compare_periods(conn, data_type, compare)
        else:
            start, end = parse_date(start_date, end_date, default_days=365)
            s, e = start.isoformat(), end.isoformat()

            if data_type == "body":
                result = _trend_body(conn, s, e, period)
            elif data_type == "sleep":
                result = _trend_sleep(conn, s, e, period)
            elif data_type == "activity":
                result = _trend_activity(conn, s, e, period)
            else:
                result = {"error": f"Unknown data_type '{data_type}'. Use: body, sleep, or activity."}

        conn.close()
        return result

    result = await anyio.to_thread.run_sync(_analyse)
    return format_response(result)
