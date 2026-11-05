"""Tests for helpers module: parse_value, parse_date, format_*, constants."""

import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from withings_mcp import helpers
from withings_mcp.errors import InvalidDateError
from withings_mcp.helpers import (
    format_distance,
    format_duration,
    format_response,
    parse_date,
    parse_value,
    resolve_measure_type,
    resolve_sleep_state,
    resolve_workout_category,
)


class TestParseValue(unittest.TestCase):
    def test_positive_exponent(self):
        # 725 * 10^0 = 725
        self.assertEqual(parse_value({"value": 725, "unit": 0}), 725)

    def test_negative_exponent(self):
        # 72500 * 10^-3 = 72.5
        self.assertAlmostEqual(parse_value({"value": 72500, "unit": -3}), 72.5, places=3)

    def test_two_decimal_exponent(self):
        # 2015 * 10^-2 = 20.15
        self.assertAlmostEqual(parse_value({"value": 2015, "unit": -2}), 20.15, places=3)

    def test_large_negative_exponent(self):
        # 700000 * 10^-4 = 70.0
        self.assertAlmostEqual(parse_value({"value": 700000, "unit": -4}), 70.0, places=3)

    def test_positive_unit(self):
        # 5 * 10^1 = 50
        self.assertEqual(parse_value({"value": 5, "unit": 1}), 50)


class TestParseDate(unittest.TestCase):
    def test_iso_date(self):
        start, end = parse_date("2026-01-15", "2026-01-31")
        self.assertEqual(start, date(2026, 1, 15))
        self.assertEqual(end, date(2026, 1, 31))

    def test_month_start(self):
        start, end = parse_date("2026-04", "2026-04")
        self.assertEqual(start, date(2026, 4, 1))
        self.assertEqual(end, date(2026, 4, 30))

    def test_month_december(self):
        start, end = parse_date("2026-12", "2026-12")
        self.assertEqual(start, date(2026, 12, 1))
        self.assertEqual(end, date(2026, 12, 31))

    def test_relative_days(self):
        start, end = parse_date("7d")
        self.assertEqual(start, date.today() - timedelta(days=7))
        self.assertEqual(end, date.today())

    def test_none_defaults(self):
        start, end = parse_date(None, None, default_days=30)
        self.assertEqual(start, date.today() - timedelta(days=30))
        self.assertEqual(end, date.today())

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_date("not-a-date")

    def test_relative_30d(self):
        start, end = parse_date("30d")
        self.assertEqual(start, date.today() - timedelta(days=30))

    def test_a_start_after_the_end_is_refused_naming_both(self):
        with self.assertRaises(InvalidDateError) as caught:
            parse_date("2026-03-02", "2026-03-01")
        self.assertEqual(
            str(caught.exception),
            "Start date 2026-03-02 is after end date 2026-03-01. "
            "Use a start on or before that date.",
        )

    def test_a_start_after_a_defaulted_end_calls_it_today(self):
        tomorrow = date.today() + timedelta(days=1)
        with self.assertRaises(InvalidDateError) as caught:
            parse_date(tomorrow.isoformat())
        self.assertIn(f"after today ({date.today()})", str(caught.exception))

    def test_a_start_on_the_end_date_is_accepted(self):
        self.assertEqual(
            parse_date("2026-03-01", "2026-03-01"), (date(2026, 3, 1), date(2026, 3, 1))
        )

    def test_a_missing_start_counts_back_from_the_end_date(self):
        # From today it would land after an end in the past and be refused,
        # though the caller sent no start.
        self.assertEqual(
            parse_date(None, "2020-01", default_days=30), (date(2020, 1, 1), date(2020, 1, 31))
        )

    def test_a_missing_start_before_a_future_end_counts_back_from_today(self):
        # From the end it would land after today and answer as no data.
        end = date.today() + timedelta(days=40)
        self.assertEqual(
            parse_date(None, end.isoformat(), default_days=30),
            (date.today() - timedelta(days=30), end),
        )

    def test_the_last_date_a_tool_can_query_past_is_refused(self):
        # The live tools ask for up to the day after the end, which date
        # cannot hold for its last day.
        with self.assertRaises(InvalidDateError) as caught:
            parse_date("2026-01-01", "9999-12-31")
        self.assertIn("9999-12-31", str(caught.exception))
        self.assertEqual(parse_date("9999-12-01", "9999-12-30")[1], date(9999, 12, 30))

    def test_a_trailing_newline_is_not_part_of_any_format(self):
        for value in ("30d\n", "2026-03\n", "2026-03-01\n"):
            with self.subTest(value=value):
                with self.assertRaises(InvalidDateError):
                    parse_date(value)

    def test_a_default_reaching_past_year_one_stops_there(self):
        self.assertEqual(
            parse_date(None, "0001-01-05", default_days=30), (date(1, 1, 1), date(1, 1, 5))
        )

    def test_a_date_of_the_right_shape_that_does_not_exist_is_refused(self):
        for value in ("2026-13", "2026-00", "2026-02-30", "99999999d"):
            for args in ((value, None), (None, value)):
                with self.subTest(args=args):
                    with self.assertRaises(InvalidDateError) as caught:
                        parse_date(*args)
                    self.assertIn("YYYY-MM-DD", str(caught.exception))

    def test_today_is_read_once(self):
        # Two reads either side of midnight would put `0d` a day after the end
        # it defaults to, and refuse it.
        days = iter([date(2026, 3, 1), date(2026, 3, 2)])

        class _MidnightPasses(date):
            @classmethod
            def today(cls):
                return next(days)

        with patch.object(helpers, "date", _MidnightPasses):
            start, end = parse_date("0d")
        self.assertEqual((start, end), (date(2026, 3, 1), date(2026, 3, 1)))


class TestFormatResponse(unittest.TestCase):
    def test_dict(self):
        result = format_response({"key": "value"})
        self.assertEqual(json.loads(result), {"key": "value"})

    def test_list(self):
        result = format_response([1, 2, 3])
        self.assertEqual(json.loads(result), [1, 2, 3])

    def test_compact_json_keeps_a_reply_as_long_as_its_contents(self):
        # Indented, the output grows with the square of a reply's nesting.
        for result in ({"a": {"b": [1, {"c": 2}]}}, [{"a": [1]}, 2]):
            with self.subTest(result=result):
                self.assertEqual(format_response(result), json.dumps(result))

    def test_none(self):
        result = format_response(None)
        self.assertEqual(json.loads(result), None)

    def test_string(self):
        result = format_response("hello")
        self.assertEqual(json.loads(result), {"result": "hello"})


class TestFormatDuration(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_duration(0), "0m")

    def test_minutes_only(self):
        self.assertEqual(format_duration(2700), "45m")

    def test_hours_and_minutes(self):
        self.assertEqual(format_duration(3661), "1h 1m")

    def test_exact_hours(self):
        self.assertEqual(format_duration(7200), "2h 0m")

    def test_large_duration(self):
        self.assertEqual(format_duration(86400), "24h 0m")

    def test_none(self):
        self.assertEqual(format_duration(None), "")


class TestFormatDistance(unittest.TestCase):
    def test_meters(self):
        self.assertEqual(format_distance(850), "850 m")

    def test_kilometers(self):
        self.assertEqual(format_distance(1500), "1.5 km")

    def test_zero(self):
        self.assertEqual(format_distance(0), "0 m")

    def test_none(self):
        self.assertEqual(format_distance(None), "")

    def test_exact_km(self):
        self.assertEqual(format_distance(5000), "5.0 km")


class TestMeasureTypeMapping(unittest.TestCase):
    def test_known_type(self):
        self.assertEqual(resolve_measure_type(1), "weight_kg")
        self.assertEqual(resolve_measure_type(6), "fat_pct")
        self.assertEqual(resolve_measure_type(76), "muscle_mass_kg")

    def test_unknown_type(self):
        self.assertEqual(resolve_measure_type(9999), "type_9999")


class TestWorkoutCategoryMapping(unittest.TestCase):
    def test_known_category(self):
        self.assertEqual(resolve_workout_category(6), "cycling")
        self.assertEqual(resolve_workout_category(2), "run")

    def test_unknown_category(self):
        self.assertEqual(resolve_workout_category(9999), "other")


class TestSleepStateMapping(unittest.TestCase):
    def test_known_states(self):
        self.assertEqual(resolve_sleep_state(0), "awake")
        self.assertEqual(resolve_sleep_state(2), "deep")
        self.assertEqual(resolve_sleep_state(3), "rem")

    def test_unknown_state(self):
        self.assertEqual(resolve_sleep_state(99), "state_99")


if __name__ == "__main__":
    unittest.main()
