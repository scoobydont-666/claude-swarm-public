"""Self-Orchestration — work generation, auto-dispatch, scaling.

DEPRECATED: This module is superseded by work_generator.py + auto_dispatch.py.
The Celery-based pipeline (celery_app.py) now handles work generation (every 30min)
and auto-dispatch (every 2min). This file remains for backward compatibility but
should not be extended. Use work_generator.WorkGenerator and auto_dispatch.AutoDispatcher
for new functionality.

Scans project plans for incomplete items, creates tasks in the queue,
and assigns them to idle agents based on capability matching.
"""

from __future__ import annotations

import logging
import re
import socket
from pathlib import Path

LOG = logging.getLogger(__name__)

try:
    from registry_redis import get_live_agents, AgentInfo
except (ImportError, Exception):
    from registry import get_live_agents, AgentInfo
try:
    from events_redis import emit
except (ImportError, Exception):
    from events import emit

from util import now_iso as _now_iso

SWARM_ROOT = Path("/var/lib/swarm")
QUEUE_DIR = SWARM_ROOT / "queue"
PLANS_DIRS = [
    "/opt/examforge/plans",
    "/opt/audit-sentinel/plans",
    "/opt/clausehound/plans",
    "/opt/documint/plans",
    "/opt/prompt-forge/plans",
    "/opt/hashrate-hedger/plans",
    "/opt/solar-sentinel/plans",
    "/opt/claude-swarm/plans",
    "/opt/hydra-project/plans",
]


def _next_task_id() -> str:
    """Generate next task ID from existing queue."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    existing = [
        f.stem for f in QUEUE_DIR.glob("task-*.yaml") if not f.stem.endswith(".lock")
    ]
    if not existing:
        return "task-100"
    max_num = max(int(t.split("-")[1]) for t in existing if t.split("-")[1].isdigit())
    return f"task-{max_num + 1}"


def _read_task(path: Path) -> dict:
    """Read task YAML file from disk.

    Args:
        path: Path to task YAML file.

    Returns:
        Parsed task dict.
    """
    import yaml

    return yaml.safe_load(path.read_text())


def _write_task(task: dict) -> Path:
    """Write task to YAML file on disk.

    Args:
        task: Task dict with 'id' field.

    Returns:
        Path to written task file.
    """
    import yaml

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    path = QUEUE_DIR / f"{task['id']}.yaml"
    path.write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))
    return path


def create_task(
    title: str,
    project: str,
    priority: str = "P2",
    requires: list[str] | None = None,
    depends_on: list[str] | None = None,
    estimated_minutes: int = 30,
    description: str = "",
) -> dict:
    """Create a new task in the queue."""
    task = {
        "id": _next_task_id(),
        "title": title,
        "project": project,
        "priority": priority,
        "requires": requires or [],
        "depends_on": depends_on or [],
        "estimated_minutes": estimated_minutes,
        "description": description,
        "state": "pending",
        "claimed_by": "",
        "created_at": _now_iso(),
        "created_by": socket.gethostname(),
        "started_at": "",
        "completed_at": "",
        "result": {},
    }
    _write_task(task)
    return task


def list_tasks(state: str | None = None) -> list[dict]:
    """List tasks, optionally filtered by state."""
    if not QUEUE_DIR.exists():
        return []

    tasks = []
    for f in sorted(QUEUE_DIR.glob("task-*.yaml")):
        if f.stem.endswith(".lock"):
            continue
        try:
            task = _read_task(f)
            if state is None or task.get("state") == state:
                tasks.append(task)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
            continue
    return tasks


def claim_task(task_id: str, agent_id: str) -> dict | None:
    """Claim a pending task for an agent. Returns task if successful."""
    import fcntl

    path = QUEUE_DIR / f"{task_id}.yaml"
    lock_path = QUEUE_DIR / f"{task_id}.lock"

    if not path.exists():
        return None

    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        task = _read_task(path)
        if task.get("state") != "pending":
            lock_fd.close()
            return None

        task["state"] = "claimed"
        task["claimed_by"] = agent_id
        task["started_at"] = _now_iso()
        _write_task(task)

        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

        emit(
            "task_claimed",
            project=task.get("project", ""),
            details={
                "task_id": task_id,
                "agent_id": agent_id,
            },
        )

        return task
    except (IOError, OSError):
        return None  # Lock contention — another agent got it


def complete_task(task_id: str, result: dict | None = None) -> dict | None:
    """Mark a task as completed."""
    path = QUEUE_DIR / f"{task_id}.yaml"
    if not path.exists():
        return None

    task = _read_task(path)
    task["state"] = "done"
    task["completed_at"] = _now_iso()
    task["result"] = result or {}
    _write_task(task)

    emit(
        "task_completed",
        project=task.get("project", ""),
        details={
            "task_id": task_id,
            "result": result or {},
        },
    )

    return task


def fail_task(task_id: str, reason: str = "") -> dict | None:
    """Mark a task as failed (will be requeued)."""
    path = QUEUE_DIR / f"{task_id}.yaml"
    if not path.exists():
        return None

    task = _read_task(path)
    task["state"] = "failed"
    task["result"] = {"error": reason}
    _write_task(task)

    emit(
        "task_failed",
        project=task.get("project", ""),
        details={
            "task_id": task_id,
            "reason": reason,
        },
    )

    return task


def requeue_failed(max_retries: int = 3) -> list[str]:
    """Requeue failed tasks (up to max_retries)."""
    requeued = []
    for task in list_tasks(state="failed"):
        retries = task.get("result", {}).get("retries", 0)
        if retries < max_retries:
            task["state"] = "pending"
            task["claimed_by"] = ""
            task["started_at"] = ""
            task["result"]["retries"] = retries + 1
            _write_task(task)
            requeued.append(task["id"])
    return requeued


def find_best_task(agent: AgentInfo) -> dict | None:
    """Find the highest-priority pending task that matches agent capabilities.

    Priority ordering: P0 > P1 > P2 > P3 > P4 > P5.
    Filters by requires vs agent capabilities.
    Respects depends_on (skip if deps not done).
    """
    pending = list_tasks(state="pending")
    if not pending:
        return None

    # Sort by priority
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5}
    pending.sort(key=lambda t: priority_order.get(t.get("priority", "P5"), 5))

    # Get agent capabilities as set
    agent_caps = {k for k, v in agent.capabilities.items() if v}
    completed_task_ids = {t["id"] for t in list_tasks(state="done")}

    for task in pending:
        # Check requirements
        required = set(task.get("requires", []))
        if not required.issubset(agent_caps):
            continue

        # Check dependencies
        deps = set(task.get("depends_on", []))
        if not deps.issubset(completed_task_ids):
            continue

        return task

    return None


def auto_dispatch() -> list[dict]:
    """Auto-dispatch: assign pending tasks to idle agents.

    Returns list of {agent_id, task_id} assignments made.
    """
    assignments = []
    idle_agents = [a for a in get_live_agents() if a.state == "idle"]

    for agent in idle_agents:
        task = find_best_task(agent)
        if task:
            claimed = claim_task(task["id"], agent.agent_id)
            if claimed:
                assignments.append(
                    {
                        "agent_id": agent.agent_id,
                        "task_id": task["id"],
                        "task_title": task.get("title", ""),
                    }
                )

    return assignments


def scan_plans_for_tasks() -> list[dict]:
    """Scan project plans for incomplete items and create tasks.

    Reads markdown plans, finds unchecked items (- [ ]), creates tasks.
    Avoids duplicates by checking existing task titles.
    """
    existing_titles = {t["title"] for t in list_tasks()}
    new_tasks = []

    for plan_dir in PLANS_DIRS:
        if not Path(plan_dir).is_dir():
            continue

        project = str(Path(plan_dir).parent)

        for plan_file in Path(plan_dir).glob("*.md"):
            content = plan_file.read_text()
            # Find unchecked items: - [ ] text
            for match in re.finditer(r"^[-*]\s+\[\s*\]\s+(.+)$", content, re.MULTILINE):
                title = match.group(1).strip()
                if title in existing_titles:
                    continue

                # Infer priority from context
                priority = "P3"  # Default
                if "Phase 3" in content or "deploy" in title.lower():
                    priority = "P1"
                if "revenue" in title.lower() or "stripe" in title.lower():
                    priority = "P0"

                # Infer requirements
                requires = []
                if any(
                    kw in title.lower() for kw in ["ollama", "gpu", "model", "generate"]
                ):
                    requires.append("gpu")
                    requires.append("ollama")
                if "chromadb" in title.lower():
                    requires.append("chromadb")

                task = create_task(
                    title=title,
                    project=project,
                    priority=priority,
                    requires=requires,
                )
                new_tasks.append(task)
                existing_titles.add(title)

    return new_tasks
