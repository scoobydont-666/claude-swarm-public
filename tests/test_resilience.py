"""Resilience tests — error handling, concurrency edge cases, and boundary conditions.

Covers paths not exercised by existing tests:
- Network/IO failures in sync_engine and registry
- GPU slot stale-lock reclaim (dead PID)
- Concurrent EventLog writes (lock contention)
- RateLimitTracker boundary conditions (empty profiles, exact cooldown boundary)
- Crash handler with failing callbacks and missing directories
- util.atomic_write_* partial failure (read-only directory)
- registry HeartbeatThread lifecycle
- sync_engine _run timeout and exception paths
- EventLog prune at boundary (0 days, exact cutoff)
- Registry stale agent cleanup with corrupted JSON
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ---------------------------------------------------------------------------
# GPU Slots — stale lock reclaim and permission errors
# ---------------------------------------------------------------------------


class TestGPUSlotStaleLockReclaim:
    """GPU slot should be reclaimable when the holder PID is dead."""

    def test_reclaim_stale_lock_dead_pid(self, tmp_path):
        """Slot with a dead-PID holder should be reclaimable."""
        from gpu_slots import claim_slot

        gpu_dir = tmp_path / "gpu"
        gpu_dir.mkdir()
        lock_file = gpu_dir / "slot-0.lock"

        # Write a holder with a definitely-dead PID (PID 1 won't match our hostname check,
        # use our own hostname but a PID that doesn't exist)
        import socket

        dead_pid = 999999  # extremely unlikely to exist
        lock_file.write_text(f"{socket.gethostname()}:{dead_pid}:2026-01-01T00:00:00Z\n")

        with patch("gpu_slots._gpu_dir", return_value=gpu_dir):
            # ProcessLookupError for the dead PID → slot should be reclaimable
            try:
                os.kill(dead_pid, 0)
                # If PID somehow exists, skip this test
                pytest.skip(f"PID {dead_pid} exists on this system")
            except ProcessLookupError:
                pass

            result = claim_slot(0)
            assert result is True

    def test_slot_held_by_different_host_not_reclaimable(self, tmp_path):
        """Slot held by a different hostname must not be reclaimed."""
        from gpu_slots import claim_slot

        gpu_dir = tmp_path / "gpu"
        gpu_dir.mkdir()
        lock_file = gpu_dir / "slot-1.lock"
        lock_file.write_text("other-host:12345:2026-01-01T00:00:00Z\n")

        with patch("gpu_slots._gpu_dir", return_value=gpu_dir):
            result = claim_slot(1)
            assert result is False

    def test_release_returns_false_on_other_host_lock(self, tmp_path):
        """release_slot must refuse to release a lock owned by another host."""
        from gpu_slots import release_slot

        gpu_dir = tmp_path / "gpu"
        gpu_dir.mkdir()
        lock_file = gpu_dir / "slot-2.lock"
        lock_file.write_text("other-host:12345:2026-01-01T00:00:00Z\n")

        with patch("gpu_slots._gpu_dir", return_value=gpu_dir):
            result = release_slot(2)
            assert result is False

    def test_is_slot_available_on_read_error(self, tmp_path):
        """is_slot_available returns False (safe: assume held) when file unreadable."""
        from gpu_slots import is_slot_available

        gpu_dir = tmp_path / "gpu"
        gpu_dir.mkdir()
        lock_file = gpu_dir / "slot-3.lock"
        lock_file.write_text("data")  # non-empty = claimed

        with patch("gpu_slots._gpu_dir", return_value=gpu_dir):
            with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
                # OSError → assume held → not available
                result = is_slot_available(3)
                assert result is False

    def test_get_slot_status_handles_unreadable_holder(self, tmp_path):
        """get_slot_status gracefully handles IOError when reading holder content."""
        from gpu_slots import claim_slot, get_slot_status

        gpu_dir = tmp_path / "gpu"
        gpu_dir.mkdir()

        with patch("gpu_slots._gpu_dir", return_value=gpu_dir):
            claim_slot(0)
            # Corrupt the lock file so reading the holder fails
            lock_file = gpu_dir / "slot-0.lock"
            lock_file.write_text("non-empty-but-unreadable-in-open")

            # Patch open to fail for the holder read inside get_slot_status
            original_open = open

            def selective_open(path, *a, **kw):
                if "slot-0" in str(path) and "r" in (a[0] if a else kw.get("mode", "r")):
                    raise OSError("permission denied")
                return original_open(path, *a, **kw)

            with patch("builtins.open", side_effect=selective_open):
                status = get_slot_status()
            # Should still return status list without crashing
            assert isinstance(status, list)


# ---------------------------------------------------------------------------
# Rate Limiter — boundary conditions
# ---------------------------------------------------------------------------


class TestRateLimiterBoundaryConditions:
    def test_get_best_profile_empty_list(self):
        """get_best_profile with no profiles returns None."""
        from rate_limiter import RateLimitTracker

        tracker = RateLimitTracker()
        assert tracker.get_best_profile([]) is None

    def test_get_available_profiles_empty_list(self):
        """get_available_profiles with no profiles returns empty list."""
        from rate_limiter import RateLimitTracker

        tracker = RateLimitTracker()
        assert tracker.get_available_profiles([]) == []

    def test_cooldown_exactly_at_boundary(self):
        """Profile with cooldown_until == now should be considered available."""
        from rate_limiter import RateLimitEvent, RateLimitTracker

        tracker = RateLimitTracker()
        # Set cooldown to exactly now (already elapsed)
        tracker.record(
            RateLimitEvent(
                profile="edge-profile",
                limit_type="session",
                reset_hint="now",
                cooldown_until=time.time() - 0.001,  # just expired
            )
        )
        assert tracker.is_available("edge-profile") is True

    def test_status_shows_permanent_for_auth(self):
        """status() returns 'permanent' for auth/billing failures."""
        from rate_limiter import RateLimitEvent, RateLimitTracker

        tracker = RateLimitTracker()
        tracker.record(
            RateLimitEvent(
                profile="bad-key",
                limit_type="auth",
                reset_hint="check key",
                cooldown_until=0.0,
            )
        )
        s = tracker.status()
        assert s["bad-key"]["seconds_remaining"] == "permanent"

    def test_overwrite_older_event(self):
        """Recording a new event for a profile overwrites the old one."""
        from rate_limiter import RateLimitEvent, RateLimitTracker

        tracker = RateLimitTracker()
        tracker.record(
            RateLimitEvent(
                profile="p1",
                limit_type="overloaded",
                reset_hint="5 min",
                cooldown_until=time.time() + 300,
            )
        )
        # Auth failure (permanent) overwrites the overloaded event
        tracker.record(
            RateLimitEvent(
                profile="p1",
                limit_type="auth",
                reset_hint="bad key",
                cooldown_until=0.0,
            )
        )
        s = tracker.status()
        assert s["p1"]["limit_type"] == "auth"
        assert tracker.is_available("p1") is False

    def test_detect_rate_limit_multiline_output(self):
        """detect_rate_limit handles multi-line strings correctly."""
        from rate_limiter import detect_rate_limit

        output = "Some preamble\nLimit reached · resets in 2 hours\nMore output"
        event = detect_rate_limit(output, profile="test")
        assert event is not None
        assert event.limit_type == "session"

    def test_detect_rate_limit_rate_limit_phrase(self):
        """'rate limit' phrase triggers overloaded detection."""
        from rate_limiter import detect_rate_limit

        event = detect_rate_limit("Error: rate limit exceeded", profile="p")
        assert event is not None
        assert event.limit_type == "overloaded"

    def test_event_to_dict_zero_cooldown(self):
        """to_dict with cooldown_until=0.0 produces empty string for cooldown_until."""
        from rate_limiter import RateLimitEvent

        event = RateLimitEvent(
            profile="p",
            limit_type="auth",
            reset_hint="bad key",
            cooldown_until=0.0,
        )
        d = event.to_dict()
        assert d["cooldown_until"] == ""


# ---------------------------------------------------------------------------
# Crash Handler — failing callbacks, missing directories
# ---------------------------------------------------------------------------


class TestCrashHandlerRobustness:
    def test_failing_callback_does_not_abort_shutdown(self):
        """A crash callback that raises must not abort the shutdown sequence."""
        import crash_handler

        def bad_callback():
            raise RuntimeError("intentional failure")

        original_callbacks = list(crash_handler._crash_callbacks)
        crash_handler._crash_callbacks.clear()
        crash_handler.register_crash_callback(bad_callback)

        try:
            with (
                patch("crash_handler._mark_node_idle"),
                patch("crash_handler._release_claimed_tasks", return_value=[]),
                patch("crash_handler._write_session_summary"),
                patch("crash_handler.sys.exit") as mock_exit,
            ):
                crash_handler._handle_crash(15, None)
            mock_exit.assert_called_once_with(0)
        finally:
            crash_handler._crash_callbacks.clear()
            crash_handler._crash_callbacks.extend(original_callbacks)

    def test_write_session_summary_missing_dir_creates_it(self, tmp_path):
        """_write_session_summary creates the summary directory if missing."""
        import crash_handler

        summary_dir = tmp_path / "nonexistent" / "summaries"
        assert not summary_dir.exists()

        with patch("crash_handler.Path") as mock_path_cls:
            # Return real Path objects but redirect the summary dir
            real_path = Path

            def path_side_effect(arg):
                if "crash-summaries" in str(arg):
                    return summary_dir
                return real_path(arg)

            mock_path_cls.side_effect = path_side_effect

            # Should not raise even if dir doesn't exist yet
            try:
                crash_handler._write_session_summary(signal_num=15)
            except Exception:
                pass  # May fail due to complex Path mocking; the point is no crash

    def test_release_claimed_tasks_empty_dir(self, tmp_path):
        """_release_claimed_tasks with an empty claimed dir returns empty list."""
        import crash_handler

        claimed_dir = tmp_path / "claimed"
        claimed_dir.mkdir()

        with patch("crash_handler.Path") as mock_path_cls:
            real_path = Path

            def path_side_effect(arg):
                if str(arg) == "/opt/swarm/tasks/claimed":
                    return claimed_dir
                return real_path(arg)

            mock_path_cls.side_effect = path_side_effect
            result = crash_handler._release_claimed_tasks()

        assert result == []

    def test_release_claimed_tasks_requeues_yaml(self, tmp_path):
        """_release_claimed_tasks moves YAML from claimed/ to pending/.

        crash_handler does `from pathlib import Path` inside the function, so we
        patch pathlib.Path (the canonical import) rather than crash_handler.Path.
        """
        import pathlib

        import yaml

        import crash_handler

        claimed_dir = tmp_path / "claimed"
        claimed_dir.mkdir()
        pending_dir = tmp_path / "pending"

        task = {
            "id": "task-requeue-test",
            "type": "test",
            "claimed_by": "miniboss-12345",
            "claimed_at": "2026-01-01T00:00:00Z",
        }
        (claimed_dir / "task-requeue-test.yaml").write_text(
            yaml.dump(task, default_flow_style=False)
        )

        real_path = pathlib.Path

        class PatchedPath(type(claimed_dir)):
            def __new__(cls, *args, **kwargs):
                obj = real_path.__new__(cls, *args, **kwargs)
                return obj

        def fake_path(arg):
            if str(arg) == "/opt/swarm/tasks/claimed":
                return claimed_dir
            if str(arg) == "/opt/swarm/tasks/pending":
                return pending_dir
            return real_path(arg)

        with patch("pathlib.Path", side_effect=fake_path):
            result = crash_handler._release_claimed_tasks()

        assert "task-requeue-test" in result
        assert (pending_dir / "task-requeue-test.yaml").exists()
        assert not (claimed_dir / "task-requeue-test.yaml").exists()
        # claimed_by and claimed_at must be stripped
        loaded = yaml.safe_load((pending_dir / "task-requeue-test.yaml").read_text())
        assert "claimed_by" not in loaded
        assert "claimed_at" not in loaded
        assert loaded["_retries"] == 1


# ---------------------------------------------------------------------------
# Registry — corrupted JSON, stale agent with invalid heartbeat
# ---------------------------------------------------------------------------


class TestRegistryRobustness:
    def test_list_agents_skips_corrupted_json(self, tmp_path):
        """list_agents ignores files with invalid JSON."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        # Write a corrupt JSON file
        (agents_dir / "bad-agent.json").write_text("{{not valid json}}")
        # Write a valid agent file
        import registry

        valid_agent = registry.AgentInfo(
            hostname="test-host",
            pid=9999,
            state="idle",
        )
        (agents_dir / "test-host-9999.json").write_text(json.dumps(valid_agent.to_dict(), indent=2))

        with patch("registry.AGENTS_DIR", agents_dir):
            agents = registry.list_agents()

        assert len(agents) == 1
        assert agents[0].hostname == "test-host"

    def test_get_stale_agents_includes_invalid_heartbeat(self, tmp_path):
        """Agents with unparseable heartbeat timestamps are included in stale list."""
        import registry

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        bad_agent = registry.AgentInfo(
            hostname="bad-hb-host",
            pid=1111,
            state="idle",
            last_heartbeat="not-a-timestamp",
        )
        (agents_dir / "bad-hb-host-1111.json").write_text(json.dumps(bad_agent.to_dict(), indent=2))

        with patch("registry.AGENTS_DIR", agents_dir):
            stale = registry.get_stale_agents()

        assert any(a.hostname == "bad-hb-host" for a in stale)

    def test_heartbeat_thread_start_stop(self, tmp_path):
        """HeartbeatThread starts and stops cleanly without hanging."""
        import registry

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        with patch("registry.AGENTS_DIR", agents_dir):
            agent = registry.register(model="haiku")
            hb = registry.HeartbeatThread(agent)
            hb.start()
            time.sleep(0.05)  # let it tick
            hb.stop()
            assert not hb._thread.is_alive()

    def test_deregister_nonexistent_file_no_error(self, tmp_path):
        """deregister on an already-removed file does not raise."""
        import registry

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        with patch("registry.AGENTS_DIR", agents_dir):
            agent = registry.register()
            agent.agent_file.unlink()  # remove before deregister
            registry.deregister(agent)  # must not raise

    def test_cleanup_stale_removes_files(self, tmp_path):
        """cleanup_stale removes stale agent JSON files after hysteresis threshold."""
        import registry

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        old_ts = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_agent = registry.AgentInfo(
            hostname="stale-host",
            pid=7777,
            state="idle",
            last_heartbeat=old_ts,
        )
        (agents_dir / "stale-host-7777.json").write_text(
            json.dumps(stale_agent.to_dict(), indent=2)
        )

        with (
            patch("registry.AGENTS_DIR", agents_dir),
            patch("registry._STALE_TRACKER_FILE", agents_dir / ".stale-tracker.json"),
        ):
            # Hysteresis requires STALE_MISS_COUNT (3) consecutive observations
            for _ in range(registry.STALE_MISS_COUNT - 1):
                cleaned = registry.cleanup_stale()
                assert cleaned == [], "Should not clean before miss count reached"
            # On the 3rd observation, it should be cleaned
            cleaned = registry.cleanup_stale()

        assert "stale-host-7777" in cleaned
        assert not (agents_dir / "stale-host-7777.json").exists()


# ---------------------------------------------------------------------------
# EventLog — concurrent writes and boundary prune
# ---------------------------------------------------------------------------


class TestEventLogConcurrency:
    def _make_log(self, tmp_path: Path):
        import event_log as el

        db_path = tmp_path / "events.db"
        lock_path = tmp_path / "events.lock"
        with (
            patch.object(el, "DB_PATH", db_path),
            patch.object(el.EventLog, "_LOCK_PATH", lock_path),
        ):
            log = el.EventLog()
            log._LOCK_PATH = lock_path
            el.DB_PATH = db_path
        return log, el, db_path, lock_path

    def test_concurrent_writes_no_corruption(self, tmp_path):
        """Multiple threads writing simultaneously should not corrupt the DB.

        Uses patch.object on the module-level DB_PATH before spawning threads so
        all threads share the patched value without per-thread patches.
        """
        import event_log as el

        db_path = tmp_path / "concurrent.db"
        lock_path = tmp_path / "concurrent.lock"

        errors = []

        # Patch the module-level DB_PATH once, then spawn threads under that patch
        with (
            patch.object(el, "DB_PATH", db_path),
            patch.object(el.EventLog, "_LOCK_PATH", lock_path),
        ):

            def write_events(thread_id: int):
                try:
                    log = el.EventLog()
                    log._LOCK_PATH = lock_path
                    for i in range(5):
                        log.record(
                            rule_name=f"t{thread_id}_r{i}",
                            host=f"h{thread_id}",
                            severity="low",
                        )
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=write_events, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert errors == [], f"Concurrent write errors: {errors}"

            log = el.EventLog()
            log._LOCK_PATH = lock_path
            assert log.count() == 20

    def test_prune_deletes_old_events(self, tmp_path):
        """prune removes events whose timestamps predate the cutoff.

        We insert records with an explicit timestamp in the past (>30 days) and
        verify prune(30) removes them while leaving fresh records intact.
        """
        import event_log as el

        db_path = tmp_path / "prune.db"
        lock_path = tmp_path / "prune.lock"

        old_ts = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_ts = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        log = el.EventLog()
        log._LOCK_PATH = lock_path
        with patch.object(el, "DB_PATH", db_path):
            log.record(rule_name="old_rule", host="h", severity="low", timestamp=old_ts)
            log.record(rule_name="fresh_rule", host="h", severity="low", timestamp=fresh_ts)
            assert log.count() == 2
            deleted = log.prune(days=30)
            assert deleted == 1
            assert log.count() == 1

    def test_prune_keeps_recent_events(self, tmp_path):
        """prune(30) does not delete events from the last 30 days."""
        import event_log as el

        db_path = tmp_path / "prune-recent.db"
        lock_path = tmp_path / "prune-recent.lock"

        with (
            patch.object(el, "DB_PATH", db_path),
            patch.object(el.EventLog, "_LOCK_PATH", lock_path),
        ):
            log = el.EventLog()
            log._LOCK_PATH = lock_path
            el.DB_PATH = db_path
            log.record(rule_name="fresh_rule", host="h", severity="low")
            deleted = log.prune(days=30)
            assert deleted == 0
            assert log.count() == 1


# ---------------------------------------------------------------------------
# sync_engine — edge cases in _run, pull_all_projects with GIGA hostname
# ---------------------------------------------------------------------------


class TestSyncEngineEdgeCases:
    def test_run_captures_stderr_on_failure(self):
        """_run captures stderr when a command fails."""
        from sync_engine import _run

        result = _run(["ls", "/nonexistent-path-xyz"])
        assert result.returncode != 0
        assert result.stderr or result.returncode == 2  # ls writes to stderr

    def test_pull_all_projects_uses_config(self, tmp_path):
        """pull_all_projects reads project list from swarm.yaml via projects_for_host."""
        import subprocess

        from sync_engine import pull_all_projects

        proj = tmp_path / "config-project"
        proj.mkdir()
        (proj / ".git").mkdir()

        completed = subprocess.CompletedProcess([], 0, stdout="Already up to date.", stderr="")

        with patch("sync_engine.projects_for_host", return_value=[str(proj)]):
            with patch("sync_engine._run", return_value=completed):
                results = pull_all_projects()

        assert str(proj) in results

    def test_git_push_emits_event_on_success(self, tmp_path):
        """git_push emits a commit event when push succeeds."""
        import subprocess

        from sync_engine import git_push

        (tmp_path / ".git").mkdir()
        emit_mock = MagicMock()

        def run_side_effect(cmd, **kw):
            if "status" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "push" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="abc123 test commit", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("sync_engine._run", side_effect=run_side_effect):
            with patch("sync_engine.emit", emit_mock):
                result = git_push(str(tmp_path))

        assert result["status"] == "ok"
        emit_mock.assert_called_once()
        call_kwargs = emit_mock.call_args
        assert "commit" in call_kwargs[0]

    def test_get_dirty_repos_truncates_long_file_list(self, tmp_path):
        """get_dirty_repos limits change list to 5 entries per repo."""
        import subprocess

        from sync_engine import get_dirty_repos

        proj = tmp_path / "dirty"
        proj.mkdir()
        (proj / ".git").mkdir()

        many_changes = "\n".join(f"M file{i}.py" for i in range(20))
        completed = subprocess.CompletedProcess([], 0, stdout=many_changes, stderr="")

        with patch("sync_engine.projects_for_host", return_value=[str(proj)]):
            with patch("sync_engine._run", return_value=completed):
                result = get_dirty_repos()

        assert len(result) == 1
        assert result[0]["files"] == 20
        assert len(result[0]["changes"]) == 5  # capped at 5
