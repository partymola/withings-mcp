"""Body composition query tool."""

import logging
from datetime import datetime, timedelta, timezone

import anyio

from ..mcp_instance import mcp
from ..helpers import (
    format_response, require_auth, parse_date, parse_value,
    resolve_measure_type, MEASURE_TYPES,
)
from .. import api, db
from ..config import WITHINGS_MEASURE_URL

logger = logging.getLogger(__name__)

_BODY_MEASTYPES = ",".join(str(k) for k in MEASURE_TYPES.keys())


def _fetch_live(start_date, end_date):
    """Fetch body measurements directly from the API."""
    start_ts = int(datetime.combine(start_date, datetime.min.time(),
                                     tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.combine(end_date + timedelta(days=1),
                                   datetime.min.time(),
                                   tzinfo=timezone.utc).timestamp())
    results = []
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
            ds = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            entry = {"date": ds}
            for m in grp.get("measures", []):
                name = resolve_measure_type(m.get("type", 0))
                if not name.startswith("type_"):
                    entry[name] = round(parse_value(m), 3)
            results.append(entry)

        if body.get("more", 0) in (1, True):
            offset = body.get("offset", 0)
        else:
            break

    return results


@mcp.tool()
@require_auth
async def withings_get_body(
    start_date: str | None = None,
    end_date: str | None = None,
    metrics: str | None = None,
    live: bool = False,
) -> str:
    """Get body composition measurements (weight, fat, muscle, etc.).

    Returns measurements from the local cache by default. Use live=True
    to fetch directly from Withings API. Run withings_sync first to
    populate the cache.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d" for
            relative days. Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        metrics: Comma-separated metric filter, e.g. "weight_kg,fat_pct".
            Default: all available metrics. Options: weight_kg, fat_pct,
            fat_mass_kg, muscle_mass_kg, hydration_kg, bone_mass_kg,
            heart_rate, systolic_bp, diastolic_bp, spo2_pct.
        live: If true, fetch from Withings API instead of cache.

    Returns measurements sorted by date, one entry per measurement group.
    Not for sleep or activity data -- use withings_get_sleep or
    withings_get_activity instead.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    if live:
        entries = await anyio.to_thread.run_sync(lambda: _fetch_live(start, end))
    else:
        def _query():
            conn = db.get_db()
            rows = db.query_body(conn, start.isoformat(), end.isoformat())
            conn.close()
            return rows
        entries = await anyio.to_thread.run_sync(_query)

    # Filter metrics if requested
    if metrics:
        keep = {m.strip() for m in metrics.split(",")}
        keep.add("date")
        entries = [{k: v for k, v in e.items() if k in keep} for e in entries]

    if not entries:
        return format_response({
            "message": "No body measurements found for this period.",
            "hint": "Run withings_sync first, or try live=True.",
        })

    return format_response({"measurements": entries, "count": len(entries)})
