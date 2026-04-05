"""Device query tool - always live."""

import logging
from datetime import datetime, timezone

import anyio

from ..mcp_instance import mcp
from ..helpers import format_response, require_auth
from .. import api
from ..config import WITHINGS_USER_V2_URL

logger = logging.getLogger(__name__)


def _fetch_devices():
    """Fetch connected devices from the API."""
    body = api.post(WITHINGS_USER_V2_URL, {"action": "getdevice"})

    devices = []
    for d in body.get("devices", []):
        last_session = d.get("last_session_date")
        if last_session:
            last_session = datetime.fromtimestamp(
                last_session, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M")

        devices.append({
            "type": d.get("type", ""),
            "model": d.get("model", ""),
            "battery": d.get("battery", ""),
            "last_session": last_session,
            "timezone": d.get("timezone", ""),
        })

    return devices


@mcp.tool()
@require_auth
async def withings_get_devices() -> str:
    """Get connected Withings devices with battery and firmware info.

    Always fetched live from the Withings API.

    Returns device type, model name, battery level (high/medium/low),
    and last session date for each connected device.
    """
    devices = await anyio.to_thread.run_sync(_fetch_devices)

    if not devices:
        return format_response({
            "message": "No connected devices found.",
            "hint": "Ensure devices are registered in the Withings app.",
        })

    return format_response({"devices": devices, "count": len(devices)})
