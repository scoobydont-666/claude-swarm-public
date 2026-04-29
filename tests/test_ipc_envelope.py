"""Tests for IPC envelope serialization and validation."""

import json
import time

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ipc.envelope import Envelope, _uuid7, MESSAGE_TYPES


class TestUUID7:
    def test_format(self):
        uid = _uuid7()
        assert len(uid) == 36
        assert uid.count("-") == 4

    def test_time_ordered(self):
        """UUIDs generated across different milliseconds are ordered."""
        import time
        ids = []
        for _ in range(5):
            ids.append(_uuid7())
            time.sleep(0.002)  # 2ms gap ensures different timestamp prefix
        assert ids == sorted(ids)

    def test_unique(self):
        ids = {_uuid7() for _ in range(1000)}
        assert len(ids) == 1000


class TestEnvelope:
    def test_create_minimal(self):
        env = Envelope(sender="a:1:0000", recipient="b:2:0000", message_type="direct")
        assert env.sender == "a:1:0000"
        assert env.recipient == "b:2:0000"
        assert env.message_type == "direct"
        assert env.priority == 3
        assert env.payload == {}
        assert env.final is True

    def test_invalid_message_type(self):
        with pytest.raises(ValueError, match="Invalid message_type"):
            Envelope(sender="a", recipient="b", message_type="invalid")

    def test_invalid_priority(self):
        with pytest.raises(ValueError, match="Priority must be 0-5"):
            Envelope(sender="a", recipient="b", message_type="direct", priority=10)

    def test_serialization_roundtrip(self):
        env = Envelope(
            sender="host:123:abcd",
            recipient="host:456:ef01",
            message_type="rpc_request",
            payload={"method": "test", "params": {"key": "value"}},
            priority=1,
            ttl=60,
            correlation_id="corr-123",
            reply_to="host:123:abcd",
            sequence=5,
            final=False,
        )
        raw = env.to_json()
        restored = Envelope.from_json(raw)
        assert restored.sender == env.sender
        assert restored.recipient == env.recipient
        assert restored.message_type == env.message_type
        assert restored.payload == env.payload
        assert restored.priority == env.priority
        assert restored.ttl == env.ttl
        assert restored.correlation_id == env.correlation_id
        assert restored.reply_to == env.reply_to
        assert restored.sequence == env.sequence
        assert restored.final == env.final

    def test_is_expired(self):
        env = Envelope(
            sender="a",
            recipient="b",
            message_type="direct",
            ttl=1,
            timestamp=time.time() - 5,
        )
        assert env.is_expired() is True

    def test_not_expired(self):
        env = Envelope(
            sender="a", recipient="b", message_type="direct", ttl=300
        )
        assert env.is_expired() is False

    def test_zero_ttl_never_expires(self):
        env = Envelope(
            sender="a",
            recipient="b",
            message_type="direct",
            ttl=0,
            timestamp=0,
        )
        assert env.is_expired() is False

    def test_make_reply(self):
        orig = Envelope(
            sender="alice:1:0000",
            recipient="bob:2:0000",
            message_type="direct",
            payload={"text": "hello"},
        )
        reply = orig.make_reply({"text": "hi back"})
        assert reply.sender == "bob:2:0000"
        assert reply.recipient == "alice:1:0000"
        assert reply.correlation_id == orig.id
        assert reply.payload == {"text": "hi back"}

    def test_all_message_types_valid(self):
        for mt in MESSAGE_TYPES:
            env = Envelope(sender="a", recipient="b", message_type=mt)
            assert env.message_type == mt

    def test_json_compact(self):
        env = Envelope(sender="a", recipient="b", message_type="direct")
        raw = env.to_json()
        # Should be compact (no spaces)
        assert " " not in raw or raw.count(" ") == 0
