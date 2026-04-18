"""Tests for IPC channel pub/sub."""

import sys
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def ipc_pair():
    """Two registered agents on fake Redis."""
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)

    with patch("ipc.transport.get_client", return_value=fake):
        import ipc.agent as agent_mod

        agent_mod._current_agent_id = None
        agent_mod._heartbeat_thread = None
        id_a = agent_mod.register(hostname="a", pid=1, auto_heartbeat=False)
        agent_mod._current_agent_id = None
        id_b = agent_mod.register(hostname="b", pid=2, auto_heartbeat=False)

        yield fake, id_a, id_b


class TestChannels:
    def test_create_channel(self, ipc_pair):
        fake, _, _ = ipc_pair
        from ipc.channels import create, list_channels

        assert create("test-channel") is True
        # Idempotent
        assert create("test-channel") is False

        chans = list_channels()
        assert len(chans) == 1
        assert chans[0]["name"] == "test-channel"
        assert chans[0]["subscribers"] == 0

    def test_subscribe_and_list(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.channels import create, get_subscribers, subscribe

        create("dev-chat")

        agent_mod._current_agent_id = id_a
        subscribe("dev-chat", agent_id=id_a)

        agent_mod._current_agent_id = id_b
        subscribe("dev-chat", agent_id=id_b)

        subs = get_subscribers("dev-chat")
        assert subs == {id_a, id_b}

    def test_publish_and_consume(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.channels import consume, create, publish, subscribe

        create("updates")
        agent_mod._current_agent_id = id_a
        subscribe("updates", agent_id=id_a)
        agent_mod._current_agent_id = id_b
        subscribe("updates", agent_id=id_b)

        # Publish from A
        agent_mod._current_agent_id = id_a
        env_id = publish("updates", {"text": "new deploy"})
        assert env_id

        # Consume as B — uses shared consumer group, so only one consumer gets it
        agent_mod._current_agent_id = id_b
        msgs = consume("updates", agent_id=id_b)
        assert len(msgs) == 1
        assert msgs[0].payload == {"text": "new deploy"}

    def test_unsubscribe(self, ipc_pair):
        fake, id_a, _ = ipc_pair
        import ipc.agent as agent_mod
        from ipc.channels import create, get_subscribers, subscribe, unsubscribe

        create("temp")
        agent_mod._current_agent_id = id_a
        subscribe("temp", agent_id=id_a)
        assert id_a in get_subscribers("temp")

        unsubscribe("temp", agent_id=id_a)
        assert id_a not in get_subscribers("temp")

    def test_delete_channel(self, ipc_pair):
        fake, _, _ = ipc_pair
        from ipc.channels import create, delete, list_channels

        create("ephemeral")
        assert len(list_channels()) == 1

        delete("ephemeral")
        assert len(list_channels()) == 0

    def test_string_payload(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.channels import consume, create, publish, subscribe

        create("chat")
        agent_mod._current_agent_id = id_a
        subscribe("chat", agent_id=id_a)

        publish("chat", "plain text")

        msgs = consume("chat", agent_id=id_a)
        assert msgs[0].payload == {"text": "plain text"}
