"""Tests for IPC direct messaging and broadcast."""

import sys
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def ipc_pair():
    """Set up two registered agents on a shared fake Redis."""
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)

    with patch("ipc.transport.get_client", return_value=fake):
        import ipc.agent as agent_mod

        # Register agent A
        agent_mod._current_agent_id = None
        agent_mod._heartbeat_thread = None
        id_a = agent_mod.register(
            hostname="host", pid=1, project="/opt/test", auto_heartbeat=False
        )

        # Register agent B
        agent_mod._current_agent_id = None
        id_b = agent_mod.register(
            hostname="host", pid=2, project="/opt/test", auto_heartbeat=False
        )

        yield fake, id_a, id_b


class TestDirectMessaging:
    def test_send_and_recv(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.direct import send, recv

        # Send from A to B
        agent_mod._current_agent_id = id_a
        delivered, env_id = send(id_b, {"text": "hello from A"})
        assert delivered is True
        assert env_id

        # Recv as B
        agent_mod._current_agent_id = id_b
        messages = recv(agent_id=id_b)
        assert len(messages) == 1
        assert messages[0].payload == {"text": "hello from A"}
        assert messages[0].sender == id_a

    def test_send_to_nonexistent_goes_to_dlq(self, ipc_pair):
        fake, id_a, _ = ipc_pair
        import ipc.agent as agent_mod
        from ipc.direct import send

        agent_mod._current_agent_id = id_a
        delivered, env_id = send("nobody:0:0000", "test")
        assert delivered is False

        # Check DLQ
        dlq_len = fake.xlen("ipc:dlq")
        assert dlq_len == 1

    def test_string_payload_wrapped(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.direct import send, recv

        agent_mod._current_agent_id = id_a
        send(id_b, "plain text message")

        agent_mod._current_agent_id = id_b
        messages = recv(agent_id=id_b)
        assert messages[0].payload == {"text": "plain text message"}

    def test_expired_messages_skipped(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.direct import send, recv
        from ipc.envelope import Envelope
        import time

        # Manually add an expired message
        env = Envelope(
            sender=id_a,
            recipient=id_b,
            message_type="direct",
            payload={"text": "expired"},
            ttl=1,
            timestamp=time.time() - 10,
        )
        fake.xadd(f"ipc:inbox:{id_b}", {"envelope": env.to_json()})

        agent_mod._current_agent_id = id_b
        messages = recv(agent_id=id_b)
        assert len(messages) == 0

    def test_multiple_messages_ordered(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.direct import send, recv

        agent_mod._current_agent_id = id_a
        for i in range(5):
            send(id_b, {"seq": i})

        agent_mod._current_agent_id = id_b
        messages = recv(agent_id=id_b, count=10)
        assert len(messages) == 5
        seqs = [m.payload["seq"] for m in messages]
        assert seqs == [0, 1, 2, 3, 4]

    def test_inbox_depth(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.direct import send, inbox_depth

        agent_mod._current_agent_id = id_a
        for _ in range(3):
            send(id_b, "msg")

        assert inbox_depth(id_b) == 3

    def test_metrics_incremented(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.direct import send

        agent_mod._current_agent_id = id_a
        send(id_b, "test")

        assert int(fake.get("ipc:metrics:sent") or 0) == 1
        assert int(fake.get("ipc:metrics:delivered") or 0) == 1


class TestBroadcast:
    def test_broadcast_all(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.direct import broadcast, recv

        agent_mod._current_agent_id = id_a
        count = broadcast("hello everyone")
        assert count == 1  # Only id_b (not self)

        agent_mod._current_agent_id = id_b
        messages = recv(agent_id=id_b)
        assert len(messages) == 1
        assert messages[0].payload == {"text": "hello everyone"}

    def test_broadcast_to_project(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.direct import broadcast, recv

        agent_mod._current_agent_id = id_a
        count = broadcast("project msg", project="/opt/test")
        assert count == 1

        agent_mod._current_agent_id = id_b
        messages = recv(agent_id=id_b)
        assert len(messages) == 1
