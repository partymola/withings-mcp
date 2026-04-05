"""Shared fictional test data factory.

ALL test data in this project MUST come from this module.
Values are obviously fictional round numbers - never use real health data.
"""

import math


def _encode_value(value, decimals=1):
    """Encode a float as Withings value + unit (value * 10^unit)."""
    unit = -decimals
    encoded = round(value * (10 ** decimals))
    return {"value": encoded, "type": 0, "unit": unit}


def fake_measure_group(
    grpid=1001,
    date="2026-01-15",
    timestamp=1736899200,
    weight=70.0,
    fat_pct=20.0,
    fat_mass=14.0,
    muscle_mass=30.0,
    hydration=38.0,
    bone_mass=3.0,
):
    """Return a Withings API-shaped measuregrp dict."""
    measures = []
    if weight is not None:
        m = _encode_value(weight, 3)
        m["type"] = 1
        measures.append(m)
    if fat_pct is not None:
        m = _encode_value(fat_pct, 2)
        m["type"] = 6
        measures.append(m)
    if fat_mass is not None:
        m = _encode_value(fat_mass, 3)
        m["type"] = 8
        measures.append(m)
    if muscle_mass is not None:
        m = _encode_value(muscle_mass, 3)
        m["type"] = 76
        measures.append(m)
    if hydration is not None:
        m = _encode_value(hydration, 3)
        m["type"] = 77
        measures.append(m)
    if bone_mass is not None:
        m = _encode_value(bone_mass, 3)
        m["type"] = 88
        measures.append(m)

    return {
        "grpid": grpid,
        "attrib": 0,
        "date": timestamp,
        "created": timestamp,
        "category": 1,
        "measures": measures,
    }


def fake_sleep_summary(
    date="2026-01-15",
    startdate=1736899200,
    enddate=1736928000,
    total_sleep=28800,
    deep_sleep=3600,
    light_sleep=14400,
    rem_sleep=7200,
    awake=3600,
    wakeup_count=2,
    hr_average=58,
    hr_min=48,
    hr_max=72,
    rr_average=15,
    rr_min=12,
    rr_max=19,
    sleep_score=78,
    snoring=600,
    model="Sleep Analyzer",
):
    """Return a Withings sleep summary dict."""
    return {
        "date": date,
        "startdate": startdate,
        "enddate": enddate,
        "data": {
            "total_sleep_time": total_sleep,
            "deepsleepduration": deep_sleep,
            "lightsleepduration": light_sleep,
            "remsleepduration": rem_sleep,
            "wakeupduration": awake,
            "wakeupcount": wakeup_count,
            "hr_average": hr_average,
            "hr_min": hr_min,
            "hr_max": hr_max,
            "rr_average": rr_average,
            "rr_min": rr_min,
            "rr_max": rr_max,
            "sleep_score": sleep_score,
            "snoring": snoring,
        },
        "model": 32,
        "model_id": 63,
    }


def fake_activity(
    date="2026-01-15",
    steps=8000,
    distance=6000,
    active_calories=250,
    total_calories=1850,
    soft=5400,
    moderate=1800,
    intense=600,
    hr_average=72,
    hr_min=55,
    hr_max=145,
):
    """Return a Withings activity summary dict."""
    return {
        "date": date,
        "steps": steps,
        "distance": distance,
        "calories": active_calories,
        "totalcalories": total_calories,
        "soft": soft,
        "moderate": moderate,
        "intense": intense,
        "hr_average": hr_average,
        "hr_min": hr_min,
        "hr_max": hr_max,
        "hr_zone_0": 3600,
        "hr_zone_1": 1800,
        "hr_zone_2": 600,
        "hr_zone_3": 120,
    }


def fake_workout(
    date="2026-01-15",
    startdate=1736935200,
    enddate=1736938800,
    category=6,
    calories=350,
    distance=15000,
    steps=0,
    hr_average=135,
    hr_min=95,
    hr_max=170,
):
    """Return a Withings workout dict."""
    return {
        "date": date,
        "startdate": startdate,
        "enddate": enddate,
        "category": category,
        "data": {
            "calories": calories,
            "distance": distance,
            "steps": steps,
            "hr_average": hr_average,
            "hr_min": hr_min,
            "hr_max": hr_max,
        },
    }


def fake_api_response(status=0, body=None):
    """Wrap a body in the Withings API response envelope."""
    resp = {"status": status}
    if body is not None:
        resp["body"] = body
    return resp
