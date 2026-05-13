# Contributing to withings-mcp

Thanks for your interest in contributing. This is a community MCP server for the Withings Health API.

## Getting started

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Withings developer account and registered application ([dashboard](https://developer.withings.com/dashboard))

### Set up the dev environment

```bash
git clone https://github.com/partymola/withings-mcp
cd withings-mcp
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"
```

### Install the pre-commit hook

The repo ships with `scripts/check-no-data.sh`, which blocks commits that contain databases, tokens, or other secrets:

```bash
ln -sf ../../scripts/check-no-data.sh .git/hooks/pre-commit
```

Please install it before your first commit.

### Run the test suite

```bash
.venv/bin/python -m pytest tests/ -v
```

Tests are fully offline - no real API calls, no real tokens. Fixtures use fictional data and round numbers; never paste real health measurements into tests.

### Run lint checks

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
```

## Making changes

- **Open an issue first** for non-trivial changes (new tools, schema migrations, new measurement types, breaking changes). Small fixes (typos, bug fixes, docs) can go straight to a PR.
- Keep PRs small and focused.
- Add or update tests for any behaviour change.
- This server is intentionally read-only - PRs that add write tools will not be accepted.
- Run `ruff check src tests` and `ruff format --check src tests` before opening a PR.
- Run `pytest tests/ -v` before opening a PR.

## Pull requests

- Branch off `main`.
- Reference any related issue.
- Maintainer aims to reply within ~7 days. Feel free to bump if you don't hear back.

## Reporting issues

Helpful details to include:

- Python version (`python --version`)
- MCP client (Claude Desktop, Claude Code, other)
- Withings device model if relevant
- Steps to reproduce
- Relevant log output, with any tokens, user IDs, or measurement values redacted

## Security

Please do not open a public issue for credential, OAuth-flow, or token-leak issues. Use [GitHub's private vulnerability reporting](https://github.com/partymola/withings-mcp/security/advisories/new) instead.

## License

By contributing, you agree that your contributions are licensed under GPL-3.0-or-later, the project's license.
