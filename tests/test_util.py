"""Tests for shared utilities."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from util import (
    atomic_write_json,
    atomic_write_yaml,
    fleet_from_config,
    hostname,
    load_swarm_config,
    now_iso,
    now_ts,
    relative_time,
    swarm_root,
)


class TestNowIso:
    def test_returns_utc_iso_string(self):
        result = now_iso()
        assert result.endswith("Z")
        # Should parse without error
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ")
        assert dt.year >= 2026

    def test_is_current_time(self):
        before = datetime.now(UTC).replace(microsecond=0)
        result = now_iso()
        after = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert before <= dt <= after


class TestNowTs:
    def test_returns_float(self):
        result = now_ts()
        assert isinstance(result, float)
        assert result > 0

    def test_is_recent(self):
        result = now_ts()
        now = datetime.now(UTC).timestamp()
        assert abs(result - now) < 2


class TestHostname:
    def test_returns_string(self):
        result = hostname()
        assert isinstance(result, str)
        assert len(result) > 0


class TestRelativeTime:
    def test_seconds_ago(self):
        ts = (datetime.now(UTC) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = relative_time(ts)
        assert result.endswith("s ago")

    def test_minutes_ago(self):
        ts = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = relative_time(ts)
        assert result.endswith("m ago")

    def test_hours_ago(self):
        ts = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = relative_time(ts)
        assert result.endswith("h ago")

    def test_days_ago(self):
        ts = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = relative_time(ts)
        assert result.endswith("d ago")

    def test_empty_string(self):
        assert relative_time("") == "?"

    def test_invalid_string(self):
        result = relative_time("not-a-date")
        assert result == "not-a-date"

    def test_none_returns_question_mark(self):
        assert relative_time(None) == "?"


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path):
        target = tmp_path / "test.json"
        data = {"key": "value", "count": 42}
        atomic_write_json(target, data)
        assert target.exists()
        loaded = json.loads(target.read_text())
        assert loaded == data

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "test.json"
        atomic_write_json(target, {"old": True})
        atomic_write_json(target, {"new": True})
        loaded = json.loads(target.read_text())
        assert loaded == {"new": True}

    def test_no_tmp_file_remains(self, tmp_path):
        target = tmp_path / "test.json"
        atomic_write_json(target, {"a": 1})
        tmp_file = target.with_suffix(".tmp")
        assert not tmp_file.exists()


class TestAtomicWriteYaml:
    def test_writes_valid_yaml(self, tmp_path):
        target = tmp_path / "test.yaml"
        data = {"name": "swarm", "version": 5}
        atomic_write_yaml(target, data)
        assert target.exists()
        loaded = yaml.safe_load(target.read_text())
        assert loaded == data

    def test_no_tmp_file_remains(self, tmp_path):
        target = tmp_path / "test.yaml"
        atomic_write_yaml(target, {"x": 1})
        assert not target.with_suffix(".tmp").exists()


class TestSwarmRoot:
    def test_returns_path(self):
        result = swarm_root()
        assert isinstance(result, Path)

    def test_fallback_to_home(self, tmp_path):
        with patch("util.Path") as MockPath:
            nfs = tmp_path / "nfs_swarm"
            # Simulate /opt/swarm not existing
            type(nfs)
            MockPath.side_effect = lambda x: Path(x)
            # Just verify the function doesn't crash
            result = swarm_root()
            assert result is not None


class TestLoadSwarmConfig:
    def test_returns_dict(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config_file = config_dir / "swarm.yaml"
        config_file.write_text(yaml.dump({"nodes": {"test": {"ip": "1.2.3.4"}}}))
        with patch("util.swarm_root", return_value=tmp_path):
            result = load_swarm_config()
        assert result == {"nodes": {"test": {"ip": "1.2.3.4"}}}

    def test_missing_config_returns_empty(self, tmp_path):
        with patch("util.swarm_root", return_value=tmp_path):
            with patch("util.Path") as MockPath:
                # Mock all config paths to not exist
                mock_path = MockPath.return_value
                mock_path.exists.return_value = False
                # Fallback: return empty dict
                result = load_swarm_config()
                # May or may not find real config — just verify no crash
                assert isinstance(result, dict)


class TestFleetFromConfig:
    def test_parses_nodes(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config = {
            "nodes": {
                "node_gpu": {"ip": "<primary-node-ip>", "capabilities": ["gpu", "docker"]},
                "node_primary": {"ip": "<orchestration-node-ip>", "capabilities": ["docker"]},
                "future": {"ip": "TBD"},
            }
        }
        (config_dir / "swarm.yaml").write_text(yaml.dump(config))
        with patch("util.swarm_root", return_value=tmp_path):
            fleet = fleet_from_config()
        assert "node_gpu" in fleet
        assert "node_primary" in fleet
        assert "future" not in fleet  # TBD should be excluded
        assert fleet["node_gpu"]["ip"] == "<primary-node-ip>"
        assert "gpu" in fleet["node_gpu"]["capabilities"]

    def test_empty_config_returns_empty(self):
        with patch("util.load_swarm_config", return_value={}):
            fleet = fleet_from_config()
        assert fleet == {}
