"""Tests for session lifecycle management."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def mock_registry():
    """Mock registry functions to avoid touching real swarm files."""
    from registry import AgentInfo

    agent = AgentInfo(
        hostname="node_primary",
        pid=12345,
        state="idle",
        project="/opt/test",
        model="sonnet",
        capabilities={"gpu": False},
    )
    with (
        patch("session.register", return_value=agent) as mock_reg,
        patch("session.deregister") as mock_dereg,
        patch("session.update_agent") as mock_update,
        patch("session.HeartbeatThread") as mock_hb,
    ):
        mock_hb_instance = MagicMock()
        mock_hb.return_value = mock_hb_instance
        yield {
            "register": mock_reg,
            "deregister": mock_dereg,
            "update_agent": mock_update,
            "heartbeat_class": mock_hb,
            "heartbeat_instance": mock_hb_instance,
            "agent": agent,
        }


@pytest.fixture
def mock_events():
    with (
        patch("session.emit") as mock_emit,
        patch("session.since_last_session", return_value=[]) as mock_since,
        patch("session.summarize_since", return_value={}) as mock_summarize,
    ):
        yield {
            "emit": mock_emit,
            "since_last_session": mock_since,
            "summarize_since": mock_summarize,
        }


@pytest.fixture
def mock_sync():
    with (
        patch("session.pull_all_projects", return_value={}) as mock_pull,
        patch("session.push_all_dirty", return_value={}) as mock_push,
        patch("session.sync_config", return_value={"collect": True}) as mock_config,
        patch("session.get_dirty_repos", return_value=[]) as mock_dirty,
    ):
        yield {
            "pull": mock_pull,
            "push": mock_push,
            "config": mock_config,
            "dirty": mock_dirty,
        }


class TestSwarmSessionStart:
    def test_registers_agent(self, mock_registry, mock_events, mock_sync):
        from session import SwarmSession

        session = SwarmSession()
        result = session.start(model="sonnet", project="/opt/test")
        mock_registry["register"].assert_called_once_with(
            model="sonnet", project="/opt/test", session_context=""
        )
        assert result["agent_id"] == "node_primary-12345"

    def test_starts_heartbeat(self, mock_registry, mock_events, mock_sync):
        from session import SwarmSession

        session = SwarmSession()
        session.start()
        mock_registry["heartbeat_instance"].start.assert_called_once()

    def test_emits_session_start_event(self, mock_registry, mock_events, mock_sync):
        from session import SwarmSession

        session = SwarmSession()
        session.start(model="opus", project="/opt/examforge")
        mock_events["emit"].assert_called_once()
        args = mock_events["emit"].call_args
        assert args[0][0] == "session_start"

    def test_pulls_repos(self, mock_registry, mock_events, mock_sync):
        from session import SwarmSession

        session = SwarmSession()
        session.start()
        mock_sync["pull"].assert_called_once()

    def test_returns_catchup_info(self, mock_registry, mock_events, mock_sync):
        from session import SwarmSession

        session = SwarmSession()
        result = session.start()
        assert "agent_id" in result
        assert "events_since_last_session" in result
        assert "repos_pulled" in result


class TestSwarmSessionEnd:
    def test_pushes_dirty_repos(self, mock_registry, mock_events, mock_sync, tmp_path):
        from session import SwarmSession

        with patch("session.SUMMARIES_DIR", tmp_path):
            session = SwarmSession()
            session.start()
            mock_events["emit"].reset_mock()
            session.end()
        mock_sync["push"].assert_called_once()

    def test_syncs_config(self, mock_registry, mock_events, mock_sync, tmp_path):
        from session import SwarmSession

        with patch("session.SUMMARIES_DIR", tmp_path):
            session = SwarmSession()
            session.start()
            session.end()
        mock_sync["config"].assert_called_once()

    def test_writes_summary_yaml(self, mock_registry, mock_events, mock_sync, tmp_path):
        from session import SwarmSession

        with patch("session.SUMMARIES_DIR", tmp_path):
            session = SwarmSession()
            session.start()
            result = session.end()
        assert "summary_path" in result
        assert Path(result["summary_path"]).exists()

    def test_deregisters_agent(self, mock_registry, mock_events, mock_sync, tmp_path):
        from session import SwarmSession

        with patch("session.SUMMARIES_DIR", tmp_path):
            session = SwarmSession()
            session.start()
            session.end()
        mock_registry["deregister"].assert_called_once()

    def test_stops_heartbeat(self, mock_registry, mock_events, mock_sync, tmp_path):
        from session import SwarmSession

        with patch("session.SUMMARIES_DIR", tmp_path):
            session = SwarmSession()
            session.start()
            session.end()
        mock_registry["heartbeat_instance"].stop.assert_called()

    def test_end_without_start_returns_no_session(self):
        from session import SwarmSession

        session = SwarmSession()
        result = session.end()
        assert result == {"status": "no active session"}


class TestSwarmSessionUpdate:
    def test_update_delegates_to_registry(self, mock_registry, mock_events, mock_sync):
        from session import SwarmSession

        session = SwarmSession()
        session.start()
        session.update(project="/opt/new-project", state="working")
        mock_registry["update_agent"].assert_called_once()

    def test_update_without_start_is_noop(self):
        from session import SwarmSession

        session = SwarmSession()
        session.update(project="/opt/test")  # Should not raise


class TestSwarmSessionCleanup:
    def test_cleanup_is_idempotent(
        self, mock_registry, mock_events, mock_sync, tmp_path
    ):
        from session import SwarmSession

        with patch("session.SUMMARIES_DIR", tmp_path):
            session = SwarmSession()
            session.start()
            session._cleanup()
            session._cleanup()  # Second call should not raise
        assert session.agent is None
        assert session.heartbeat is None


class TestSwarmSessionElapsed:
    def test_elapsed_returns_int(self, mock_registry, mock_events, mock_sync):
        from session import SwarmSession

        session = SwarmSession()
        session.start()
        elapsed = session._elapsed()
        assert isinstance(elapsed, int)
        assert elapsed >= 0

    def test_elapsed_without_start(self):
        from session import SwarmSession

        session = SwarmSession()
        assert session._elapsed() == 0


class TestModuleLevelFunctions:
    def test_start_and_end_session(
        self, mock_registry, mock_events, mock_sync, tmp_path
    ):
        from session import start_session, end_session, get_session

        with patch("session.SUMMARIES_DIR", tmp_path):
            result = start_session(model="sonnet")
            assert get_session() is not None
            end_result = end_session()
            assert get_session() is None

    def test_end_session_without_start(self):
        from session import end_session

        with patch("session._session", None):
            result = end_session()
        assert result == {"status": "no active session"}
