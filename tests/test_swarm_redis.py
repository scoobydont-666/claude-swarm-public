"""Tests for Redis-backed swarm operations (swarm_redis.py).

Uses fakeredis — no real Redis needed.
"""

import os
import sys

import fakeredis
import pytest

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from src import redis_client, swarm_redis


@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    """Replace real Redis with fakeredis."""
    fake_server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    # Patch both the test-imported module AND the one swarm_redis uses (may be different objects)
    monkeypatch.setattr(redis_client, "get_client", lambda: fake_client)
    monkeypatch.setattr(redis_client, "health_check", lambda: True)
    monkeypatch.setattr(swarm_redis._rc, "get_client", lambda: fake_client)
    monkeypatch.setattr(swarm_redis._rc, "health_check", lambda: True)
    # Reset cached state
    monkeypatch.setattr(swarm_redis, "_USE_REDIS", None)
    yield fake_server


@pytest.fixture(autouse=True)
def mock_util(monkeypatch):
    """Mock util functions."""
    monkeypatch.setattr(swarm_redis, "hostname", lambda: "test-host")
    monkeypatch.setattr(swarm_redis, "now_iso", lambda: "2026-04-01T00:00:00Z")


class TestTaskOperations:
    def test_create_task(self):
        from src.swarm_redis import create_task

        task = create_task(
            "Build feature",
            description="Do the thing",
            project="hydra",
            priority="high",
        )
        assert task["title"] == "Build feature"
        assert task["project"] == "hydra"
        assert task["priority"] == "high"
        assert task["id"].startswith("task-")

    def test_create_and_list(self):
        from src.swarm_redis import create_task, list_tasks

        create_task("Task A", priority="high")
        create_task("Task B", priority="low")
        pending = list_tasks("pending")
        assert len(pending) == 2

    def test_claim_task(self):
        from src.swarm_redis import claim_task, create_task, list_tasks

        task = create_task("Claimable task")
        claimed = claim_task(task["id"])
        assert claimed["claimed_by"] == "test-host"
        assert len(list_tasks("pending")) == 0
        assert len(list_tasks("claimed")) == 1

    def test_claim_nonexistent_raises(self):
        from src.swarm_redis import claim_task

        with pytest.raises(FileNotFoundError):
            claim_task("task-9999")

    def test_claim_next_task(self):
        from src.swarm_redis import claim_next_task, create_task

        create_task("Low prio", priority="low")
        create_task("High prio", priority="high")
        result = claim_next_task()
        assert result is not None
        assert result["title"] == "High prio"

    def test_claim_next_empty(self):
        from src.swarm_redis import claim_next_task

        assert claim_next_task() is None

    def test_complete_task(self):
        from src.swarm_redis import claim_task, complete_task, create_task, list_tasks

        task = create_task("Completable")
        claim_task(task["id"])
        complete_task(task["id"], result_artifact="/tmp/output.txt")
        assert len(list_tasks("completed")) == 1
        assert len(list_tasks("claimed")) == 0

    def test_get_matching_tasks(self):
        from src.swarm_redis import create_task, get_matching_tasks

        create_task("GPU work", requires=["gpu"])
        create_task("CPU work", requires=[])
        matched = get_matching_tasks(["cpu"])
        assert len(matched) == 1
        assert matched[0]["title"] == "CPU work"

    def test_get_matching_tasks_with_gpu(self):
        from src.swarm_redis import create_task, get_matching_tasks

        create_task("GPU work", requires=["gpu"])
        create_task("CPU work", requires=[])
        matched = get_matching_tasks(["gpu", "cpu"])
        assert len(matched) == 2


class TestStatusOperations:
    def test_update_and_get_status(self):
        from src.swarm_redis import get_status, update_status

        update_status(state="busy", task_id="task-001")
        status = get_status("test-host")
        assert status is not None
        assert status["state"] == "busy"
        assert status["task_id"] == "task-001"

    def test_get_all_status(self):
        from src.swarm_redis import get_all_status, update_status

        update_status(state="idle")
        all_status = get_all_status()
        hostnames = [s.get("hostname", s.get("host", "")) for s in all_status]
        assert "test-host" in hostnames

    def test_health_check(self):
        from src.swarm_redis import create_task, health_check

        create_task("Pending task")
        hc = health_check()
        assert hc["nfs_available"] is not None
        assert hc["pending_tasks"] >= 1


class TestMessaging:
    def test_send_and_read(self):
        from src.swarm_redis import read_inbox, send_message

        send_message("test-host", {"action": "deploy"})
        msgs = read_inbox("test-host")
        assert len(msgs) == 1
        assert msgs[0]["action"] == "deploy"

    def test_broadcast(self):
        from src.swarm_redis import broadcast_message, read_inbox

        broadcast_message({"action": "alert"})
        msgs = read_inbox("broadcast")
        assert len(msgs) == 1


class TestDecomposition:
    def test_decompose_task(self):
        from src.swarm_redis import create_task, decompose_task, list_tasks

        parent = create_task("Big task")
        subtasks = decompose_task(
            parent["id"],
            [
                {"title": "Sub A", "priority": "high"},
                {"title": "Sub B", "priority": "medium"},
            ],
        )
        assert len(subtasks) == 2
        assert subtasks[0]["id"].endswith("-a")
        assert subtasks[1]["id"].endswith("-b")
        # Parent should be gone from pending
        pending = list_tasks("pending")
        assert all(t["id"] != parent["id"] for t in pending)

    def test_check_parent_completion(self):
        from src.swarm_redis import (
            check_parent_completion,
            claim_task,
            complete_task,
            create_task,
            decompose_task,
        )

        parent = create_task("Parent")
        subtasks = decompose_task(
            parent["id"],
            [
                {"title": "Sub A"},
                {"title": "Sub B"},
            ],
        )
        # Complete both subtasks
        for sub in subtasks:
            claim_task(sub["id"])
            complete_task(sub["id"])
        assert check_parent_completion(parent["id"]) is True

    def test_parent_not_complete_if_subtask_pending(self):
        from src.swarm_redis import (
            check_parent_completion,
            claim_task,
            complete_task,
            create_task,
            decompose_task,
        )

        parent = create_task("Parent")
        subtasks = decompose_task(
            parent["id"],
            [
                {"title": "Sub A"},
                {"title": "Sub B"},
            ],
        )
        # Only complete one
        claim_task(subtasks[0]["id"])
        complete_task(subtasks[0]["id"])
        assert check_parent_completion(parent["id"]) is False


class TestArtifacts:
    def test_list_artifacts_empty(self):
        from src.swarm_redis import list_artifacts

        arts = list_artifacts()
        assert isinstance(arts, list)

    def test_session_summary(self):
        from src.swarm_redis import get_latest_summary_context

        ctx = get_latest_summary_context("hydra")
        assert isinstance(ctx, str)
