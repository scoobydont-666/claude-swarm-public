"""Tests for the event bus."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def tmp_events_dir(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    with patch("events.EVENTS_DIR", events_dir):
        yield events_dir


class TestEmit:
    def test_emit_creates_file(self, tmp_events_dir):
        from events import emit

        path = emit("test_event", project="/opt/test", details={"key": "value"})
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["type"] == "test_event"
        assert data["project"] == "/opt/test"
        assert data["details"]["key"] == "value"

    def test_emit_commit_shorthand(self, tmp_events_dir):
        from events import emit_commit

        path = emit_commit("/opt/test", "abc123", "feat: something", files_changed=3)
        data = json.loads(path.read_text())
        assert data["type"] == "commit"
        assert data["details"]["commit"] == "abc123"
        assert data["details"]["files_changed"] == 3

    def test_emit_test_result(self, tmp_events_dir):
        from events import emit_test_result

        path = emit_test_result("/opt/test", passed=95, failed=1, total=96)
        data = json.loads(path.read_text())
        assert data["type"] == "test_result"
        assert data["details"]["all_green"] is False


class TestQuery:
    def test_query_empty(self, tmp_events_dir):
        from events import query

        assert query() == []

    def test_query_by_type(self, tmp_events_dir):
        from events import emit, query

        emit("commit", project="/opt/a")
        emit("test_result", project="/opt/b")
        emit("commit", project="/opt/c")
        results = query(event_type="commit")
        assert len(results) == 2
        assert all(r["type"] == "commit" for r in results)

    def test_query_by_project(self, tmp_events_dir):
        from events import emit, query

        emit("commit", project="/opt/a")
        emit("commit", project="/opt/b")
        results = query(project="/opt/a")
        assert len(results) == 1

    def test_query_limit(self, tmp_events_dir):
        from events import emit, query

        for i in range(10):
            emit("test", details={"i": i})
        results = query(limit=3)
        assert len(results) == 3


class TestSummarize:
    def test_summarize_counts(self, tmp_events_dir):
        from events import emit, emit_commit, emit_test_result, summarize_since

        emit("session_start")
        emit_commit("/opt/a", "abc", "feat: x", 2)
        emit_commit("/opt/b", "def", "fix: y", 1)
        emit_test_result("/opt/a", 50, 0, 50)

        summary = summarize_since("2000-01-01T00:00:00Z")
        assert summary["event_count"] == 4
        assert len(summary["commits"]) == 2
        assert len(summary["tests"]) == 1
        assert "/opt/a" in summary["projects_touched"]
        assert "/opt/b" in summary["projects_touched"]


class TestEmitRateLimit:
    def test_rate_limit_event(self, tmp_events_dir):
        from events import emit_rate_limit

        path = emit_rate_limit("MAX-200", "5hr_burst", "wait 30m")
        data = json.loads(path.read_text())
        assert data["type"] == "rate_limit"
        assert data["details"]["profile"] == "MAX-200"
        assert data["details"]["limit_type"] == "5hr_burst"
        assert data["details"]["reset_hint"] == "wait 30m"


class TestEmitTaskComplete:
    def test_task_complete_event(self, tmp_events_dir):
        from events import emit_task_complete

        path = emit_task_complete("task-42", "/opt/examforge", {"passed": 100})
        data = json.loads(path.read_text())
        assert data["type"] == "task_completed"
        assert data["project"] == "/opt/examforge"
        assert data["details"]["task_id"] == "task-42"
        assert data["details"]["result"]["passed"] == 100


class TestSinceLastSession:
    def test_returns_all_when_no_session_end(self, tmp_events_dir):
        from events import emit, since_last_session

        emit("commit", project="/opt/a")
        emit("commit", project="/opt/b")
        results = since_last_session()
        assert len(results) == 2

    def test_returns_events_after_session_end(self, tmp_events_dir):
        import time
        from events import emit, since_last_session

        emit("commit", project="/opt/a")
        time.sleep(0.01)  # ensure ordering
        emit("session_end")
        time.sleep(0.01)
        emit("commit", project="/opt/b")
        results = since_last_session()
        # Should get the commit after session_end
        assert any(r["project"] == "/opt/b" for r in results)


class TestQueryByHostname:
    def test_filter_by_hostname(self, tmp_events_dir):
        from events import emit, query

        # All events from this host
        emit("commit", project="/opt/a")
        emit("commit", project="/opt/b")
        import socket

        hostname = socket.gethostname()
        results = query(hostname=hostname)
        assert len(results) == 2
        results_other = query(hostname="nonexistent-host")
        assert len(results_other) == 0


class TestEventSequence:
    def test_sequence_numbers_increase(self, tmp_events_dir):
        from events import emit

        p1 = emit("test1")
        p2 = emit("test2")
        d1 = json.loads(p1.read_text())
        d2 = json.loads(p2.read_text())
        assert d2["sequence"] > d1["sequence"]


class TestEventWatcher:
    def test_watcher_starts_and_stops(self, tmp_events_dir):
        import time
        from events import EventWatcher

        with patch("events.EventWatcher._check_for_commits"):
            watcher = EventWatcher(interval=0.1)
            watcher.start()
            assert watcher._thread is not None
            assert watcher._thread.is_alive()
            time.sleep(0.15)
            watcher.stop()
            assert not watcher._thread.is_alive() if watcher._thread else True

    def test_watcher_calls_check_for_commits(self, tmp_events_dir):
        import time
        from events import EventWatcher

        with patch(
            "sync_engine.process_commit_events", return_value={"/opt/test": ["pulled"]}
        ) as mock_pce:
            watcher = EventWatcher(interval=0.05)
            watcher.start()
            time.sleep(0.2)
            watcher.stop()
            assert mock_pce.call_count >= 1
            assert watcher.pull_count >= 0

    def test_watcher_idempotent_start(self, tmp_events_dir):
        from events import EventWatcher

        with patch("events.EventWatcher._check_for_commits"):
            watcher = EventWatcher(interval=60)
            watcher.start()
            thread1 = watcher._thread
            watcher.start()  # Second start should be no-op
            assert watcher._thread is thread1
            watcher.stop()


class TestRotate:
    """Tests for event rotation and archive pruning."""

    def _make_event_file(
        self, events_dir: Path, ts: datetime, suffix: str = "host-1234"
    ) -> Path:
        """Write a minimal event JSON with a timestamp-prefixed filename."""
        filename = ts.strftime("%Y%m%dT%H%M%S") + f"000000-{suffix}.json"
        path = events_dir / filename
        path.write_text('{"type": "test", "timestamp": "' + ts.isoformat() + '"}')
        return path

    def test_rotate_moves_old_files(self, tmp_events_dir):
        from events import rotate

        now = datetime.now(timezone.utc)
        old_ts = now.replace(year=now.year - 1)  # 1 year ago — definitely old
        new_ts = now  # right now — should NOT be rotated

        old_file = self._make_event_file(tmp_events_dir, old_ts, "host-001")
        new_file = self._make_event_file(tmp_events_dir, new_ts, "host-002")

        count = rotate(max_age_days=7)

        assert count == 1
        assert not old_file.exists(), "Old file should have been moved"
        assert new_file.exists(), "New file should remain"

        # Archive should contain the old file
        archive_dir = tmp_events_dir / "archive" / old_ts.strftime("%Y-%m")
        assert (archive_dir / old_file.name).exists()

    def test_rotate_keeps_recent_files(self, tmp_events_dir):
        from events import rotate

        now = datetime.now(timezone.utc)
        recent_ts = now  # within max_age_days

        f = self._make_event_file(tmp_events_dir, recent_ts, "host-003")
        count = rotate(max_age_days=7)

        assert count == 0
        assert f.exists(), "Recent file must not be rotated"

    def test_rotate_creates_archive_subdirs(self, tmp_events_dir):
        from events import rotate

        now = datetime.now(timezone.utc)
        old_ts = now.replace(year=now.year - 1)
        self._make_event_file(tmp_events_dir, old_ts, "host-004")

        rotate(max_age_days=7)

        archive_dir = tmp_events_dir / "archive" / old_ts.strftime("%Y-%m")
        assert archive_dir.is_dir()

    def test_rotate_max_files_cap(self, tmp_events_dir):
        """When total files exceed max_files, oldest files beyond the cap are archived."""
        from events import rotate

        now = datetime.now(timezone.utc)
        # Create 5 files all within max_age_days, but cap at 3
        for i in range(5):
            ts = now.replace(hour=(i % 24))
            self._make_event_file(tmp_events_dir, ts, f"host-cap{i:02d}")

        count = rotate(max_age_days=7, max_files=3)
        # 2 oldest should be rotated to meet cap
        assert count == 2
        remaining = list(tmp_events_dir.glob("*.json"))
        assert len(remaining) == 3

    def test_prune_archive_deletes_old(self, tmp_events_dir):
        from events import prune_archive

        now = datetime.now(timezone.utc)
        very_old_ts = now.replace(year=now.year - 1)

        archive_dir = tmp_events_dir / "archive" / very_old_ts.strftime("%Y-%m")
        archive_dir.mkdir(parents=True, exist_ok=True)
        filename = very_old_ts.strftime("%Y%m%dT%H%M%S") + "000000-host-999.json"
        archived_file = archive_dir / filename
        archived_file.write_text('{"type": "old"}')

        pruned = prune_archive(max_age_days=30)
        assert pruned == 1
        assert not archived_file.exists()

    def test_prune_archive_keeps_recent(self, tmp_events_dir):
        from events import prune_archive

        now = datetime.now(timezone.utc)
        # File 10 days old — keep it (max_age_days=30)
        recent_ts = now

        archive_dir = tmp_events_dir / "archive" / recent_ts.strftime("%Y-%m")
        archive_dir.mkdir(parents=True, exist_ok=True)
        filename = recent_ts.strftime("%Y%m%dT%H%M%S") + "000000-host-888.json"
        archived_file = archive_dir / filename
        archived_file.write_text('{"type": "recent"}')

        pruned = prune_archive(max_age_days=30)
        assert pruned == 0
        assert archived_file.exists()

    def test_prune_archive_removes_empty_dirs(self, tmp_events_dir):
        from events import prune_archive

        now = datetime.now(timezone.utc)
        very_old_ts = now.replace(year=now.year - 1)

        archive_dir = tmp_events_dir / "archive" / very_old_ts.strftime("%Y-%m")
        archive_dir.mkdir(parents=True, exist_ok=True)
        filename = very_old_ts.strftime("%Y%m%dT%H%M%S") + "000000-host-777.json"
        (archive_dir / filename).write_text('{"type": "old"}')

        prune_archive(max_age_days=30)
        # Directory should be removed since it became empty
        assert not archive_dir.exists()
