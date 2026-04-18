"""Tests for the agent registry."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def tmp_agents_dir(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    with patch("registry.AGENTS_DIR", agents_dir):
        yield agents_dir


class TestRegister:
    def test_register_creates_file(self, tmp_agents_dir):
        from registry import register

        agent = register(model="opus-4.6", project="/opt/examforge")
        assert agent.agent_file.exists()
        data = json.loads(agent.agent_file.read_text())
        assert data["model"] == "opus-4.6"
        assert data["project"] == "/opt/examforge"
        assert data["state"] == "idle"

    def test_register_sets_capabilities(self, tmp_agents_dir):
        from registry import register

        agent = register()
        assert isinstance(agent.capabilities, dict)
        assert "gpu" in agent.capabilities

    def test_agent_id_format(self, tmp_agents_dir):
        from registry import register

        agent = register()
        assert f"{agent.hostname}-{agent.pid}" == agent.agent_id


class TestHeartbeat:
    def test_heartbeat_updates_file(self, tmp_agents_dir):
        from registry import heartbeat, register

        agent = register()
        old_mtime = agent.agent_file.stat().st_mtime
        import time

        time.sleep(0.05)
        heartbeat(agent)
        new_mtime = agent.agent_file.stat().st_mtime
        assert new_mtime >= old_mtime
        # Verify file still valid JSON
        data = json.loads(agent.agent_file.read_text())
        assert data["last_heartbeat"] == agent.last_heartbeat


class TestDeregister:
    def test_deregister_removes_file(self, tmp_agents_dir):
        from registry import deregister, register

        agent = register()
        assert agent.agent_file.exists()
        deregister(agent)
        assert not agent.agent_file.exists()


class TestListAgents:
    def test_list_empty(self, tmp_agents_dir):
        from registry import list_agents

        assert list_agents() == []

    def test_list_after_register(self, tmp_agents_dir):
        from registry import list_agents, register

        register(model="test")
        agents = list_agents()
        assert len(agents) == 1
        assert agents[0].model == "test"


class TestUpdateAgent:
    def test_update_fields(self, tmp_agents_dir):
        from registry import register, update_agent

        agent = register()
        update_agent(agent, state="working", project="/opt/test", task_id="task-42")
        assert agent.state == "working"
        assert agent.project == "/opt/test"
        data = json.loads(agent.agent_file.read_text())
        assert data["state"] == "working"
