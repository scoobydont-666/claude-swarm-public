"""Tests for auto-scale launcher."""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from launcher import AutoScaler, SpawnResult


@pytest.fixture
def scaler():
    return AutoScaler(
        queue_threshold=3,
        max_instances=5,
        spawn_cooldown=60,
        profiles=["default", "pro-1"],
    )


@pytest.fixture
def swarm_root(tmp_path):
    """Create a mock swarm directory structure."""
    (tmp_path / "tasks" / "pending").mkdir(parents=True)
    (tmp_path / "tasks" / "claimed").mkdir(parents=True)
    (tmp_path / "agents").mkdir(parents=True)
    return tmp_path


class TestGetPendingCount:
    def test_empty_queue(self, swarm_root):
        assert AutoScaler.get_pending_count(swarm_root) == 0

    def test_with_pending_tasks(self, swarm_root):
        for i in range(5):
            (swarm_root / "tasks" / "pending" / f"task-{i}.yaml").write_text("id: test")
        assert AutoScaler.get_pending_count(swarm_root) == 5

    def test_missing_directory(self, tmp_path):
        assert AutoScaler.get_pending_count(tmp_path / "nonexistent") == 0


class TestGetActiveAgentCount:
    def test_no_agents(self, swarm_root):
        assert AutoScaler.get_active_agent_count(swarm_root) == 0

    def test_live_agent(self, swarm_root):
        from datetime import datetime, timezone

        agent_data = {
            "hostname": "test",
            "pid": 1234,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        }
        (swarm_root / "agents" / "test-1234.json").write_text(json.dumps(agent_data))
        assert AutoScaler.get_active_agent_count(swarm_root) == 1

    def test_stale_agent_not_counted(self, swarm_root):
        from datetime import datetime, timezone, timedelta

        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        agent_data = {
            "hostname": "test",
            "pid": 1234,
            "last_heartbeat": old_time,
        }
        (swarm_root / "agents" / "test-1234.json").write_text(json.dumps(agent_data))
        assert AutoScaler.get_active_agent_count(swarm_root) == 0


class TestShouldScale:
    def test_below_threshold(self, scaler):
        should, reason = scaler.should_scale(pending_count=2, active_agents=1)
        assert should is False
        assert "threshold" in reason

    def test_at_max_instances(self, scaler):
        should, reason = scaler.should_scale(pending_count=5, active_agents=5)
        assert should is False
        assert "max instances" in reason

    def test_cooldown_active(self, scaler):
        scaler._last_spawn_time = time.time()  # just spawned
        should, reason = scaler.should_scale(pending_count=5, active_agents=1)
        assert should is False
        assert "cooldown" in reason

    def test_all_profiles_limited(self, scaler):
        tracker = MagicMock()
        tracker.get_available_profiles.return_value = []
        should, reason = scaler.should_scale(
            pending_count=5, active_agents=1, rate_tracker=tracker
        )
        assert should is False
        assert "rate-limited" in reason

    def test_should_scale_true(self, scaler):
        scaler._last_spawn_time = 0  # no cooldown
        should, reason = scaler.should_scale(pending_count=5, active_agents=1)
        assert should is True
        assert "queue depth" in reason

    def test_threshold_boundary(self, scaler):
        scaler._last_spawn_time = 0
        should_below, _ = scaler.should_scale(pending_count=2, active_agents=0)
        should_at, _ = scaler.should_scale(pending_count=3, active_agents=0)
        assert should_below is False
        assert should_at is True


class TestSpawnInstance:
    def test_claude_not_found(self, scaler):
        with patch("launcher.shutil.which", return_value=None):
            result = scaler.spawn_instance()
        assert result.success is False
        assert "not found" in result.reason

    def test_successful_spawn(self, scaler):
        mock_proc = MagicMock()
        mock_proc.pid = 42

        with (
            patch("launcher.shutil.which", return_value="/usr/bin/claude"),
            patch("launcher.subprocess.Popen", return_value=mock_proc),
        ):
            result = scaler.spawn_instance(project_dir="/opt/test", profile="pro-1")

        assert result.success is True
        assert result.pid == 42
        assert result.profile == "pro-1"
        assert 42 in scaler._spawned_pids
        assert scaler._last_spawn_time > 0

    def test_spawn_failure(self, scaler):
        with (
            patch("launcher.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "launcher.subprocess.Popen", side_effect=OSError("permission denied")
            ),
        ):
            result = scaler.spawn_instance()

        assert result.success is False
        assert "permission denied" in result.reason

    def test_task_hint_included(self, scaler):
        mock_proc = MagicMock()
        mock_proc.pid = 99
        captured_args = {}

        def capture_popen(args, **kwargs):
            captured_args["cmd"] = args
            return mock_proc

        with (
            patch("launcher.shutil.which", return_value="/usr/bin/claude"),
            patch("launcher.subprocess.Popen", side_effect=capture_popen),
        ):
            scaler.spawn_instance(task_hint="Fix the tests")

        prompt = captured_args["cmd"][4]  # -p argument
        assert "Fix the tests" in prompt


class TestCheckAndScale:
    def test_no_scale_needed(self, scaler, swarm_root):
        result = scaler.check_and_scale(swarm_root=swarm_root)
        assert result is None

    def test_scales_when_needed(self, scaler, swarm_root):
        # Add pending tasks above threshold
        for i in range(5):
            (swarm_root / "tasks" / "pending" / f"task-{i}.yaml").write_text("id: test")

        scaler._last_spawn_time = 0  # no cooldown
        mock_proc = MagicMock()
        mock_proc.pid = 77

        with (
            patch("launcher.shutil.which", return_value="/usr/bin/claude"),
            patch("launcher.subprocess.Popen", return_value=mock_proc),
        ):
            result = scaler.check_and_scale(swarm_root=swarm_root)

        assert result is not None
        assert result.success is True
        assert result.pid == 77

    def test_uses_best_profile_from_tracker(self, scaler, swarm_root):
        for i in range(5):
            (swarm_root / "tasks" / "pending" / f"task-{i}.yaml").write_text("id: test")

        scaler._last_spawn_time = 0
        mock_proc = MagicMock()
        mock_proc.pid = 88

        tracker = MagicMock()
        tracker.get_available_profiles.return_value = ["pro-1"]
        tracker.get_best_profile.return_value = "pro-1"

        with (
            patch("launcher.shutil.which", return_value="/usr/bin/claude"),
            patch("launcher.subprocess.Popen", return_value=mock_proc),
        ):
            result = scaler.check_and_scale(rate_tracker=tracker, swarm_root=swarm_root)

        assert result.profile == "pro-1"


class TestCleanup:
    def test_cleanup_dead_processes(self, scaler):
        scaler._spawned_pids = [99999999, 99999998]  # unlikely to exist
        cleaned = scaler.cleanup_dead_processes()
        assert len(cleaned) == 2
        assert scaler._spawned_pids == []

    def test_cleanup_keeps_live_processes(self, scaler):
        scaler._spawned_pids = [os.getpid()]  # current process is alive
        cleaned = scaler.cleanup_dead_processes()
        assert len(cleaned) == 0
        assert os.getpid() in scaler._spawned_pids


class TestStatus:
    def test_status_fields(self, scaler):
        status = scaler.status()
        assert "queue_threshold" in status
        assert "max_instances" in status
        assert "spawned_count" in status
        assert "profiles" in status
        assert status["queue_threshold"] == 3
        assert status["max_instances"] == 5


class TestSpawnResult:
    def test_to_dict(self):
        result = SpawnResult(success=True, pid=42, host="node_primary", profile="default")
        d = result.to_dict()
        assert d["success"] is True
        assert d["pid"] == 42
        assert d["host"] == "node_primary"
