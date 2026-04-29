"""Tests for Redis-backed registry, events, and GPU slots modules."""

import os
import sys

import fakeredis
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from src import redis_client


@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    """Replace real Redis with fakeredis."""
    fake_server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    monkeypatch.setattr(redis_client, "get_client", lambda: fake_client)
    monkeypatch.setattr(redis_client, "health_check", lambda: True)

    # Skip Redis health check at import time
    monkeypatch.setenv("SWARM_REDIS_SKIP_CHECK", "1")
    from src import events_redis, gpu_slots_redis, registry_redis

    monkeypatch.setattr(registry_redis._rc, "get_client", lambda: fake_client)
    monkeypatch.setattr(registry_redis._rc, "health_check", lambda: True)
    monkeypatch.setattr(events_redis._rc, "get_client", lambda: fake_client)
    monkeypatch.setattr(events_redis._rc, "health_check", lambda: True)
    monkeypatch.setattr(gpu_slots_redis._rc, "get_client", lambda: fake_client)
    monkeypatch.setattr(gpu_slots_redis._rc, "health_check", lambda: True)
    yield fake_server


@pytest.fixture(autouse=True)
def mock_util(monkeypatch):
    from src import events_redis, gpu_slots_redis, registry_redis

    monkeypatch.setattr(registry_redis, "hostname", lambda: "test-host")
    monkeypatch.setattr(registry_redis, "now_iso", lambda: "2026-04-01T00:00:00Z")
    monkeypatch.setattr(events_redis, "hostname", lambda: "test-host")
    monkeypatch.setattr(events_redis, "now_iso", lambda: "2026-04-01T00:00:00Z")
    monkeypatch.setattr(gpu_slots_redis, "hostname", lambda: "test-host")


# -----------------------------------------------------------------------
# Registry tests
# -----------------------------------------------------------------------


class TestRegistry:
    def test_register_and_list(self):
        from src.registry_redis import list_agents, register

        agent = register(model="opus", project="hydra")
        assert agent.hostname == "test-host"
        agents = list_agents()
        assert len(agents) >= 1

    def test_deregister(self):
        from src.registry_redis import deregister, list_agents, register

        agent = register()
        deregister(agent)
        agents = list_agents()
        assert len(agents) == 0

    def test_heartbeat(self):
        from src.registry_redis import heartbeat, register

        agent = register()
        heartbeat(agent)  # Should not raise

    def test_update_agent(self):
        from src.registry_redis import register, update_agent

        agent = register()
        update_agent(agent, state="busy", task_id="task-001")
        assert agent.state == "busy"
        assert agent.task_id == "task-001"

    def test_get_live_agents(self):
        from src.registry_redis import get_live_agents, register

        register()
        live = get_live_agents()
        assert len(live) >= 1

    def test_no_stale_agents(self):
        from src.registry_redis import get_stale_agents

        assert get_stale_agents() == []

    def test_heartbeat_thread(self):
        from src.registry_redis import HeartbeatThread, register

        agent = register()
        hb = HeartbeatThread(agent, interval=1)
        hb.start()
        hb.stop()  # Should not raise


# -----------------------------------------------------------------------
# Events tests
# -----------------------------------------------------------------------


class TestEvents:
    def test_emit_and_query(self):
        from src.events_redis import emit, query

        emit("test_event", project="hydra", details={"key": "value"})
        events = query()
        assert len(events) >= 1
        assert events[0]["type"] == "test_event"

    def test_emit_commit(self):
        from src.events_redis import emit_commit, query

        emit_commit("hydra", "abc123", "fix bug", files_changed=3)
        events = query(event_type="commit")
        assert len(events) == 1

    def test_emit_test_result(self):
        from src.events_redis import emit_test_result, query

        emit_test_result("hydra", passed=10, failed=2, total=12)
        events = query(event_type="test_result")
        assert len(events) == 1

    def test_summarize(self):
        from src.events_redis import emit, emit_commit, summarize_since

        emit("session_start", project="hydra")
        emit_commit("hydra", "abc", "msg")
        summary = summarize_since()
        assert summary["event_count"] >= 2
        assert len(summary["commits"]) >= 1

    def test_rotate(self):
        from src.events_redis import emit, rotate

        for i in range(10):
            emit("test", details={"i": i})
        rotate(max_files=5)
        # Should not raise


# -----------------------------------------------------------------------
# GPU Slots tests
# -----------------------------------------------------------------------


class TestGPUSlots:
    def test_claim_and_release(self):
        from src.gpu_slots_redis import claim_slot, is_slot_available, release_slot

        assert is_slot_available(0)
        assert claim_slot(0)
        assert not is_slot_available(0)
        assert release_slot(0)
        assert is_slot_available(0)

    def test_double_claim_fails(self):
        from src.gpu_slots_redis import claim_slot

        assert claim_slot(0)
        assert not claim_slot(0)

    def test_get_slot_status(self):
        from src.gpu_slots_redis import claim_slot, get_slot_status

        claim_slot(0)
        status = get_slot_status()
        assert len(status) >= 1
        assert status[0]["gpu_id"] == 0

    def test_setup_ollama(self):
        from src.gpu_slots_redis import is_slot_available, setup_ollama_slot

        assert setup_ollama_slot()
        assert not is_slot_available(0)
