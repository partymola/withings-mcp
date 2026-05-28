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
