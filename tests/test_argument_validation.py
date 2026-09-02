"""An enumerated argument must be refused, never answered under the wrong label.

`withings_trends(period="not-a-period")` returned a correct year of *monthly*
figures and echoed `"aggregation": "not-a-period"`. Nothing was empty, nothing
raised, and nothing about the reply looked wrong: the model was handed real
data under a label that misdescribes it, which is worse than an error.

The calls here go through `mcp.call_tool`, the only layer that applies the
argument schema. Calling the Python function directly skips it, so a test
written that way passes whether the constraint is there or not. The rest read
the docstring or the dispatches, which have no other reader.
"""

import asyncio
import inspect
import json
import re
import sqlite3
import unittest
from typing import get_args
from unittest.mock import patch

from tests.fixtures import fake_activity_db_row, fake_body_db_row, fake_sleep_db_row
from withings_mcp import db, helpers
from withings_mcp.mcp_instance import mcp
from withings_mcp.tools import analysis_tools
from withings_mcp.tools.analysis_tools import (
    _COMPARE_QUERY_FNS,
    _TREND_FNS,
    TrendPeriod,
    TrendType,
    _get_period_key,
    withings_trends,
)

#: How many rows of each type the cache holds. The counts must differ, or a
#: summary built from the wrong table is indistinguishable from the right
#: one: two dispatch entries swapped then answer the wrong question under the
#: right label, which is the same defect this file exists for, one dict
#: along. `test_the_row_counts_can_tell_the_tables_apart` holds it.
ROWS_PER_TYPE = {"body": 2, "sleep": 3, "activity": 5}

#: What a bucket key looks like per period. An independent oracle: the enum
#: and the dispatch can agree with each other while the aggregation happens
#: by something else, and the echoed label would still come back correct.
PERIOD_KEY_SHAPE = {
    "weekly": r"^\d{4}-W\d{2}$",
    "monthly": r"^\d{4}-\d{2}$",
    "quarterly": r"^\d{4}-Q[1-4]$",
}


class _Exists:
    """A credential path `require_auth` is satisfied by."""

    def exists(self):
        return True


def _seeded_db():
    """A cache holding a different number of rows for each analysable type.

    Built with `check_same_thread=False`: the tool body runs in a worker
    thread via anyio, and this connection is made on the test's.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for i in range(ROWS_PER_TYPE["body"]):
        db.save_body_measurement(conn, fake_body_db_row(date=f"2026-03-{10 + i:02d}", grpid=i + 1))
    for i in range(ROWS_PER_TYPE["sleep"]):
        db.save_sleep_summary(conn, fake_sleep_db_row(date=f"2026-03-{10 + i:02d}"))
    for i in range(ROWS_PER_TYPE["activity"]):
        db.save_activity(conn, fake_activity_db_row(date=f"2026-03-{10 + i:02d}"))
    conn.commit()
    return conn


def call(name, args):
    """Call a registered tool, returning (raised_message, result_text)."""
    conn = _seeded_db()
    try:
        with (
            patch.object(helpers, "WITHINGS_CLIENT_PATH", _Exists()),
            patch.object(helpers, "WITHINGS_TOKENS_PATH", _Exists()),
            patch.object(analysis_tools.db, "get_db", lambda *a, **k: conn),
            patch.object(analysis_tools, "auto_sync_if_stale", lambda _t: None),
        ):
            try:
                result = asyncio.run(mcp.call_tool(name, args))
            except Exception as e:
                # The message is what is under test, so every type is caught.
                return str(e), None
    finally:
        conn.close()
    return None, "".join(c.text for c in result.content if getattr(c, "text", None))


def names_the_argument(said: str | None, argument: str, value) -> bool:
    """Whether a message is about this argument, rather than any reply at all.

    The empty string is excluded from the value half deliberately: it is a
    substring of every message, so admitting it would count any reply,
    including a crash whose text `mcp` has masked to the tool's name.
    """
    return bool(said) and (argument in said or bool(value) and str(value) in said)


def refusal(args, argument, value):
    """The refusal of *this* argument, or None if the tool answered regardless.

    A message counts only when it names the argument or the value, so that a
    failure with some other cause is not read as the refusal under test.
    """
    raised, text = call("withings_trends", args)
    said = raised
    if said is None:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        said = parsed.get("error") if isinstance(parsed, dict) else None
    return said if names_the_argument(said, argument, value) else None


def documented_default(argument: str, doc: str | None = None) -> str | None:
    """The value the docstring quotes as this argument's default, if it says."""
    marked = re.search(r'Default:\s*"([^"]+)"', documented_chunk(argument, doc))
    return marked.group(1) if marked else None


def documented_chunk(argument: str, doc: str | None = None) -> str:
    """The docstring text describing one argument.

    Runs from the argument's own line to the next argument at the same
    indent; a continuation line is indented further, so it stays inside.
    """
    doc = withings_trends.__doc__ if doc is None else doc
    opening = re.search(rf"^(?P<indent> *){argument}:", doc, re.MULTILINE)
    assert opening, f"the docstring documents no {argument!r} argument"
    rest = doc[opening.end() :]
    end = re.search(rf"^{opening['indent']}\w+:", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def documented_values(argument: str, doc: str | None = None) -> set[str]:
    """The values the docstring offers for one argument.

    The schema enum and the "Options:" list are the same fact written twice.
    Narrowed to what sits between `Options:` and `Default:`, which is
    load-bearing: with the default quoted in the same chunk, a value dropped
    from `Options:` while remaining the default is still found and the check
    passes over it.
    """
    options = documented_chunk(argument, doc).split("Options:", 1)
    assert len(options) == 2, f"{argument} documents no Options: list"
    return set(re.findall(r'"([^"]+)"', options[1].split("Default:", 1)[0]))


BAD_VALUES = {
    "data_type": ["not-a-type", "steps", ""],
    "period": ["not-a-period", "daily", "MONTHLY"],
}


class TestAnInvalidEnumeratedArgumentIsRefused(unittest.TestCase):
    def test_every_bad_value_is_refused(self):
        for argument, values in sorted(BAD_VALUES.items()):
            for value in values:
                with self.subTest(argument=argument, value=value):
                    said = refusal({argument: value}, argument, value)
                    self.assertIsNotNone(
                        said,
                        f"withings_trends accepted {argument}={value!r} "
                        "and answered as though it were valid",
                    )

    def test_a_refusal_names_the_values_that_would_have_worked(self):
        """A refusal the model cannot act on is barely better than a wrong label."""
        for argument, values in sorted(BAD_VALUES.items()):
            for value in values:
                with self.subTest(argument=argument, value=value):
                    said = refusal({argument: value}, argument, value) or ""
                    expected = get_args(TrendType if argument == "data_type" else TrendPeriod)
                    # An alias that stopped being a Literal yields no values,
                    # and every assertion below it would pass vacuously.
                    self.assertTrue(expected, f"{argument} has no enumerated values")
                    self.assertTrue(
                        all(v in said for v in expected),
                        f"refusing {argument}={value!r} did not name the accepted values: {said!r}",
                    )

    def test_a_bad_data_type_is_refused_in_compare_mode_too(self):
        """The schema refuses above both branches, so `compare=` needs no guard of its own.

        Its dispatch is covered separately, by the test that counts rows.
        """
        said = refusal(
            {"data_type": "not-a-type", "compare": "2026-03 vs 2026-02"},
            "data_type",
            "not-a-type",
        )
        self.assertIsNotNone(said, "compare mode accepted a data_type that does not exist")


class TestTheSchemaCarriesTheValues(unittest.TestCase):
    """Refusing is not enough: the model reads the schema before it calls.

    Nothing above distinguishes a schema enum from a check inside the body,
    and only the schema is visible to a caller deciding what to send.
    """

    def test_both_enumerated_arguments_declare_their_values(self):
        tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["withings_trends"]
        properties = tool.input_schema["properties"]
        for argument, alias in (("data_type", TrendType), ("period", TrendPeriod)):
            with self.subTest(argument=argument):
                self.assertEqual(
                    properties[argument].get("enum"),
                    list(get_args(alias)),
                    f"{argument} is not constrained in withings_trends' schema",
                )


class TestTheChecksHereHoldTheirOwnGround(unittest.TestCase):
    """Every guard in this file needs a pin, or it reverts in one line.

    Each of these came in with a fix, and each is a single edit away from
    passing over the defect it was added for while the suite stays green.
    """

    def test_the_row_counts_can_tell_the_tables_apart(self):
        """Equal counts make a summary of the wrong table look like the right one."""
        self.assertEqual(len(set(ROWS_PER_TYPE.values())), len(ROWS_PER_TYPE))
        self.assertEqual(set(ROWS_PER_TYPE), set(get_args(TrendType)))

    def test_a_documented_value_is_not_found_in_the_default_alone(self):
        """The `Options:` to `Default:` narrowing, which a tidy-up removes in one line."""
        doc = (
            '        period: Aggregation period. Options: "weekly",\n'
            '            "quarterly". Default: "monthly".\n'
            "        after: something else.\n"
        )
        self.assertEqual(documented_values("period", doc), {"weekly", "quarterly"})

    def test_an_empty_value_does_not_count_as_naming_itself(self):
        """`"" in said` is true of every message, so it would accept any reply."""
        self.assertFalse(names_the_argument("Error executing tool withings_trends", "period", ""))
        self.assertFalse(names_the_argument(None, "period", "daily"))
        self.assertTrue(names_the_argument("Input should be 'weekly'", "period", "weekly"))
        self.assertTrue(names_the_argument("period is wrong", "period", "zz"))

    def test_the_key_shapes_tell_the_periods_apart(self):
        """Loosened to anything, the shapes pass over every period and go quiet."""
        self.assertEqual(set(PERIOD_KEY_SHAPE), set(get_args(TrendPeriod)))
        for period in get_args(TrendPeriod):
            key = _get_period_key("2026-03-15", period)
            with self.subTest(period=period):
                self.assertRegex(key, PERIOD_KEY_SHAPE[period])
                for other, shape in PERIOD_KEY_SHAPE.items():
                    if other != period:
                        self.assertNotRegex(key, shape)


class TestTheDocstringOffersWhatTheSchemaAccepts(unittest.TestCase):
    def test_the_documented_data_types_are_the_accepted_ones(self):
        self.assertEqual(documented_values("data_type"), set(get_args(TrendType)))

    def test_the_documented_periods_are_the_accepted_ones(self):
        self.assertEqual(documented_values("period"), set(get_args(TrendPeriod)))

    def test_the_extractor_reads_the_argument_it_was_asked_for(self):
        """Otherwise both assertions above could be comparing empty sets."""
        self.assertIn("weekly", documented_values("period"))
        self.assertNotIn("weekly", documented_values("data_type"))


class TestEveryAcceptedPeriodBucketsDifferently(unittest.TestCase):
    """The property behind the enum, rather than a list of three names.

    `_get_period_key` ends in an unlabelled `else` that returns the month, so
    a value added to `TrendPeriod` and nowhere else is aggregated monthly and
    reported under its own name, which is the whole defect this file is
    about, one line along.
    """

    def test_each_period_produces_a_key_of_its_own(self):
        keys = [_get_period_key("2026-03-15", p) for p in get_args(TrendPeriod)]
        self.assertEqual(len(set(keys)), len(keys), f"two periods bucket alike: {keys}")


class TestTheDispatchesCoverEveryAcceptedType(unittest.TestCase):
    def test_both_dispatches_answer_every_accepted_data_type(self):
        accepted = set(get_args(TrendType))
        self.assertTrue(accepted, "data_type has no enumerated values")
        # Tautological while TrendType is unpacked from _TREND_FNS. It is
        # here for the version that writes the list out by hand, which is
        # the only way the two can come apart.
        self.assertEqual(accepted, set(_TREND_FNS))
        # This one is not: _COMPARE_QUERY_FNS is a separate dict.
        self.assertEqual(accepted, set(_COMPARE_QUERY_FNS))


class TestValidArgumentsAreUnaffected(unittest.TestCase):
    """The refusals must not be so eager that real calls stop working."""

    def test_every_accepted_data_type_answers_about_the_type_it_was_asked_for(self):
        """Each trend function names its own type, so a swapped dispatch shows here.

        Asserting only that nothing failed leaves two entries exchangeable,
        so the tool answers about sleep under the label body and nothing
        looks wrong, which is this file's subject one dict along.
        """
        for data_type in get_args(TrendType):
            with self.subTest(data_type=data_type):
                raised, text = call("withings_trends", {"data_type": data_type})
                self.assertIsNone(raised, raised)
                result = json.loads(text)
                self.assertNotIn("error", result)
                self.assertEqual(result["data_type"], data_type)

    def test_every_accepted_period_still_answers_under_its_own_label(self):
        """The bucket keys are asserted as well as the echoed label.

        The label is written from the argument and says nothing about what
        was aggregated, so bucketing by the wrong span answers every period
        correctly labelled and wrongly grouped.
        """
        self.assertEqual(set(PERIOD_KEY_SHAPE), set(get_args(TrendPeriod)))
        for period in get_args(TrendPeriod):
            with self.subTest(period=period):
                raised, text = call(
                    "withings_trends",
                    {"data_type": "activity", "period": period, "start_date": "2026-03-01"},
                )
                self.assertIsNone(raised, raised)
                result = json.loads(text)
                self.assertEqual(result["aggregation"], period)
                self.assertTrue(result["periods"], "a valid period returned no buckets")
                for bucket in result["periods"]:
                    self.assertRegex(bucket["period"], PERIOD_KEY_SHAPE[period])

    def test_compare_mode_summarises_the_type_it_was_asked_for(self):
        """`data_type` comes back from the argument, so it proves nothing on its own.

        The row count does: the cache holds a different number of each type,
        so a summary built from the wrong table is visible in it. Without
        that, two entries of `_COMPARE_QUERY_FNS` are exchangeable.
        """
        for data_type, rows in sorted(ROWS_PER_TYPE.items()):
            with self.subTest(data_type=data_type):
                raised, text = call(
                    "withings_trends", {"data_type": data_type, "compare": "2026-03 vs 2026-02"}
                )
                self.assertIsNone(raised, raised)
                result = json.loads(text)
                self.assertEqual(result["data_type"], data_type)
                self.assertEqual(result["period_1"]["count"], rows)

    def test_the_defaults_are_values_the_schema_accepts(self):
        """A default is never validated, so it is the one way back to the defect.

        Pydantic checks what a caller sends and not what the signature falls
        back to, so `period: TrendPeriod = "daily"` refuses every bad value a
        caller can send and then aggregates monthly under the label "daily"
        for everyone who omits the argument. The docstring quotes the same
        value a second time, and moving one without the other makes it a
        false claim with nothing to notice.
        """
        signature = inspect.signature(withings_trends)
        for argument, alias in (("data_type", TrendType), ("period", TrendPeriod)):
            with self.subTest(argument=argument):
                accepted = get_args(alias)
                self.assertTrue(accepted, f"{argument} has no enumerated values")
                default = signature.parameters[argument].default
                self.assertIn(default, accepted)
                documented = documented_default(argument)
                if documented is not None:
                    self.assertEqual(documented, default)
        raised, text = call("withings_trends", {})
        self.assertIsNone(raised, raised)
        self.assertNotIn("error", json.loads(text))
