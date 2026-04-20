"""Tests for hydra_dispatch — fleet dispatch orchestration."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestModelForTask:
    def test_opus_for_architecture(self):
        from hydra_dispatch import _model_for_task

        assert _model_for_task("architect the new auth system") == "opus"

    def test_opus_for_security(self):
        from hydra_dispatch import _model_for_task

        assert _model_for_task("security audit of the API") == "opus"

    def test_opus_for_debug(self):
        from hydra_dispatch import _model_for_task

        assert _model_for_task("debug the failing pipeline") == "opus"

    def test_haiku_for_status(self):
        from hydra_dispatch import _model_for_task

        assert _model_for_task("check status of deployment") == "haiku"

    def test_haiku_for_search(self):
        from hydra_dispatch import _model_for_task

        assert _model_for_task("search for the config file") == "haiku"

    def test_haiku_for_list(self):
        from hydra_dispatch import _model_for_task

        assert _model_for_task("list all running services") == "haiku"

    def test_sonnet_default(self):
        from hydra_dispatch import _model_for_task

        assert _model_for_task("implement the new feature") == "sonnet"

    def test_sonnet_for_general_work(self):
        from hydra_dispatch import _model_for_task

        assert _model_for_task("write unit tests for auth module") == "sonnet"

    def test_case_insensitive(self):
        from hydra_dispatch import _model_for_task

        assert _model_for_task("ANALYZE the performance regression") == "opus"


class TestFindBestHost:
    def test_finds_gpu_host(self):
        from hydra_dispatch import _find_best_host

        fleet = {
            "miniboss": {"capabilities": ["docker", "tailscale"]},
            "GIGA": {"capabilities": ["gpu", "docker", "ollama"]},
        }
        with patch("hydra_dispatch.FLEET", fleet):
            result = _find_best_host(["gpu"])
        assert result == "GIGA"

    def test_no_match_returns_none(self):
        from hydra_dispatch import _find_best_host

        fleet = {
            "miniboss": {"capabilities": ["docker"]},
        }
        with patch("hydra_dispatch.FLEET", fleet):
            result = _find_best_host(["gpu", "tpu"])
        assert result is None

    def test_empty_requirements_matches_any(self):
        from hydra_dispatch import _find_best_host

        fleet = {
            "miniboss": {"capabilities": ["docker"]},
        }
        with patch("hydra_dispatch.FLEET", fleet):
            result = _find_best_host([])
        assert result == "miniboss"

    def test_subset_matching(self):
        from hydra_dispatch import _find_best_host

        fleet = {
            "GIGA": {"capabilities": ["gpu", "docker", "ollama", "nfs_primary"]},
        }
        with patch("hydra_dispatch.FLEET", fleet):
            result = _find_best_host(["gpu", "docker"])
        assert result == "GIGA"


class TestDispatch:
    def test_unknown_host_raises(self, tmp_path):
        from hydra_dispatch import dispatch

        with patch("hydra_dispatch.FLEET", {"GIGA": {}}):
            with pytest.raises(ValueError, match="Unknown host"):
                dispatch(host="UNKNOWN", task="test")

    def test_dispatch_creates_record(self, tmp_path):
        from hydra_dispatch import dispatch

        dispatch_dir = tmp_path / "dispatches"
        dispatch_dir.mkdir()
        fleet = {
            "GIGA": {
                "ip": "192.168.200.163",
                "ssh_user": "josh",
                "claude_path": "/usr/bin/claude",
                "capabilities": ["gpu"],
                "default_model": "sonnet",
            }
        }
        with (
            patch("hydra_dispatch.FLEET", fleet),
            patch("hydra_dispatch.DISPATCH_DIR", dispatch_dir),
            patch("subprocess.Popen") as mock_popen,
            patch("hydra_dispatch.swarm"),
        ):
            mock_popen.return_value = MagicMock(pid=12345)
            result = dispatch(host="GIGA", task="run tests", background=True)

        assert result.host == "GIGA"
        assert result.status == "running"
        assert result.model == "claude-sonnet-4-6"
        # Check YAML record was written
        records = list(dispatch_dir.glob("dispatch-*.yaml"))
        assert len(records) == 1

    def test_dispatch_auto_selects_model(self, tmp_path):
        from hydra_dispatch import dispatch

        dispatch_dir = tmp_path / "dispatches"
        dispatch_dir.mkdir()
        fleet = {
            "GIGA": {
                "ip": "192.168.200.163",
                "ssh_user": "josh",
                "claude_path": "/usr/bin/claude",
                "capabilities": ["gpu"],
                "default_model": "sonnet",
            }
        }
        with (
            patch("hydra_dispatch.FLEET", fleet),
            patch("hydra_dispatch.DISPATCH_DIR", dispatch_dir),
            patch("subprocess.Popen") as mock_popen,
            patch("hydra_dispatch.swarm"),
        ):
            mock_popen.return_value = MagicMock(pid=12345)
            result = dispatch(host="GIGA", task="security audit of codebase")
        # model_router returns full IDs (4.7 family). Security audit is a
        # moderate-complexity task → sonnet tier per current routing rules.
        assert result.model == "claude-sonnet-4-6"


class TestListDispatches:
    def test_lists_dispatch_records(self, tmp_path):
        from hydra_dispatch import list_dispatches

        dispatch_dir = tmp_path / "dispatches"
        dispatch_dir.mkdir()
        record = {
            "dispatch_id": "dispatch-123-GIGA",
            "host": "GIGA",
            "status": "completed",
            "started_at": "2026-03-25T10:00:00Z",
        }
        (dispatch_dir / "dispatch-123-GIGA.yaml").write_text(yaml.dump(record))
        with patch("hydra_dispatch.DISPATCH_DIR", dispatch_dir):
            results = list_dispatches()
        assert len(results) == 1
        assert results[0]["dispatch_id"] == "dispatch-123-GIGA"

    def test_active_only_filter(self, tmp_path):
        from hydra_dispatch import list_dispatches

        dispatch_dir = tmp_path / "dispatches"
        dispatch_dir.mkdir()
        for status in ["completed", "running", "failed"]:
            record = {
                "dispatch_id": f"dispatch-{status}",
                "status": status,
                "started_at": "2026-03-25T10:00:00Z",
            }
            (dispatch_dir / f"dispatch-{status}.yaml").write_text(yaml.dump(record))
        with patch("hydra_dispatch.DISPATCH_DIR", dispatch_dir):
            results = list_dispatches(active_only=True)
        assert len(results) == 1
        assert results[0]["status"] == "running"

    def test_empty_dispatch_dir(self, tmp_path):
        from hydra_dispatch import list_dispatches

        dispatch_dir = tmp_path / "dispatches"
        dispatch_dir.mkdir()
        with patch("hydra_dispatch.DISPATCH_DIR", dispatch_dir):
            results = list_dispatches()
        assert results == []


class TestRecall:
    def test_reads_output_file(self, tmp_path):
        from hydra_dispatch import recall

        dispatch_dir = tmp_path / "dispatches"
        dispatch_dir.mkdir()
        output = dispatch_dir / "dispatch-123.output"
        output.write_text("Task completed successfully\nAll tests passed")
        with patch("hydra_dispatch.DISPATCH_DIR", dispatch_dir):
            result = recall("dispatch-123")
        assert "Task completed successfully" in result

    def test_missing_output(self, tmp_path):
        from hydra_dispatch import recall

        dispatch_dir = tmp_path / "dispatches"
        dispatch_dir.mkdir()
        with patch("hydra_dispatch.DISPATCH_DIR", dispatch_dir):
            result = recall("nonexistent")
        assert "No output found" in result
