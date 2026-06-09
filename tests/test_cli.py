"""Tests for the withings-mcp command-line entry point."""

from importlib.metadata import version
from unittest.mock import Mock, patch

import pytest

import main


def test_version_flag_prints_package_version(capsys):
    with patch("sys.argv", ["withings-mcp", "--version"]):
        with pytest.raises(SystemExit) as exc_info:
            main.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"withings-mcp {version('withings-mcp')}"


def test_version_flag_takes_precedence_over_subcommand(capsys):
    with patch("sys.argv", ["withings-mcp", "auth", "--version"]):
        with pytest.raises(SystemExit) as exc_info:
            main.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"withings-mcp {version('withings-mcp')}"


def test_version_flag_does_not_mask_invalid_subcommand_args(capsys):
    with patch("sys.argv", ["withings-mcp", "bogus", "--version"]):
        with pytest.raises(SystemExit) as exc_info:
            main.main()

    captured = capsys.readouterr()
    assert exc_info.value.code != 0
    assert f"withings-mcp {version('withings-mcp')}" not in captured.out


def test_no_args_starts_stdio_server():
    run_mock = Mock()

    with patch("sys.argv", ["withings-mcp"]), patch.object(main.mcp, "run", run_mock):
        main.main()

    run_mock.assert_called_once_with(transport="stdio")


def test_auth_subcommand_runs_setup_auth():
    with (
        patch("sys.argv", ["withings-mcp", "auth"]),
        patch("withings_mcp.auth.setup_auth") as setup_auth,
    ):
        main.main()

    setup_auth.assert_called_once_with()


def test_sync_subcommand_defaults_to_all_types_30_days():
    run_sync = Mock(return_value={})
    with (
        patch("sys.argv", ["withings-mcp", "sync"]),
        patch.object(main.sync_tools, "run_sync", run_sync),
    ):
        main.main()

    run_sync.assert_called_once_with(["body", "sleep", "activity", "workouts"], 30)


def test_sync_subcommand_all_expands_to_four_types():
    run_sync = Mock(return_value={})
    with (
        patch("sys.argv", ["withings-mcp", "sync", "--types", "all"]),
        patch.object(main.sync_tools, "run_sync", run_sync),
    ):
        main.main()

    run_sync.assert_called_once_with(["body", "sleep", "activity", "workouts"], 30)


def test_sync_subcommand_accepts_type_subset():
    run_sync = Mock(return_value={})
    with (
        patch("sys.argv", ["withings-mcp", "sync", "--types", "body,sleep"]),
        patch.object(main.sync_tools, "run_sync", run_sync),
    ):
        main.main()

    run_sync.assert_called_once_with(["body", "sleep"], 30)


def test_sync_subcommand_days_flag_controls_history():
    run_sync = Mock(return_value={})
    with (
        patch("sys.argv", ["withings-mcp", "sync", "--days", "7"]),
        patch.object(main.sync_tools, "run_sync", run_sync),
    ):
        main.main()

    run_sync.assert_called_once_with(["body", "sleep", "activity", "workouts"], 7)


def test_sync_subcommand_does_not_start_server():
    run_mock = Mock()
    with (
        patch("sys.argv", ["withings-mcp", "sync"]),
        patch.object(main.sync_tools, "run_sync", Mock(return_value={})),
        patch.object(main.mcp, "run", run_mock),
    ):
        main.main()

    run_mock.assert_not_called()


def test_sync_subcommand_prints_per_type_summary(capsys):
    results = {"body": {"status": "ok", "records": 3, "range": "2026-03-01 to 2026-03-10"}}
    with (
        patch("sys.argv", ["withings-mcp", "sync", "--types", "body"]),
        patch.object(main.sync_tools, "run_sync", Mock(return_value=results)),
    ):
        main.main()

    assert "body: 3 records (2026-03-01 to 2026-03-10)" in capsys.readouterr().out


def test_sync_subcommand_exits_nonzero_when_a_type_fails():
    results = {"body": {"status": "error", "message": "boom"}}
    with (
        patch("sys.argv", ["withings-mcp", "sync", "--types", "body"]),
        patch.object(main.sync_tools, "run_sync", Mock(return_value=results)),
    ):
        with pytest.raises(SystemExit) as exc_info:
            main.main()

    assert exc_info.value.code == 1
