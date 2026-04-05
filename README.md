# withings-mcp

MCP server for the [Withings Health API](https://developer.withings.com/) with OAuth, local SQLite cache, and trend analysis.

**What makes this different from other Withings MCP servers:**
- Local SQLite cache for fast offline queries and historical trend analysis
- Incremental sync - only fetches new data since last sync
- All 200+ Withings measurement types supported (body comp, sleep, activity, workouts, ECG)
- Automatic OAuth token refresh (access tokens: 3h, refresh tokens: 1 year)
- Zero dependencies beyond `mcp` (HTTP via stdlib)
- Python 3.13+

## Tools

| Tool | Description |
|------|-------------|
| `withings_sync` | Sync data from Withings API to local cache |
| `withings_get_body` | Body composition (weight, fat%, muscle, bone, BP, SpO2) |
| `withings_get_sleep` | Sleep summaries or detailed phase time-series |
| `withings_get_activity` | Daily steps, distance, calories, active time |
| `withings_get_workouts` | Workout sessions with type, duration, HR |
| `withings_get_heart` | ECG recordings and AFib detection |
| `withings_get_devices` | Connected devices with battery status |
| `withings_trends` | Period averages, weekly/monthly/quarterly trends, comparisons |

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Withings developer account and registered application

## Installation

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

# Run tests (74 tests, all use in-memory SQLite with fictional data)
.venv/bin/python -m pytest tests/ -v
```

## Security

- **Read-only**: No tools modify data on Withings servers
- **Local storage**: Health data stays in your local SQLite database
- **Token storage**: OAuth tokens stored in `config/` (gitignored, file permissions 0600)
- **Error messages**: Never contain health data values - only status codes
- **Pre-commit hook**: Rejects database files and credentials from commits

## License

GPL-3.0-or-later
