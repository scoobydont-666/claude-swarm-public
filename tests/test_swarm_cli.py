"""Tests for swarm_cli — CLI command coverage using Typer's CliRunner."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from typer.testing import CliRunner

import swarm_cli

runner = CliRunner()


class TestStatusCommand:
    def test_status_no_nodes(self, swarm_tmpdir, monkeypatch):
        """status with no registered nodes prints dim message."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        with (
            patch("swarm_cli.lib.get_all_status", return_value=[]),
            patch("swarm_cli.lib.verify_stale_pids"),
        ):
            result = runner.invoke(swarm_cli.app, ["status"])
        assert result.exit_code == 0
        assert "No nodes" in result.output

    def test_status_shows_table_headers(self, swarm_tmpdir, monkeypatch):
        """status with nodes renders a table with expected headers."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        fake_nodes = [
            {
                "hostname": "node_gpu",
                "state": "active",
                "current_task": "task-001",
                "project": "/opt/examforge",
                "model": "sonnet",
                "updated_at": "2026-03-31T10:00:00+00:00",
                "pid": 12345,
            }
        ]
        with (
            patch("swarm_cli.lib.get_all_status", return_value=fake_nodes),
            patch("swarm_cli.lib.verify_stale_pids"),
        ):
            result = runner.invoke(swarm_cli.app, ["status"])
        assert result.exit_code == 0
        assert "Swarm Status" in result.output
        assert "Host" in result.output
        assert "State" in result.output
        assert "node_gpu" in result.output

    def test_status_stale_node_marked(self, swarm_tmpdir, monkeypatch):
        """A node with old updated_at is labeled STALE."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        fake_nodes = [
            {
                "hostname": "node_reserve2",
                "state": "active",
                "current_task": "",
                "project": "",
                "model": "",
                "updated_at": "2020-01-01T00:00:00+00:00",  # very old
                "pid": 99,
            }
        ]
        with (
            patch("swarm_cli.lib.get_all_status", return_value=fake_nodes),
            patch("swarm_cli.lib.verify_stale_pids"),
        ):
            result = runner.invoke(swarm_cli.app, ["status"])
        assert result.exit_code == 0
        assert "STALE" in result.output


class TestTasksCommand:
    def test_tasks_list_no_tasks(self, swarm_tmpdir, monkeypatch):
        """tasks list with no tasks prints dim message."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        with patch("swarm_cli.lib.list_tasks", return_value=[]):
            result = runner.invoke(swarm_cli.app, ["tasks"])
        assert result.exit_code == 0
        assert "No tasks" in result.output

    def test_tasks_list_shows_table(self, swarm_tmpdir, monkeypatch):
        """tasks list with tasks renders table with correct columns."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        fake_tasks = [
            {
                "id": "task-001",
                "_stage": "pending",
                "priority": "high",
                "title": "Implement logging",
                "created_by": "node_gpu",
                "claimed_by": None,
            },
            {
                "id": "task-002",
                "_stage": "claimed",
                "priority": "medium",
                "title": "Write tests",
                "created_by": "node_primary",
                "claimed_by": "node_reserve2",
            },
        ]
        with patch("swarm_cli.lib.list_tasks", return_value=fake_tasks):
            result = runner.invoke(swarm_cli.app, ["tasks"])
        assert result.exit_code == 0
        assert "Swarm Tasks" in result.output
        assert "task-001" in result.output
        assert "task-002" in result.output
        assert "ID" in result.output
        assert "Stage" in result.output
        assert "Priority" in result.output


class TestInboxCommand:
    def test_inbox_empty(self, swarm_tmpdir, monkeypatch):
        """inbox with no messages prints dim message."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        with patch("swarm_cli.lib.read_inbox", return_value=[]):
            result = runner.invoke(swarm_cli.app, ["inbox"])
        assert result.exit_code == 0
        assert "No messages" in result.output

    def test_inbox_shows_messages(self, swarm_tmpdir, monkeypatch):
        """inbox with messages prints sender and text."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        fake_msgs = [
            {
                "from": "node_gpu",
                "text": "Task task-001 is ready for review",
                "sent_at": "2026-03-31T10:00:00Z",
                "_source": "direct",
                "_file": str(swarm_tmpdir / "messages/inbox/testhost/msg-001.yaml"),
            }
        ]
        with patch("swarm_cli.lib.read_inbox", return_value=fake_msgs):
            result = runner.invoke(swarm_cli.app, ["inbox"])
        assert result.exit_code == 0
        assert "node_gpu" in result.output
        assert "Task task-001" in result.output

    def test_inbox_broadcast_renders_message(self, swarm_tmpdir, monkeypatch):
        """inbox renders broadcast messages with sender and text."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        fake_msgs = [
            {
                "from": "node_primary",
                "text": "Swarm maintenance in 5 minutes",
                "sent_at": "2026-03-31T11:00:00Z",
                "_source": "broadcast",
                "_file": str(swarm_tmpdir / "messages/inbox/broadcast/bcast-001.yaml"),
            }
        ]
        with patch("swarm_cli.lib.read_inbox", return_value=fake_msgs):
            result = runner.invoke(swarm_cli.app, ["inbox"])
        assert result.exit_code == 0
        assert "node_primary" in result.output
        assert "Swarm maintenance in 5 minutes" in result.output


class TestHealthCommand:
    def test_health_default(self, swarm_tmpdir, monkeypatch):
        """health default shows health overview with expected sections."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        fake_result = {
            "timestamp": "2026-03-31T10:00:00Z",
            "swarm_root": str(swarm_tmpdir),
            "nfs_available": True,
            "config_loaded": True,
            "nodes": {},
            "stale_nodes": [],
            "pending_tasks": 0,
            "claimed_tasks": 0,
            "completed_tasks": 0,
        }
        with patch("swarm_cli.lib.health_check", return_value=fake_result):
            result = runner.invoke(swarm_cli.app, ["health"])
        assert result.exit_code == 0
        assert "Swarm Health Check" in result.output
        assert "NFS available" in result.output

    def test_health_nfs_unavailable(self, swarm_tmpdir, monkeypatch):
        """health shows NFS as unavailable when flag is False."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        fake_result = {
            "timestamp": "2026-03-31T10:00:00Z",
            "swarm_root": str(swarm_tmpdir),
            "nfs_available": False,
            "config_loaded": True,
            "nodes": {},
            "stale_nodes": [],
            "pending_tasks": 0,
            "claimed_tasks": 0,
            "completed_tasks": 0,
        }
        with patch("swarm_cli.lib.health_check", return_value=fake_result):
            result = runner.invoke(swarm_cli.app, ["health"])
        assert result.exit_code == 0
        # Rich renders "no" in red for nfs_available=False
        assert "no" in result.output

    def test_health_with_nodes(self, swarm_tmpdir, monkeypatch):
        """health shows node table when nodes are present."""
        monkeypatch.setenv("SWARM_ROOT", str(swarm_tmpdir))
        fake_result = {
            "timestamp": "2026-03-31T10:00:00Z",
            "swarm_root": str(swarm_tmpdir),
            "nfs_available": True,
            "config_loaded": True,
            "nodes": {
                "node_gpu": {
                    "state": "active",
                    "age_seconds": 45,
                    "stale": False,
                    "current_task": "task-001",
                }
            },
            "stale_nodes": [],
            "pending_tasks": 2,
            "claimed_tasks": 1,
            "completed_tasks": 5,
        }
        with patch("swarm_cli.lib.health_check", return_value=fake_result):
            result = runner.invoke(swarm_cli.app, ["health"])
        assert result.exit_code == 0
        assert "Node Health" in result.output
        assert "node_gpu" in result.output
