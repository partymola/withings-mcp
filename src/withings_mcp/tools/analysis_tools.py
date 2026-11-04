"""Trend analysis tool - works from local cache only."""

import calendar
import logging
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

import anyio

from .. import db
from ..errors import InvalidDateError
from ..helpers import format_duration, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import auto_sync_if_stale

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
        return {
            "message": (
                "No body data in cache for this period. Try live=True on the "
                "get_* tools to fetch directly from the API."
            )
        }

    fields = [
        "weight_kg",
        "fat_pct",
        "fat_mass_kg",
        "muscle_mass_kg",
        "bone_mass_kg",
        "hydration_kg",
    ]
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
        return {
            "message": (
                "No sleep data in cache for this period. Try live=True on the "
                "get_* tools to fetch directly from the API."
            )
        }

    fields = [
        "total_sleep_sec",
        "deep_sleep_sec",
        "light_sleep_sec",
        "rem_sleep_sec",
        "sleep_score",
        "hr_average",
        "rr_average",
    ]
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
        return {
            "message": (
                "No activity data in cache for this period. Try live=True on the "
                "get_* tools to fetch directly from the API."
            )
        }

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
            "total_distance_km": round(sum(b.get("distance_m", [])) / 1000, 1)
            if b.get("distance_m")
            else None,
            "avg_active_calories": _avg(b.get("active_calories", [])),
        }
        periods.append(entry)

    return {"periods": periods, "data_type": "activity", "aggregation": period}


_TREND_FNS = {
    "body": _trend_body,
    "sleep": _trend_sleep,
    "activity": _trend_activity,
}
# Compare mode reads its own tables, so it has to hold the same types as the
# dispatch the schema is built from.
_COMPARE_QUERY_FNS = {
    "body": db.query_body,
    "sleep": db.query_sleep,
    "activity": db.query_activities,
}

# Unpacked from the dispatch rather than written out, so the schema offers
# exactly the types there is a trend function for.
TrendType = Literal[*_TREND_FNS]
TrendPeriod = Literal["weekly", "monthly", "quarterly"]


def _compare_ranges(compare_str: str) -> list[tuple[date, date]]:
    """The two spans of 'last_30d vs previous_30d', or InvalidDateError."""
    parts = re.split(r"\s+vs\s+", compare_str.strip(), maxsplit=1)
    if len(parts) != 2:
        raise InvalidDateError(
            "Invalid compare format. Use: 'last_30d vs previous_30d' or '2026-03 vs 2026-02'"
        )

    today = date.today()
    ranges = []
    for part in parts:
        part = part.strip()
        span = None
        # A month of 13 or a count of days past year 1 matches the pattern and
        # raises, which reaches the caller masked.
        try:
            if m := re.fullmatch(r"(last|previous)_(\d+)d", part):
                days = int(m.group(2))
                if days > 0:
                    end = today if m.group(1) == "last" else today - timedelta(days=days)
                    span = (end - timedelta(days=days - 1), end)
            elif re.fullmatch(r"\d{4}-\d{2}", part):
                year, month = int(part[:4]), int(part[5:7])
                span = (
                    date(year, month, 1),
                    date(year, month, calendar.monthrange(year, month)[1]),
                )
            elif m := re.fullmatch(r"(\d{4})-Q([1-4])", part):
                year, q = int(m.group(1)), int(m.group(2))
                span = (
                    date(year, q * 3 - 2, 1),
                    date(year, q * 3, calendar.monthrange(year, q * 3)[1]),
                )
        except (ValueError, OverflowError):
            pass
        if span is None:
            raise InvalidDateError(
                f"Cannot parse period '{part}'. Use: last_30d, previous_30d, 2026-03, or 2026-Q1"
            )
        ranges.append(span)
    return ranges


def _compare_periods(conn, data_type: TrendType, ranges: list[tuple[date, date]]) -> dict:
    """Summarise the two spans `_compare_ranges` returned, side by side."""
    query_fn = _COMPARE_QUERY_FNS[data_type]

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
    data_type: TrendType = "body",
    period: TrendPeriod = "monthly",
    start_date: str | None = None,
    end_date: str | None = None,
    compare: str | None = None,
) -> str:
    """Analyse trends in cached health data.

    Computes averages, min/max, and changes over time from the local
    cache. Auto-syncs if the cache is stale (no sync today).

    Args:
        data_type: What to analyse. Options: "body", "sleep", "activity".
        period: Aggregation period. Options: "weekly", "monthly",
            "quarterly". Default: "monthly".
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "365d" for
            relative days. Default: 365 days back from the end date or
            today, whichever is earlier.
        end_date: End date as "YYYY-MM-DD", "YYYY-MM" (its last day), or
            "Nd". Default: today.
        compare: Compare two periods. Format: "last_30d vs previous_30d",
            "2026-03 vs 2026-02", "2026-Q1 vs 2025-Q4".
            When set, period/start_date/end_date are ignored.

    Returns aggregated averages with change indicators. For body data:
    weight, fat%, muscle trends. For sleep: duration, score, HR trends.
    For activity: steps, distance, calorie trends.
    Not for raw data -- use withings_get_body/sleep/activity instead.
    """

    if compare:
        ranges = _compare_ranges(compare)
    else:
        start, end = parse_date(start_date, end_date, default_days=365)

    def _analyse():
        auto_sync_if_stale(data_type)
        conn = db.get_db()

        if compare:
            result = _compare_periods(conn, data_type, ranges)
        else:
            result = _TREND_FNS[data_type](conn, start.isoformat(), end.isoformat(), period)

        conn.close()
        return result

    result = await anyio.to_thread.run_sync(_analyse)
    return format_response(result)
