"""Tests for event sequence dedup and rate-limit event emission."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestEventSequence:
    def test_emit_includes_sequence(self, tmp_path):
        import json

        with patch("events.EVENTS_DIR", tmp_path):
            from events import emit

            path = emit("test_event", project="/opt/test")
            event = json.loads(path.read_text())
            assert "sequence" in event
            assert isinstance(event["sequence"], int)
            assert event["sequence"] > 0

    def test_sequences_are_monotonic(self, tmp_path):
        import json

        with patch("events.EVENTS_DIR", tmp_path):
            from events import emit

            p1 = emit("event_a")
            p2 = emit("event_b")
            p3 = emit("event_c")
            s1 = json.loads(p1.read_text())["sequence"]
            s2 = json.loads(p2.read_text())["sequence"]
            s3 = json.loads(p3.read_text())["sequence"]
            assert s1 < s2 < s3


class TestEventConsumer:
    def test_first_pass_returns_all(self):
        from events import EventConsumer

        consumer = EventConsumer()
        events = [
            {"agent_id": "a-1", "sequence": 1, "type": "test"},
            {"agent_id": "a-1", "sequence": 2, "type": "test"},
        ]
        result = consumer.process(events)
        assert len(result) == 2

    def test_duplicate_sequences_dropped(self):
        from events import EventConsumer

        consumer = EventConsumer()
        batch1 = [
            {"agent_id": "a-1", "sequence": 1, "type": "test"},
            {"agent_id": "a-1", "sequence": 2, "type": "test"},
        ]
        batch2 = [
            {"agent_id": "a-1", "sequence": 1, "type": "test"},  # dupe
            {"agent_id": "a-1", "sequence": 2, "type": "test"},  # dupe
            {"agent_id": "a-1", "sequence": 3, "type": "test"},  # new
        ]
        consumer.process(batch1)
        result = consumer.process(batch2)
        assert len(result) == 1
        assert result[0]["sequence"] == 3

    def test_different_agents_independent(self):
        from events import EventConsumer

        consumer = EventConsumer()
        consumer.process([{"agent_id": "a-1", "sequence": 5, "type": "test"}])
        result = consumer.process(
            [
                {"agent_id": "a-1", "sequence": 5, "type": "test"},  # dupe for a-1
                {"agent_id": "a-2", "sequence": 5, "type": "test"},  # new for a-2
            ]
        )
        assert len(result) == 1
        assert result[0]["agent_id"] == "a-2"

    def test_events_without_sequence_pass_through(self):
        from events import EventConsumer

        consumer = EventConsumer()
        events = [{"agent_id": "a-1", "type": "legacy"}]  # no sequence field
        result = consumer.process(events)
        assert len(result) == 1

    def test_reset_agent(self):
        from events import EventConsumer

        consumer = EventConsumer()
        consumer.process([{"agent_id": "a-1", "sequence": 10, "type": "test"}])
        consumer.reset(agent_id="a-1")
        result = consumer.process([{"agent_id": "a-1", "sequence": 5, "type": "test"}])
        assert len(result) == 1  # seen before reset, but reset clears

    def test_reset_all(self):
        from events import EventConsumer

        consumer = EventConsumer()
        consumer.process(
            [
                {"agent_id": "a-1", "sequence": 10, "type": "test"},
                {"agent_id": "a-2", "sequence": 20, "type": "test"},
            ]
        )
        consumer.reset()
        result = consumer.process(
            [
                {"agent_id": "a-1", "sequence": 1, "type": "test"},
                {"agent_id": "a-2", "sequence": 1, "type": "test"},
            ]
        )
        assert len(result) == 2


class TestEmitRateLimit:
    def test_emit_rate_limit_event(self, tmp_path):
        import json

        with patch("events.EVENTS_DIR", tmp_path):
            from events import emit_rate_limit

            path = emit_rate_limit("pro-1", "session", "in 3 hours")
            event = json.loads(path.read_text())
            assert event["type"] == "rate_limit"
            assert event["details"]["profile"] == "pro-1"
            assert event["details"]["limit_type"] == "session"
