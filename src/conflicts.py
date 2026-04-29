"""Conflict Detection + GPU Resource Arbitration.

Prevents two agents from editing the same files or loading models into
the same GPU simultaneously.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
import subprocess
from typing import Any

LOG = logging.getLogger(__name__)

try:
    from registry_redis import list_agents, AgentInfo, SWARM_ROOT
except (ImportError, Exception):
    from registry import list_agents, AgentInfo, SWARM_ROOT

GPU_SLOTS_FILE = SWARM_ROOT / "gpu_slots.json"


def get_working_agents_by_project() -> dict[str, list[AgentInfo]]:
    """Map project paths to agents currently working on them."""
    project_agents: dict[str, list[AgentInfo]] = {}
    for agent in list_agents():
        if agent.state == "working" and agent.project:
            project_agents.setdefault(agent.project, []).append(agent)
    return project_agents


def check_project_conflict(project: str, my_agent_id: str = "") -> dict[str, Any]:
    """Check if another agent is working on a project.

    Returns:
        {"conflict": bool, "agents": [...], "safe": bool}
    """
    agents_by_project = get_working_agents_by_project()
    others = [
        a for a in agents_by_project.get(project, []) if a.agent_id != my_agent_id
    ]

    return {
        "conflict": len(others) > 0,
        "agents": [a.agent_id for a in others],
        "safe": len(others) == 0,
    }


def get_changed_files(project: str) -> set[str]:
    """Get currently modified files in a project (unstaged + staged)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=5,
        )
        files = set(result.stdout.strip().splitlines())
        # Also staged
        result2 = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=5,
        )
        files.update(result2.stdout.strip().splitlines())
        return files
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Suppressed: %s", exc)
        return set()


def check_file_conflict(project: str, files: set[str], other_project: str) -> list[str]:
    """Check if any of our files overlap with another agent's changes."""
    other_files = get_changed_files(other_project)
    return sorted(files & other_files)


# --- GPU Slot Management ---


def _read_gpu_slots() -> dict[str, Any]:
    if GPU_SLOTS_FILE.exists():
        return json.loads(GPU_SLOTS_FILE.read_text())
    return {"slots": {}}


def _write_gpu_slots(data: dict[str, Any]) -> None:
    GPU_SLOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GPU_SLOTS_FILE.write_text(json.dumps(data, indent=2))


def claim_gpu_slot(gpu_index: int, agent_id: str, model: str = "") -> bool:
    """Claim a GPU slot. Returns True if successful, False if already claimed."""
    data = _read_gpu_slots()
    slot_key = f"gpu-{gpu_index}"
    existing = data["slots"].get(slot_key)

    if existing and existing.get("agent_id") != agent_id:
        # Check if holder is still alive
        try:
            from registry_redis import get_live_agents
        except (ImportError, Exception):
            from registry import get_live_agents
        live_ids = {a.agent_id for a in get_live_agents()}
        if existing["agent_id"] in live_ids:
            return False  # Slot is held by a live agent
        # Holder is dead, reclaim

    data["slots"][slot_key] = {
        "agent_id": agent_id,
        "model": model,
        "claimed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_gpu_slots(data)
    return True


def release_gpu_slot(gpu_index: int, agent_id: str) -> None:
    """Release a GPU slot."""
    data = _read_gpu_slots()
    slot_key = f"gpu-{gpu_index}"
    existing = data["slots"].get(slot_key)
    if existing and existing.get("agent_id") == agent_id:
        del data["slots"][slot_key]
        _write_gpu_slots(data)


def get_gpu_status() -> dict[str, Any]:
    """Get current GPU slot assignments."""
    return _read_gpu_slots()
