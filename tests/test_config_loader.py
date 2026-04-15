"""Tests for config_loader — centralized config with env var overrides."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config_loader import (
    _resolve_env_vars,
    _walk_resolve,
    load_config,
    get_config,
    get_nodes,
    get_node_config,
    get_fleet,
    get_auto_dispatch_config,
    get_capability_routing,
    get_swarm_root,
)


class TestEnvVarResolution:
    def test_simple_var(self):
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            assert _resolve_env_vars("${MY_VAR}") == "hello"

    def test_var_with_default(self):
        # Var not set, use default
        result = _resolve_env_vars("${UNSET_VAR:-fallback}")
        assert result == "fallback"

    def test_var_with_default_but_set(self):
        with patch.dict(os.environ, {"SET_VAR": "real"}):
            assert _resolve_env_vars("${SET_VAR:-fallback}") == "real"

    def test_unresolved_kept(self):
        result = _resolve_env_vars("${TOTALLY_UNSET}")
        assert result == "${TOTALLY_UNSET}"

    def test_mixed_text(self):
        with patch.dict(os.environ, {"HOST": "giga"}):
            assert _resolve_env_vars("ssh admin@example.com == "ssh admin@example.com

    def test_no_vars(self):
        assert _resolve_env_vars("plain text") == "plain text"


class TestWalkResolve:
    def test_dict_values_resolved(self):
        with patch.dict(os.environ, {"PORT": "8080"}):
            result = _walk_resolve({"port": "${PORT}", "host": "127.0.0.1"})
            assert result["port"] == "8080"
            assert result["host"] == "127.0.0.1"

    def test_nested_dict(self):
        with patch.dict(os.environ, {"DB_PASS": "secret"}):
            result = _walk_resolve({"db": {"password": "${DB_PASS}"}})
            assert result["db"]["password"] == "secret"

    def test_list_values(self):
        with patch.dict(os.environ, {"A": "x", "B": "y"}):
            result = _walk_resolve(["${A}", "${B}", "literal"])
            assert result == ["x", "y", "literal"]

    def test_non_string_passthrough(self):
        result = _walk_resolve({"count": 42, "flag": True, "rate": 3.14})
        assert result == {"count": 42, "flag": True, "rate": 3.14}


class TestLoadConfig:
    def test_loads_from_disk(self):
        """Config loads successfully from the real swarm.yaml."""
        config = load_config(force_reload=True)
        assert "nodes" in config or "swarm" in config

    def test_caching(self):
        """Second call returns cached result."""
        c1 = load_config(force_reload=True)
        c2 = load_config()
        assert c1 is c2

    def test_force_reload(self):
        """force_reload re-reads from disk."""
        c1 = load_config(force_reload=True)
        c2 = load_config(force_reload=True)
        assert c1 == c2  # Same content but different objects


class TestTypedAccessors:
    def test_get_nodes(self):
        nodes = get_nodes()
        assert isinstance(nodes, dict)
        # Should have at least gpu-server-1
        assert "gpu-server-1" in nodes or len(nodes) > 0

    def test_get_node_config_exact(self):
        node = get_node_config("gpu-server-1")
        if node:  # May not exist in test env
            assert "ip" in node or "capabilities" in node

    def test_get_node_config_case_insensitive(self):
        node_upper = get_node_config("gpu-server-1")
        node_lower = get_node_config("giga")
        assert node_upper == node_lower

    def test_get_node_config_missing(self):
        assert get_node_config("NONEXISTENT") == {}

    def test_get_fleet(self):
        fleet = get_fleet()
        assert isinstance(fleet, dict)
        for name, info in fleet.items():
            assert "ip" in info
            assert "capabilities" in info

    def test_get_auto_dispatch_config(self):
        ad = get_auto_dispatch_config()
        assert isinstance(ad, dict)

    def test_get_capability_routing(self):
        routing = get_capability_routing()
        assert isinstance(routing, dict)

    def test_get_swarm_root(self):
        root = get_swarm_root()
        assert isinstance(root, Path)


class TestCustomConfig:
    def test_env_override_path(self, tmp_path):
        """SWARM_CONFIG env var overrides default paths."""
        config_file = tmp_path / "custom.yaml"
        config_file.write_text(yaml.dump({
            "nodes": {"TEST_HOST": {"ip": "10.0.0.1", "capabilities": ["cpu"]}},
            "auto_dispatch": {"mode": "off"},
        }))

        with patch.dict(os.environ, {"SWARM_CONFIG": str(config_file)}):
            config = load_config(force_reload=True)
            assert "TEST_HOST" in config.get("nodes", {})

        # Restore real config
        load_config(force_reload=True)

    def test_env_vars_in_config(self, tmp_path):
        """Environment variables in config values are resolved."""
        config_file = tmp_path / "env_test.yaml"
        config_file.write_text(yaml.dump({
            "database_url": "${DB_URL:-postgresql://127.0.0.1/swarm}",
            "nodes": {"HOST": {"ip": "${HOST_IP:-10.0.0.1}"}},
        }))

        with patch.dict(os.environ, {"SWARM_CONFIG": str(config_file), "DB_URL": "postgresql://prod/swarm"}):
            config = load_config(force_reload=True)
            assert config["database_url"] == "postgresql://prod/swarm"
            assert config["nodes"]["HOST"]["ip"] == "10.0.0.1"  # default

        # Restore
        load_config(force_reload=True)
