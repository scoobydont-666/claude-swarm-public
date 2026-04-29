"""Tests for conflict detection and GPU resource arbitration."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def tmp_gpu_slots(tmp_path):
    slots_file = tmp_path / "gpu_slots.json"
    with patch("conflicts.GPU_SLOTS_FILE", slots_file):
        yield slots_file


@pytest.fixture
def mock_agents():
    """Create mock agents for conflict testing."""
    from registry import AgentInfo

    agents = [
        AgentInfo(
            hostname="node_primary",
            pid=1001,
            state="working",
            project="/opt/examforge",
            model="sonnet",
            capabilities={},
        ),
        AgentInfo(
            hostname="node_gpu",
            pid=2001,
            state="working",
            project="<project-a-path>",
            model="opus",
            capabilities={},
        ),
        AgentInfo(
            hostname="node_primary",
            pid=1002,
            state="idle",
            project="/opt/examforge",
            model="haiku",
            capabilities={},
        ),
    ]
    return agents


class TestGetWorkingAgentsByProject:
    def test_groups_by_project(self, mock_agents):
        with patch("conflicts.list_agents", return_value=mock_agents):
            from conflicts import get_working_agents_by_project

            result = get_working_agents_by_project()
        assert "/opt/examforge" in result
        assert "<project-a-path>" in result
        # agent-3 is idle, shouldn't be included
        assert len(result["/opt/examforge"]) == 1
        assert result["/opt/examforge"][0].agent_id == "node_primary-1001"

    def test_empty_when_no_agents(self):
        with patch("conflicts.list_agents", return_value=[]):
            from conflicts import get_working_agents_by_project

            result = get_working_agents_by_project()
        assert result == {}


class TestCheckProjectConflict:
    def test_no_conflict_on_free_project(self, mock_agents):
        with patch("conflicts.list_agents", return_value=mock_agents):
            from conflicts import check_project_conflict

            result = check_project_conflict("/opt/documint")
        assert result["conflict"] is False
        assert result["safe"] is True
        assert result["agents"] == []

    def test_conflict_detected(self, mock_agents):
        with patch("conflicts.list_agents", return_value=mock_agents):
            from conflicts import check_project_conflict

            result = check_project_conflict("/opt/examforge", my_agent_id="other-99")
        assert result["conflict"] is True
        assert "node_primary-1001" in result["agents"]

    def test_no_self_conflict(self, mock_agents):
        with patch("conflicts.list_agents", return_value=mock_agents):
            from conflicts import check_project_conflict

            result = check_project_conflict(
                "/opt/examforge", my_agent_id="node_primary-1001"
            )
        assert result["conflict"] is False
        assert result["safe"] is True


class TestGetChangedFiles:
    def test_returns_set(self):
        from conflicts import get_changed_files

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="file1.py\nfile2.py\n")
            result = get_changed_files("/opt/test")
        assert isinstance(result, set)
        assert "file1.py" in result

    def test_handles_exception(self):
        from conflicts import get_changed_files

        with patch("subprocess.run", side_effect=Exception("git not found")):
            result = get_changed_files("/nonexistent")
        assert result == set()

    def test_combines_staged_and_unstaged(self):
        from conflicts import get_changed_files

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="unstaged.py\n"),
                MagicMock(stdout="staged.py\n"),
            ]
            result = get_changed_files("/opt/test")
        assert result == {"unstaged.py", "staged.py"}


class TestCheckFileConflict:
    def test_finds_overlapping_files(self):
        from conflicts import check_file_conflict

        with patch(
            "conflicts.get_changed_files", return_value={"shared.py", "other.py"}
        ):
            result = check_file_conflict("/opt/a", {"shared.py", "mine.py"}, "/opt/b")
        assert result == ["shared.py"]

    def test_no_overlap(self):
        from conflicts import check_file_conflict

        with patch("conflicts.get_changed_files", return_value={"their.py"}):
            result = check_file_conflict("/opt/a", {"mine.py"}, "/opt/b")
        assert result == []


class TestGPUSlotManagement:
    def test_claim_empty_slot(self, tmp_gpu_slots):
        from conflicts import claim_gpu_slot

        with patch("conflicts.SWARM_ROOT", tmp_gpu_slots.parent):
            result = claim_gpu_slot(0, "agent-1", "llama3")
        assert result is True
        data = json.loads(tmp_gpu_slots.read_text())
        assert data["slots"]["gpu-0"]["agent_id"] == "agent-1"
        assert data["slots"]["gpu-0"]["model"] == "llama3"

    def test_claim_slot_held_by_live_agent(self, tmp_gpu_slots):
        from conflicts import claim_gpu_slot, _write_gpu_slots

        # Pre-claim slot
        _write_gpu_slots({"slots": {"gpu-0": {"agent_id": "test-1001"}}})
        # Try to claim with different agent while original is alive
        from registry import AgentInfo

        live_agent = AgentInfo(hostname="test", pid=1001, state="working")
        with patch("registry.get_live_agents", return_value=[live_agent]):
            result = claim_gpu_slot(0, "other-2002")
        assert result is False

    def test_claim_slot_held_by_dead_agent(self, tmp_gpu_slots):
        from conflicts import claim_gpu_slot, _write_gpu_slots

        _write_gpu_slots({"slots": {"gpu-0": {"agent_id": "dead-9999"}}})
        with patch("registry.get_live_agents", return_value=[]):
            result = claim_gpu_slot(0, "agent-2")
        assert result is True
        data = json.loads(tmp_gpu_slots.read_text())
        assert data["slots"]["gpu-0"]["agent_id"] == "agent-2"

    def test_release_slot(self, tmp_gpu_slots):
        from conflicts import claim_gpu_slot, release_gpu_slot

        claim_gpu_slot(0, "agent-1")
        release_gpu_slot(0, "agent-1")
        data = json.loads(tmp_gpu_slots.read_text())
        assert "gpu-0" not in data["slots"]

    def test_release_wrong_agent_noop(self, tmp_gpu_slots):
        from conflicts import claim_gpu_slot, release_gpu_slot

        claim_gpu_slot(0, "agent-1")
        release_gpu_slot(0, "agent-2")  # Wrong agent
        data = json.loads(tmp_gpu_slots.read_text())
        assert data["slots"]["gpu-0"]["agent_id"] == "agent-1"

    def test_get_gpu_status_empty(self, tmp_gpu_slots):
        from conflicts import get_gpu_status

        result = get_gpu_status()
        assert result == {"slots": {}}

    def test_get_gpu_status_with_claims(self, tmp_gpu_slots):
        from conflicts import claim_gpu_slot, get_gpu_status

        claim_gpu_slot(0, "agent-1", "ollama")
        result = get_gpu_status()
        assert "gpu-0" in result["slots"]
