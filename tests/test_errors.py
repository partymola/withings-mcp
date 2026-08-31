"""Which exceptions keep their message when they leave a tool.

`mcp` 2.1 keeps a `ToolError`'s text and replaces every other exception's with
"Error executing tool <name>", so `require_auth` converts a `WithingsError`
and deliberately leaves everything else to be masked. Both halves are easy to
break without the suite noticing: widening the clause puts an unplanned
failure's text on the wire, and a new class that forgets the base loses its
message a layer above the one every behavioural test calls.
"""

import asyncio
import importlib
import json
import pkgutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import withings_mcp
from withings_mcp.api import WithingsAPIError
from withings_mcp.errors import WithingsError
from withings_mcp.helpers import require_auth

#: Exceptions deliberately left for `mcp` to mask, named so that leaving the
#: base off stays a decision rather than an omission. Fully qualified, or an
#: entry would exempt that bare name in every module at once. Empty today:
#: every exception class this package defines is written for the model to read.
_NOT_MODEL_FACING: frozenset[str] = frozenset()


class TestWhichErrorsAreExplainedToTheModel:
    @patch("withings_mcp.helpers.WITHINGS_CLIENT_PATH")
    @patch("withings_mcp.helpers.WITHINGS_TOKENS_PATH")
    def test_a_deliberate_error_keeps_its_message(self, tokens_path, client_path):
        client_path.exists.return_value = True
        tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            raise WithingsAPIError("Withings API error (status 503).")

        with pytest.raises(ToolError) as excinfo:
            asyncio.run(tool_fn())
        assert "status 503" in str(excinfo.value)

    # More than one type, because a partial widening is the realistic mistake
    # rather than `except Exception`. The two that look like tidying are the
    # point: TokenRefused and RefreshNetworkError already carry RuntimeError,
    # and InvalidDateError already carries ValueError, so adding either to the
    # clause reads as making it agree with itself.
    @pytest.mark.parametrize(
        "unplanned",
        [
            OSError("/home/someone/.config/withings-mcp/withings_tokens.json"),
            RuntimeError("an unanticipated failure"),
            ValueError("an unanticipated bad value"),
            KeyError("access_token"),
            sqlite3.OperationalError("no such table: body_measurements"),
        ],
        ids=["oserror", "runtimeerror", "valueerror", "keyerror", "sqlite"],
    )
    @patch("withings_mcp.helpers.WITHINGS_CLIENT_PATH")
    @patch("withings_mcp.helpers.WITHINGS_TOKENS_PATH")
    def test_an_unplanned_error_is_left_to_be_masked(self, tokens_path, client_path, unplanned):
        # The OSError is the measured case: a failure to read the token file or
        # the cache names an absolute path. Converting everything puts it on
        # the wire.
        client_path.exists.return_value = True
        tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            raise unplanned

        with pytest.raises(type(unplanned)):
            asyncio.run(tool_fn())

    @patch("withings_mcp.helpers.WITHINGS_CLIENT_PATH")
    @patch("withings_mcp.helpers.WITHINGS_TOKENS_PATH")
    def test_a_bad_date_still_names_the_formats(self, tokens_path, client_path, monkeypatch):
        # Through a real tool: parse_date runs inside the tool body, so nothing
        # converts it unless the decorator does.
        #
        # The sync and the database are stubbed rather than relied on being
        # unreachable. This suite has no autouse network guard, and the only
        # thing that keeps this test offline is `parse_date` being the tool's
        # first statement: move it below the cache branch and an unstubbed
        # version reaches the live API and the developer's own database, which
        # `config.py` defaults to a path inside the checkout.
        client_path.exists.return_value = True
        tokens_path.exists.return_value = True

        from withings_mcp import db
        from withings_mcp.tools import body_tools

        def _refuse(*args, **kwargs):
            pytest.fail("the tool reached past parse_date")

        monkeypatch.setattr(body_tools, "auto_sync_if_stale", _refuse)
        monkeypatch.setattr(db, "get_db", _refuse)

        with pytest.raises(ToolError) as excinfo:
            asyncio.run(body_tools.withings_get_body(start_date="not-a-date"))

        assert "YYYY-MM-DD" in str(excinfo.value)

    @patch("withings_mcp.helpers.WITHINGS_CLIENT_PATH")
    @patch("withings_mcp.helpers.WITHINGS_TOKENS_PATH")
    def test_a_missing_credential_still_answers_rather_than_raising(self, tokens_path, client_path):
        # The gate returns JSON and must not become a ToolError with the rest:
        # a caller with no credentials gets an answer, not a failed call.
        client_path.exists.return_value = False
        tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            raise AssertionError("must not be reached")

        parsed = json.loads(asyncio.run(tool_fn()))
        assert "Run: withings-mcp auth" in parsed["error"]


def test_every_exception_this_package_defines_reaches_the_model():
    """A class that forgets the base passes every behavioural test.

    Keyed by module and name, or a conforming class in a later-walked module
    hides a broken one of the same name. Seeded with the package module,
    because `walk_packages` yields what is inside the directory and never
    `__init__.py`. Both are pinned by mutants. `onerror` is not, and cannot be
    while `tools` is the only subpackage: the loop imports it directly before
    the walk would recurse into it. It guards a nested subpackage this layout
    does not have yet.

    It sees classes this package defines, not raise sites: a bare
    `RuntimeError` or `ValueError` raised here is masked and nothing reports
    it.
    """
    package = Path(withings_mcp.__file__).parent
    modules = [withings_mcp]
    for info in pkgutil.walk_packages(
        [str(package)],
        f"{withings_mcp.__name__}.",
        onerror=lambda name: pytest.fail(f"{name} could not be imported, so it went unchecked"),
    ):
        modules.append(importlib.import_module(info.name))

    found = {}
    for module in modules:
        for name, obj in vars(module).items():
            # Defined here, not imported into here: every module that raises
            # one of these also imports it.
            if (
                isinstance(obj, type)
                and issubclass(obj, Exception)
                and obj.__module__ == module.__name__
            ):
                found[f"{module.__name__}.{name}"] = obj

    assert found, "no exception class found: the package layout has moved"
    stragglers = sorted(
        name
        for name, obj in found.items()
        if name not in _NOT_MODEL_FACING and not issubclass(obj, WithingsError)
    )
    assert not stragglers, (
        f"raised with a message the model will never see: {stragglers}. Subclass "
        "WithingsError, or name it in _NOT_MODEL_FACING and say why"
    )
