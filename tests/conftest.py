"""Shared pytest fixtures for claude-swarm tests."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Mark test environment as dev so config_validation / celery_app don't
# fail-closed on empty SWARM_REDIS_PASSWORD (fakeredis doesn't use auth).
os.environ.setdefault("HYDRA_ENV", "dev")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import swarm_lib as lib


@pytest.fixture
def swarm_tmpdir(tmp_path):
    """Create a temporary swarm directory structure.

    This is the union of all directory sets used across the test suite.
    Tests requiring specific sub-directories will find them already present.
    """
    for d in [
        "status",
        "tasks/pending",
        "tasks/claimed",
        "tasks/completed",
        "tasks/decomposed",
        "artifacts",
        "artifacts/summaries",
        "artifacts/branches",
        "messages/inbox/testhost",
        "messages/inbox/broadcast",
        "messages/archive",
        "config",
    ]:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    config = {
        "swarm_root": str(tmp_path),
        "swarm": {"name": "test-swarm", "version": 1},
        "nodes": {
            "testhost": {
                "ip": "192.168.200.99",
                "role": "client",
                "capabilities": ["docker", "gpu"],
                "projects": ["test-project"],
            },
            "otherhost": {
                "ip": "192.168.200.100",
                "role": "client",
                "capabilities": ["docker"],
                "projects": [],
            },
        },
        "heartbeat": {"interval_seconds": 60, "stale_threshold_seconds": 300},
        "tasks": {"auto_claim": False, "notify_on_new": True},
        "work_generator": {
            "enabled": True,
            "max_pending_tasks": 10,
            "prometheus_url": "http://127.0.0.1:9090",
            "projects": {},
        },
        "auto_dispatch": {
            "enabled": False,
            "require_approval_for": ["opus"],
            "max_concurrent_dispatches": 3,
        },
        "decomposition": {
            "enabled": True,
            "auto_suggest": True,
            "capability_routing": {
                "ollama": ["GIGA"],
                "gpu": ["GIGA"],
                "mining": ["miniboss"],
            },
        },
        "worktrees": {
            "enabled": True,
            "base_path": str(tmp_path / "worktrees"),
            "auto_cleanup_hours": 24,
            "branch_prefix": "swarm",
        },
        "summaries": {
            "enabled": True,
            "auto_generate": True,
            "max_decisions": 10,
            "max_files": 20,
            "retention_days": 30,
        },
        "scheduled_maintenance": {"daily_hour": 0, "weekly_day": 0},
    }
    with open(tmp_path / "config" / "swarm.yaml", "w") as f:
        yaml.dump(config, f)

    with (
        patch.object(lib, "_swarm_root", return_value=tmp_path),
        patch.object(lib, "_config_path", return_value=tmp_path / "config" / "swarm.yaml"),
        patch.object(lib, "_hostname", return_value="testhost"),
    ):
        yield tmp_path
