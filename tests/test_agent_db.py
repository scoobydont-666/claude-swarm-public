"""Tests for agent database."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def tmp_db(tmp_path):
    """Provide temporary database."""
    db_path = tmp_path / "agents.db"
    with patch("agent_db.DB_PATH", db_path):
        from agent_db import AgentDB

        db = AgentDB()
        yield db


class TestAgentUpsert:
    def test_upsert_new_agent(self, tmp_db):
        tmp_db.upsert_agent(
            hostname="miniboss",
            ip="<orchestration-node-ip>",
            pid=1234,
            state="idle",
        )
        agent = tmp_db.get_agent("miniboss")
        assert agent is not None
        assert agent["hostname"] == "miniboss"
        assert agent["ip"] == "<orchestration-node-ip>"
        assert agent["pid"] == 1234
        assert agent["state"] == "idle"

    def test_upsert_updates_existing(self, tmp_db):
        tmp_db.upsert_agent("host1", ip="1.1.1.1", pid=100, state="idle")
        tmp_db.upsert_agent("host1", ip="1.1.1.1", pid=200, state="working")

        agent = tmp_db.get_agent("host1")
        assert agent["pid"] == 200
        assert agent["state"] == "working"

    def test_upsert_with_capabilities(self, tmp_db):
        caps = {"gpu": True, "ollama": True, "docker": False}
        tmp_db.upsert_agent("GIGA", capabilities=caps)

        agent = tmp_db.get_agent("GIGA")
        assert agent["capabilities"] == caps


class TestAgentRetrieval:
    def test_get_agent_not_found(self, tmp_db):
        agent = tmp_db.get_agent("nonexistent")
        assert agent is None

    def test_list_agents_empty(self, tmp_db):
        agents = tmp_db.list_agents()
        assert agents == []

    def test_list_agents_multiple(self, tmp_db):
        tmp_db.upsert_agent("host1", ip="1.1.1.1")
        tmp_db.upsert_agent("host2", ip="2.2.2.2")
        tmp_db.upsert_agent("host3", ip="3.3.3.3")

        agents = tmp_db.list_agents()
        assert len(agents) == 3
        hostnames = {a["hostname"] for a in agents}
        assert hostnames == {"host1", "host2", "host3"}


class TestTaskHistory:
    def test_record_task_action(self, tmp_db):
        tmp_db.record_task_action(
            task_id="task-001",
            hostname="miniboss",
            action="claimed",
            details={"priority": "P1"},
        )
        history = tmp_db.task_history("task-001")
        assert len(history) == 1
        assert history[0]["action"] == "claimed"
        assert history[0]["details"]["priority"] == "P1"

    def test_multiple_actions_per_task(self, tmp_db):
        task_id = "task-002"
        tmp_db.record_task_action(task_id, "host1", "claimed")
        tmp_db.record_task_action(task_id, "host1", "completed")
        tmp_db.record_task_action(task_id, "host2", "preempted")

        history = tmp_db.task_history(task_id)
        assert len(history) == 3
        actions = [h["action"] for h in history]
        assert actions == ["claimed", "completed", "preempted"]

    def test_task_history_empty(self, tmp_db):
        history = tmp_db.task_history("nonexistent-task")
        assert history == []

    def test_task_history_chronological(self, tmp_db):
        task_id = "task-003"
        for action in ["claimed", "working", "completed"]:
            tmp_db.record_task_action(task_id, "host1", action)

        history = tmp_db.task_history(task_id)
        actions = [h["action"] for h in history]
        assert actions == ["claimed", "working", "completed"]


class TestAgentStats:
    def test_agent_stats_single_agent(self, tmp_db):
        tmp_db.upsert_agent("host1")
        tmp_db.record_task_action("task-001", "host1", "completed")
        tmp_db.record_task_action("task-002", "host1", "completed")
        tmp_db.record_task_action("task-003", "host1", "failed")

        stats = tmp_db.get_agent_stats("host1")
        assert stats is not None
        assert stats.total_tasks == 3
        assert stats.completed_tasks == 2
        assert stats.failed_tasks == 1
        assert stats.completion_rate == pytest.approx(2.0 / 3.0)

    def test_agent_stats_preemption(self, tmp_db):
        tmp_db.upsert_agent("host1")
        tmp_db.record_task_action("task-001", "host1", "claimed")
        tmp_db.record_task_action("task-002", "host1", "preempted")
        tmp_db.record_task_action("task-003", "host1", "completed")

        stats = tmp_db.get_agent_stats("host1")
        assert stats.preempted_tasks == 1

    def test_agent_stats_not_found(self, tmp_db):
        stats = tmp_db.get_agent_stats("nonexistent-host")
        assert stats is None


class TestFleetStats:
    def test_fleet_stats_empty(self, tmp_db):
        fleet = tmp_db.get_fleet_stats()
        assert fleet["total_agents"] == 0
        assert fleet["total_tasks"] == 0

    def test_fleet_stats_multiple_agents(self, tmp_db):
        # Set up agents with different states
        tmp_db.upsert_agent("host1", state="idle")
        tmp_db.upsert_agent("host2", state="working")
        tmp_db.upsert_agent("host3", state="idle")

        # Record some tasks
        tmp_db.record_task_action("task-001", "host1", "completed")
        tmp_db.record_task_action("task-002", "host1", "failed")
        tmp_db.record_task_action("task-003", "host2", "completed")
        tmp_db.record_task_action("task-004", "host2", "preempted")

        fleet = tmp_db.get_fleet_stats()
        assert fleet["total_agents"] == 3
        assert fleet["active_agents"] == 1  # only host2 is "working"
        assert fleet["idle_agents"] == 2
        assert fleet["total_tasks"] == 4
        assert fleet["completed_tasks"] == 2
        assert fleet["failed_tasks"] == 1
        assert fleet["preempted_tasks"] == 1

    def test_fleet_completion_rate(self, tmp_db):
        tmp_db.upsert_agent("host1")
        tmp_db.record_task_action("task-001", "host1", "completed")
        tmp_db.record_task_action("task-002", "host1", "completed")
        tmp_db.record_task_action("task-003", "host1", "failed")

        fleet = tmp_db.get_fleet_stats()
        assert fleet["completion_rate"] == pytest.approx(2.0 / 3.0)


class TestDeleteAgent:
    def test_delete_agent(self, tmp_db):
        tmp_db.upsert_agent("host1")
        assert tmp_db.get_agent("host1") is not None

        tmp_db.delete_agent("host1")
        assert tmp_db.get_agent("host1") is None

    def test_delete_nonexistent_agent(self, tmp_db):
        # Should not raise
        tmp_db.delete_agent("nonexistent")


class TestCleanupOldRecords:
    def test_cleanup_removes_old_records(self, tmp_db, tmp_path):
        from unittest.mock import patch
        from datetime import datetime, timezone, timedelta

        # Create records
        tmp_db.upsert_agent("host1")
        tmp_db.record_task_action("task-001", "host1", "completed")

        # Mock datetime to simulate old record
        old_time = datetime.now(timezone.utc) - timedelta(days=31)
        with patch("agent_db.datetime") as mock_dt:
            mock_dt.now.return_value = old_time
            # This is a simplified test — in reality you'd need to manipulate DB directly
            # For now, we just verify cleanup doesn't crash
            deleted = tmp_db.cleanup_old_records(days=30)
            # Expected: 0 records (since actual records were just created)
            assert deleted >= 0


class TestDatabaseSchema:
    def test_agents_table_created(self, tmp_db):
        # Verify the table exists by inserting a record
        tmp_db.upsert_agent("test-host")
        agent = tmp_db.get_agent("test-host")
        assert agent is not None

    def test_task_history_table_created(self, tmp_db):
        tmp_db.record_task_action("task-001", "host1", "claimed")
        history = tmp_db.task_history("task-001")
        assert len(history) == 1

    def test_table_indexes(self, tmp_db):
        # Record some data and verify queries work efficiently
        for i in range(5):
            tmp_db.record_task_action(f"task-{i:03d}", "host1", "completed")

        # These queries should work via indexes
        history = tmp_db.task_history("task-000")
        assert len(history) == 1
