"""The exception base this package raises deliberately.

Imports nothing from the package, so `api`, `auth` and `helpers` can all raise
from it without a cycle.
"""


class WithingsError(Exception):
    """An error this package raises on purpose, with text written for the model.

    `require_auth` converts these into `ToolError`, whose message `mcp` keeps
    on the wire; every other exception reaches the client as `Error executing
    tool <name>`. Anything not descended from this is treated as unplanned,
    and its text is what must not travel: a filesystem error on the token file
    or the cache names an absolute path, which is measured rather than assumed
    and is what the masking test raises.

    Membership is not a promise about the message. What each raise site may
    say is governed by the Data Safety Rules in AGENTS.md.
    """


class InvalidDateError(WithingsError, ValueError):
    """A date argument the tools cannot parse.

    Keeps `ValueError` so `parse_date`'s existing contract is unchanged for
    any caller catching it, which `TestParseDate.test_invalid_raises` pins.
    """
