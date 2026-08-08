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

import json
import os
import sqlite3
import time
from datetime import date, timedelta

import pytest

from withings_mcp import config, db, doctor

FAKE_CLIENT_SECRET = "not-a-real-secret-0000"
FAKE_ACCESS_TOKEN = "fake-access-token-0000"
FAKE_REFRESH_TOKEN = "fake-refresh-token-0000"


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
        "userid": 12345678,
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


class TestStaysOffline:
    """Invariant: no code path can reach the token-rotating half of the server.

    Asserted structurally rather than by behaviour. `auth.refresh_token` spends
    the stored refresh token and writes a new one, so a diagnostic that imported
    it could break whichever host owns the credentials - and no test that only
    watches a healthy setup would notice.
    """

    def test_doctor_imports_neither_auth_nor_api(self):
        import ast
        from pathlib import Path

        tree = ast.parse(Path(doctor.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(a.name for a in node.names)
                if node.module:
                    imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)

        assert "auth" not in imported
        assert "api" not in imported
        assert {"config", "db"} <= imported


class TestNoCredentialLeaks:
    """Invariant 3 - the report is meant to be pasted into a bug report."""

    def test_secrets_never_appear_in_any_finding(self, setup):
        config_dir, db_path = setup
        _write_client(config_dir)
        _write_tokens(config_dir)
        report = doctor.format_report(doctor.run_checks())
        for secret in (FAKE_CLIENT_SECRET, FAKE_ACCESS_TOKEN, FAKE_REFRESH_TOKEN):
            assert secret not in report

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

    @pytest.mark.parametrize("name", ["with#hash.db", "with?query.db", "plain.db"])
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

    @pytest.mark.parametrize("name", ["with#hash.db", "with?query.db"])
    def test_missing_db_at_an_awkward_path_is_not_created(self, tmp_path, monkeypatch, name):
        target = tmp_path / name
        monkeypatch.setattr(config, "DB_PATH", target)
        doctor.check_database()
        assert not target.exists()

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

    def test_unwritable_token_file_fails(self, setup):
        """A refresh rotates the token remotely; a failed write loses it entirely."""
        config_dir, _ = setup
        _write_client(config_dir)
        path = _write_tokens(config_dir)
        os.chmod(path, 0o400)
        try:
            assert doctor.FAIL in _severities(doctor.check_credentials(), "credentials")
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
