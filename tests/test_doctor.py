"""Tests for the doctor subcommand.

The properties under test are the ones that make a diagnostic trustworthy
rather than merely informative: it must not create the database it is asked to
inspect, it must not print a credential, and it must survive the broken setups
it exists to describe.

The credential placeholders below are defined here rather than in
`tests/fixtures.py`: that module is the factory for health measurements, and
putting token-shaped strings in it would invite exactly the confusion its rule
exists to prevent. No measurement value appears in this file.
"""

import contextlib
import json
import os
import sqlite3
import sys
import time
from datetime import date, timedelta

import pytest

from withings_mcp import auth, config, db, doctor

FAKE_CLIENT_SECRET = "not-a-real-secret-0000"
FAKE_ACCESS_TOKEN = "fake-access-token-0000"
FAKE_REFRESH_TOKEN = "fake-refresh-token-0000"
FAKE_USER_ID = 12345678

_POSIX = sys.platform != "win32"

# Windows has no mode bits, and os.access there consults neither those nor ACLs,
# so the permission findings cannot fire at all. These tests assert a semantics
# the platform does not have, and skip rather than being made to pass.
skip_non_posix = pytest.mark.skipif(not _POSIX, reason="POSIX-only file semantics")

# A separate reason from the one above: the case cannot even be constructed.
skip_illegal_on_windows = pytest.mark.skipif(
    not _POSIX, reason="`?` is not a legal Windows filename"
)

# os.access ignores permission bits under CAP_DAC_OVERRIDE, so every permissions
# test below is meaningless as root. os.geteuid is POSIX-only and this is
# evaluated at import, so the platform test has to be the left operand or a
# Windows run dies here rather than skipping.
skip_as_root = pytest.mark.skipif(
    _POSIX and os.geteuid() == 0, reason="root bypasses permission bits"
)


def _write_client(config_dir, client_id="fake-client-id-0000"):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "withings_client.json").write_text(
        json.dumps({"client_id": client_id, "client_secret": FAKE_CLIENT_SECRET})
    )


def _write_tokens(config_dir, expires_at=None, **overrides):
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": FAKE_ACCESS_TOKEN,
        "refresh_token": FAKE_REFRESH_TOKEN,
        "userid": FAKE_USER_ID,
        "expires_at": time.time() + 3600 if expires_at is None else expires_at,
    }
    payload.update(overrides)
    (config_dir / "withings_tokens.json").write_text(json.dumps(payload))
    return config_dir / "withings_tokens.json"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Point config at a throwaway directory, credentials absent by default."""
    config_dir = tmp_path / "config"
    db_path = tmp_path / "withings.db"
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "WITHINGS_CLIENT_PATH", config_dir / "withings_client.json")
    monkeypatch.setattr(config, "WITHINGS_TOKENS_PATH", config_dir / "withings_tokens.json")
    return config_dir, db_path


def _named(findings, name):
    return [f for f in findings if f.name == name]


def _severities(findings, name):
    return {f.severity for f in _named(findings, name)}


class TestNeverCreatesTheDatabase:
    """Invariant 1 and 2 - the reason the command exists.

    A wrong path must still report as missing. Opening it through db.get_db()
    would manufacture an empty database there and destroy the evidence.
    """

    def test_run_against_absent_db_creates_nothing(self, setup):
        _, db_path = setup
        doctor.run_checks()
        assert not db_path.exists()

    def test_absent_db_is_reported_not_created(self, setup):
        _, db_path = setup
        findings = doctor.check_database()
        assert _severities(findings, "database") == {doctor.WARN}
        assert str(db_path) in findings[0].detail

    def test_full_run_leaves_no_new_files(self, setup):
        config_dir, _ = setup
        before = set(config_dir.parent.rglob("*"))
        doctor.run_checks()
        assert set(config_dir.parent.rglob("*")) == before


class TestResolvedPaths:
    """The first thing to establish when a working setup looks empty."""

    def test_env_override_is_named_as_the_source(self, setup, monkeypatch):
        monkeypatch.setenv("WITHINGS_MCP_DB_PATH", "/var/data/withings.sqlite")
        detail = _named(doctor.check_environment(), "database path")[0].detail
        assert "from $WITHINGS_MCP_DB_PATH" in detail

    def test_unset_env_reads_as_default(self, setup, monkeypatch):
        monkeypatch.delenv("WITHINGS_MCP_DB_PATH", raising=False)
        monkeypatch.delenv("WITHINGS_MCP_CONFIG_DIR", raising=False)
        details = [f.detail for f in doctor.check_environment()]
        assert all("(default)" in d for d in details)

    def test_empty_env_is_not_reported_as_the_default(self, setup, monkeypatch):
        """config.py reads the variable raw, so empty means the working directory.

        Calling that "default" sends the reader hunting the wrong fault.
        """
        monkeypatch.setenv("WITHINGS_MCP_DB_PATH", "")
        detail = _named(doctor.check_environment(), "database path")[0].detail
        assert "default" not in detail
        assert "empty" in detail


class TestStaysOffline:
    """Invariant: no code path can reach the token-writing half of the server.

    Asserted structurally rather than by behaviour. `auth.refresh_token` sends
    the stored refresh token and rewrites the file with what comes back, so a
    diagnostic that imported it could disturb whichever host owns the
    credentials - and no test watching a healthy setup would notice.
    """

    def _imported_names(self):
        """Every module name doctor.py imports, dotted forms included."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path(doctor.__file__).read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # `from . import x` has no module; `from .x import y` has both.
                for alias in node.names:
                    names.add(f"{node.module}.{alias.name}" if node.module else alias.name)
                if node.module:
                    names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
        return names

    def test_doctor_imports_neither_auth_nor_api(self):
        # Match on the last dotted segment so `import withings_mcp.auth` is
        # caught as well as `from . import auth`.
        segments = {seg for name in self._imported_names() for seg in name.split(".")}
        assert "auth" not in segments
        assert "api" not in segments

    def test_doctor_imports_nothing_else_from_the_package(self):
        """A subset assertion would pass for any new sibling import."""
        siblings = {
            seg
            for name in self._imported_names()
            for seg in name.split(".")
            if seg in {"api", "auth", "config", "db", "helpers", "mcp_instance", "tools"}
        }
        assert siblings == {"config", "db"}


class TestNoCredentialLeaks:
    """No credential value reaches the report."""

    def test_no_fragment_of_any_credential_appears(self, setup):
        """Whole-string absence would miss a truncated token or a user id.

        Both are things a well-meaning edit adds - "tokens present (abc12345)" -
        and both are named as identifiers by this repo's data-safety rules.
        """
        config_dir, _ = setup
        _write_client(config_dir)
        _write_tokens(config_dir)
        report = doctor.format_report(doctor.run_checks())

        for secret in (FAKE_CLIENT_SECRET, FAKE_ACCESS_TOKEN, FAKE_REFRESH_TOKEN):
            runs = {secret[i : i + 8] for i in range(len(secret) - 7)}
            assert not any(run in report for run in runs)
        assert str(FAKE_USER_ID) not in report

    def test_malformed_credential_file_content_is_not_echoed(self, setup):
        config_dir, _ = setup
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "withings_tokens.json").write_text(
            '{"access_token": "' + FAKE_ACCESS_TOKEN + '", oops'
        )
        report = doctor.format_report(doctor.check_credentials())
        assert FAKE_ACCESS_TOKEN not in report


class TestExitStatus:
    """Invariant 4."""

    def test_failures_exit_nonzero(self):
        findings = [doctor.Finding("x", doctor.FAIL, "broken")]
        assert doctor.exit_code(findings) == 1

    def test_warnings_alone_exit_zero(self):
        findings = [doctor.Finding("x", doctor.WARN, "odd"), doctor.Finding("y", doctor.OK, "fine")]
        assert doctor.exit_code(findings) == 0


class TestNoCheckCanEndTheRun:
    """Invariant 5 - a diagnostic that dies on a broken setup is worthless."""

    def test_raising_check_becomes_a_finding(self, setup, monkeypatch):
        def check_database():
            raise RuntimeError(FAKE_ACCESS_TOKEN)

        monkeypatch.setattr(doctor, "check_database", check_database)
        findings = doctor.run_checks()
        assert any(f.severity == doctor.FAIL and f.name == "check_database" for f in findings)
        # The run continues: later checks still contribute.
        assert _named(findings, "config path")

    def test_exception_message_is_not_reported(self, setup, monkeypatch):
        """The checks read token files; a message could carry one."""

        def check_credentials():
            raise RuntimeError(FAKE_REFRESH_TOKEN)

        monkeypatch.setattr(doctor, "check_credentials", check_credentials)
        report = doctor.format_report(doctor.run_checks())
        assert FAKE_REFRESH_TOKEN not in report
        assert "RuntimeError" in report


class TestAwkwardPaths:
    """Invariant 6 - the read-only URI must survive characters with URI meaning."""

    # `#` is legal on both platforms and is the character the escaping exists
    # for, so the Windows run still exercises `quote(os.fsencode(...))`.
    @pytest.mark.parametrize(
        "name",
        [
            "with#hash.db",
            pytest.param("with?query.db", marks=skip_illegal_on_windows),
            "plain.db",
        ],
    )
    def test_existing_db_is_read_without_being_touched(self, tmp_path, monkeypatch, name):
        """An unescaped `#` truncates the URI and reopens the file writable.

        The database must exist for this to mean anything: on a missing path
        check_database returns before opening anything, which would pass
        whatever the encoding did.
        """
        target = tmp_path / name
        db.get_db(target).close()
        before = (target.stat().st_mtime_ns, target.read_bytes())

        monkeypatch.setattr(config, "DB_PATH", target)
        findings = doctor.check_database()

        assert doctor.OK in _severities(findings, "schema")
        assert (target.stat().st_mtime_ns, target.read_bytes()) == before
        assert not list(tmp_path.glob("*-journal")) and not list(tmp_path.glob("*-wal"))

    def test_readonly_connection_refuses_writes(self, tmp_path):
        target = tmp_path / "with#hash.db"
        conn = db.get_db(target)
        conn.close()
        with doctor._open_db_readonly(target) as ro:
            with pytest.raises(sqlite3.OperationalError):
                ro.execute("CREATE TABLE nope (x INTEGER)")


class TestFreshness:
    """Invariant 7 - staleness is judged on the newest row across all types."""

    def _seed(self, db_path, when, table="activities"):
        """Insert directly: the property under test is MAX(date), not the writers."""
        conn = db.get_db(db_path)
        conn.execute(f"INSERT INTO {table} (date) VALUES (?)", (when,))
        conn.commit()
        conn.close()

    def test_recent_data_reads_ok(self, setup):
        _, db_path = setup
        self._seed(db_path, date.today().isoformat())
        assert _severities(doctor.check_database(), "cache") == {doctor.OK}

    def test_long_stale_data_warns(self, setup):
        _, db_path = setup
        self._seed(db_path, (date.today() - timedelta(days=30)).isoformat())
        assert _severities(doctor.check_database(), "cache") == {doctor.WARN}

    def test_one_fresh_type_keeps_the_cache_ok(self, setup):
        """Body and workouts lag by design when nothing was logged."""
        _, db_path = setup
        self._seed(db_path, date.today().isoformat())
        conn = db.get_db(db_path)
        conn.execute(
            "INSERT INTO workouts (date, startdate, enddate) VALUES (?, ?, ?)",
            ((date.today() - timedelta(days=90)).isoformat(), "2026-01-01", "2026-01-01"),
        )
        conn.commit()
        conn.close()
        assert _severities(doctor.check_database(), "cache") == {doctor.OK}

    def test_empty_database_warns(self, setup):
        _, db_path = setup
        db.get_db(db_path).close()
        assert _severities(doctor.check_database(), "cache") == {doctor.WARN}


class TestSchemaDrift:
    """Invariant 8 - report only what the next ordinary open will not repair."""

    def test_matching_schema_is_ok(self, setup):
        _, db_path = setup
        db.get_db(db_path).close()
        assert _severities(doctor.check_database(), "schema") == {doctor.OK}

    def test_missing_table_warns(self, setup):
        _, db_path = setup
        conn = db.get_db(db_path)
        conn.execute("DROP TABLE workouts")
        conn.commit()
        conn.close()
        assert _severities(doctor.check_database(), "schema") == {doctor.WARN}

    def test_missing_column_fails(self, setup):
        """withings-mcp has no migration step, so a missing column stays missing."""
        _, db_path = setup
        conn = db.get_db(db_path)
        conn.execute("DROP TABLE activities")
        conn.execute("CREATE TABLE activities (id INTEGER PRIMARY KEY, date TEXT NOT NULL)")
        conn.commit()
        conn.close()
        assert doctor.FAIL in _severities(doctor.check_database(), "schema")

    def test_corrupt_file_fails_without_raising(self, setup):
        _, db_path = setup
        db_path.write_bytes(b"this is not a database")
        assert doctor.FAIL in _severities(doctor.check_database(), "database")


class TestCredentials:
    """Invariants 9 and 10."""

    def test_missing_client_file_fails(self, setup):
        assert doctor.FAIL in _severities(doctor.check_credentials(), "app config")

    def test_client_file_without_secret_fails(self, setup):
        config_dir, _ = setup
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "withings_client.json").write_text(json.dumps({"client_id": "abc"}))
        assert doctor.FAIL in _severities(doctor.check_credentials(), "app config")

    def test_complete_credentials_read_ok(self, setup):
        config_dir, _ = setup
        _write_client(config_dir)
        _write_tokens(config_dir)
        findings = doctor.check_credentials()
        assert _severities(findings, "app config") == {doctor.OK}
        assert _severities(findings, "credentials") == {doctor.OK}

    def test_missing_refresh_token_fails(self, setup):
        config_dir, _ = setup
        _write_client(config_dir)
        _write_tokens(config_dir, refresh_token="")
        assert doctor.FAIL in _severities(doctor.check_credentials(), "credentials")

    def test_expired_access_token_is_normal(self, setup):
        """Withings access tokens last three hours; expiry between syncs is routine."""
        config_dir, _ = setup
        _write_client(config_dir)
        _write_tokens(config_dir, expires_at=time.time() - 60)
        assert _severities(doctor.check_credentials(), "access token") == {doctor.OK}

    def test_milliseconds_expiry_warns_without_raising(self, setup):
        config_dir, _ = setup
        _write_client(config_dir)
        _write_tokens(config_dir, expires_at=time.time() * 1000)
        assert _severities(doctor.check_credentials(), "access token") == {doctor.WARN}

    @skip_as_root
    @skip_non_posix
    def test_a_read_only_config_directory_fails(self, setup):
        """The save writes a new file in the directory and renames it over."""
        config_dir, _ = setup
        _write_client(config_dir)
        _write_tokens(config_dir)
        os.chmod(config_dir, 0o500)
        try:
            findings = doctor.check_credentials()
            assert doctor.FAIL in _severities(findings, "credentials")
            detail = " ".join(f.detail for f in _named(findings, "credentials"))
            assert "does not allow this user to create files in it" in detail
        finally:
            os.chmod(config_dir, 0o700)

    @skip_as_root
    @skip_non_posix
    def test_a_read_only_token_file_is_not_a_problem(self, setup):
        """os.replace needs nothing on the file, so this used to fail falsely."""
        config_dir, _ = setup
        _write_client(config_dir)
        path = _write_tokens(config_dir)
        os.chmod(path, 0o400)
        try:
            findings = doctor.check_credentials()
            assert doctor.FAIL not in _severities(findings, "credentials")
        finally:
            os.chmod(path, 0o600)


class TestSyncLog:
    def _log(self, db_path, rows):
        conn = db.get_db(db_path)
        for data_type, status, notes in rows:
            db.log_sync(conn, data_type, status, notes=notes)
        conn.commit()
        conn.close()

    def test_clean_log_is_ok(self, setup):
        _, db_path = setup
        self._log(db_path, [("activity", "ok", None)])
        assert _severities(doctor.check_sync_health(), "sync log") == {doctor.OK}

    def test_auth_failure_fails(self, setup):
        _, db_path = setup
        self._log(db_path, [("activity", "auth_error", None)])
        assert doctor.FAIL in _severities(doctor.check_sync_health(), "sync log")

    def test_rate_limit_warns(self, setup):
        _, db_path = setup
        self._log(db_path, [("body", "partial", "rate limited")])
        assert _severities(doctor.check_sync_health(), "sync log") == {doctor.WARN}

    def test_only_the_latest_attempt_per_type_counts(self, setup):
        """A fault fixed last week must stop being reported."""
        _, db_path = setup
        self._log(db_path, [("activity", "error", "boom"), ("activity", "ok", None)])
        assert _severities(doctor.check_sync_health(), "sync log") == {doctor.OK}

    def test_absent_database_reports_nothing(self, setup):
        assert doctor.check_sync_health() == []


class TestTheCommandItself:
    """run_doctor joins run_checks, format_report and exit_code.

    Tested end to end because each of those passing separately says nothing
    about the command a user runs: gutting run_doctor to `return 0` left the
    rest of this file green.
    """

    def test_a_broken_setup_prints_findings_and_exits_one(self, setup, capsys):
        assert doctor.run_doctor() == 1
        out = capsys.readouterr().out
        assert "app config" in out
        assert "need fixing" in out

    def test_remediation_is_printed_for_a_failure(self, setup, capsys):
        """The fix line is half of what the command is for."""
        doctor.run_doctor()
        out = capsys.readouterr().out
        assert "->" in out
        assert "withings-mcp auth" in out

    def test_a_healthy_setup_prints_no_remediation(self, setup, capsys, monkeypatch):
        monkeypatch.setattr(doctor, "run_checks", lambda: [doctor.Finding("x", doctor.OK, "fine")])
        assert doctor.run_doctor() == 0
        out = capsys.readouterr().out
        assert "All checks passed." in out
        assert "->" not in out


class TestEveryCheckRuns:
    def test_a_raising_check_does_not_stop_the_ones_after_it(self, setup, monkeypatch):
        """Asserting on an EARLIER check cannot observe continuation."""
        sentinel = doctor.Finding("sentinel", doctor.OK, "ran")

        def check_database():
            raise RuntimeError(FAKE_ACCESS_TOKEN)

        monkeypatch.setattr(doctor, "check_database", check_database)
        monkeypatch.setattr(doctor, "check_sync_health", lambda: [sentinel])

        findings = doctor.run_checks()
        assert sentinel in findings
        assert any(f.name == "check_database" and f.severity == doctor.FAIL for f in findings)

    def test_no_check_is_dropped_from_the_run(self, setup, monkeypatch):
        """Deleting a check from the tuple otherwise passes silently."""
        expected = (
            "check_environment",
            "check_credentials",
            "check_database",
            "check_sync_health",
            "check_auth_prerequisites",
        )
        for name in expected:
            monkeypatch.setattr(
                doctor, name, (lambda n: lambda: [doctor.Finding(n, doctor.OK, "")])(name)
            )
        assert {f.name for f in doctor.run_checks()} == set(expected)


class TestUnreadableOrLockedDatabase:
    """A database that cannot be opened is not a corrupt one.

    Both raise from sqlite3, and the corruption remedy is "delete it and
    re-sync" - catastrophic advice for a healthy database owned by root.
    """

    @skip_as_root
    @skip_non_posix
    def test_unreadable_database_is_not_called_corrupt(self, setup):
        _, db_path = setup
        db.get_db(db_path).close()
        os.chmod(db_path, 0o000)
        try:
            findings = doctor.check_database()
            detail = " ".join(f.detail for f in _named(findings, "database"))
            fixes = " ".join(f.fix or "" for f in _named(findings, "database"))
            assert doctor.FAIL in _severities(findings, "database")
            assert "not readable" in detail
            assert "delete" not in fixes.replace("do not delete", "")
        finally:
            os.chmod(db_path, 0o600)

    @skip_as_root
    @skip_non_posix
    def test_read_only_directory_is_reported_as_unwritable(self, setup):
        """SQLite writes a journal beside the database, so the directory counts."""
        _, db_path = setup
        db.get_db(db_path).close()
        os.chmod(db_path.parent, 0o500)
        try:
            findings = doctor.check_database()
            assert doctor.WARN in _severities(findings, "database")
            assert "sync will fail" in " ".join(f.detail for f in _named(findings, "database"))
        finally:
            os.chmod(db_path.parent, 0o700)

    @skip_non_posix
    def test_a_fifo_does_not_hang_the_run(self, setup):
        """sqlite3.connect on a named pipe blocks forever."""
        _, db_path = setup
        os.mkfifo(db_path)
        try:
            findings = doctor.check_database()
            assert doctor.FAIL in _severities(findings, "database")
            assert "not a regular file" in " ".join(f.detail for f in _named(findings, "database"))
        finally:
            db_path.unlink()


class TestFreshnessEdges:
    def _seed(self, db_path, when):
        conn = db.get_db(db_path)
        conn.execute("INSERT INTO activities (date) VALUES (?)", (when,))
        conn.commit()
        conn.close()

    def test_at_the_threshold_is_still_ok(self, setup):
        _, db_path = setup
        self._seed(db_path, (date.today() - timedelta(days=3)).isoformat())
        assert _severities(doctor.check_database(), "cache") == {doctor.OK}

    def test_one_day_past_the_threshold_warns(self, setup):
        _, db_path = setup
        self._seed(db_path, (date.today() - timedelta(days=4)).isoformat())
        assert _severities(doctor.check_database(), "cache") == {doctor.WARN}

    def test_a_malformed_date_does_not_read_as_fresh(self, setup):
        """Dates come from the API verbatim, and a junk value sorts high."""
        _, db_path = setup
        self._seed(db_path, "not-a-date")
        findings = doctor.check_database()
        assert _severities(findings, "cache") == {doctor.WARN}
        assert "not a calendar date" in " ".join(f.detail for f in _named(findings, "cache"))


class TestAuthPrerequisites:
    def test_headless_host_is_told_how_to_tunnel(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        findings = doctor.check_auth_prerequisites()
        assert _severities(findings, "auth browser") == {doctor.WARN}
        assert "ssh -L 8585:localhost:8585" in _named(findings, "auth browser")[0].fix

    def test_a_display_means_no_warning(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "linux")
        monkeypatch.setenv("DISPLAY", ":0")
        assert not _named(doctor.check_auth_prerequisites(), "auth browser")

    def test_macos_is_not_reported_as_headless(self, monkeypatch):
        """Neither variable is set on macOS, yet the browser opens fine."""
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert not _named(doctor.check_auth_prerequisites(), "auth browser")

    def test_a_bound_callback_port_is_reported(self, monkeypatch):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                held.bind(("localhost", config.WITHINGS_CALLBACK_PORT))
            except OSError:
                pytest.skip("callback port already in use by something else")
            held.listen(1)
            assert _severities(doctor.check_auth_prerequisites(), "auth callback") == {doctor.WARN}

    def test_a_free_callback_port_is_silent(self):
        import socket

        # Same guard as the sibling above: the real port may legitimately be
        # taken on whichever machine this runs on, and six runners is six
        # chances of it.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("localhost", config.WITHINGS_CALLBACK_PORT))
            except OSError:
                pytest.skip("callback port already in use by something else")

        assert not _named(doctor.check_auth_prerequisites(), "auth callback")


class TestSyncLogAcrossTypes:
    def _log(self, db_path, rows):
        conn = db.get_db(db_path)
        for data_type, status in rows:
            db.log_sync(conn, data_type, status)
        conn.commit()
        conn.close()

    def test_a_stale_failure_is_reported_when_another_type_succeeded_later(self, setup):
        """One data type cannot mask another's failure.

        With a single type the per-type maximum and the global maximum are the
        same row, so a correlated subquery and an uncorrelated one agree.
        """
        _, db_path = setup
        self._log(db_path, [("body", "error"), ("activity", "ok")])
        findings = doctor.check_sync_health()
        assert _severities(findings, "sync log") != {doctor.OK}
        assert "body" in _named(findings, "sync log")[0].detail


class TestIntegrityCheck:
    """A database that opens but reports damage is its own case.

    Not tested with a real corrupted file on purpose: whether SQLite answers a
    damaged page by returning a row from PRAGMA integrity_check or by raising
    depends on the library build, so a manufactured corruption pins the
    platform rather than this code. Both routes end in FAIL - the other one is
    covered by test_a_file_that_is_not_a_database_still_says_so.
    """

    def test_a_non_ok_integrity_result_is_reported_as_such(self, setup, monkeypatch):
        _, db_path = setup
        db.get_db(db_path).close()
        real_open = doctor._open_db_readonly

        class _DamagedReport:
            """Passes queries through, but answers integrity_check with damage."""

            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if "integrity_check" in sql:
                    return _OneRow(("row 42 missing from index idx_workout_date",))
                return self._conn.execute(sql, *args)

        class _OneRow:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        @contextlib.contextmanager
        def damaged(path):
            with real_open(path) as conn:
                yield _DamagedReport(conn)

        monkeypatch.setattr(doctor, "_open_db_readonly", damaged)
        findings = doctor.check_database()

        assert doctor.FAIL in _severities(findings, "database")
        assert "integrity check" in " ".join(f.detail for f in _named(findings, "database"))


class TestConfigPathSource:
    def test_config_path_reads_its_own_variable(self, setup, monkeypatch):
        """A copy-paste of the DB variable here would go unnoticed."""
        monkeypatch.setenv("WITHINGS_MCP_CONFIG_DIR", "/opt/withings/cfg")
        monkeypatch.delenv("WITHINGS_MCP_DB_PATH", raising=False)
        findings = doctor.check_environment()
        assert "from $WITHINGS_MCP_CONFIG_DIR" in _named(findings, "config path")[0].detail
        assert "(default)" in _named(findings, "database path")[0].detail


class TestOpenFailureIsNotCalledCorruption:
    """A database SQLite cannot open is not a database it found broken.

    Reached when a sync holds the write lock, or the file is unreadable in a
    way the earlier permission check cannot see. Both used to share the
    corruption branch, whose remedy is to delete the cache.
    """

    def test_a_database_that_will_not_open_keeps_its_contents_unaccused(self, setup, monkeypatch):
        _, db_path = setup
        db.get_db(db_path).close()

        def refuse(path):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(doctor, "_open_db_readonly", refuse)
        findings = doctor.check_database()

        assert doctor.FAIL in _severities(findings, "database")
        detail = " ".join(f.detail for f in _named(findings, "database"))
        fixes = " ".join(f.fix or "" for f in _named(findings, "database"))
        assert "locked" in detail
        assert "not implicated" in detail
        assert "delete" not in fixes
        assert "backup" not in fixes

    def test_a_file_that_is_not_a_database_still_says_so(self, setup):
        _, db_path = setup
        db_path.write_bytes(b"this is not a database")
        findings = doctor.check_database()
        assert doctor.FAIL in _severities(findings, "database")
        assert "not a readable SQLite database" in " ".join(
            f.detail for f in _named(findings, "database")
        )


@skip_as_root
@skip_non_posix
@pytest.mark.parametrize(
    "dir_mode,file_mode",
    [
        (0o700, 0o600),
        (0o700, 0o400),
        (0o700, 0o000),
        (0o500, 0o600),
        (0o555, 0o600),
        (0o600, 0o600),
    ],
    ids=[
        "both-writable",
        "read-only-file",
        "unreadable-file",
        "read-only-dir",
        "r-x-dir",
        # Writable but not searchable: the only mode that isolates X_OK, since
        # 0o500 and 0o555 are the same from the owner's seat.
        "no-exec-dir",
    ],
)
def test_the_diagnostic_agrees_with_what_saving_actually_does(tmp_path, dir_mode, file_mode):
    """Run the real save, then ask doctor, and require them to agree.

    A diagnostic written from a reading of the save path drifts the moment
    the save path changes, and reads as correct throughout. Comparing the two
    by execution is the only form that cannot.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    token_path = config_dir / "withings_tokens.json"
    token_path.write_text("{}")
    os.chmod(token_path, file_mode)
    os.chmod(config_dir, dir_mode)

    try:
        try:
            auth._save_json(token_path, {"refresh_token": "fictional"})
            save_failed = False
        except OSError:
            save_failed = True

        findings = doctor._check_token_file_writability(token_path)
        reported = bool(findings)
    finally:
        os.chmod(config_dir, 0o700)

    assert reported == save_failed, (
        f"dir={oct(dir_mode)} file={oct(file_mode)}: "
        f"save {'failed' if save_failed else 'succeeded'} but doctor "
        f"{'reported a problem' if reported else 'reported none'}"
    )

    # The check fires when either bit is missing, so naming only one sends
    # the user to fix a permission the directory already has. Asserting on
    # the boolean alone let that wording stand through the no-exec-dir case.
    if findings:
        detail = findings[0].detail
        assert "write and execute" in detail, detail
        # Both bits AND what they are on: blaming the file instead reads
        # plausibly and is wrong in the same way the original was.
        assert "on the directory" in detail, detail
        assert "is not writable by this user" not in detail, detail
