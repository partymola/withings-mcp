"""Shared utilities for the Withings MCP server."""

import functools
import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from .config import WITHINGS_CLIENT_PATH, WITHINGS_TOKENS_PATH

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

_RELATIVE_RE = re.compile(r"^(\d+)d$")


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
        None          -> default_days ago from today (start) or today (end)

    Returns (start_date, end_date) as date objects.
    """
    today = date.today()
    end_date = _parse_single_date(end_str, today, is_end=True)
    start_date = _parse_single_date(start_str, today - timedelta(days=default_days), is_end=False)
    return start_date, end_date


def _parse_single_date(date_str: str | None, default: date, is_end: bool) -> date:
    """Parse a single date string."""
    if date_str is None:
        return default

    # Relative: "30d", "7d", etc.
    m = _RELATIVE_RE.match(date_str)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))

    # Month: "2026-04"
    if re.match(r"^\d{4}-\d{2}$", date_str):
        year, month = int(date_str[:4]), int(date_str[5:7])
        if is_end:
            # Last day of month
            if month == 12:
                return date(year + 1, 1, 1) - timedelta(days=1)
            return date(year, month + 1, 1) - timedelta(days=1)
        return date(year, month, 1)

    # Full date: "2026-04-15"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date.fromisoformat(date_str)

    raise ValueError(
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
    """Decorator that checks credentials exist before calling a tool."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if not WITHINGS_CLIENT_PATH.exists() or not WITHINGS_TOKENS_PATH.exists():
            return json.dumps({
                "error": "Withings not configured. Run: withings-mcp auth",
            })
        return await func(*args, **kwargs)
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
    1: "walk", 2: "run", 3: "hiking", 4: "skating", 5: "bmx",
    6: "cycling", 7: "swimming", 8: "surfing", 9: "kitesurfing",
    10: "windsurfing", 11: "bodyboard", 12: "tennis", 13: "table_tennis",
    14: "squash", 15: "badminton", 16: "weightlifting", 17: "calisthenics",
    18: "elliptical", 19: "pilates", 20: "basketball", 21: "soccer",
    22: "football", 23: "rugby", 24: "volleyball", 25: "waterpolo",
    26: "horse_riding", 27: "golf", 28: "yoga", 29: "dancing",
    30: "boxing", 31: "fencing", 32: "wrestling", 33: "martial_arts",
    34: "skiing", 35: "snowboarding", 36: "other",
    187: "rowing", 188: "zumba", 191: "baseball", 192: "handball",
    193: "hockey", 194: "ice_hockey", 195: "climbing", 196: "ice_skating",
    272: "multi_sport", 306: "indoor_walk", 307: "indoor_running",
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
