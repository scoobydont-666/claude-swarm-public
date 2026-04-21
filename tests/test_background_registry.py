"""Tests for BackgroundRegistry — async dispatch tracking."""

import os
import signal
import time

import pytest
import yaml

from background_registry import BackgroundRegistry, start_task


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
        task = tmp_registry.register("dispatch-001", host="node_gpu", description="test task")
        assert task.dispatch_id == "dispatch-001"
        assert task.host == "node_gpu"
        assert task.status == "running"

    def test_register_persists_to_disk(self, tmp_registry):
        tmp_registry.register("dispatch-001", host="node_gpu")
        assert tmp_registry._path.exists()
        data = yaml.safe_load(tmp_registry._path.read_text())
        assert "dispatch-001" in data

    def test_register_with_task_id(self, tmp_registry):
        task = tmp_registry.register("dispatch-001", host="node_gpu", task_id="task-042")
        assert task.task_id == "task-042"

    def test_register_reads_pid_from_file(self, tmp_registry, dispatch_dir):
        pid_file = dispatch_dir / "dispatch-001.pid"
        pid_file.write_text("12345")
        task = tmp_registry.register("dispatch-001", host="node_gpu")
        assert task.pid == 12345

    def test_register_with_explicit_pid(self, tmp_registry):
        task = tmp_registry.register("dispatch-001", host="node_gpu", pid=9999)
        assert task.pid == 9999


class TestActive:
    def test_active_returns_running_only(self, tmp_registry):
        tmp_registry.register("d-1", host="node_gpu")
        tmp_registry.register("d-2", host="node_reserve2")
        tmp_registry._tasks["d-1"].status = "completed"
        active = tmp_registry.active()
        assert len(active) == 1
        assert active[0].dispatch_id == "d-2"

    def test_active_empty_when_none_running(self, tmp_registry):
        assert tmp_registry.active() == []


class TestPoll:
    def test_poll_detects_dead_process(self, tmp_registry):
        tmp_registry.register("d-1", host="node_gpu", pid=99999999)
        completed = tmp_registry.poll()
        assert len(completed) == 1
        assert completed[0].status == "completed"

    def test_poll_ignores_live_process(self, tmp_registry):
        tmp_registry.register("d-1", host="node_gpu", pid=os.getpid())
        completed = tmp_registry.poll()
        assert len(completed) == 0
        assert tmp_registry._tasks["d-1"].status == "running"

    def test_poll_updates_dispatch_record(self, tmp_registry, dispatch_dir):
        record_path = dispatch_dir / "d-1.yaml"
        record_path.write_text(yaml.dump({"status": "running", "exit_code": 0}))
        tmp_registry.register("d-1", host="node_gpu", pid=99999999)
        tmp_registry.poll()
        updated = yaml.safe_load(record_path.read_text())
        assert updated["status"] == "completed"

    def test_poll_marks_nonzero_exit_as_failed(self, tmp_registry, dispatch_dir):
        record_path = dispatch_dir / "d-1.yaml"
        record_path.write_text(yaml.dump({"status": "running", "exit_code": 1}))
        tmp_registry.register("d-1", host="node_gpu", pid=99999999)
        completed = tmp_registry.poll()
        assert completed[0].status == "failed"

    def test_poll_reads_pid_lazily(self, tmp_registry, dispatch_dir):
        tmp_registry.register("d-1", host="node_gpu", pid=-1)
        pid_file = dispatch_dir / "d-1.pid"
        pid_file.write_text("99999999")
        completed = tmp_registry.poll()
        assert len(completed) == 1


class TestSummary:
    def test_summary_counts(self, tmp_registry):
        tmp_registry.register("d-1", host="node_gpu")
        tmp_registry.register("d-2", host="node_reserve2")
        tmp_registry._tasks["d-1"].status = "completed"
        s = tmp_registry.summary()
        assert s["total"] == 2
        assert s["running"] == 1
        assert s["completed"] == 1

    def test_summary_active_hosts(self, tmp_registry):
        tmp_registry.register("d-1", host="node_gpu")
        tmp_registry.register("d-2", host="node_reserve2")
        s = tmp_registry.summary()
        assert set(s["active_hosts"]) == {"node_gpu", "node_reserve2"}


class TestCleanup:
    def test_cleanup_removes_old_completed(self, tmp_registry):
        tmp_registry.register("d-1", host="node_gpu")
        tmp_registry._tasks["d-1"].status = "completed"
        tmp_registry._tasks["d-1"].completed_at = "2020-01-01T00:00:00Z"
        removed = tmp_registry.cleanup(max_age_hours=1)
        assert removed == 1
        assert "d-1" not in tmp_registry._tasks

    def test_cleanup_keeps_recent(self, tmp_registry):
        tmp_registry.register("d-1", host="node_gpu")
        tmp_registry._tasks["d-1"].status = "completed"
        tmp_registry._tasks["d-1"].completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        removed = tmp_registry.cleanup(max_age_hours=1)
        assert removed == 0

    def test_cleanup_keeps_running(self, tmp_registry):
        tmp_registry.register("d-1", host="node_gpu")
        removed = tmp_registry.cleanup(max_age_hours=0)
        assert removed == 0


class TestPersistence:
    def test_reload_from_disk(self, tmp_path):
        path = tmp_path / "reg.yaml"
        reg1 = BackgroundRegistry(registry_path=path)
        reg1.register("d-1", host="node_gpu", description="test")
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


class TestCancel:
    """U1 — cancel() method on BackgroundRegistry."""

    def test_cancel_running_task(self, tmp_registry, monkeypatch):
        # Use os.getpid() so the signal target is real but won't terminate the test runner
        sent_signals = []
        monkeypatch.setattr(
            "background_registry.os.kill",
            lambda pid, sig: sent_signals.append((pid, sig)),
        )
        tmp_registry.register("d-cancel", host="node_gpu", pid=99999999)
        ok = tmp_registry.cancel("d-cancel")
        assert ok is True
        task = tmp_registry.get("d-cancel")
        assert task.status == "canceled"
        assert task.completed_at != ""
        assert sent_signals == [(99999999, signal.SIGTERM)]

    def test_cancel_unknown_dispatch_returns_false(self, tmp_registry):
        assert tmp_registry.cancel("does-not-exist") is False

    def test_cancel_already_completed_returns_false(self, tmp_registry, monkeypatch):
        monkeypatch.setattr("background_registry.os.kill", lambda *args: None)
        tmp_registry.register("d-done", host="node_gpu", pid=12345)
        tmp_registry._tasks["d-done"].status = "completed"
        assert tmp_registry.cancel("d-done") is False
        # Status must not be downgraded from completed back to canceled
        assert tmp_registry.get("d-done").status == "completed"

    def test_cancel_tolerates_already_dead_pid(self, tmp_registry, monkeypatch):
        def _raise_lookup(_pid, _sig):
            raise ProcessLookupError("no such process")

        monkeypatch.setattr("background_registry.os.kill", _raise_lookup)
        tmp_registry.register("d-ghost", host="node_gpu", pid=99999999)
        # Even though the PID is already gone, cancel() should still flip the
        # task to canceled and persist — the user intent was to cancel.
        assert tmp_registry.cancel("d-ghost") is True
        assert tmp_registry.get("d-ghost").status == "canceled"

    def test_cancel_accepts_custom_signal(self, tmp_registry, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "background_registry.os.kill",
            lambda pid, sig: sent.append((pid, sig)),
        )
        tmp_registry.register("d-kill", host="node_gpu", pid=99999999)
        tmp_registry.cancel("d-kill", sig=signal.SIGKILL)
        assert sent == [(99999999, signal.SIGKILL)]

    def test_cancel_persists_to_disk(self, tmp_registry, monkeypatch):
        monkeypatch.setattr("background_registry.os.kill", lambda *args: None)
        tmp_registry.register("d-persist", host="node_gpu", pid=99999999)
        tmp_registry.cancel("d-persist")
        # Read back from disk to confirm save
        reg2 = BackgroundRegistry(registry_path=tmp_registry._path)
        assert reg2.get("d-persist").status == "canceled"


class TestCleanupIncludesCanceled:
    """U1 — cleanup() must also reap canceled tasks."""

    def test_cleanup_removes_old_canceled(self, tmp_registry, monkeypatch):
        monkeypatch.setattr("background_registry.os.kill", lambda *args: None)
        tmp_registry.register("d-1", host="node_gpu", pid=99999999)
        tmp_registry.cancel("d-1")
        tmp_registry._tasks["d-1"].completed_at = "2020-01-01T00:00:00Z"
        removed = tmp_registry.cleanup(max_age_hours=1)
        assert removed == 1
        assert "d-1" not in tmp_registry._tasks


class TestStartTask:
    """U1 — start_task convenience wrapper around hydra_dispatch + register."""

    def test_start_task_dispatches_and_registers(self, tmp_registry, monkeypatch):
        """Happy path: dispatch returns running, registry gets the entry."""
        from dataclasses import dataclass

        @dataclass
        class FakeResult:
            dispatch_id: str = "dispatch-101"
            host: str = "node_gpu"
            task: str = "test task"
            model: str = "sonnet"
            status: str = "running"
            output_file: str = "/tmp/out"
            error: str = ""

        captured_kwargs = {}

        def fake_dispatch(**kwargs):
            captured_kwargs.update(kwargs)
            return FakeResult()

        # Patch the name that start_task imports via local import
        import hydra_dispatch as hd

        monkeypatch.setattr(hd, "dispatch", fake_dispatch)

        task = start_task(
            tmp_registry,
            host="node_gpu",
            task="test task",
            description="unit test",
            model="sonnet",
        )

        assert task.dispatch_id == "dispatch-101"
        assert task.host == "node_gpu"
        assert task.description == "unit test"
        assert task.status == "running"
        assert captured_kwargs["background"] is True
        assert captured_kwargs["task"] == "test task"

    def test_start_task_falls_back_to_task_as_description(self, tmp_registry, monkeypatch):
        from dataclasses import dataclass

        @dataclass
        class FakeResult:
            dispatch_id: str = "dispatch-102"
            host: str = "node_gpu"
            task: str = "this is the task text that will be truncated to 80 chars " * 2
            model: str = "sonnet"
            status: str = "running"
            output_file: str = ""
            error: str = ""

        import hydra_dispatch as hd

        monkeypatch.setattr(hd, "dispatch", lambda **kwargs: FakeResult())

        long_task = "z" * 200
        task = start_task(tmp_registry, host="node_gpu", task=long_task)
        # Description was not provided → falls back to task[:80]
        assert task.description == "z" * 80

    def test_start_task_raises_on_dispatch_failure(self, tmp_registry, monkeypatch):
        from dataclasses import dataclass

        @dataclass
        class FakeResult:
            dispatch_id: str = "dispatch-bad"
            host: str = "node_gpu"
            task: str = "bad"
            model: str = "sonnet"
            status: str = "failed"
            output_file: str = ""
            error: str = "ssh connection refused"

        import hydra_dispatch as hd

        monkeypatch.setattr(hd, "dispatch", lambda **kwargs: FakeResult())

        with pytest.raises(RuntimeError, match="ssh connection refused"):
            start_task(tmp_registry, host="node_gpu", task="bad")

    def test_start_task_reads_pid_from_file(self, tmp_registry, monkeypatch, dispatch_dir):
        from dataclasses import dataclass

        @dataclass
        class FakeResult:
            dispatch_id: str = "dispatch-pid"
            host: str = "node_gpu"
            task: str = "pid test"
            model: str = "sonnet"
            status: str = "running"
            output_file: str = ""
            error: str = ""

        # Write pid file BEFORE dispatching
        (dispatch_dir / "dispatch-pid.pid").write_text("54321")

        import hydra_dispatch as hd

        monkeypatch.setattr(hd, "dispatch", lambda **kwargs: FakeResult())

        task = start_task(tmp_registry, host="node_gpu", task="pid test")
        assert task.pid == 54321
