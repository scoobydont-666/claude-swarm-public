"""Tests for the orchestrator — task queue, dispatch, work generation."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def tmp_queue_dir(tmp_path):
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    with (
        patch("orchestrator.QUEUE_DIR", queue_dir),
        patch("orchestrator.SWARM_ROOT", tmp_path),
        patch("events.EVENTS_DIR", events_dir),
    ):
        yield queue_dir


class TestCreateTask:
    def test_creates_yaml(self, tmp_queue_dir):
        from orchestrator import create_task

        task = create_task("Test task", "/opt/test", priority="P1")
        assert task["state"] == "pending"
        assert task["priority"] == "P1"
        path = tmp_queue_dir / f"{task['id']}.yaml"
        assert path.exists()

    def test_increments_id(self, tmp_queue_dir):
        from orchestrator import create_task

        t1 = create_task("First", "/opt/a")
        t2 = create_task("Second", "/opt/b")
        assert t1["id"] != t2["id"]


class TestClaimTask:
    def test_claim_pending(self, tmp_queue_dir):
        from orchestrator import create_task, claim_task

        task = create_task("Claimable", "/opt/test")
        claimed = claim_task(task["id"], "gpu-server-1-123")
        assert claimed is not None
        assert claimed["state"] == "claimed"
        assert claimed["claimed_by"] == "gpu-server-1-123"

    def test_claim_already_claimed(self, tmp_queue_dir):
        from orchestrator import create_task, claim_task

        task = create_task("Taken", "/opt/test")
        claim_task(task["id"], "gpu-server-1-123")
        second = claim_task(task["id"], "orchestration-node-456")
        assert second is None


class TestCompleteTask:
    def test_complete(self, tmp_queue_dir):
        from orchestrator import create_task, claim_task, complete_task

        task = create_task("Doable", "/opt/test")
        claim_task(task["id"], "gpu-server-1-123")
        done = complete_task(task["id"], result={"output": "success"})
        assert done["state"] == "done"
        assert done["result"]["output"] == "success"


class TestFindBestTask:
    def test_matches_capabilities(self, tmp_queue_dir):
        from orchestrator import create_task, find_best_task
        from registry import AgentInfo

        create_task("GPU task", "/opt/test", requires=["gpu", "ollama"])
        create_task("CPU task", "/opt/test2", requires=[])

        cpu_agent = AgentInfo(
            hostname="orchestration-node",
            pid=1,
            capabilities={"gpu": False, "ollama": False, "docker": True},
        )
        best = find_best_task(cpu_agent)
        assert best is not None
        assert best["title"] == "CPU task"

    def test_respects_priority(self, tmp_queue_dir):
        from orchestrator import create_task, find_best_task
        from registry import AgentInfo

        create_task("Low priority", "/opt/a", priority="P4")
        create_task("High priority", "/opt/b", priority="P0")

        agent = AgentInfo(hostname="test", pid=1, capabilities={})
        best = find_best_task(agent)
        assert best["title"] == "High priority"

    def test_no_matching_task(self, tmp_queue_dir):
        from orchestrator import create_task, find_best_task
        from registry import AgentInfo

        create_task("Needs GPU", "/opt/test", requires=["gpu"])

        cpu_agent = AgentInfo(
            hostname="orchestration-node",
            pid=1,
            capabilities={"gpu": False},
        )
        assert find_best_task(cpu_agent) is None


class TestListTasks:
    def test_list_by_state(self, tmp_queue_dir):
        from orchestrator import create_task, claim_task, list_tasks

        t1 = create_task("Pending", "/opt/a")
        t2 = create_task("Also pending", "/opt/b")
        claim_task(t1["id"], "agent-1")

        pending = list_tasks(state="pending")
        assert len(pending) == 1
        assert pending[0]["title"] == "Also pending"

        claimed = list_tasks(state="claimed")
        assert len(claimed) == 1
