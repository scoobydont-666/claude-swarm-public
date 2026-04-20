"""Tests for celery_app beat tasks and work_generator utilities."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# --- Tests that do NOT require celery ---


class TestHumanSkipRegex:
    """Verify the tightened HUMAN_SKIP_RE doesn't over-match."""

    def test_machine_review_not_skipped(self):
        from work_generator import is_human_task

        assert not is_human_task("Run linter and review output")
        assert not is_human_task("Review test results automatically")
        assert not is_human_task("Code review via static analysis")

    def test_human_tasks_still_skipped(self):
        from work_generator import is_human_task

        assert is_human_task("Josh reviews the output")
        assert is_human_task("Manual review of tax forms")
        assert is_human_task("Human review of generated questions")
        assert is_human_task("Approve deployment to production")
        assert is_human_task("Sign off on the release")
        assert is_human_task("Requires physical access to server")


# --- Tests that require celery ---

celery = pytest.importorskip("celery", reason="celery not installed")


def _make_config(swarm_root: Path, mode: str = "full") -> dict:
    return {
        "swarm_root": str(swarm_root),
        "work_generator": {
            "enabled": True,
            "prometheus_url": "http://127.0.0.1:9090",
            "max_pending_tasks": 10,
            "projects": {},
        },
        "auto_dispatch": {
            "enabled": mode != "off",
            "mode": mode,
            "max_concurrent_dispatches": 3,
        },
        "scheduled_maintenance": {"daily_hour": 0, "weekly_day": 0},
    }


class TestGenerateWork:
    """Tests for the generate_work Celery task."""

    @patch("celery_app._load_config")
    @patch("auto_dispatch.AutoDispatcher.generate_and_create")
    def test_generate_work_creates_tasks(self, mock_gen, mock_config, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, mode="full")
        mock_config.return_value = config
        mock_gen.return_value = [{"title": "test task"}]

        from celery_app import generate_work

        result = generate_work()
        assert result["status"] == "ok"
        assert result["tasks_created"] == 1

    @patch("celery_app._load_config")
    def test_generate_work_skipped_when_disabled(self, mock_config, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, mode="off")
        mock_config.return_value = config

        from celery_app import generate_work

        result = generate_work()
        assert result["status"] == "skipped"

    @patch("celery_app._load_config")
    def test_generate_work_handles_errors(self, mock_config):
        mock_config.side_effect = Exception("config broken")

        from celery_app import generate_work

        result = generate_work()
        assert result["status"] == "error"
        assert "config broken" in result["message"]


class TestAutoDispatchScan:
    """Tests for the auto_dispatch_scan Celery task."""

    @patch("celery_app._load_config")
    @patch("auto_dispatch.AutoDispatcher.process_pending_tasks")
    def test_dispatch_scan_runs(self, mock_process, mock_config, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, mode="full")
        mock_config.return_value = config
        mock_process.return_value = [{"task_id": "task-001", "host": "GIGA"}]

        from celery_app import auto_dispatch_scan

        result = auto_dispatch_scan()
        assert result["status"] == "ok"
        assert result["dispatched"] == 1

    @patch("celery_app._load_config")
    def test_dispatch_scan_skipped_when_disabled(self, mock_config, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, mode="off")
        mock_config.return_value = config

        from celery_app import auto_dispatch_scan

        result = auto_dispatch_scan()
        assert result["status"] == "skipped"

    @patch("celery_app._load_config")
    def test_dispatch_scan_handles_errors(self, mock_config):
        mock_config.side_effect = Exception("redis down")

        from celery_app import auto_dispatch_scan

        result = auto_dispatch_scan()
        assert result["status"] == "error"
        assert "redis down" in result["message"]


class TestDispatchTaskReturnType:
    """Verify dispatch_task returns a string (task ID), not a dict."""

    @patch("celery_app.gpu_task")
    def test_dispatch_gpu_task_returns_string(self, mock_gpu):
        mock_result = MagicMock()
        mock_result.id = "abc123"
        mock_gpu.apply_async.return_value = mock_result

        from celery_app import dispatch_task

        result = dispatch_task({"requires": ["gpu"]})
        assert isinstance(result, str)
        assert result == "abc123"

    @patch("celery_app.cpu_task")
    def test_dispatch_cpu_task_returns_string(self, mock_cpu):
        mock_result = MagicMock()
        mock_result.id = "def456"
        mock_cpu.apply_async.return_value = mock_result

        from celery_app import dispatch_task

        result = dispatch_task({"requires": []})
        assert isinstance(result, str)
        assert result == "def456"
