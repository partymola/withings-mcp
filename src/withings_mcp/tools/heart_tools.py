"""Heart/ECG query tool - always live, no cache."""

import logging
from datetime import datetime, timedelta, timezone

import anyio

from ..mcp_instance import mcp
from ..helpers import format_response, require_auth, parse_date
from .. import api
from ..config import WITHINGS_HEART_V2_URL

logger = logging.getLogger(__name__)

_AFIB_STATUS = {0: "negative", 1: "positive", 2: "inconclusive"}


def _fetch_heart(start_date, end_date):
    """Fetch ECG recordings from the API."""
    start_ts = int(datetime.combine(start_date, datetime.min.time(),
                                     tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.combine(end_date + timedelta(days=1),
                                   datetime.min.time(),
                                   tzinfo=timezone.utc).timestamp())
    results = []
    offset = 0

    while True:
        body = api.post(WITHINGS_HEART_V2_URL, {
            "action": "list",
            "startdate": start_ts,
            "enddate": end_ts,
            "offset": offset,
        })

        for entry in body.get("series", []):
            ecg = entry.get("ecg", {})
            results.append({
                "date": datetime.fromtimestamp(
                    entry.get("timestamp", 0), tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M"),
                "heart_rate": entry.get("heart_rate"),
                "afib": _AFIB_STATUS.get(ecg.get("afib"), "unknown"),
                "signal_id": entry.get("signalid"),
            })

        if body.get("more", False):
            offset = body.get("offset", 0)
        else:
            break

    return results


@mcp.tool()
@require_auth
async def withings_get_heart(
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Get ECG recordings and atrial fibrillation detection results.

    Always fetched live from the Withings API (not cached due to large
    signal data). Requires a Withings device with ECG capability
    (ScanWatch, BPM Core).

    Args:
        start_date: Start date as "YYYY-MM-DD" or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.

    Returns ECG recording list with timestamps, AFib classification
    (negative/positive/inconclusive), and heart rate. Does not include
    raw signal waveforms.
    For resting heart rate trends, use withings_get_body or
    withings_get_sleep instead.
    """
    start, end = parse_date(start_date, end_date, default_days=30)
    recordings = await anyio.to_thread.run_sync(lambda: _fetch_heart(start, end))

    if not recordings:
        return format_response({
            "message": "No ECG recordings found for this period.",
            "hint": "Requires a Withings device with ECG capability (ScanWatch, BPM Core).",
        })

    return format_response({"recordings": recordings, "count": len(recordings)})
