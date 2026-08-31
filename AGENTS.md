# withings-mcp - agent guide

`CLAUDE.md` symlinks to this file. It orients AI agents and contributors working *in* the code, and deliberately does not repeat the user-facing docs:

- **What it is, install, auth, tools, config, CLI, usage** -> [README.md](README.md)
- **Dev environment, running tests, pre-commit hook, PR & security process** -> [CONTRIBUTING.md](CONTRIBUTING.md)

**This is a public open-source repository and health data is sensitive PII.** Read the Data Safety Rules before committing.

## Data Safety Rules

Before committing ANY change, verify:

- **No real health measurements** in code, tests, commits, or docs - no real weights, sleep data, heart rates, blood pressure readings, SpO2 values, or body composition figures
- **No personal identifiers** - no real names, Withings user IDs, device IDs, MAC addresses, or dates of birth
- **No credentials** - no OAuth tokens, client secrets, API keys, or session data
- **Test fixtures**: always import from `tests/fixtures.py` factory functions - never hardcode values that could resemble real health data
- **Error messages and logs**: status codes and operation names only - never measurement values or API response bodies
- **`config/` and `*.db` are gitignored for a reason** - never override this
- **Before committing**: run `git diff --cached` and verify nothing resembles real user data

The `scripts/check-no-data.sh` pre-commit hook rejects database files, config secrets, and large files - install it per [CONTRIBUTING.md](CONTRIBUTING.md).

## Architecture

- **Entry point**: `src/withings_mcp/cli.py` - routes `auth`/`sync`/`doctor` subcommands or starts the MCP stdio server. **Keep it inside the package.** As `src/main.py` with a `main:main` console script, the wheel installed a top-level `main` module into site-packages, where any other package doing the same overwrites it - installing a sibling MCP server made `withings-mcp` start that server instead
- **MCP server**: `mcp_instance.py` creates the shared `MCPServer("withings-mcp")` instance
- **Auth**: `auth.py` - OAuth with a 30-second inline code exchange in the callback handler, token refresh with a 5-min buffer. Redirect `http://localhost:8585`; scopes `user.info,user.metrics,user.activity`; access tokens 3h, refresh tokens 1 year. **The callback listener is `_CallbackServer`, not a bare `HTTPServer`, and that matters on Windows.** `HTTPServer` sets `allow_reuse_address`, which on POSIX only waives TIME_WAIT. On Windows it is consent to be displaced: per Microsoft's same-user table for a specific-address bind, a first socket holding the address with `SO_REUSEADDR` is bound over by a second one asking for the same, so another process could take the authorisation code. **Not asking is what closes it** - the same table shows a first bind with neither option refusing that second bind. `SO_EXCLUSIVEADDRUSE` is asked for as well because Microsoft recommends it for server sockets and because it is the half that would still hold for a wildcard bind, which this listener is not but a sibling's is. Never ask for both: reuse requested after exclusive use is the configuration Microsoft calls insecure, and `allow_reuse_port` (the POSIX-side equivalent hazard) is pinned off for the same reason. The cost is that a Windows port stays held until the previous connection finishes closing. `setup_auth` catches the resulting `OSError` and binds before opening the browser. `TestTheCallbackPortIsNotShared` drives the Windows branch on a POSIX runner against a recording socket rather than reading the source for it - source assertions here let the option be set after the bind, at the wrong level, on the wrong socket or with a value of 0, all of which pass a check that only looks for the name. A revert to `HTTPServer` also silently disarms the "never reaches the socket" guard in `TestSetupAuthSurvivesABrokenClientFile`, which patches the name the code constructs
- **Failure classification**: `refresh_token` is a boundary over `_refresh_token` and raises exactly two types. `TokenRefused` only where the server or the credential files judged the credentials unusable; `RefreshNetworkError` for everything else, via a catch-all, so an unanticipated failure lands there by construction rather than by listing exception types. `api.post` maps the first to `WithingsAuthError`, which `doctor` grades FAIL and answers with "run auth" - rewriting a token file other hosts may share - and the second to `WithingsAPIError`, which grades WARN. **Never widen `TokenRefused` to a condition that can clear on its own** (a rate limit, a 403 from a WAF, an unreadable response): that turns a transient fault into a rotated token on every host. Pinned by `TestTheRefreshBoundary` and `TestAgainstTheRealRefresh` in `tests/test_api.py`
- **API**: `api.py` - POST wrapper that handles Withings' status-in-body errors, typed exceptions
- **DB**: `db.py` - SQLite schema, insert/query helpers
- **Tools**: `tools/` - domain-grouped modules
- **Helpers**: `helpers.py` - value parsing, date coercion, formatting; `MEASURE_TYPES` and `WORKOUT_CATEGORIES` constants live here. **`require_auth` is also the one place an exception that escapes a tool keeps its message.** `mcp` 2.1 keeps a `ToolError`'s text and replaces every other exception's with `Error executing tool <name>`, so a `WithingsError` is converted and nothing else is. The split follows the Data Safety Rules: this package writes the messages it raises deliberately and keeps response bodies out of them, while an unplanned failure's text is what must not travel, since a filesystem error on the token file or the cache names an absolute path. **Never widen the conversion to `Exception`**: it would undo that in one line, and `test_an_unplanned_error_is_left_to_be_masked` is the half that fails if you do. The credential gate above it still *returns* JSON rather than raising, so a caller with no credentials gets an answer instead of a failed call. Pinned in both directions by `TestWhichErrorsAreExplainedToTheModel` in `tests/test_errors.py`, and structurally by `test_every_exception_this_package_defines_reaches_the_model` in the same file, which sees classes this package *defines* and not raise sites: a bare `RuntimeError` or `ValueError` raised here is masked and nothing reports it
- **Config**: env vars `WITHINGS_MCP_CONFIG_DIR`, `WITHINGS_MCP_DB_PATH`
- **Doctor**: `doctor.py` - the `doctor` subcommand. Every check is read-only and makes no API call, and two properties are load-bearing rather than incidental: it never opens the database through `db.get_db()` (which would create it and destroy the evidence for the wrong-path and stale-cache checks), and of this package it imports only `config` and `db` - never `auth` or `api`, so no code path can send the stored refresh token or rewrite the token file. Both are pinned in `tests/test_doctor.py`, the second by parsing this module's imports rather than by observing behaviour; keep them true when adding a check. **The permission findings are inert on Windows** - `os.access` there consults neither mode bits nor ACLs, so an unreadable database or an unwritable config directory cannot be reported at all, and the tests asserting them skip rather than being made to pass. A Windows database that is genuinely ACL-denied still reports, via the open-failure branch, which says the contents are not implicated - so the gap is a missing diagnosis, never a wrong one

## Database schema

SQLite at `withings.db` (gitignored). Tables:

- `body_measurements` - grpid-keyed body comp (weight, fat, muscle, bone, BP, SpO2)
- `sleep_summaries` - nightly sleep data (durations, HR, RR, score, snoring)
- `activities` - daily activity (steps, distance, calories, HR zones)
- `workouts` - individual sessions (type, duration, HR, calories)
- `sync_log` - sync history with timestamps and record counts

## Key patterns

- All tools are `async def` with `@mcp.tool()` + `@require_auth` decorators
- Sync HTTP calls wrapped in `anyio.to_thread.run_sync()` to avoid blocking
- Cache-first with a `live=True` flag for fresh API queries; `withings_get_heart` and `withings_get_devices` are always live; `withings_get_sleep(detail=True)` is live (minute-by-minute phases, capped at 7 days)
- Date parameters accept ISO dates, month strings, and relative days (e.g. "30d")
- All logging to stderr (stdout reserved for JSON-RPC); error messages carry status codes only, never health values
- **Every datetime conversion names a zone.** The Withings API speaks epoch seconds and this server stores ISO dates, so a naive `fromtimestamp` or `combine` silently reads the machine's zone and moves a date by a day. `doctor.py:_timestamp_or_none` is the single exception, deliberately, because it prints a token expiry for whoever is reading the terminal. A source-reading test in `tests/test_query_tools.py` requires the zone everywhere else and exempts that one by function name, so moving it fails the test rather than widening the exemption. The test exists because the behavioural guard - running the suite under a forced western zone - needs `time.tzset`, which Windows does not have

## Running tests

See [CONTRIBUTING.md](CONTRIBUTING.md#run-the-test-suite), which carries the command and the per-platform interpreter paths. Keep it the only copy.

All tests use in-memory SQLite and fictional data from `tests/fixtures.py`.
