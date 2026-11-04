"""Shared utilities for the Withings MCP server."""

import calendar
import functools
import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .config import WITHINGS_CLIENT_PATH, WITHINGS_TOKENS_PATH
from .errors import InvalidDateError, WithingsError

logger = logging.getLogger(__name__)


# --- Response formatting ---


def format_response(result: Any) -> str:
    """JSON-serialize a result for MCP transport."""
    if isinstance(result, (dict, list)):
        return json.dumps(result, indent=2, default=str)
    elif result is None:
        return json.dumps(None)
    else:
        return json.dumps({"result": str(result)})


# --- Withings value decoding ---


def parse_value(measure: dict) -> float:
    """Decode a Withings measurement value.

    Withings encodes values as integers with a power-of-10 exponent:
    actual_value = value * 10^unit

    Example: weight 72.5 kg -> {"value": 72500, "unit": -3} -> 72500 * 10^-3 = 72.5
    """
    return measure["value"] * (10 ** measure["unit"])


# --- Date parsing with coercion ---


def parse_date(
    start_str: str | None,
    end_str: str | None = None,
    default_days: int = 30,
) -> tuple[date, date]:
    """Parse flexible date inputs into a (start_date, end_date) tuple.

    Accepted formats:
        "YYYY-MM-DD"  -> exact date
        "YYYY-MM"     -> first of month (start) or last of month (end)
        "30d"         -> 30 days ago from today
        None          -> default_days before the end date or today, whichever is
                         earlier (start), or today (end)

    A start after the end is refused, because the empty answer reads as no data.
    """
    # Read once, or `0d` across midnight lands after the end date it defaults to.
    today = date.today()
    end_date = _parse_single_date(end_str, today, today, is_end=True)
    if end_date == date.max:
        # The live tools ask for up to the day after the end.
        raise InvalidDateError(
            f"End date {end_date} is too late to query. Use 9999-12-30 or earlier."
        )
    # By ordinal, because the date arithmetic raises past year 1. From today
    # when the end is later, or the whole default span lies in the future.
    default_start = date.fromordinal(max(min(end_date, today).toordinal() - default_days, 1))
    start_date = _parse_single_date(start_str, default_start, today, is_end=False)
    if start_date > end_date:
        end_name = f"end date {end_date}" if end_str is not None else f"today ({end_date})"
        raise InvalidDateError(
            f"Start date {start_date} is after {end_name}. Use a start on or before that date."
        )
    return start_date, end_date


def _parse_single_date(date_str: str | None, default: date, today: date, is_end: bool) -> date:
    if date_str is None:
        return default

    # The patterns match the shape and the stdlib rejects the value: a month of
    # 13, a 30th of February or a count of days past year 1 raises a bare
    # ValueError or OverflowError, which reaches the caller masked.
    try:
        if m := re.fullmatch(r"(\d+)d", date_str):
            return today - timedelta(days=int(m.group(1)))

        if re.fullmatch(r"\d{4}-\d{2}", date_str):
            year, month = int(date_str[:4]), int(date_str[5:7])
            return date(year, month, calendar.monthrange(year, month)[1] if is_end else 1)

        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            return date.fromisoformat(date_str)
    except (ValueError, OverflowError):
        pass

    raise InvalidDateError(
        f"Invalid date '{date_str}'. Use YYYY-MM-DD, YYYY-MM, or Nd (e.g. '30d')."
    )


# --- Formatting helpers ---


def format_duration(seconds: int | None) -> str:
    """Convert seconds to human-readable duration."""
    if seconds is None:
        return ""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_distance(meters: float | None) -> str:
    """Convert meters to human-readable distance."""
    if meters is None:
        return ""
    if meters >= 1000:
        return f"{meters / 1000:.1f} km"
    return f"{int(meters)} m"


# --- Auth decorator ---


def require_auth(func):
    """Gate a tool on credentials, and let its own errors keep their message.

    `mcp` 2.1 keeps a `ToolError`'s text and replaces every other exception's
    with "Error executing tool <name>", so a WithingsError is converted and
    nothing else is. Why only those: errors.py.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if not WITHINGS_CLIENT_PATH.exists() or not WITHINGS_TOKENS_PATH.exists():
            return json.dumps(
                {
                    "error": "Withings not configured. Run: withings-mcp auth",
                }
            )
        try:
            return await func(*args, **kwargs)
        except WithingsError as e:
            raise ToolError(str(e)) from e

    return wrapper


# --- Constants ---

MEASURE_TYPES = {
    1: "weight_kg",
    4: "height_m",
    5: "lean_mass_kg",
    6: "fat_pct",
    8: "fat_mass_kg",
    9: "diastolic_bp",
    10: "systolic_bp",
    11: "heart_rate",
    12: "temperature_c",
    54: "spo2_pct",
    71: "body_temperature_c",
    76: "muscle_mass_kg",
    77: "hydration_kg",
    88: "bone_mass_kg",
    91: "pulse_wave_velocity",
    170: "visceral_fat_index",
    226: "basal_metabolic_rate",
}

WORKOUT_CATEGORIES = {
    1: "walk",
    2: "run",
    3: "hiking",
    4: "skating",
    5: "bmx",
    6: "cycling",
    7: "swimming",
    8: "surfing",
    9: "kitesurfing",
    10: "windsurfing",
    11: "bodyboard",
    12: "tennis",
    13: "table_tennis",
    14: "squash",
    15: "badminton",
    16: "weightlifting",
    17: "calisthenics",
    18: "elliptical",
    19: "pilates",
    20: "basketball",
    21: "soccer",
    22: "football",
    23: "rugby",
    24: "volleyball",
    25: "waterpolo",
    26: "horse_riding",
    27: "golf",
    28: "yoga",
    29: "dancing",
    30: "boxing",
    31: "fencing",
    32: "wrestling",
    33: "martial_arts",
    34: "skiing",
    35: "snowboarding",
    36: "other",
    187: "rowing",
    188: "zumba",
    191: "baseball",
    192: "handball",
    193: "hockey",
    194: "ice_hockey",
    195: "climbing",
    196: "ice_skating",
    272: "multi_sport",
    306: "indoor_walk",
    307: "indoor_running",
    308: "indoor_cycling",
}

SLEEP_STATES = {
    0: "awake",
    1: "light",
    2: "deep",
    3: "rem",
    4: "manual",
    5: "unspecified",
}


def resolve_measure_type(type_id: int) -> str:
    """Resolve a Withings measure type ID to a name."""
    return MEASURE_TYPES.get(type_id, f"type_{type_id}")


def resolve_workout_category(category_id: int) -> str:
    """Resolve a Withings workout category ID to a name."""
    return WORKOUT_CATEGORIES.get(category_id, "other")


def resolve_sleep_state(state_id: int) -> str:
    """Resolve a Withings sleep state ID to a name."""
    return SLEEP_STATES.get(state_id, f"state_{state_id}")
