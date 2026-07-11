"""Shared fictional test data factory.

ALL test data in this project MUST come from this module.
Values are obviously fictional round numbers - never use real health data.
"""


def _encode_value(value, decimals=1):
    """Encode a float as Withings value + unit (value * 10^unit)."""
    unit = -decimals
    encoded = round(value * (10**decimals))
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
    temperature=None,
    body_temperature=None,
):
    """Return a Withings API-shaped measuregrp dict.

    ``temperature`` populates measure type 12 (temperature_c) and
    ``body_temperature`` populates type 71 (body_temperature_c); both default
    to absent so callers opt in per test.
    """
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
    if temperature is not None:
        m = _encode_value(temperature, 1)
        m["type"] = 12
        measures.append(m)
    if body_temperature is not None:
        m = _encode_value(body_temperature, 1)
        m["type"] = 71
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


# --- Cache-row factories (shaped for db.save_* / query_* round-trips) ---


def fake_body_db_row(
    date="2026-01-15",
    grpid=1001,
    measured_at="2026-01-15T08:00:00+00:00",
    weight_kg=70.0,
    fat_pct=20.0,
    fat_mass_kg=14.0,
    muscle_mass_kg=30.0,
    hydration_kg=38.0,
    bone_mass_kg=3.0,
    heart_rate=None,
    systolic_bp=None,
    diastolic_bp=None,
    spo2_pct=None,
    temperature_c=None,
):
    """Return a body_measurements row dict (keys match db.save_body_measurement)."""
    return {
        "date": date,
        "measured_at": measured_at,
        "grpid": grpid,
        "weight_kg": weight_kg,
        "fat_pct": fat_pct,
        "fat_mass_kg": fat_mass_kg,
        "lean_mass_kg": None,
        "muscle_mass_kg": muscle_mass_kg,
        "hydration_kg": hydration_kg,
        "bone_mass_kg": bone_mass_kg,
        "heart_rate": heart_rate,
        "systolic_bp": systolic_bp,
        "diastolic_bp": diastolic_bp,
        "spo2_pct": spo2_pct,
        "temperature_c": temperature_c,
        "visceral_fat_index": None,
        "basal_metabolic_rate": None,
    }


def fake_sleep_db_row(
    date="2026-01-15",
    device_model="32",
    total_sleep_sec=28800,
    deep_sleep_sec=3600,
    light_sleep_sec=14400,
    rem_sleep_sec=7200,
    awake_sec=3600,
    wakeup_count=2,
    hr_average=58,
    rr_average=15,
    sleep_score=78,
    snoring_sec=600,
):
    """Return a sleep_summaries row dict (keys match db.save_sleep_summary)."""
    return {
        "date": date,
        "startdate": f"{date}T23:00:00+00:00",
        "enddate": f"{date}T07:00:00+00:00",
        "total_sleep_sec": total_sleep_sec,
        "deep_sleep_sec": deep_sleep_sec,
        "light_sleep_sec": light_sleep_sec,
        "rem_sleep_sec": rem_sleep_sec,
        "awake_sec": awake_sec,
        "wakeup_count": wakeup_count,
        "hr_average": hr_average,
        "hr_min": 48,
        "hr_max": 72,
        "rr_average": rr_average,
        "rr_min": 12,
        "rr_max": 19,
        "sleep_score": sleep_score,
        "snoring_sec": snoring_sec,
        "apnea_hypopnea_index": None,
        "device_model": device_model,
    }


def fake_activity_db_row(
    date="2026-01-15",
    steps=8000,
    distance_m=6000,
    active_calories=250,
    total_calories=1850,
    soft_sec=5400,
    moderate_sec=1800,
    intense_sec=600,
):
    """Return an activities row dict (keys match db.save_activity)."""
    return {
        "date": date,
        "steps": steps,
        "distance_m": distance_m,
        "active_calories": active_calories,
        "total_calories": total_calories,
        "soft_sec": soft_sec,
        "moderate_sec": moderate_sec,
        "intense_sec": intense_sec,
        "hr_average": 72,
        "hr_min": 55,
        "hr_max": 145,
        "hr_zone_0_sec": 3600,
        "hr_zone_1_sec": 1800,
        "hr_zone_2_sec": 600,
        "hr_zone_3_sec": 120,
    }


def fake_workout_db_row(
    date="2026-01-15",
    startdate="2026-01-15T10:00:00+00:00",
    category=6,
    category_name="cycling",
    duration_sec=3600,
    calories=350,
    distance_m=15000,
    steps=0,
    hr_average=135,
):
    """Return a workouts row dict (keys match db.save_workout)."""
    return {
        "date": date,
        "startdate": startdate,
        "enddate": "2026-01-15T11:00:00+00:00",
        "category": category,
        "category_name": category_name,
        "duration_sec": duration_sec,
        "calories": calories,
        "distance_m": distance_m,
        "steps": steps,
        "hr_average": hr_average,
        "hr_min": 95,
        "hr_max": 170,
    }


# --- Always-live API payload factories ---


def fake_ecg_recording(timestamp=1736899200, heart_rate=60, afib=0, signalid=5001):
    """Return an ECG list entry shaped for withings_get_heart's parser."""
    return {
        "timestamp": timestamp,
        "heart_rate": heart_rate,
        "ecg": {"afib": afib},
        "signalid": signalid,
    }


def fake_device(
    device_type="Scale",
    model="Body Comp",
    battery="high",
    last_session_date=1736899200,
    timezone="Europe/London",
):
    """Return a device entry shaped for withings_get_devices' parser."""
    return {
        "type": device_type,
        "model": model,
        "battery": battery,
        "last_session_date": last_session_date,
        "timezone": timezone,
    }


def fake_sleep_phase(startdate=1736899200, enddate=1736902800, state=1, hr=60, rr=15):
    """Return a detailed sleep-phase series entry (action 'get')."""
    return {
        "startdate": startdate,
        "enddate": enddate,
        "state": state,
        "hr": hr,
        "rr": rr,
    }
