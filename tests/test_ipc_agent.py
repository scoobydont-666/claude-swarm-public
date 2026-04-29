"""Tests for IPC agent registration and presence."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def fake_redis():
    """Provide a fakeredis instance and patch redis_client."""
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    with patch("ipc.transport.get_client", return_value=fake):
        # Reset module state between tests
        import ipc.agent as agent_mod
        agent_mod._current_agent_id = None
        agent_mod._heartbeat_thread = None
        yield fake


class TestRegistration:
    def test_register_creates_agent(self, fake_redis):
        from ipc.agent import register, get_current_agent_id

        agent_id = register(
            project="/opt/test",
            model="opus-4-6",
            hostname="testhost",
            pid=12345,
            auto_heartbeat=False,
        )

        assert agent_id == "testhost:12345:0000"
        assert get_current_agent_id() == agent_id

        # Verify Redis state
        data = fake_redis.hgetall(f"ipc:agent:{agent_id}")
        assert data["hostname"] == "testhost"
        assert data["pid"] == "12345"
        assert data["project"] == "/opt/test"
        assert data["model"] == "opus-4-6"
        assert data["status"] == "online"

        # Verify indexes
        assert fake_redis.sismember("ipc:agents:index", agent_id)
        assert fake_redis.sismember("ipc:agents:project:/opt/test", agent_id)

        # Verify inbox stream + consumer group exist
        groups = fake_redis.xinfo_groups(f"ipc:inbox:{agent_id}")
        assert len(groups) == 1
        assert groups[0]["name"] == "reader"

    def test_register_with_session_id(self, fake_redis):
        from ipc.agent import register

        with patch.dict(os.environ, {"CLAUDE_SESSION_ID": "abcdef1234"}):
            agent_id = register(hostname="host", pid=1, auto_heartbeat=False)
        assert agent_id == "host:1:abcd"

    def test_deregister_cleans_up(self, fake_redis):
        from ipc.agent import register, deregister, get_current_agent_id

        agent_id = register(
            project="/opt/test", hostname="host", pid=1, auto_heartbeat=False
        )
        assert get_current_agent_id() == agent_id

        deregister(agent_id)

        assert get_current_agent_id() is None
        assert not fake_redis.exists(f"ipc:agent:{agent_id}")
        assert not fake_redis.sismember("ipc:agents:index", agent_id)
        assert not fake_redis.sismember("ipc:agents:project:/opt/test", agent_id)

    def test_deregister_preserves_inbox(self, fake_redis):
        from ipc.agent import register, deregister

        agent_id = register(hostname="host", pid=1, auto_heartbeat=False)
        inbox_key = f"ipc:inbox:{agent_id}"

        # Add a message to inbox
        fake_redis.xadd(inbox_key, {"envelope": "test"})

        deregister(agent_id)

        # Inbox stream should still exist
        assert fake_redis.xlen(inbox_key) == 1


class TestHeartbeat:
    def test_refresh_updates_ttl(self, fake_redis):
        from ipc.agent import register, refresh_heartbeat

        agent_id = register(hostname="host", pid=1, auto_heartbeat=False)
        time.sleep(0.1)
        refresh_heartbeat(agent_id)

        hb = float(fake_redis.hget(f"ipc:agent:{agent_id}", "last_heartbeat"))
        assert time.time() - hb < 1

    def test_refresh_unregistered_returns_false(self, fake_redis):
        from ipc.agent import refresh_heartbeat

        assert refresh_heartbeat("nonexistent:0:0000") is False


class TestPresence:
    def test_list_agents(self, fake_redis):
        from ipc.agent import register, list_agents

        register(hostname="a", pid=1, auto_heartbeat=False)
        register(hostname="b", pid=2, auto_heartbeat=False)

        agents = list_agents()
        assert len(agents) == 2
        hostnames = {a["hostname"] for a in agents}
        assert hostnames == {"a", "b"}

    def test_list_agents_by_project(self, fake_redis):
        from ipc.agent import register, list_agents

        # Reset between registrations
        import ipc.agent as mod
        mod._current_agent_id = None
        register(project="/opt/a", hostname="x", pid=1, auto_heartbeat=False)
        mod._current_agent_id = None
        register(project="/opt/b", hostname="y", pid=2, auto_heartbeat=False)

        a_agents = list_agents(project="/opt/a")
        assert len(a_agents) == 1
        assert a_agents[0]["hostname"] == "x"

    def test_cleanup_stale(self, fake_redis):
        from ipc.agent import register, cleanup_stale

        agent_id = register(hostname="host", pid=1, auto_heartbeat=False)
        # Delete the agent hash (simulating TTL expiry)
        fake_redis.delete(f"ipc:agent:{agent_id}")

        stale = cleanup_stale()
        assert agent_id in stale
        assert not fake_redis.sismember("ipc:agents:index", agent_id)

    def test_get_agent(self, fake_redis):
        from ipc.agent import register, get_agent

        agent_id = register(
            hostname="host", pid=1, model="sonnet", auto_heartbeat=False
        )
        data = get_agent(agent_id)
        assert data is not None
        assert data["model"] == "sonnet"
        assert data["agent_id"] == agent_id

    def test_get_nonexistent_agent(self, fake_redis):
        from ipc.agent import get_agent

        assert get_agent("nope:0:0000") is None

    def test_update_status(self, fake_redis):
        from ipc.agent import register, update_status, get_agent

        agent_id = register(hostname="host", pid=1, auto_heartbeat=False)
        update_status(agent_id, status="busy", project="/opt/new")

        data = get_agent(agent_id)
        assert data["status"] == "busy"
        assert data["project"] == "/opt/new"
