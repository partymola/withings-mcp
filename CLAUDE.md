# Withings MCP Server

**This is a public open-source repository.** Health data is sensitive PII. Every commit, PR, and file is visible to anyone.

## Data Safety Rules

Before committing ANY change, verify:

- **No real health measurements** in code, tests, commits, or docs - no real weights, sleep data, heart rates, blood pressure readings, SpO2 values, or body composition figures
- **No personal identifiers** - no real names, Withings user IDs, device IDs, MAC addresses, or dates of birth
- **No credentials** - no OAuth tokens, client secrets, API keys, or session data
- **Test fixtures**: always import from `tests/fixtures.py` factory functions - never hardcode values that could resemble real health data
- **Error messages and logs**: include status codes and operation names only - never measurement values or API response bodies
- **`config/` and `*.db` are gitignored for a reason** - never override this
- **Before committing**: run `git diff --cached` and verify nothing resembles real user data

The pre-commit hook (`scripts/check-no-data.sh`) automatically rejects database files, config secrets, and large files.

## Quick Reference

```bash
withings-mcp auth     # Interactive OAuth setup (opens browser)
withings-mcp          # Start MCP server (stdio transport, used by Claude Code)
```

## Tools

| Tool | Source | Purpose |
|------|--------|---------|
| `withings_sync` | Live API -> SQLite | Incremental sync of body, sleep, activity, workouts |
| `withings_get_body` | Cache / Live | Body composition (weight, fat%, muscle, bone, etc.) |
| `withings_get_sleep` | Cache / Live | Sleep summaries or detailed phase time-series |
| `withings_get_activity` | Cache / Live | Daily steps, distance, calories, active time |
| `withings_get_workouts` | Cache / Live | Workout sessions with type, duration, HR |
| `withings_get_heart` | Live only | ECG recordings and AFib detection |
| `withings_get_devices` | Live only | Connected devices, battery, firmware |
| `withings_trends` | Cache only | Period averages, comparisons, min/max/delta |

## Architecture

- **Entry point**: `src/main.py` - routes `auth` subcommand or starts MCP stdio server
- **FastMCP**: `mcp_instance.py` creates the shared `FastMCP("withings-mcp")` instance
- **Auth**: `auth.py` - OAuth setup with 30-second inline code exchange, token refresh with 5-minute buffer
- **API**: `api.py` - POST wrapper handling Withings status-in-body errors, typed exceptions
- **DB**: `db.py` - SQLite schema (5 tables), insert/query helpers
- **Tools**: `tools/` directory with domain-grouped modules
- **Helpers**: `helpers.py` - value parsing, date coercion, formatting, constants

## Auth and Credentials

- OAuth app registered at https://developer.withings.com/dashboard
- Redirect URL: `http://localhost:8585`
- Scopes: `user.info,user.metrics,user.activity`
- Credentials in `config/withings_client.json` and `config/withings_tokens.json` (gitignored)
- Token auto-refreshes with 5-minute buffer before expiry
- Access tokens: 3 hours. Refresh tokens: 1 year.
- Auth codes expire in 30 seconds (exchange happens inline in callback handler)

## Database

SQLite at `withings.db` (gitignored). Tables:
- `body_measurements` - grpid-keyed body comp (weight, fat, muscle, bone, BP, SpO2)
- `sleep_summaries` - nightly sleep data (durations, HR, RR, score, snoring)
- `activities` - daily activity (steps, distance, calories, HR zones)
- `workouts` - individual sessions (type, duration, HR, calories)
- `sync_log` - sync history with timestamps and record counts

## Key Patterns

- All tools are `async def` with `@mcp.tool()` + `@require_auth` decorators
- Sync HTTP calls wrapped in `anyio.to_thread.run_sync()` to avoid blocking
- Cache-first with `live=True` flag for fresh API queries
- Date parameters accept ISO dates, month strings, and relative days (e.g. "30d")
- All logging to stderr (stdout reserved for JSON-RPC)
- Error messages contain status codes only, never health data values

## Running Tests

```bash
cd withings-mcp
.venv/bin/python -m pytest tests/ -v   # 74 tests
```

All tests use in-memory SQLite and fictional data from `tests/fixtures.py`.

## Troubleshooting

- **"Withings not configured"**: Run `withings-mcp auth`
- **"Token refresh failed"**: Re-run `withings-mcp auth` (refresh token may have expired)
- **Empty results**: Run `withings_sync` first to populate the cache
- **"Rate limited"**: Wait 60 seconds and retry
- **Python 3.14 issues**: Use Python 3.13 (pydantic-core compatibility)
