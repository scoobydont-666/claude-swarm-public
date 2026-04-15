"""Tests for Redis-backed task orchestration.

Uses fakeredis for isolation — no real Redis needed for unit tests.
Integration tests against real Redis are in test_redis_integration.py.
"""

import json

import fakeredis
import pytest

from src import redis_client


@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    """Replace real Redis with fakeredis for all tests."""
    fake_server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    monkeypatch.setattr(redis_client, "get_client", lambda: fake_client)
    yield fake_server


class TestTasks:
    def test_create_task(self):
        assert redis_client.create_task("t1", {"cmd": "build"}, priority=3)
        task = redis_client.get_task("t1")
        assert task["id"] == "t1"
        assert task["state"] == "pending"
        assert task["priority"] == "3"
        data = json.loads(task["data"])
        assert data["cmd"] == "build"

    def test_list_pending_tasks(self):
        redis_client.create_task("t1", {"a": 1}, priority=5)
        redis_client.create_task("t2", {"b": 2}, priority=1)
        tasks = redis_client.list_tasks("pending")
        assert len(tasks) == 2

    def test_claim_task_returns_highest_priority(self):
        redis_client.create_task("low", {"x": 1}, priority=10)
        redis_client.create_task("high", {"x": 2}, priority=1)
        claimed = redis_client.claim_task("worker-1")
        assert claimed == "high"
        task = redis_client.get_task("high")
        assert task["state"] == "claimed"
        assert task["claimed_by"] == "worker-1"

    def test_claim_empty_returns_none(self):
        assert redis_client.claim_task("worker-1") is None

    def test_complete_task(self):
        redis_client.create_task("t1", {"cmd": "test"})
        redis_client.claim_task("w1")
        redis_client.complete_task("t1", {"status": "ok"})
        task = redis_client.get_task("t1")
        assert task["state"] == "completed"
        result = json.loads(task["result"])
        assert result["status"] == "ok"

    def test_claim_removes_from_pending(self):
        redis_client.create_task("t1", {})
        redis_client.claim_task("w1")
        pending = redis_client.list_tasks("pending")
        assert len(pending) == 0
        claimed = redis_client.list_tasks("claimed")
        assert len(claimed) == 1

    def test_complete_removes_from_claimed(self):
        redis_client.create_task("t1", {})
        redis_client.claim_task("w1")
        redis_client.complete_task("t1")
        claimed = redis_client.list_tasks("claimed")
        assert len(claimed) == 0
        completed = redis_client.list_tasks("completed")
        assert len(completed) == 1


class TestAgentRegistry:
    def test_register_and_list(self):
        redis_client.register_agent("GIGA", 1234, {"gpu": True})
        agents = redis_client.list_agents()
        assert len(agents) == 1
        assert agents[0]["host"] == "GIGA"
        assert agents[0]["pid"] == "1234"

    def test_heartbeat_refreshes(self):
        redis_client.register_agent("GIGA", 1234)
        assert redis_client.heartbeat("GIGA", 1234)

    def test_heartbeat_unregistered_returns_false(self):
        assert not redis_client.heartbeat("GIGA", 9999)

    def test_unregister(self):
        redis_client.register_agent("GIGA", 1234)
        redis_client.unregister_agent("GIGA", 1234)
        agents = redis_client.list_agents()
        assert len(agents) == 0


class TestEvents:
    def test_emit_and_query(self):
        redis_client.emit_event("task_created", {"task_id": "t1"})
        redis_client.emit_event("task_claimed", {"task_id": "t1"})
        events = redis_client.query_events()
        assert len(events) == 2
        assert events[0]["type"] == "task_created"

    def test_query_by_type(self):
        redis_client.emit_event("task_created", {"task_id": "t1"})
        redis_client.emit_event("task_claimed", {"task_id": "t1"})
        events = redis_client.query_events(event_type="task_claimed")
        assert len(events) == 1

    def test_trim(self):
        for i in range(20):
            redis_client.emit_event("test", {"i": i})
        redis_client.trim_events(max_len=5)
        events = redis_client.query_events()
        assert len(events) <= 10  # approximate trimming


class TestGPUSlots:
    def test_claim_slot(self):
        assert redis_client.claim_gpu_slot(0, "worker-1")
        assert redis_client.gpu_slot_holder(0) == "worker-1"

    def test_double_claim_fails(self):
        redis_client.claim_gpu_slot(0, "worker-1")
        assert not redis_client.claim_gpu_slot(0, "worker-2")

    def test_release_slot(self):
        redis_client.claim_gpu_slot(0, "worker-1")
        assert redis_client.release_gpu_slot(0, "worker-1")
        assert redis_client.gpu_slot_holder(0) is None

    def test_release_wrong_holder_fails(self):
        redis_client.claim_gpu_slot(0, "worker-1")
        assert not redis_client.release_gpu_slot(0, "worker-2")
        assert redis_client.gpu_slot_holder(0) == "worker-1"


class TestMessaging:
    def test_send_and_read(self):
        redis_client.send_message("GIGA", {"action": "sync"})
        msgs = redis_client.read_inbox("GIGA")
        assert len(msgs) == 1
        assert msgs[0]["action"] == "sync"

    def test_read_pop(self):
        redis_client.send_message("GIGA", {"action": "sync"})
        msgs = redis_client.read_inbox("GIGA", pop=True)
        assert len(msgs) == 1
        # Inbox should be empty after pop
        assert redis_client.read_inbox("GIGA") == []

    def test_empty_inbox(self):
        assert redis_client.read_inbox("GIGA") == []


class TestStatus:
    def test_update_and_get(self):
        redis_client.update_status("GIGA", {"load": 0.5, "gpu_free": True})
        status = redis_client.get_status("GIGA")
        assert status is not None
        assert status["load"] == "0.5"
        assert status["gpu_free"] == "True"

    def test_missing_status(self):
        assert redis_client.get_status("nonexistent") is None


class TestHealthCheck:
    def test_health(self):
        assert redis_client.health_check()
