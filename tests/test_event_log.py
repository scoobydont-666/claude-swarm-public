"""Tests for event_log.py — persistence, query, cooldown."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _make_event_log(tmp_path: Path):
    """Return an EventLog instance pointing at a temp DB."""
    import event_log as el

    db_path = tmp_path / "test-health-events.db"
    lock_path = tmp_path / "test-health-events.lock"
    with (
        patch.object(el, "DB_PATH", db_path),
        patch.object(el.EventLog, "_LOCK_PATH", lock_path),
    ):
        log = el.EventLog()
        # Patch instance attributes so all calls use temp paths
        log._LOCK_PATH = lock_path
        # Monkey-patch the module-level DB_PATH for the duration of the test
        el.DB_PATH = db_path
        return log, el


class TestEventLogRecord:
    def test_insert_returns_id(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            row_id = log.record(
                rule_name="disk_space_low",
                host="node_primary",
                severity="high",
                description="Disk at 92%",
                action_taken="alert_email",
                action_result="OK: email sent",
            )
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_multiple_inserts(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            for i in range(5):
                log.record(rule_name=f"rule_{i}", host="node_gpu")
            rows = log.recent_events(limit=10)
        assert len(rows) == 5


class TestEventLogQuery:
    def test_filter_by_rule(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            log.record(rule_name="service_down", host="node_primary")
            log.record(rule_name="disk_space_low", host="node_primary")
            log.record(rule_name="service_down", host="node_gpu")

            rows = log.query(rule_name="service_down")
        assert len(rows) == 2
        assert all(r["rule_name"] == "service_down" for r in rows)

    def test_filter_by_host(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            log.record(rule_name="service_down", host="node_primary")
            log.record(rule_name="service_down", host="node_gpu")

            rows = log.query(host="node_primary")
        assert len(rows) == 1
        assert rows[0]["host"] == "node_primary"

    def test_filter_by_timerange(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        now = datetime.now(UTC)
        past = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            log.record(rule_name="test_rule", host="node_primary")
            rows = log.query(since=past, until=future)
        assert len(rows) == 1

    def test_limit_respected(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            for i in range(10):
                log.record(rule_name="test_rule")
            rows = log.query(limit=3)
        assert len(rows) == 3


class TestEventLogLastActionTime:
    def test_returns_none_when_no_actions(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            # Record event with no action
            log.record(rule_name="service_down", host="node_primary", action_taken="")
            result = log.last_action_time("service_down", "node_primary")
        assert result is None

    def test_returns_timestamp_when_action_exists(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            log.record(
                rule_name="service_down",
                host="node_primary",
                action_taken="restart_service",
                action_result="OK",
            )
            result = log.last_action_time("service_down", "node_primary")
        assert result is not None
        # Should be a valid ISO timestamp
        datetime.fromisoformat(result.replace("Z", "+00:00"))


class TestEventLogRuleSummary:
    def test_summary_aggregates_correctly(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            log.record(rule_name="service_down", action_taken="restart_service")
            log.record(rule_name="service_down", action_taken="restart_service")
            log.record(rule_name="disk_space_low", action_taken="")

            summary = log.rule_summary()

        by_rule = {r["rule_name"]: r for r in summary}
        assert by_rule["service_down"]["total"] == 2
        assert by_rule["service_down"]["actions_taken"] == 2
        assert by_rule["disk_space_low"]["total"] == 1
        assert by_rule["disk_space_low"]["actions_taken"] == 0


class TestEventLogPrune:
    def test_prune_removes_old_events(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            # Insert a recent event (creates table via EventLog)
            log.record(rule_name="recent_rule", host="testhost", severity="low")

            # Now insert an old event directly via raw SQL
            import sqlite3

            db_path = tmp_path / "test-health-events.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "INSERT INTO health_events (timestamp, rule_name, host, severity) "
                "VALUES ('2020-01-01T00:00:00Z', 'old_rule', 'testhost', 'low')"
            )
            conn.commit()
            conn.close()

            assert log.count() == 2
            deleted = log.prune(days=30)
            assert deleted == 1
            assert log.count() == 1

    def test_prune_no_op_when_all_recent(self, tmp_path):
        log, el = _make_event_log(tmp_path)
        with patch.object(el, "DB_PATH", tmp_path / "test-health-events.db"):
            log.record(rule_name="recent", host="testhost", severity="low")
            deleted = log.prune(days=30)
            assert deleted == 0
            assert log.count() == 1
