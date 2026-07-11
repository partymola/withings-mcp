"""Tests for configuration path resolution (config.py).

Verifies the WITHINGS_MCP_CONFIG_DIR / WITHINGS_MCP_DB_PATH environment
overrides, the packaged defaults, and that the credential paths are derived
from CONFIG_DIR. The module reads os.environ at import time, so each test
reloads it under a controlled environment and the fixture restores the
default afterwards.
"""

import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from withings_mcp import config

_ENV_KEYS = ("WITHINGS_MCP_CONFIG_DIR", "WITHINGS_MCP_DB_PATH")


@pytest.fixture(autouse=True)
def _restore_config():
    """Reload config back to the ambient environment after each test."""
    yield
    importlib.reload(config)


def _reload_with(env):
    with patch.dict(os.environ, {}, clear=False):
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(env)
        importlib.reload(config)
        return config


class TestDefaults:
    def test_defaults_sit_under_package_root(self):
        cfg = _reload_with({})
        # Derive the expected root independently of the SUT's own parent-walk.
        root = Path(cfg.__file__).resolve().parents[2]
        assert cfg.CONFIG_DIR == root / "config"
        assert cfg.DB_PATH == root / "withings.db"

    def test_credential_paths_derive_from_config_dir(self):
        cfg = _reload_with({})
        assert cfg.WITHINGS_CLIENT_PATH == cfg.CONFIG_DIR / "withings_client.json"
        assert cfg.WITHINGS_TOKENS_PATH == cfg.CONFIG_DIR / "withings_tokens.json"


class TestEnvOverrides:
    def test_config_dir_override_moves_credential_paths(self):
        cfg = _reload_with({"WITHINGS_MCP_CONFIG_DIR": "/opt/withings/cfg"})
        assert cfg.CONFIG_DIR == Path("/opt/withings/cfg")
        assert cfg.WITHINGS_CLIENT_PATH == Path("/opt/withings/cfg/withings_client.json")
        assert cfg.WITHINGS_TOKENS_PATH == Path("/opt/withings/cfg/withings_tokens.json")

    def test_db_path_override_is_independent_of_config_dir(self):
        cfg = _reload_with(
            {
                "WITHINGS_MCP_CONFIG_DIR": "/opt/withings/cfg",
                "WITHINGS_MCP_DB_PATH": "/var/data/withings.sqlite",
            }
        )
        assert cfg.DB_PATH == Path("/var/data/withings.sqlite")
        # DB override does not drag the config dir with it.
        assert cfg.CONFIG_DIR == Path("/opt/withings/cfg")

    def test_db_path_override_alone_leaves_config_default(self):
        cfg = _reload_with({"WITHINGS_MCP_DB_PATH": "/var/data/withings.sqlite"})
        assert cfg.DB_PATH == Path("/var/data/withings.sqlite")
        assert cfg.CONFIG_DIR == cfg._PACKAGE_ROOT / "config"


class TestStaticConstants:
    def test_callback_port_and_redirect_uri_are_pinned(self):
        # Registered Withings apps whitelist this exact callback; pin it literally.
        cfg = _reload_with({})
        assert cfg.WITHINGS_CALLBACK_PORT == 8585
        assert cfg.WITHINGS_REDIRECT_URI == "http://localhost:8585"

    def test_scopes_cover_metrics_and_activity(self):
        cfg = _reload_with({})
        scopes = cfg.WITHINGS_SCOPES.split(",")
        assert "user.metrics" in scopes
        assert "user.activity" in scopes
