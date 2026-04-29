"""Tests for collaborative mode."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def tmp_collab_root(tmp_path):
    """Provide temporary collaborative root."""
    collab_root = tmp_path / "collaborative"
    collab_root.mkdir()
    with patch("collaborative.COLLAB_ROOT", collab_root):
        yield collab_root


class TestStartCollaborative:
    def test_start_creates_session(self, tmp_collab_root):
        from collaborative import start_collaborative

        session = start_collaborative(
            task="Implement feature X",
            worker_host="node_gpu",
            orchestrator_host="node_primary",
            project_dir="/opt/examforge",
            model="opus",
        )
        assert session.session_id.startswith("collab-")
        assert session.worker_host == "node_gpu"
        assert session.orchestrator_host == "node_primary"
        assert session.status == "active"
        assert session.context_dir.exists()

    def test_start_writes_context(self, tmp_collab_root):
        from collaborative import start_collaborative, read_context

        session = start_collaborative(
            task="Test task",
            worker_host="node_gpu",
            orchestrator_host="node_primary",
        )
        context = read_context(session.session_id)
        assert context is not None
        assert context["task"] == "Test task"
        assert context["worker_host"] == "node_gpu"


class TestContextExchange:
    def test_write_and_read_context(self, tmp_collab_root):
        from collaborative import write_context, read_context

        session_id = "test-session-001"
        context = {
            "task": "Do something",
            "project_dir": "/opt/test",
            "custom_data": {"nested": "value"},
        }
        write_context(session_id, context)
        read_back = read_context(session_id)
        assert read_back["task"] == "Do something"
        assert read_back["project_dir"] == "/opt/test"
        assert read_back["custom_data"]["nested"] == "value"

    def test_read_missing_context_returns_none(self, tmp_collab_root):
        from collaborative import read_context

        result = read_context("nonexistent-session")
        assert result is None

    def test_context_includes_timestamp(self, tmp_collab_root):
        from collaborative import write_context, read_context

        write_context("test-session", {"task": "x"})
        context = read_context("test-session")
        assert "updated_at" in context


class TestProgressTracking:
    def test_write_and_read_progress(self, tmp_collab_root):
        from collaborative import write_progress, read_progress

        session_id = "test-session-002"
        progress = {
            "steps_completed": 3,
            "current_step": "Analyzing code",
            "completion_percentage": 45,
        }
        write_progress(session_id, progress)
        read_back = read_progress(session_id)
        assert read_back["steps_completed"] == 3
        assert read_back["current_step"] == "Analyzing code"

    def test_progress_includes_timestamp(self, tmp_collab_root):
        from collaborative import write_progress, read_progress

        write_progress("test-session", {"step": 1})
        progress = read_progress("test-session")
        assert "updated_at" in progress

    def test_read_missing_progress_returns_none(self, tmp_collab_root):
        from collaborative import read_progress

        result = read_progress("nonexistent-session")
        assert result is None

    def test_progress_updates_overwrite(self, tmp_collab_root):
        from collaborative import write_progress, read_progress

        session_id = "test-session-003"
        write_progress(session_id, {"step": 1})
        write_progress(session_id, {"step": 2, "data": "updated"})
        progress = read_progress(session_id)
        assert progress["step"] == 2
        assert progress["data"] == "updated"


class TestBlockerFlow:
    def test_write_blocker(self, tmp_collab_root):
        from collaborative import write_blocker, read_blockers, Blocker

        session_id = "test-session-004"
        blocker = Blocker(
            blocker_id="block-001",
            reported_at="2026-03-24T12:00:00Z",
            description="Cannot find required file",
            context={"file": "/opt/missing.txt"},
        )
        write_blocker(session_id, blocker)
        blockers = read_blockers(session_id)
        assert len(blockers) == 1
        assert blockers[0]["blocker_id"] == "block-001"
        assert blockers[0]["description"] == "Cannot find required file"

    def test_multiple_blockers(self, tmp_collab_root):
        from collaborative import write_blocker, read_blockers, Blocker

        session_id = "test-session-005"
        for i in range(3):
            blocker = Blocker(
                blocker_id=f"block-{i}",
                reported_at="2026-03-24T12:00:00Z",
                description=f"Blocker {i}",
            )
            write_blocker(session_id, blocker)
        blockers = read_blockers(session_id)
        assert len(blockers) == 3

    def test_resolve_blocker(self, tmp_collab_root):
        from collaborative import write_blocker, resolve_blocker, read_blockers, Blocker

        session_id = "test-session-006"
        blocker = Blocker(
            blocker_id="block-002",
            reported_at="2026-03-24T12:00:00Z",
            description="Need help with X",
        )
        write_blocker(session_id, blocker)

        resolution = {"guidance": "Do this instead", "file_provided": True}
        resolve_blocker(session_id, "block-002", resolution)

        blockers = read_blockers(session_id)
        resolved_blocker = next(b for b in blockers if b["blocker_id"] == "block-002")
        assert resolved_blocker["resolved"] is True
        assert resolved_blocker["resolution"]["guidance"] == "Do this instead"


class TestPolling:
    def test_poll_for_resolution_found(self, tmp_collab_root):
        from collaborative import (
            write_blocker,
            resolve_blocker,
            poll_for_resolution,
            Blocker,
        )

        session_id = "test-session-007"
        blocker = Blocker(
            blocker_id="block-003",
            reported_at="2026-03-24T12:00:00Z",
            description="Test blocker",
        )
        write_blocker(session_id, blocker)
        resolve_blocker(session_id, "block-003", {"solution": "xyz"})

        # Poll with short timeout since already resolved
        resolution = poll_for_resolution(session_id, "block-003", timeout_seconds=1)
        assert resolution is not None
        assert resolution["solution"] == "xyz"

    def test_poll_for_resolution_timeout(self, tmp_collab_root):
        from collaborative import poll_for_resolution

        session_id = "test-session-008"
        # No blocker created, so poll should timeout
        resolution = poll_for_resolution(session_id, "block-999", timeout_seconds=1)
        assert resolution is None


class TestSessionStatus:
    def test_update_session_status(self, tmp_collab_root):
        from collaborative import (
            start_collaborative,
            update_session_status,
            get_session_status,
        )

        session = start_collaborative("Task", "node_gpu")
        assert get_session_status(session.session_id) == "active"

        update_session_status(session.session_id, "blocked")
        assert get_session_status(session.session_id) == "blocked"

        update_session_status(session.session_id, "completed")
        assert get_session_status(session.session_id) == "completed"

    def test_get_status_missing_session(self, tmp_collab_root):
        from collaborative import get_session_status

        result = get_session_status("nonexistent")
        assert result is None


class TestListSessions:
    def test_list_empty(self, tmp_collab_root):
        from collaborative import list_sessions

        sessions = list_sessions()
        assert sessions == []

    def test_list_multiple_sessions(self, tmp_collab_root):
        import time
        from collaborative import start_collaborative, list_sessions

        s1 = start_collaborative("Task 1", "node_gpu", "node_primary")
        time.sleep(0.01)  # Ensure different timestamps
        s2 = start_collaborative("Task 2", "node_gpu", "node_primary")

        sessions = list_sessions()
        assert len(sessions) == 2
        session_ids = {s["session_id"] for s in sessions}
        assert s1.session_id in session_ids
        assert s2.session_id in session_ids


class TestCleanup:
    def test_cleanup_session(self, tmp_collab_root):
        from collaborative import start_collaborative, cleanup_session, list_sessions

        session = start_collaborative("Task", "node_gpu")
        assert len(list_sessions()) == 1

        cleanup_session(session.session_id)
        assert len(list_sessions()) == 0

    def test_cleanup_missing_session(self, tmp_collab_root):
        from collaborative import cleanup_session

        # Should not raise
        cleanup_session("nonexistent-session")


class TestBlockerDataclass:
    def test_blocker_to_dict(self):
        from collaborative import Blocker

        blocker = Blocker(
            blocker_id="b1",
            reported_at="2026-03-24T12:00:00Z",
            description="Test",
            context={"key": "value"},
            resolution={"fix": "applied"},
            resolved=True,
        )
        d = blocker.to_dict()
        assert d["blocker_id"] == "b1"
        assert d["context"] == {"key": "value"}
        assert d["resolved"] is True


class TestCollaborativeSessionDataclass:
    def test_session_defaults(self):
        from collaborative import CollaborativeSession

        session = CollaborativeSession(
            session_id="test-123",
            orchestrator_host="node_primary",
            worker_host="node_gpu",
            task="Test task",
        )
        assert session.status == "active"
        assert session.created_at != ""
        assert session.updated_at != ""
        assert session.context_dir == Path("/opt/swarm/collaborative/test-123")
