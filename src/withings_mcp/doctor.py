"""Preflight diagnostic: report what is wrong with a setup, and how to fix it.

Every check reads only, and makes no API call. Two consequences are load-bearing
rather than incidental:

- Nothing is opened through `db.get_db()`, which creates the database if it is
  absent. Doing so would manufacture an empty database at the resolved path and
  destroy the evidence for the misconfigured-path and stale-cache checks - the
  two this command exists for. The database is opened read-only throughout, so
  on a database in WAL mode SQLite may still create the `-wal` and `-shm` side
  files; the cache itself is never altered.
- No credential value is ever placed in a finding. The report is meant to be
  read out or pasted into a bug report, so files are described by shape -
  present, parseable, which fields are set - and never by content. It does name
  the resolved paths, which carry the user's home directory, because a wrong
  path is the fault this command most often has to show.

Of this package only `config` and `db` are imported: nothing here calls
`auth.refresh_token`, which sends the stored refresh token and rewrites the
token file with whatever comes back. A diagnostic that did that could disturb
whichever host legitimately owns the credentials.
"""

import json
import os
import socket
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from . import config, db

OK = "ok"
WARN = "warn"
FAIL = "fail"

_SEVERITY_MARK = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}

# A cache older than this has stopped updating rather than merely lagging. The
# server syncs at most once a day per type, so three days without a new row is
# past what a missed run or a day away from the scales explains.
_CACHE_STALE_AFTER = timedelta(days=3)


@dataclass
class Finding:
    name: str
    severity: str
    detail: str
    fix: str | None = None


def _open_db_readonly(path: Path) -> closing:
    """Open the database with no possibility of creating or altering it.

    The path is percent-encoded because this is a URI, not a filename: an
    unescaped `#` would start a fragment, so `?mode=ro` would land inside it and
    be silently ignored - handing back a writable connection to a truncated
    path. `quote` leaves `/` alone and escapes `#` and `?`.

    Encoding via `os.fsencode` rather than passing the str: a filename holding
    non-UTF-8 bytes arrives as surrogate escapes, which `quote` refuses. Going
    through bytes round-trips those unchanged. `Path.as_uri()` is not usable at
    all here - it rejects relative paths, and WITHINGS_MCP_DB_PATH may be one.
    """
    conn = sqlite3.connect(f"file:{quote(os.fsencode(path))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def _reference_schema() -> dict[str, set[str]]:
    """Tables and columns this version expects, read from db.SCHEMA itself.

    Built by running the real schema into an in-memory database rather than
    restating a column list, so it cannot drift from the code.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(db.SCHEMA)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: {r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")} for t in tables}
    finally:
        conn.close()


def _describe_path_source(env_var: str) -> str:
    """Say where a path came from, matching how config.py actually resolves it.

    config.py reads the variable with a plain `os.environ.get`, so an empty
    value is still an override - it resolves to `Path("")`, the current
    directory. Testing truthiness here would report that as the default and
    send the reader looking for the wrong fault.
    """
    if env_var not in os.environ:
        return "default"
    if not os.environ[env_var]:
        return f"${env_var} is set but empty, so the path resolves to the working directory"
    return f"from ${env_var}"


def _timestamp_or_none(value) -> datetime | None:
    """Convert an epoch-seconds value, or None if it is not one.

    Out-of-range values return None rather than raising, and they are not
    exotic: a token written with milliseconds instead of seconds lands tens of
    thousands of years out. A diagnostic must report that, not die on it.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value)
    except (ValueError, OverflowError, OSError):
        return None


def check_environment() -> list[Finding]:
    return [
        Finding(
            "config path",
            OK,
            f"{config.CONFIG_DIR} ({_describe_path_source('WITHINGS_MCP_CONFIG_DIR')})",
        ),
        Finding(
            "database path",
            OK,
            f"{config.DB_PATH} ({_describe_path_source('WITHINGS_MCP_DB_PATH')})",
        ),
    ]


def _check_client_file() -> list[Finding]:
    path = config.WITHINGS_CLIENT_PATH
    if not path.exists():
        return [
            Finding(
                "app config",
                FAIL,
                f"No withings_client.json at {path}.",
                "Run `withings-mcp auth` to register your app and authorise access. "
                "If you set it up with WITHINGS_MCP_CONFIG_DIR set - a systemd unit "
                "does this - export the same value before running this command.",
            )
        ]

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return [
            Finding(
                "app config",
                FAIL,
                "withings_client.json is unreadable or malformed.",
                "Re-run `withings-mcp auth`.",
            )
        ]

    if not isinstance(data, dict):
        return [
            Finding(
                "app config",
                FAIL,
                "withings_client.json is malformed: expected an object.",
                "Re-run `withings-mcp auth`.",
            )
        ]

    # Withings is a confidential client: the secret is sent on every refresh, so
    # a file carrying only the id authorises once and then stops working.
    missing = [k for k in ("client_id", "client_secret") if not data.get(k)]
    if missing:
        return [
            Finding(
                "app config",
                FAIL,
                f"withings_client.json is missing: {', '.join(missing)}.",
                "Re-run `withings-mcp auth` and enter both values.",
            )
        ]

    return [Finding("app config", OK, "client id and secret present")]


def _check_token_file() -> list[Finding]:
    path = config.WITHINGS_TOKENS_PATH
    if not path.exists():
        return [
            Finding(
                "credentials",
                FAIL,
                f"No withings_tokens.json at {path}.",
                "Run `withings-mcp auth`.",
            )
        ]

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return [
            Finding(
                "credentials",
                FAIL,
                "withings_tokens.json is unreadable or malformed.",
                "Re-run `withings-mcp auth`.",
            )
        ]

    if not isinstance(data, dict):
        return [
            Finding(
                "credentials",
                FAIL,
                "withings_tokens.json is malformed: expected an object.",
                "Re-run `withings-mcp auth`.",
            )
        ]

    findings = []
    missing = [k for k in ("access_token", "refresh_token") if not data.get(k)]
    if missing:
        findings.append(
            Finding(
                "credentials",
                FAIL,
                f"withings_tokens.json is missing: {', '.join(missing)}.",
                "Re-run `withings-mcp auth`.",
            )
        )
    else:
        findings.append(Finding("credentials", OK, "access and refresh tokens present"))

    expires_at = data.get("expires_at")
    when = _timestamp_or_none(expires_at)
    if when is None:
        findings.append(
            Finding(
                "access token",
                WARN,
                "expires_at is missing, not a number, or not a usable timestamp, so "
                "expiry cannot be checked. A value in milliseconds rather than "
                "seconds does this.",
                "Re-run `withings-mcp auth`.",
            )
        )
    elif expires_at < time.time():
        findings.append(
            Finding(
                "access token",
                OK,
                f"expired at {when:%Y-%m-%d %H:%M} - normal between syncs; Withings "
                "access tokens last three hours and are refreshed on the next call.",
            )
        )
    else:
        findings.append(Finding("access token", OK, f"valid until {when:%Y-%m-%d %H:%M}"))

    findings.extend(_check_token_file_writability(path))
    return findings


def _check_token_file_writability(path: Path) -> list[Finding]:
    """An unwritable token file cannot store the result of a refresh.

    A refresh may return a replacement refresh token - auth.py stores whichever
    it gets and falls back to the existing one when the response carries none.
    Either way the new access token has nowhere to go, so every later call
    refreshes again, and a replacement that did arrive is lost while the token
    it replaced may already be spent.

    Two things are required, and write permission alone is not enough: auth.py
    follows the write with `os.chmod`, which needs ownership. A token file owned
    by another user but group-writable passes `os.access` and then fails on the
    chmod, after the write has already happened.

    The directory is deliberately not checked. The rewrite truncates an existing
    file, which needs no write permission on the directory, so a read-only
    config directory holding a writable token file works and must not be
    reported.
    """
    writable = os.access(path, os.W_OK)
    try:
        owned = path.stat().st_uid == os.geteuid()
    except OSError:
        owned = True  # Cannot tell; do not invent a fault.

    if writable and owned:
        return []

    detail = (
        "withings_tokens.json is not writable by this user"
        if not writable
        else "withings_tokens.json is owned by another user, and saving a refreshed "
        "token also sets its permissions, which requires ownership"
    )
    return [
        Finding(
            "credentials",
            FAIL,
            f"{detail}, so a refresh cannot be stored. Every call will refresh again, "
            "and if the refresh returns a replacement refresh token it is lost while "
            "the one it replaced may already be spent. Until this process restarts it "
            "keeps using the token it holds in memory, so the failure may not show "
            "immediately.",
            "Fix ownership/permissions before the next sync runs.",
        )
    ]


def check_credentials() -> list[Finding]:
    return _check_client_file() + _check_token_file()


def check_database() -> list[Finding]:
    path = config.DB_PATH
    if not path.exists():
        return [
            Finding(
                "database",
                WARN,
                f"No database at {path}. It is created on the first sync.",
                "Run `withings-mcp sync` to populate it, or check WITHINGS_MCP_DB_PATH "
                "points at the database you expect.",
            )
        ]

    # is_file() rather than not is_dir(): a FIFO passes the directory test and
    # then blocks the open forever, and a diagnostic that hangs is worse than
    # one that reports.
    if not path.is_file():
        return [
            Finding(
                "database",
                FAIL,
                f"{path} is not a regular file, so it cannot be a database.",
            )
        ]

    findings = []
    if not os.access(path, os.R_OK):
        # Reported before opening, because the open would fail with the same
        # error SQLite raises for a corrupt file - and that path recommends
        # deleting the database, which would be catastrophic advice here.
        return findings + [
            Finding(
                "database",
                FAIL,
                "Database exists but is not readable by this user. Nothing can be "
                "queried, and this says nothing about whether its contents are sound.",
                "Fix ownership/permissions - do not delete it.",
            )
        ]

    # SQLite writes its rollback journal beside the database, so a writable file
    # in a read-only directory still fails every insert. Checking only the file
    # would miss precisely the case this finding describes.
    if not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK):
        findings.append(
            Finding(
                "database",
                WARN,
                "Database cannot be written by this user; queries will work but every "
                "sync will fail. SQLite needs to write both the database file and a "
                "journal in its directory.",
                "Fix ownership/permissions on the file and its directory, or run syncs "
                "as the owning user.",
            )
        )

    try:
        with _open_db_readonly(path) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return findings + [
                    Finding(
                        "database",
                        FAIL,
                        "Database fails its integrity check.",
                        "Restore from backup, or delete it and re-sync.",
                    )
                ]
            findings.extend(_check_schema(conn))
            findings.extend(_check_freshness(conn))
    except sqlite3.OperationalError:
        # Split from DatabaseError below on purpose. SQLite raises this when it
        # cannot open or read the file - locked by a running sync, or blocked by
        # permissions - which says nothing about the contents. Sharing the
        # corruption branch would tell a user with a healthy database to delete
        # it.
        return findings + [
            Finding(
                "database",
                FAIL,
                f"{path} could not be opened. It may be locked by a sync running now, "
                "or unreadable by this user. Its contents are not implicated.",
                "Retry once any sync has finished, and check ownership/permissions.",
            )
        ]
    except sqlite3.DatabaseError:
        return findings + [
            Finding(
                "database",
                FAIL,
                f"{path} is not a readable SQLite database.",
                "Restore from backup, or delete it and re-sync.",
            )
        ]

    return findings


def _check_schema(conn: sqlite3.Connection) -> list[Finding]:
    """Separate drift the next ordinary open repairs from drift it does not.

    A whole missing table is recreated by get_db()'s `CREATE TABLE IF NOT
    EXISTS`, so it needs no remedy - but the history it held is gone, which is
    worth saying. A missing column has no such recovery: this server has no
    migration step, so the column stays missing and its inserts keep failing.
    """
    expected = _reference_schema()
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    absent, problems = [], []
    for table, columns in sorted(expected.items()):
        if table not in present:
            absent.append(table)
            continue
        actual = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
        if columns - actual:
            problems.append(f"{table}.{'/'.join(sorted(columns - actual))}")

    findings = []
    if problems:
        findings.append(
            Finding(
                "schema",
                FAIL,
                f"Database predates this version and has no migration for: "
                f"{', '.join(problems)}. Syncing these types will fail.",
                "Back the database up, then re-create and re-import it.",
            )
        )
    if absent:
        findings.append(
            Finding(
                "schema",
                WARN,
                f"{len(absent)} table(s) absent: {', '.join(absent)}. They are recreated "
                "empty on the next open, so any history in them is lost.",
                "Re-sync to refill them.",
            )
        )
    if not findings:
        findings.append(Finding("schema", OK, "matches this version"))
    return findings


def _cached_tables() -> list[str]:
    """Every table this version caches measurements in, read from db.SCHEMA.

    Derived rather than listed so that a table added later is covered without
    anyone remembering to come back here - the same reason _reference_schema
    builds itself from the schema. A cache table is one keyed by date, which
    also excludes sync_log: it records attempts, so it would stay current while
    every sync failed and mask the staleness worth reporting.
    """
    return sorted(t for t, columns in _reference_schema().items() if "date" in columns)


def _check_freshness(conn: sqlite3.Connection) -> list[Finding]:
    newest = {}
    for table in _cached_tables():
        try:
            row = conn.execute(f"SELECT MAX(date) FROM '{table}'").fetchone()
        except sqlite3.DatabaseError:
            continue
        if row and row[0]:
            newest[table] = row[0]

    if not newest:
        return [
            Finding(
                "cache",
                WARN,
                "Database has no data in any table.",
                "Run `withings-mcp sync --days 30`.",
            )
        ]

    latest = max(newest.values())
    summary = f"{len(newest)} data type(s) cached, newest date {latest}"

    # Judged on the newest row across all types, not per type. Body measurements
    # and workouts only appear when the user steps on the scales or records a
    # session, so flagging those individually would cry wolf on a healthy cache.
    # Whole days between two dates. Measuring from `datetime.now()` instead
    # would fold in the time of day, so the same cache read stale at 23:00 and
    # fresh at 01:00.
    try:
        age = date.today() - datetime.strptime(latest, "%Y-%m-%d").date()
    except ValueError:
        # Dates are stored as the API returned them, so a malformed one sorts
        # high and would otherwise mark any cache current, however old.
        return [
            Finding(
                "cache",
                WARN,
                f"{summary} - that date is not a calendar date, so freshness cannot be judged.",
                "Re-sync to overwrite it.",
            )
        ]

    if age <= _CACHE_STALE_AFTER:
        return [Finding("cache", OK, summary)]
    return [
        Finding(
            "cache",
            WARN,
            f"{summary} - {age.days} days old, so syncing has stopped.",
            "Check the sync log below.",
        )
    ]


def check_sync_health() -> list[Finding]:
    """Read the sync log for failures that never surfaced anywhere else.

    A sync run reports its own failures, but nothing re-reports them afterwards:
    a token that died three weeks ago leaves queries quietly serving an ageing
    cache. The log is the only in-band record that this is happening.
    """
    path = config.DB_PATH
    if not path.exists():
        return []

    try:
        with _open_db_readonly(path) as conn:
            # The latest attempt per type is what says whether it is broken NOW.
            # Counting failures over a window would keep reporting a fault fixed
            # weeks ago, until enough rows aged out.
            rows = conn.execute(
                "SELECT data_type, status, notes, synced_at FROM sync_log s "
                "WHERE synced_at = (SELECT MAX(synced_at) FROM sync_log "
                "                   WHERE data_type = s.data_type)"
            ).fetchall()
    except sqlite3.DatabaseError:
        return []

    if not rows:
        return []

    failing = [r for r in rows if r["status"] in ("auth_error", "error")]
    if not failing:
        if any(r["status"] == "partial" and (r["notes"] or "") == "rate limited" for r in rows):
            return [
                Finding(
                    "sync log",
                    WARN,
                    "The last sync was cut short by Withings' rate limit.",
                    "It resumes on the next run; use --types to sync fewer types at once.",
                )
            ]
        return [Finding("sync log", OK, "no failures recorded")]

    types = ", ".join(sorted(r["data_type"] for r in failing))
    auth = [r for r in failing if r["status"] == "auth_error"]
    return [
        Finding(
            "sync log",
            FAIL if auth else WARN,
            f"Last sync failed for: {types} ({'auth' if auth else 'error'}, most recent "
            f"{max(r['synced_at'] for r in failing)}).",
            "Run `withings-mcp auth` if this is an auth failure, then `withings-mcp sync "
            "--days N` to backfill what was missed.",
        )
    ]


def check_auth_prerequisites() -> list[Finding]:
    """Things that break `withings-mcp auth` itself, checked before it is needed."""
    findings = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("localhost", config.WITHINGS_CALLBACK_PORT))
        except OSError:
            findings.append(
                Finding(
                    "auth callback",
                    WARN,
                    f"Port {config.WITHINGS_CALLBACK_PORT} is in use, so `withings-mcp auth` "
                    "cannot receive the OAuth callback.",
                    "Free the port before authorising; it must match the callback URL "
                    "registered with Withings and so cannot be changed.",
                )
            )

    # DISPLAY/WAYLAND_DISPLAY are X11/Wayland only; macOS and Windows open a
    # browser without either, so testing them alone flags every mac as headless.
    headless = sys.platform not in ("darwin", "win32") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )
    if headless:
        findings.append(
            Finding(
                "auth browser",
                WARN,
                "No display detected, so `withings-mcp auth` cannot open a browser and the "
                "callback must still reach this host.",
                f"Authorise over a tunnel: ssh -L {config.WITHINGS_CALLBACK_PORT}:"
                f"localhost:{config.WITHINGS_CALLBACK_PORT} <user>@<host>",
            )
        )

    return findings


def run_checks() -> list[Finding]:
    findings = []
    for check in (
        check_environment,
        check_credentials,
        check_database,
        check_sync_health,
        check_auth_prerequisites,
    ):
        try:
            findings.extend(check())
        except Exception as e:
            # A diagnostic that dies on a broken setup is worthless precisely
            # when it is needed, so no single check may end the run. Only the
            # exception type is reported: these checks read files containing
            # tokens, and a message built from file content could carry one.
            findings.append(
                Finding(
                    check.__name__,
                    FAIL,
                    f"This check could not run ({type(e).__name__}).",
                    "Please report this as a bug.",
                )
            )
    return findings


def format_report(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        lines.append(f"[{_SEVERITY_MARK[f.severity]}] {f.name}: {f.detail}")
        if f.fix and f.severity != OK:
            lines.append(f"         -> {f.fix}")

    failures = sum(1 for f in findings if f.severity == FAIL)
    warnings = sum(1 for f in findings if f.severity == WARN)
    lines.append("")
    if failures:
        lines.append(f"{failures} problem(s) need fixing, {warnings} warning(s).")
    elif warnings:
        lines.append(f"No blocking problems, {warnings} warning(s).")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)


def exit_code(findings: list[Finding]) -> int:
    return 1 if any(f.severity == FAIL for f in findings) else 0


def run_doctor() -> int:
    findings = run_checks()
    print(format_report(findings))
    return exit_code(findings)
