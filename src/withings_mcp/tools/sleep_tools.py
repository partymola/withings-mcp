"""Sleep data query tool."""

import logging
from datetime import datetime, timedelta, timezone

import anyio

from ..mcp_instance import mcp
from ..helpers import (
    format_response, require_auth, parse_date,
    format_duration, resolve_sleep_state,
)
from .. import api, db
from ..config import WITHINGS_SLEEP_V2_URL

logger = logging.getLogger(__name__)

_SUMMARY_FIELDS = (
    "total_sleep_time,deepsleepduration,lightsleepduration,remsleepduration,"
    "wakeupduration,wakeupcount,hr_average,hr_min,hr_max,rr_average,rr_min,"
    "rr_max,sleep_score,snoring,apnea_hypopnea_index"
)


def _fetch_summary_live(start_date, end_date):
    """Fetch sleep summaries directly from the API."""
    results = []
    offset = 0

    while True:
        body = api.post(WITHINGS_SLEEP_V2_URL, {
            "action": "getsummary",
            "startdateymd": start_date.isoformat(),
            "enddateymd": end_date.isoformat(),
            "data_fields": _SUMMARY_FIELDS,
            "offset": offset,
        })

        for entry in body.get("series", []):
            data = entry.get("data", {})
            results.append({
                "date": entry.get("date", ""),
                "total_sleep": format_duration(data.get("total_sleep_time")),
                "deep_sleep": format_duration(data.get("deepsleepduration")),
                "light_sleep": format_duration(data.get("lightsleepduration")),
                "rem_sleep": format_duration(data.get("remsleepduration")),
                "awake": format_duration(data.get("wakeupduration")),
                "wakeup_count": data.get("wakeupcount"),
                "hr_average": data.get("hr_average"),
                "hr_min": data.get("hr_min"),
                "hr_max": data.get("hr_max"),
                "rr_average": data.get("rr_average"),
                "sleep_score": data.get("sleep_score"),
                "snoring": format_duration(data.get("snoring")),
            })

        if body.get("more", False):
            offset = body.get("offset", 0)
        else:
            break

    return results


def _fetch_detail_live(start_date, end_date):
    """Fetch detailed sleep phases from the API. Max 7 days."""
    start_ts = int(datetime.combine(start_date, datetime.min.time(),
                                     tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.combine(end_date + timedelta(days=1),
                                   datetime.min.time(),
                                   tzinfo=timezone.utc).timestamp())

    body = api.post(WITHINGS_SLEEP_V2_URL, {
        "action": "get",
        "startdate": start_ts,
        "enddate": end_ts,
        "data_fields": "hr,rr,snoring",
    })

    phases = []
    for entry in body.get("series", []):
        phases.append({
            "start": datetime.fromtimestamp(entry["startdate"], tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(entry["enddate"], tz=timezone.utc).isoformat(),
            "state": resolve_sleep_state(entry.get("state", 5)),
            "hr": entry.get("hr"),
            "rr": entry.get("rr"),
        })

    return phases


@mcp.tool()
@require_auth
async def withings_get_sleep(
    start_date: str | None = None,
    end_date: str | None = None,
    detail: bool = False,
    live: bool = False,
) -> str:
    """Get sleep data (summaries or detailed phases).

    Summary mode (default): nightly totals with duration, sleep score,
    HR, respiratory rate, and snoring. From local cache unless live=True.

    Detail mode (detail=True): minute-by-minute sleep phases (awake,
    light, deep, REM) with HR and respiratory rate. Always fetched live.
    Maximum 7 days per request (Withings API limit).

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "7d".
            Default: last 7 days (detail) or last 30 days (summary).
        end_date: End date as "YYYY-MM-DD". Default: today.
        detail: If true, return minute-by-minute sleep phases instead
            of nightly summaries. Always live, max 7 days.
        live: If true, fetch summaries from API instead of cache.
            Ignored when detail=True (always live).

    Returns nightly sleep data sorted by date.
    Not for body composition -- use withings_get_body instead.
    """
    default_days = 7 if detail else 30
    start, end = parse_date(start_date, end_date, default_days=default_days)

    if detail:
        # Enforce 7-day API limit
        if (end - start).days > 7:
            end = start + timedelta(days=7)

        phases = await anyio.to_thread.run_sync(lambda: _fetch_detail_live(start, end))
        if not phases:
            return format_response({
                "message": "No detailed sleep data found for this period.",
            })
        return format_response({"phases": phases, "count": len(phases)})

    if live:
        entries = await anyio.to_thread.run_sync(lambda: _fetch_summary_live(start, end))
    else:
        def _query():
            conn = db.get_db()
            rows = db.query_sleep(conn, start.isoformat(), end.isoformat())
            conn.close()
            # Format durations for display
            for r in rows:
                r["total_sleep"] = format_duration(r.get("total_sleep_sec"))
                r["deep_sleep"] = format_duration(r.get("deep_sleep_sec"))
                r["light_sleep"] = format_duration(r.get("light_sleep_sec"))
                r["rem_sleep"] = format_duration(r.get("rem_sleep_sec"))
                r["awake"] = format_duration(r.get("awake_sec"))
                r["snoring"] = format_duration(r.get("snoring_sec"))
            return rows
        entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response({
            "message": "No sleep data found for this period.",
            "hint": "Run withings_sync first, or try live=True.",
        })

    return format_response({"nights": entries, "count": len(entries)})
