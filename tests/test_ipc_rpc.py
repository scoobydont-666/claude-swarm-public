"""Tests for IPC RPC request/response."""

import sys
import threading
import time
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


class TestRPC:
    def test_respond_and_request(self, ipc_pair):
        """Test RPC by having a responder thread."""
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.rpc import request, respond
        from ipc.direct import recv

        # Responder thread: reads from B's inbox, sends response
        def responder():
            agent_mod._current_agent_id = id_b
            # Give the request time to be sent
            time.sleep(0.1)
            msgs = recv(agent_id=id_b)
            for msg in msgs:
                if msg.message_type == "rpc_request":
                    method = msg.payload.get("method", "")
                    if method == "add":
                        params = msg.payload.get("params", {})
                        result = params.get("a", 0) + params.get("b", 0)
                        respond(msg, {"result": result})

        t = threading.Thread(target=responder)
        t.start()

        # Request from A
        agent_mod._current_agent_id = id_a
        resp = request(id_b, "add", {"a": 3, "b": 4}, timeout=5)
        t.join(timeout=5)

        assert resp.payload == {"result": 7}
        assert resp.message_type == "rpc_response"

    def test_rpc_timeout(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.rpc import request, RPCTimeout

        agent_mod._current_agent_id = id_a
        with pytest.raises(RPCTimeout):
            request(id_b, "slow_method", timeout=1)

    def test_rpc_error_response(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.rpc import request, respond_error, RPCError
        from ipc.direct import recv

        def responder():
            agent_mod._current_agent_id = id_b
            time.sleep(0.1)
            msgs = recv(agent_id=id_b)
            for msg in msgs:
                if msg.message_type == "rpc_request":
                    respond_error(msg, "method not found")

        t = threading.Thread(target=responder)
        t.start()

        agent_mod._current_agent_id = id_a
        with pytest.raises(RPCError, match="method not found"):
            request(id_b, "nonexistent", timeout=5)
        t.join(timeout=5)

    def test_cleanup_expired_rpcs(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.rpc import cleanup_expired_rpcs

        agent_mod._current_agent_id = id_a

        # Add an expired RPC entry
        fake.zadd("ipc:rpc:pending", {"expired-corr": time.time() - 100})
        fake.set("ipc:rpc:resp:expired-corr", "stale")

        cleaned = cleanup_expired_rpcs()
        assert cleaned == 1
        assert fake.zcard("ipc:rpc:pending") == 0

    def test_metrics_tracked(self, ipc_pair):
        fake, id_a, id_b = ipc_pair
        import ipc.agent as agent_mod
        from ipc.rpc import request, RPCTimeout

        agent_mod._current_agent_id = id_a
        try:
            request(id_b, "test", timeout=1)
        except RPCTimeout:
            pass

        assert int(fake.get("ipc:metrics:rpc_sent") or 0) == 1
        assert int(fake.get("ipc:metrics:rpc_timeout") or 0) == 1
