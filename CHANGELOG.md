# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-08-08

### Added

- `withings-mcp doctor` checks a setup and reports what needs fixing. It resolves and prints the config and database paths actually in use and where each came from, validates the credential files by shape, and reads the database read-only for schema drift, corruption and how current the cache is. It reports failures recorded in the sync log, which is otherwise the only trace that syncing stopped while queries carried on serving an ageing cache. It also flags what would break `withings-mcp auth` before you run it: the callback port already in use, and a host with no display, which gets the SSH tunnel to authorise over.

  The command makes no API call, so it costs no rate-limit quota and cannot disturb a token another host is using. It does not create the database or change its contents, so a wrong `WITHINGS_MCP_DB_PATH` still reports as missing rather than being silently created empty. No credential value appears in the output. Exit status is non-zero when a check fails; warnings alone exit zero.

### Fixed

- An authentication failure during sync is now recorded in `sync_log` with status `auth_error`. Rate-limit and API errors were already logged and this one was not, so it passed through the sync run and left nothing behind: queries carried on serving the cache with no record of why it had stopped growing. The resume cursor is unaffected, since it only advances on a successful sync.
- A failure to obtain an access token is now an authentication failure rather than an escaping error. Token refresh reported its failures with plain builtins, which the sync loop does not classify, so the commonest way syncing dies - a revoked or expired refresh token - left no log row at all and its database connection unclosed. This covers a refresh the server rejects, and a token file that is missing, unparseable, of the wrong shape, or carrying a non-numeric expiry. Failures are translated where the token is requested, so a genuine bug elsewhere still surfaces as one instead of being filed as an auth problem.
- A refresh that cannot reach Withings is reported as a network error, not an authentication one. Being unable to contact the server says nothing about whether the credentials are still valid, and the auth classification would have answered a dropped connection with "run `withings-mcp auth`" - re-authorising for no reason, and rewriting a token file another host may share.

### Packaging

- The container image is built on Python 3.14 instead of 3.13, and 3.14 joins the supported-version classifiers. `requires-python` is unchanged at `>=3.13`: the package still supports both, and only the published image moves. Installing from PyPI is unaffected - that uses whichever Python the user already has.
- Dependency updates are automated. Every dependency, the base image and the CI actions are pinned to exact versions, so nothing changes without a deliberate bump; Dependabot now proposes those bumps rather than leaving the pins to rot.

## [0.3.0] - 2026-08-03

### Changed

- Ported to the `mcp` 2.x server API. 2.0.0 renamed `mcp.server.fastmcp` to `mcp.server.mcpserver` and the `FastMCP` class to `MCPServer`, with no compatibility alias. The tool contract is unchanged: every tool keeps its name, description, and input and output schemas.
- Every dependency is pinned to an exact version instead of a lower bound: `mcp` 2.0.0, and for development `pytest` 9.1.1 and `ruff` 0.16.1.

### Fixed

- A fresh install no longer breaks on import. The `mcp` spec was `>=1.6.0` with no upper bound, so once 2.0.0 was published the resolver picked it and the server failed to start.

### Packaging

- The build toolchain is pinned alongside the dependencies: `setuptools` to an exact version, the `python:3.13-slim` base image by digest, and every GitHub Action to a full commit SHA rather than a moving major tag. A floating tag can change what a build produces with nobody deciding, which is the same failure the dependency pins address.

## [0.2.2] - 2026-07-11

### Packaging

- Listed in the official MCP registry (`io.github.partymola/withings-mcp`); the release workflow now publishes to the registry alongside PyPI.

## [0.2.1] - 2026-07-11

### Packaging

- Published to PyPI (`pip install withings-mcp` / `uvx withings-mcp`) via GitHub Actions Trusted Publishing.

## [0.2.0] - 2026-06-09

### Added

- `withings-mcp sync` CLI subcommand (with `--types` and `--days` flags) for parity with fitbit-mcp; previously a sync could only be triggered through an MCP client. `--types all` expands to `body,sleep,activity,workouts`, and each data type gets a summary line (status, record count, date range).
- `withings-mcp --version` prints the installed package version.
- Continuous integration now runs the test suite on Python 3.14 in addition to 3.13.

## [0.1.0] - 2026-04-26

### Added

- Initial release.
- OAuth 2.0 authentication against the Withings Health API with automatic token refresh (3-hour access tokens, 1-year refresh tokens).
- Local SQLite cache for body measurements, sleep summaries, activity, and workouts, with auto-sync on stale data.
- Incremental sync that only fetches new data since the last successful sync per data type.
- MCP tools: `withings_sync`, `withings_get_body`, `withings_get_sleep`, `withings_get_activity`, `withings_get_workouts`, `withings_get_heart`, `withings_get_devices`, `withings_trends`.
- Trend analysis with weekly / monthly / quarterly aggregation and period-over-period comparisons.
- ECG and AFib detection support via `withings_get_heart` (live, not cached).
- Pre-commit hook (`scripts/check-no-data.sh`) blocking commit of databases, tokens, and other secrets.

[Unreleased]: https://github.com/partymola/withings-mcp/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/partymola/withings-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/partymola/withings-mcp/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/partymola/withings-mcp/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/partymola/withings-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/partymola/withings-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/partymola/withings-mcp/releases/tag/v0.1.0
