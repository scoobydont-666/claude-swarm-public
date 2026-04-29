"""Tests for IPC dead-letter queue."""

import sys
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def fake_redis():
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    with patch("ipc.transport.get_client", return_value=fake):
        import ipc.agent as agent_mod

        agent_mod._current_agent_id = None
        agent_mod._heartbeat_thread = None
        yield fake


class TestDLQ:
    def test_list_empty_dlq(self, fake_redis):
        from ipc.dlq import list_dlq

        assert list_dlq() == []

    def test_dlq_depth(self, fake_redis):
        from ipc.dlq import dlq_depth

        assert dlq_depth() == 0

    def test_send_to_missing_agent_populates_dlq(self, fake_redis):
        from ipc.agent import register
        from ipc.direct import send
        from ipc.dlq import dlq_depth, list_dlq

        register(hostname="host", pid=1, auto_heartbeat=False)
        send("nobody:0:0000", "test message")

        assert dlq_depth() == 1
        entries = list_dlq()
        assert len(entries) == 1
        assert entries[0]["reason"] == "recipient_not_found"
        assert entries[0]["envelope"].payload == {"text": "test message"}

    def test_requeue_from_dlq(self, fake_redis):
        import ipc.agent as agent_mod
        from ipc.agent import register
        from ipc.direct import recv, send
        from ipc.dlq import list_dlq, requeue

        # Agent A sends to nonexistent B
        register(hostname="a", pid=1, auto_heartbeat=False)
        send("nobody:0:0000", "test")

        # Now register the real target
        agent_mod._current_agent_id = None
        id_b = register(hostname="b", pid=2, auto_heartbeat=False)

        # Requeue to B
        entries = list_dlq()
        ok = requeue(entries[0]["stream_id"], new_recipient=id_b)
        assert ok is True

        # B can now receive it
        agent_mod._current_agent_id = id_b
        msgs = recv(agent_id=id_b)
        assert len(msgs) == 1
        assert msgs[0].payload == {"text": "test"}

    def test_purge_old_entries(self, fake_redis):
        from ipc.dlq import dlq_depth, purge
        from ipc.envelope import Envelope

        # Add an entry with old stream ID
        env = Envelope(sender="a", recipient="b", message_type="direct")
        # Use a very old timestamp (1 second since epoch)
        fake_redis.xadd("ipc:dlq", {"envelope": env.to_json(), "reason": "test"})

        assert dlq_depth() == 1
        # Purge entries older than 0 seconds (everything)
        purged = purge(older_than_seconds=0)
        assert purged == 1
        assert dlq_depth() == 0
