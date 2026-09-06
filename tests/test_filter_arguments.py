"""A filter value nothing can match is refused, not answered with stripped data.

`withings_get_body(metrics="no_such_metric")` returned one entry per
measurement group carrying nothing but a date, which reads as measurements
that exist and are empty. `withings_get_workouts(category="cycl1ng")` returned
no workouts, which reads as a period without any.

Withings defines both vocabularies and this package holds the names, so the
accepted values are a list it can state. That is the difference from the
sibling servers, where the provider can name something the code has never
seen and the cache is the only vocabulary there is.

The calls go through `mcp.call_tool`, the layer a model reaches, and the
refusal arrives as a `ToolError` because `require_auth` converts what this
package raises deliberately.
"""

import asyncio
import json
import re
import sqlite3
from unittest.mock import patch

import pytest

from tests.fixtures import fake_body_db_row, fake_workout, fake_workout_db_row
from withings_mcp import db, helpers
from withings_mcp.mcp_instance import mcp
from withings_mcp.tools import activity_tools, body_tools

WINDOW = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
CACHED_CATEGORY = "cycling"


def seeded_db(rows):
    """A cache holding the given rows, on the test's own connection."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for save, row in rows:
        save(conn, row)
    conn.commit()
    return conn


def call(tool, module, args, rows=()):
    """Call a registered tool, returning (raised_message, parsed_result)."""
    conn = seeded_db(rows)
    try:
        with (
            patch.object(helpers, "WITHINGS_CLIENT_PATH", _Exists()),
            patch.object(helpers, "WITHINGS_TOKENS_PATH", _Exists()),
            patch.object(module.db, "get_db", lambda *a, **k: conn),
            patch.object(module, "auto_sync_if_stale", lambda _t: None),
        ):
            try:
                result = asyncio.run(mcp.call_tool(tool, args))
            except Exception as e:
                # The message is what is under test, so every type is caught.
                return str(e), None
    finally:
        conn.close()
    text = "".join(c.text for c in result.content if getattr(c, "text", None))
    return None, json.loads(text)


class _Exists:
    """A credential path `require_auth` is satisfied by."""

    def exists(self):
        return True


def body(args, **kw):
    return call("withings_get_body", body_tools, {**WINDOW, **args}, **kw)


def workouts(args, **kw):
    return call("withings_get_workouts", activity_tools, {**WINDOW, **args}, **kw)


BODY_ROW = (db.save_body_measurement, fake_body_db_row(date="2026-01-10", grpid=1001))
WORKOUT_ROW = (
    db.save_workout,
    fake_workout_db_row(date="2026-01-10", category_name=CACHED_CATEGORY),
)


def test_the_fixtures_hold_what_these_tests_name():
    """Every assertion below is vacuous against a cache without them."""
    _, data = body({}, rows=[BODY_ROW])
    assert "weight_kg" in data["measurements"][0]

    _, data = workouts({}, rows=[WORKOUT_ROW])
    assert [w["type"] for w in data["workouts"]] == [CACHED_CATEGORY]


@pytest.mark.parametrize(
    "value", ["no_such_metric", "weight", "weight_kg,no_such_metric", "", " ,"]
)
def test_a_metric_this_server_cannot_report_is_refused(value):
    raised, data = body({"metrics": value}, rows=[BODY_ROW])
    assert raised is not None, f"metrics={value!r} was answered rather than refused: {data}"


def test_the_metric_refusal_names_the_bad_value_and_the_usable_ones():
    raised, _ = body({"metrics": "weight_kg,no_such_metric"}, rows=[BODY_ROW])
    assert "no_such_metric" in raised, raised
    assert "weight_kg" in raised, raised
    # A metric the caller did not ask for, so the list is the vocabulary
    # rather than an echo of the request.
    assert "bone_mass_kg" in raised, raised


def test_a_known_metric_still_filters():
    raised, data = body({"metrics": "weight_kg"}, rows=[BODY_ROW])
    assert raised is None, raised
    assert set(data["measurements"][0]) == {"date", "weight_kg"}


def test_a_metric_the_cache_has_no_column_for_is_an_empty_answer_not_a_refusal():
    """A real metric this period has none of is a true empty result.

    Withings reports what its devices measured, so a metric absent from every
    group is an answer about the period rather than about the filter.
    """
    raised, data = body({"metrics": "pulse_wave_velocity"}, rows=[BODY_ROW])
    assert raised is None, raised
    assert "measurements" not in data, data


def test_a_group_that_recorded_none_of_the_asked_metrics_is_dropped():
    """The cached row carries every column, so the key is always there.

    A blood-pressure group holds no weight, and testing keys rather than
    values returned it as `{"date": ..., "weight_kg": null}`, which is the
    defect this refusal exists to remove wearing a filter.
    """
    blood_pressure = fake_body_db_row(date="2026-01-12", grpid=1002, weight_kg=None)
    raised, data = body(
        {"metrics": "weight_kg"},
        rows=[BODY_ROW, (db.save_body_measurement, blood_pressure)],
    )
    assert raised is None, raised
    assert data["count"] == 1, data
    assert data["measurements"][0]["weight_kg"] is not None


def test_whitespace_around_a_metric_is_ignored():
    raised, data = body({"metrics": " weight_kg , fat_pct "}, rows=[BODY_ROW])
    assert raised is None, raised
    assert set(data["measurements"][0]) == {"date", "weight_kg", "fat_pct"}


@pytest.mark.parametrize("value", ["cycl1ng", "not-a-sport", ""])
def test_a_workout_category_this_server_cannot_produce_is_refused(value):
    raised, data = workouts({"category": value}, rows=[WORKOUT_ROW])
    assert raised is not None, f"category={value!r} was answered rather than refused: {data}"


def test_the_category_refusal_names_what_the_cache_holds():
    """The full vocabulary is 48 names, which is the docstring's job.

    What a caller can act on is the handful they actually recorded, so the
    refusal names those and the schema carries the rest.
    """
    raised, _ = workouts({"category": "not-a-sport"}, rows=[WORKOUT_ROW])
    assert CACHED_CATEGORY in raised, raised


@pytest.mark.parametrize("value", ["cycling", "CYCLING", "cycl", " cycling "])
def test_a_category_that_matches_a_known_name_still_answers(value):
    """The padded value is the one a guard that normalises privately loses.

    Stripping and folding inside the check while handing the raw string to
    the query accepts this and then matches nothing, which is the empty
    answer the refusal replaced.
    """
    raised, data = workouts({"category": value}, rows=[WORKOUT_ROW])
    assert raised is None, raised
    assert [w["type"] for w in data["workouts"]] == [CACHED_CATEGORY]


@pytest.mark.parametrize("value", ["%", "cycl_ng"])
def test_a_value_matching_no_category_is_refused_wildcard_or_not(value):
    """Neither is a substring of any category name, so neither can filter.

    Unescaped they are LIKE patterns: `%` matches everything and `cycl_ng`
    matches `cycling`, so the query would answer where the guard refused.
    """
    raised, data = workouts({"category": value}, rows=[WORKOUT_ROW])
    assert raised is not None, f"category={value!r} was answered rather than refused: {data}"


def test_an_underscore_is_a_character_rather_than_a_wildcard():
    """`_` is a real character in names like horse_riding, so it filters.

    Unescaped in the LIKE pattern it matches any single character, which
    answers a narrowed question with every workout in the window.
    """
    riding = fake_workout_db_row(
        date="2026-01-12",
        startdate="2026-01-12T10:00:00+00:00",
        category=25,
        category_name="horse_riding",
    )
    raised, data = workouts({"category": "_"}, rows=[WORKOUT_ROW, (db.save_workout, riding)])
    assert raised is None, raised
    assert [w["type"] for w in data["workouts"]] == ["horse_riding"]


@pytest.mark.parametrize("value", ["cycling", "CYCLING", " cycling "])
def test_the_live_path_matches_on_the_same_normalised_value(value):
    """The live path has no cache to fall back on, so its half needs its own pin.

    Both call sites take the normalised value, and a test on the cached one
    cannot see the live one reverting: a mutant handing `_fetch_workouts_live`
    the raw string again survived the whole suite, because the only live test
    with a category passed a value normalisation does not change.
    """
    session = fake_workout(category=6)  # cycling
    with patch.object(activity_tools.api, "post", return_value={"series": [session]}):
        raised, data = workouts({"category": value, "live": True})
    assert raised is None, raised
    assert [w["type"] for w in data["workouts"]] == [CACHED_CATEGORY]


def test_a_known_category_the_cache_lacks_is_an_empty_answer_not_a_refusal():
    """Withings names the categories, so one you have not done is a real zero."""
    raised, data = workouts({"category": "yoga"}, rows=[WORKOUT_ROW])
    assert raised is None, raised
    assert "workouts" not in data, data


def documented_values(tool) -> set[str]:
    """The values a tool's docstring offers, read from its Options list.

    One extractor for both lists rather than one each: two would drift in how
    they read a docstring, which is the same failure a level up from the one
    these comparisons catch. The blank line ending the block is the terminator.
    """
    listed = re.search(r"Options:(.*?)\n\s*\n", tool.__doc__, re.DOTALL)
    assert listed, "the argument documents no Options: list"
    return {name.strip(" .`") for name in listed.group(1).replace("\n", " ").split(",")}


def test_the_documented_categories_are_the_ones_the_server_can_produce():
    """The docstring is where a model reads the vocabulary before calling.

    A list written by hand beside the constant it restates is the paired fact
    this repo keeps getting wrong, so it is compared rather than trusted.
    """
    assert documented_values(activity_tools.withings_get_workouts) == set(
        helpers.WORKOUT_CATEGORIES.values()
    )


def test_the_documented_metrics_are_the_ones_the_server_reports():
    """The same pairing on the other tool, and the list that just changed.

    A measure type added to the constant and not to the docstring is accepted
    by the filter and offered to nobody, which nothing else here would see.
    """
    assert documented_values(body_tools.withings_get_body) == set(helpers.MEASURE_TYPES.values())


@pytest.mark.parametrize(
    "tool, present",
    [
        (activity_tools.withings_get_workouts, "cycling"),
        (body_tools.withings_get_body, "weight_kg"),
    ],
)
def test_the_extractor_reads_a_list_rather_than_the_whole_docstring(tool, present):
    """Otherwise the comparisons above could be passing on prose."""
    documented = documented_values(tool)
    assert present in documented
    assert not any(" " in name for name in documented), documented
