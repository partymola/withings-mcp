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
- **Auth**: `auth.py` - OAuth with a 30-second inline code exchange in the callback handler, token refresh with a 5-min buffer. Redirect `http://localhost:8585`; scopes `user.info,user.metrics,user.activity`; access tokens 3h, refresh tokens 1 year
- **Failure classification**: `refresh_token` is a boundary over `_refresh_token` and raises exactly two types. `TokenRefused` only where the server or the credential files judged the credentials unusable; `RefreshNetworkError` for everything else, via a catch-all, so an unanticipated failure lands there by construction rather than by listing exception types. `api.post` maps the first to `WithingsAuthError`, which `doctor` grades FAIL and answers with "run auth" - rewriting a token file other hosts may share - and the second to `WithingsAPIError`, which grades WARN. **Never widen `TokenRefused` to a condition that can clear on its own** (a rate limit, a 403 from a WAF, an unreadable response): that turns a transient fault into a rotated token on every host. Pinned by `TestTheRefreshBoundary` and `TestAgainstTheRealRefresh` in `tests/test_api.py`
- **API**: `api.py` - POST wrapper that handles Withings' status-in-body errors, typed exceptions
- **DB**: `db.py` - SQLite schema, insert/query helpers
- **Tools**: `tools/` - domain-grouped modules
- **Helpers**: `helpers.py` - value parsing, date coercion, formatting; `MEASURE_TYPES` and `WORKOUT_CATEGORIES` constants live here
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

## Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

All tests use in-memory SQLite and fictional data from `tests/fixtures.py`.
