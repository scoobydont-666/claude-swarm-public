"""Tests for BackgroundRegistry — async dispatch tracking."""

import os
import time

import pytest
import yaml

from background_registry import BackgroundRegistry


@pytest.fixture
def tmp_registry(tmp_path):
    """Create a registry with a temp path."""
    registry_path = tmp_path / "bg-registry.yaml"
    return BackgroundRegistry(registry_path=registry_path)


@pytest.fixture
def dispatch_dir(tmp_path, monkeypatch):
    """Create a temp dispatch directory."""
    d = tmp_path / "dispatches"
    d.mkdir()
    monkeypatch.setattr("background_registry.DISPATCH_DIR", d)
    return d


class TestRegister:
    def test_register_creates_task(self, tmp_registry):
        task = tmp_registry.register(
            "dispatch-001", host="GIGA", description="test task"
        )
        assert task.dispatch_id == "dispatch-001"
        assert task.host == "GIGA"
        assert task.status == "running"

    def test_register_persists_to_disk(self, tmp_registry):
        tmp_registry.register("dispatch-001", host="GIGA")
        assert tmp_registry._path.exists()
        data = yaml.safe_load(tmp_registry._path.read_text())
        assert "dispatch-001" in data

    def test_register_with_task_id(self, tmp_registry):
        task = tmp_registry.register("dispatch-001", host="GIGA", task_id="task-042")
        assert task.task_id == "task-042"

    def test_register_reads_pid_from_file(self, tmp_registry, dispatch_dir):
        pid_file = dispatch_dir / "dispatch-001.pid"
        pid_file.write_text("12345")
        task = tmp_registry.register("dispatch-001", host="GIGA")
        assert task.pid == 12345

    def test_register_with_explicit_pid(self, tmp_registry):
        task = tmp_registry.register("dispatch-001", host="GIGA", pid=9999)
        assert task.pid == 9999


class TestActive:
    def test_active_returns_running_only(self, tmp_registry):
        tmp_registry.register("d-1", host="GIGA")
        tmp_registry.register("d-2", host="MECHA")
        tmp_registry._tasks["d-1"].status = "completed"
        active = tmp_registry.active()
        assert len(active) == 1
        assert active[0].dispatch_id == "d-2"

    def test_active_empty_when_none_running(self, tmp_registry):
        assert tmp_registry.active() == []


class TestPoll:
    def test_poll_detects_dead_process(self, tmp_registry):
        task = tmp_registry.register("d-1", host="GIGA", pid=99999999)
        completed = tmp_registry.poll()
        assert len(completed) == 1
        assert completed[0].status == "completed"

    def test_poll_ignores_live_process(self, tmp_registry):
        task = tmp_registry.register("d-1", host="GIGA", pid=os.getpid())
        completed = tmp_registry.poll()
        assert len(completed) == 0
        assert tmp_registry._tasks["d-1"].status == "running"

    def test_poll_updates_dispatch_record(self, tmp_registry, dispatch_dir):
        record_path = dispatch_dir / "d-1.yaml"
        record_path.write_text(yaml.dump({"status": "running", "exit_code": 0}))
        tmp_registry.register("d-1", host="GIGA", pid=99999999)
        tmp_registry.poll()
        updated = yaml.safe_load(record_path.read_text())
        assert updated["status"] == "completed"

    def test_poll_marks_nonzero_exit_as_failed(self, tmp_registry, dispatch_dir):
        record_path = dispatch_dir / "d-1.yaml"
        record_path.write_text(yaml.dump({"status": "running", "exit_code": 1}))
        tmp_registry.register("d-1", host="GIGA", pid=99999999)
        completed = tmp_registry.poll()
        assert completed[0].status == "failed"

    def test_poll_reads_pid_lazily(self, tmp_registry, dispatch_dir):
        tmp_registry.register("d-1", host="GIGA", pid=-1)
        pid_file = dispatch_dir / "d-1.pid"
        pid_file.write_text("99999999")
        completed = tmp_registry.poll()
        assert len(completed) == 1


class TestSummary:
    def test_summary_counts(self, tmp_registry):
        tmp_registry.register("d-1", host="GIGA")
        tmp_registry.register("d-2", host="MECHA")
        tmp_registry._tasks["d-1"].status = "completed"
        s = tmp_registry.summary()
        assert s["total"] == 2
        assert s["running"] == 1
        assert s["completed"] == 1

    def test_summary_active_hosts(self, tmp_registry):
        tmp_registry.register("d-1", host="GIGA")
        tmp_registry.register("d-2", host="MECHA")
        s = tmp_registry.summary()
        assert set(s["active_hosts"]) == {"GIGA", "MECHA"}


class TestCleanup:
    def test_cleanup_removes_old_completed(self, tmp_registry):
        tmp_registry.register("d-1", host="GIGA")
        tmp_registry._tasks["d-1"].status = "completed"
        tmp_registry._tasks["d-1"].completed_at = "2020-01-01T00:00:00Z"
        removed = tmp_registry.cleanup(max_age_hours=1)
        assert removed == 1
        assert "d-1" not in tmp_registry._tasks

    def test_cleanup_keeps_recent(self, tmp_registry):
        tmp_registry.register("d-1", host="GIGA")
        tmp_registry._tasks["d-1"].status = "completed"
        tmp_registry._tasks["d-1"].completed_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        removed = tmp_registry.cleanup(max_age_hours=1)
        assert removed == 0

    def test_cleanup_keeps_running(self, tmp_registry):
        tmp_registry.register("d-1", host="GIGA")
        removed = tmp_registry.cleanup(max_age_hours=0)
        assert removed == 0


class TestPersistence:
    def test_reload_from_disk(self, tmp_path):
        path = tmp_path / "reg.yaml"
        reg1 = BackgroundRegistry(registry_path=path)
        reg1.register("d-1", host="GIGA", description="test")
        reg2 = BackgroundRegistry(registry_path=path)
        assert "d-1" in reg2._tasks
        assert reg2._tasks["d-1"].description == "test"

    def test_handles_missing_file(self, tmp_path):
        path = tmp_path / "nonexistent.yaml"
        reg = BackgroundRegistry(registry_path=path)
        assert len(reg._tasks) == 0

    def test_handles_corrupt_file(self, tmp_path):
        path = tmp_path / "corrupt.yaml"
        path.write_text("not: [valid: yaml: {")
        reg = BackgroundRegistry(registry_path=path)
        assert len(reg._tasks) == 0
