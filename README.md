# withings-mcp

<!-- mcp-name: io.github.partymola/withings-mcp -->

[![CI](https://github.com/partymola/withings-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/partymola/withings-mcp/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/withings-mcp)](https://pypi.org/project/withings-mcp/)
[![Glama MCP Server](https://glama.ai/mcp/servers/partymola/withings-mcp/badges/score.svg)](https://glama.ai/mcp/servers/partymola/withings-mcp)

MCP server for the [Withings Health API](https://developer.withings.com/) with OAuth, local SQLite cache, and trend analysis.

**What makes this different from other Withings MCP servers:**
- Local SQLite cache for fast offline queries and historical trend analysis
- Incremental sync - only fetches new data since last sync
- Broad Withings coverage: 17 body-composition metrics plus sleep, daily activity, workouts, and ECG/AFib
- Automatic OAuth token refresh (access tokens: 3h, refresh tokens: 1 year)
- Zero dependencies beyond `mcp` (HTTP via stdlib)
- Python 3.13+ (tested on 3.13 and 3.14, on Linux, macOS and Windows, in CI)

## Tools

| Tool | Description | Data source |
|------|-------------|-------------|
| `withings_sync` | Sync data from Withings API to local cache | Live API -> SQLite |
| `withings_get_body` | Body composition (weight, fat%, muscle, bone, BP, SpO2) | Local cache (auto-syncs if stale) |
| `withings_get_sleep` | Sleep summaries, or detailed phase time-series with `detail=True` | Cache (summary) / live (detail) |
| `withings_get_activity` | Daily steps, distance, calories, active time | Local cache (auto-syncs if stale) |
| `withings_get_workouts` | Workout sessions with type, duration, HR | Local cache (auto-syncs if stale) |
| `withings_get_heart` | ECG recordings and AFib detection | Live API (always) |
| `withings_get_devices` | Connected devices with battery status | Live API (always) |
| `withings_trends` | Period averages, weekly/monthly/quarterly trends, comparisons | Local cache (auto-syncs if stale) |

The cache-backed query tools auto-sync when their data is stale, and accept `live=True` to bypass the cache and fetch straight from the Withings API. `withings_get_heart` and `withings_get_devices` are always live. `withings_get_sleep(detail=True)` returns minute-by-minute sleep phases (live, up to 7 days per request).

## Prerequisites

- Python 3.13+ (tested on 3.13 and 3.14, on Linux, macOS and Windows, in CI)
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Withings developer account and registered application

## Installation

```bash
pip install withings-mcp
```

Or run it without installing with `uvx withings-mcp`. For development from a clone:

```bash
git clone https://github.com/partymola/withings-mcp.git
cd withings-mcp
uv venv --python 3.13 .venv
uv pip install -e .
```

## Setup

### 1. Register a Withings app

1. Go to https://developer.withings.com/dashboard
2. Create a new application
3. Set the callback URL to `http://localhost:8585`
4. Note your Client ID and Client Secret

### 2. Authenticate

```bash
.venv/bin/withings-mcp auth
```

This opens your browser for Withings authorization. After approving, tokens are saved locally in `config/`.

### 3. Register with Claude Code

```bash
claude mcp add -s user withings -- /path/to/withings-mcp/.venv/bin/withings-mcp
```

### 4. First sync

In Claude Code, say: "Sync my Withings data"

This runs `withings_sync` to populate the local cache. Subsequent syncs only fetch new data.

If anything does not work - no data where you expect it, a sync that stops happening - run `withings-mcp doctor`. It reports the paths and credentials actually in use and what needs fixing, without making an API call.

You can also sync from the command line without an MCP client:

```bash
.venv/bin/withings-mcp sync                      # all data types, last 30 days
.venv/bin/withings-mcp sync --types body,sleep   # a subset
.venv/bin/withings-mcp sync --days 90            # deeper history on first sync
```

## CLI

```
withings-mcp              Start the MCP server (stdio transport)
withings-mcp auth         Interactive OAuth setup (opens the browser)
withings-mcp sync         Sync data to the local cache (--types, --days)
withings-mcp doctor       Check the setup and report what needs fixing
withings-mcp --version    Print the installed package version
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `WITHINGS_MCP_CONFIG_DIR` | `./config/` | Directory for credentials and tokens |
| `WITHINGS_MCP_DB_PATH` | `./withings.db` | SQLite database path |

## Example Prompts

- "Sync my Withings data"
- "Show my weight for the last 3 months"
- "How has my sleep changed this year?"
- "Compare my body composition this month vs last month"
- "What workouts did I do in March?"
- "What Withings devices do I have connected?"
- "Show my sleep trends quarterly"

## Development

```bash
# Install with dev dependencies
uv pip install -e . && uv pip install pytest

# Run tests (all use in-memory SQLite with fictional data)
.venv/bin/python -m pytest tests/ -v      # .venv\Scripts\python on Windows
```

## Security

- **Read-only**: No tools modify data on Withings servers
- **Local storage**: Health data stays in your local SQLite database
- **Token storage**: OAuth tokens stored in `config/` (gitignored; created 0600 on POSIX - Windows ignores the mode and governs access by ACLs)
- **Error messages**: Never contain health data values - only status codes
- **Pre-commit hook**: An optional hook (`scripts/check-no-data.sh`) blocks database files and credentials from commits - install it with the one-liner in [CONTRIBUTING.md](https://github.com/partymola/withings-mcp/blob/main/CONTRIBUTING.md)

## Contributing

See [CONTRIBUTING.md](https://github.com/partymola/withings-mcp/blob/main/CONTRIBUTING.md) for development setup, the test workflow, and the pre-commit hook. Changes are tracked in [CHANGELOG.md](https://github.com/partymola/withings-mcp/blob/main/CHANGELOG.md).

## License

GPL-3.0-or-later
