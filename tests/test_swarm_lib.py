"""Tests for swarm_lib — status, tasks, messages, artifacts."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import swarm_lib as lib


# ---------------------------------------------------------------------------
# Status tests
# ---------------------------------------------------------------------------


class TestStatus:
    def test_update_and_get_status(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            status = lib.update_status(
                state="active", current_task="testing", model="opus"
            )
            assert status["hostname"] == "testhost"
            assert status["state"] == "active"
            assert status["current_task"] == "testing"
            assert status["model"] == "opus"
            assert status["ip"] == "192.168.200.99"
            assert status["capabilities"]["docker"] is True
            assert status["capabilities"]["gpu"] is True

            # Read it back
            loaded = lib.get_status("testhost")
            assert loaded is not None
            assert loaded["state"] == "active"

    def test_get_all_status(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.update_status(state="active")

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="otherhost"),
        ):
            lib.update_status(state="idle")

        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            all_status = lib.get_all_status()
            assert len(all_status) == 2
            hostnames = {s["hostname"] for s in all_status}
            assert hostnames == {"testhost", "otherhost"}

    def test_get_nonexistent_status(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            assert lib.get_status("nohost") is None

    def test_mark_stale_nodes(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            # Write a status with old timestamp
            status = {
                "hostname": "stalehost",
                "ip": "10.0.0.1",
                "state": "active",
                "updated_at": "2020-01-01T00:00:00Z",
            }
            path = swarm_tmpdir / "status" / "stalehost.json"
            with open(path, "w") as f:
                json.dump(status, f)

            stale = lib.mark_stale_nodes(threshold_seconds=300)
            assert "stalehost" in stale

            # Verify it was marked offline
            reloaded = lib.get_status("stalehost")
            assert reloaded["state"] == "offline"

    def test_cleanup_stale_nodes_resets_to_idle(self, swarm_tmpdir):
        """cleanup_stale_nodes should reset dead nodes to idle and requeue orphaned tasks."""
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            # Create a stale active node
            status = {
                "hostname": "deadhost",
                "ip": "10.0.0.2",
                "state": "active",
                "pid": 99999999,  # Non-existent PID
                "current_task": "task-orphan",
                "project": "/opt/test",
                "model": "opus",
                "session_id": "dead-session",
                "updated_at": "2020-01-01T00:00:00Z",
            }
            path = swarm_tmpdir / "status" / "deadhost.json"
            with open(path, "w") as f:
                json.dump(status, f)

            # Create a claimed task owned by the dead host
            claimed_task = {
                "id": "task-orphan",
                "title": "Orphaned task",
                "description": "Should be requeued",
                "project": "/opt/test",
                "priority": "high",
                "claimed_by": "deadhost",
                "claimed_at": "2020-01-01T00:00:00Z",
            }
            claimed_path = swarm_tmpdir / "tasks" / "claimed" / "task-orphan.yaml"
            with open(claimed_path, "w") as f:
                yaml.dump(claimed_task, f)

            result = lib.cleanup_stale_nodes(threshold_seconds=60, verify_pid=False)
            assert "deadhost" in result["cleaned"]
            assert "task-orphan" in result["orphaned_tasks"]

            # Verify node is idle
            reloaded = lib.get_status("deadhost")
            assert reloaded["state"] == "idle"
            assert reloaded["current_task"] == ""

            # Verify task moved back to pending
            assert not claimed_path.exists()
            requeued = swarm_tmpdir / "tasks" / "pending" / "task-orphan.yaml"
            assert requeued.exists()

    def test_cleanup_skips_live_nodes(self, swarm_tmpdir):
        """cleanup_stale_nodes should skip nodes within threshold."""
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.update_status(state="active", current_task="working")
            result = lib.cleanup_stale_nodes(threshold_seconds=300, verify_pid=False)
            assert result["cleaned"] == []
            assert result["orphaned_tasks"] == []

    def test_cleanup_with_pid_verification_local(self, swarm_tmpdir):
        """cleanup_stale_nodes with verify_pid checks local PID."""
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            # Create stale node on same host with dead PID
            status = {
                "hostname": "testhost",
                "ip": "10.0.0.1",
                "state": "active",
                "pid": 99999999,
                "current_task": "dead-task",
                "project": "/opt/test",
                "model": "opus",
                "session_id": "dead",
                "updated_at": "2020-01-01T00:00:00Z",
            }
            path = swarm_tmpdir / "status" / "testhost.json"
            with open(path, "w") as f:
                json.dump(status, f)

            result = lib.cleanup_stale_nodes(threshold_seconds=60, verify_pid=True)
            assert "testhost" in result["cleaned"]


# ---------------------------------------------------------------------------
# Task tests
# ---------------------------------------------------------------------------


class TestTasks:
    def test_create_task(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            task = lib.create_task(
                title="Test task",
                description="Do the thing",
                project="/opt/test",
                priority="high",
                requires=["gpu"],
                estimated_minutes=15,
            )
            assert task["id"] == "task-001"
            assert task["title"] == "Test task"
            assert task["priority"] == "high"
            assert task["created_by"] == "testhost"
            assert "gpu" in task["requires"]

            # Verify file exists
            assert (swarm_tmpdir / "tasks" / "pending" / "task-001.yaml").exists()

    def test_task_lifecycle(self, swarm_tmpdir):
        """Test create -> claim -> complete lifecycle."""
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            # Create
            task = lib.create_task(title="Lifecycle test")
            task_id = task["id"]
            assert (swarm_tmpdir / "tasks" / "pending" / f"{task_id}.yaml").exists()

            # Claim
            claimed = lib.claim_task(task_id)
            assert claimed["claimed_by"] == "testhost"
            assert "claimed_at" in claimed
            assert not (swarm_tmpdir / "tasks" / "pending" / f"{task_id}.yaml").exists()
            assert (swarm_tmpdir / "tasks" / "claimed" / f"{task_id}.yaml").exists()

            # Complete
            completed = lib.complete_task(task_id, result_artifact="results.md")
            assert completed["completed_by"] == "testhost"
            assert completed["result_artifact"] == "results.md"
            assert not (swarm_tmpdir / "tasks" / "claimed" / f"{task_id}.yaml").exists()
            assert (swarm_tmpdir / "tasks" / "completed" / f"{task_id}.yaml").exists()

    def test_claim_nonexistent_task(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            with pytest.raises(FileNotFoundError):
                lib.claim_task("task-999")

    def test_complete_nonexistent_task(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            with pytest.raises(FileNotFoundError):
                lib.complete_task("task-999")

    def test_list_tasks(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(title="Task A", priority="high")
            lib.create_task(title="Task B", priority="low")

            all_tasks = lib.list_tasks()
            assert len(all_tasks) == 2
            assert all(t["_stage"] == "pending" for t in all_tasks)

            pending = lib.list_tasks("pending")
            assert len(pending) == 2

    def test_next_task_id_increments(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            t1 = lib.create_task(title="First")
            t2 = lib.create_task(title="Second")
            assert t1["id"] == "task-001"
            assert t2["id"] == "task-002"

    def test_get_matching_tasks(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            # Update status so capabilities are known
            lib.update_status(state="active")

            # Task requiring GPU (testhost has gpu)
            lib.create_task(title="GPU task", requires=["gpu"])
            # Task requiring ollama (testhost does NOT have ollama)
            lib.create_task(title="Ollama task", requires=["ollama"])
            # Task with no requirements
            lib.create_task(title="Any task")

            matching = lib.get_matching_tasks()
            titles = [t["title"] for t in matching]
            assert "GPU task" in titles
            assert "Any task" in titles
            assert "Ollama task" not in titles


# ---------------------------------------------------------------------------
# Message tests
# ---------------------------------------------------------------------------


class TestMessages:
    def test_send_and_read_message(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.send_message("testhost", "Hello from sender", sender="otherhost")

            messages = lib.read_inbox("testhost")
            assert len(messages) >= 1
            assert any("Hello from sender" in m.get("text", "") for m in messages)

    def test_broadcast_message(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.broadcast_message("Broadcast test", sender="otherhost")

            messages = lib.read_inbox("testhost")
            broadcast_msgs = [m for m in messages if m.get("_source") == "broadcast"]
            assert len(broadcast_msgs) >= 1

    def test_archive_message(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            path = lib.send_message("testhost", "Archive me", sender="other")
            assert path.exists()

            lib.archive_message(str(path))
            assert not path.exists()
            # Check archive has it
            archived = list((swarm_tmpdir / "messages" / "archive").glob("*.yaml"))
            assert len(archived) >= 1


# ---------------------------------------------------------------------------
# Artifact tests
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_share_and_list_artifact(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            # Create a temp file to share
            src = swarm_tmpdir / "testfile.txt"
            src.write_text("test content")

            dst = lib.share_artifact(str(src))
            assert dst.exists()
            assert dst.name == "testfile.txt"

            artifacts = lib.list_artifacts()
            assert len(artifacts) == 1
            assert artifacts[0]["name"] == "testfile.txt"

    def test_share_artifact_with_name(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            src = swarm_tmpdir / "original.txt"
            src.write_text("renamed")

            dst = lib.share_artifact(str(src), name="custom-name.txt")
            assert dst.name == "custom-name.txt"

    def test_share_nonexistent_file(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            with pytest.raises(FileNotFoundError):
                lib.share_artifact("/nonexistent/file.txt")


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_health_check_runs(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.update_status(state="active")

            result = lib.health_check()
            assert "swarm_root" in result
            assert "nodes" in result
            assert "testhost" in result["nodes"]
            assert result["pending_tasks"] == 0


# ---------------------------------------------------------------------------
# NFS fallback tests (S6.3)
# ---------------------------------------------------------------------------


class TestNFSFallbackClaimTask:
    """claim_task() falls back to ~/.swarm-tasks/claimed/ when NFS is unhealthy."""

    def test_claim_falls_back_to_local_on_nfs_failure(self, swarm_tmpdir, tmp_path):
        """When NFS write raises OSError and NFS is unhealthy, task goes to local dir."""
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
        ):
            task = lib.create_task(title="NFS fallback test")
            task_id = task["id"]

        local_claimed = tmp_path / ".swarm-tasks" / "claimed"

        original_atomic_write_yaml = lib._atomic_write_yaml
        write_calls = []

        def patched_atomic_write(path, data):
            write_calls.append(path)
            # Simulate NFS failure for the claimed/ dir write
            if "claimed" in str(path) and ".swarm-tasks" not in str(path):
                raise OSError("NFS write failed")
            original_atomic_write_yaml(path, data)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_atomic_write_yaml", side_effect=patched_atomic_write),
            patch.object(lib, "_is_nfs_healthy", return_value=False),
            patch.object(lib, "_local_tasks_dir", return_value=local_claimed),
        ):
            local_claimed.mkdir(parents=True, exist_ok=True)
            claimed = lib.claim_task(task_id)

        assert claimed["claimed_by"] == "testhost"
        local_file = local_claimed / f"{task_id}.yaml"
        assert local_file.exists(), "Task should be in local fallback dir"

    def test_claim_raises_on_nfs_failure_when_nfs_healthy(self, swarm_tmpdir):
        """If NFS write fails but NFS is considered healthy, OSError propagates."""
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
        ):
            task = lib.create_task(title="Should raise test")
            task_id = task["id"]

        original_atomic_write_yaml = lib._atomic_write_yaml

        def patched_atomic_write(path, data):
            if "claimed" in str(path) and ".swarm-tasks" not in str(path):
                raise OSError("NFS write failed but NFS is healthy")
            original_atomic_write_yaml(path, data)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_atomic_write_yaml", side_effect=patched_atomic_write),
            patch.object(lib, "_is_nfs_healthy", return_value=True),
        ):
            with pytest.raises(OSError):
                lib.claim_task(task_id)


class TestReconcileLocalTasks:
    """_reconcile_local_tasks() moves cached files back to NFS when healthy."""

    def test_reconcile_moves_files_to_nfs_when_healthy(self, swarm_tmpdir, tmp_path):
        """Files in ~/.swarm-tasks/claimed/ are moved to NFS claimed/ when NFS is healthy."""
        local_claimed = tmp_path / ".swarm-tasks" / "claimed"
        local_claimed.mkdir(parents=True, exist_ok=True)

        # Write a fake cached task
        task_data = {
            "id": "task-cached-001",
            "claimed_by": "testhost",
            "claimed_at": "2026-03-31T00:00:00Z",
        }
        local_file = local_claimed / "task-cached-001.yaml"
        import yaml as _yaml

        with open(local_file, "w") as f:
            _yaml.dump(task_data, f)

        nfs_claimed = swarm_tmpdir / "tasks" / "claimed"
        nfs_claimed.mkdir(parents=True, exist_ok=True)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_is_nfs_healthy", return_value=True),
            patch.object(
                lib,
                "_local_tasks_dir",
                side_effect=lambda stage: tmp_path / ".swarm-tasks" / stage,
            ),
        ):
            moved = lib._reconcile_local_tasks()

        assert "task-cached-001" in moved
        assert not local_file.exists(), (
            "Local cached file should be removed after reconcile"
        )
        assert (nfs_claimed / "task-cached-001.yaml").exists(), (
            "File should appear on NFS"
        )

    def test_reconcile_skips_when_nfs_unhealthy(self, swarm_tmpdir, tmp_path):
        """When NFS is unhealthy, reconciliation does nothing."""
        local_claimed = tmp_path / ".swarm-tasks" / "claimed"
        local_claimed.mkdir(parents=True, exist_ok=True)

        task_file = local_claimed / "task-nfs-down-001.yaml"
        import yaml as _yaml

        task_file.write_text(_yaml.dump({"id": "task-nfs-down-001"}))

        with (
            patch.object(lib, "_is_nfs_healthy", return_value=False),
            patch.object(
                lib,
                "_local_tasks_dir",
                side_effect=lambda stage: tmp_path / ".swarm-tasks" / stage,
            ),
        ):
            moved = lib._reconcile_local_tasks()

        assert moved == []
        assert task_file.exists(), "File should remain when NFS is unhealthy"

    def test_reconcile_no_local_dir(self, tmp_path):
        """If local task dirs are empty, reconcile returns empty list."""
        empty_dir = tmp_path / ".swarm-tasks" / "claimed"
        # Do NOT create the dir — _local_tasks_dir creates it on demand but it will be empty.
        # Patch _is_nfs_healthy to True and _local_tasks_dir to point to non-existent path.
        nonexistent = tmp_path / "does-not-exist"

        with (
            patch.object(lib, "_is_nfs_healthy", return_value=True),
            patch.object(
                lib, "_local_tasks_dir", side_effect=lambda stage: nonexistent / stage
            ),
        ):
            moved = lib._reconcile_local_tasks()
        assert moved == []


# ---------------------------------------------------------------------------
# TaskIndex cache tests (S6.6)
# ---------------------------------------------------------------------------


class TestTaskIndex:
    """Tests for TaskIndex mtime-based cache."""

    def _write_task(
        self, stage_dir: Path, task_id: str, title: str = "Test task"
    ) -> Path:
        """Write a minimal task YAML file."""
        path = stage_dir / f"{task_id}.yaml"
        with open(path, "w") as f:
            yaml.dump({"id": task_id, "title": title, "priority": "medium"}, f)
        return path

    def test_cache_returns_tasks(self, swarm_tmpdir):
        """TaskIndex.list_tasks returns tasks from disk."""
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            pending_dir = swarm_tmpdir / "tasks" / "pending"
            self._write_task(pending_dir, "task-001", "First task")
            self._write_task(pending_dir, "task-002", "Second task")

            index = lib.TaskIndex()
            tasks = index.list_tasks("pending")
            assert len(tasks) == 2
            ids = {t["id"] for t in tasks}
            assert ids == {"task-001", "task-002"}

    def test_cache_hit_returns_same_results(self, swarm_tmpdir):
        """Second call with unchanged directory returns cached result."""
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            pending_dir = swarm_tmpdir / "tasks" / "pending"
            self._write_task(pending_dir, "task-003", "Cached task")

            index = lib.TaskIndex()
            first = index.list_tasks("pending")
            second = index.list_tasks("pending")
            assert first == second

    def test_cache_invalidates_on_mtime_change(self, swarm_tmpdir):
        """Adding a new file (changes directory mtime) causes re-scan."""
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            pending_dir = swarm_tmpdir / "tasks" / "pending"
            self._write_task(pending_dir, "task-004", "Pre-invalidation")

            index = lib.TaskIndex()
            first = index.list_tasks("pending")
            assert len(first) == 1

            # Write a new file — directory mtime changes
            import time as _time

            _time.sleep(0.01)  # ensure mtime actually changes
            self._write_task(pending_dir, "task-005", "Post-invalidation")

            second = index.list_tasks("pending")
            assert len(second) == 2

    def test_cache_nonexistent_stage(self, swarm_tmpdir):
        """list_tasks on a non-existent stage returns empty list."""
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            index = lib.TaskIndex()
            result = index.list_tasks("nonexistent")
            assert result == []

    def test_cache_invalidate_specific_stage(self, swarm_tmpdir):
        """invalidate(stage) clears only that stage's cache."""
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            pending_dir = swarm_tmpdir / "tasks" / "pending"
            self._write_task(pending_dir, "task-006", "Stage invalidate test")

            index = lib.TaskIndex()
            index.list_tasks("pending")
            # Seeded — now invalidate and verify re-scan
            index.invalidate("pending")
            assert "pending" not in index._mtimes

    def test_module_singleton_list_tasks(self, swarm_tmpdir):
        """The module-level list_tasks() function uses TaskIndex under the hood."""
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            pending_dir = swarm_tmpdir / "tasks" / "pending"
            self._write_task(pending_dir, "task-007", "Singleton test")

            # list_tasks(stage) should return the task
            tasks = lib.list_tasks(stage="pending")
            assert any(t["id"] == "task-007" for t in tasks)

    def test_tasks_include_stage_and_file_metadata(self, swarm_tmpdir):
        """TaskIndex should attach _stage and _file to each task."""
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            pending_dir = swarm_tmpdir / "tasks" / "pending"
            self._write_task(pending_dir, "task-008", "Metadata test")

            index = lib.TaskIndex()
            tasks = index.list_tasks("pending")
            assert len(tasks) == 1
            t = tasks[0]
            assert t.get("_stage") == "pending"
            assert "_file" in t
