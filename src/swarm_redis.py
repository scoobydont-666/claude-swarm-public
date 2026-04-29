"""Redis-backed implementation of swarm core operations.

Drop-in replacement for the filesystem operations in swarm_lib.py.
Uses redis_client.py for all state management. No NFS, no fcntl, no lockfiles.

Usage:
    from swarm_redis import use_redis, is_redis_available
    if is_redis_available():
        # All swarm_lib functions auto-delegate to Redis
        pass
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)

try:
    import redis_client as _rc
except ImportError:
    from src import redis_client as _rc

try:
    from util import now_iso, hostname
except ImportError:
    from src.util import now_iso, hostname


# -----------------------------------------------------------------------
# Backend selection
# -----------------------------------------------------------------------

_USE_REDIS: bool | None = None


def is_redis_available() -> bool:
    """Check if Redis is available and configured."""
    global _USE_REDIS
    if _USE_REDIS is not None:
        return _USE_REDIS
    _USE_REDIS = _rc.health_check()
    return _USE_REDIS


def use_redis() -> bool:
    """Return True if Redis should be used as the backend."""
    return os.environ.get("SWARM_BACKEND", "redis") == "redis" and is_redis_available()


# -----------------------------------------------------------------------
# Task operations (replaces swarm_lib.py filesystem ops)
# -----------------------------------------------------------------------

_task_counter = 0


def _next_task_id() -> str:
    """Generate next task ID from Redis."""
    global _task_counter
    r = _rc.get_client()
    _task_counter = r.incr("swarm:task_counter")
    return f"task-{_task_counter:04d}"


def create_task(
    title: str,
    description: str = "",
    project: str = "",
    priority: str = "medium",
    requires: Optional[list[str]] = None,
    estimated_minutes: int = 0,
) -> dict:
    """Create a new pending task in Redis."""
    task_id = _next_task_id()
    priority_score = {"critical": 1, "high": 3, "medium": 5, "low": 7}.get(priority, 5)
    task_data = {
        "id": task_id,
        "title": title,
        "description": description,
        "project": project,
        "priority": priority,
        "requires": requires or [],
        "created_by": hostname(),
        "created_at": now_iso(),
        "estimated_minutes": estimated_minutes,
    }
    _rc.create_task(task_id, task_data, priority=priority_score)
    return task_data


def claim_task(task_id: str) -> dict:
    """Claim a pending task. Atomic via Lua script."""
    host = hostname()

    # If task_id is specified, we need to claim that specific one
    # The redis_client.claim_task() pops the highest-priority task
    # For specific task claims, use direct Redis operations
    r = _rc.get_client()
    score = r.zscore("tasks:pending", task_id)
    if score is None:
        raise FileNotFoundError(f"Task {task_id} not found in pending")

    # Remove from pending, add to claimed
    pipe = r.pipeline()
    pipe.zrem("tasks:pending", task_id)
    pipe.zadd("tasks:claimed", {task_id: score})
    pipe.hset(
        f"task:{task_id}",
        mapping={
            "state": "claimed",
            "claimed_by": host,
            "claimed_at": now_iso(),
        },
    )
    pipe.execute()

    task = _rc.get_task(task_id)
    if task and "data" in task:
        data = json.loads(task["data"])
        data.update(
            {
                "claimed_by": host,
                "claimed_at": task.get("claimed_at", now_iso()),
            }
        )
        return data
    return task or {}


def claim_next_task() -> dict | None:
    """Claim the highest-priority pending task. Returns task dict or None."""
    host = hostname()
    task_id = _rc.claim_task(host)
    if task_id is None:
        return None
    task = _rc.get_task(task_id)
    if task and "data" in task:
        data = json.loads(task["data"])
        data.update(
            {
                "claimed_by": host,
                "claimed_at": task.get("claimed_at", now_iso()),
            }
        )
        return data
    return task


def complete_task(task_id: str, result_artifact: str = "") -> dict:
    """Complete a claimed task."""
    result_data = {"artifact": result_artifact} if result_artifact else None
    _rc.complete_task(task_id, result_data)
    task = _rc.get_task(task_id)
    if task and "data" in task:
        data = json.loads(task["data"])
        data["completed_at"] = task.get("completed_at", now_iso())
        if result_artifact:
            data["result_artifact"] = result_artifact
        return data
    return task or {}


def list_tasks(stage: Optional[str] = None) -> list[dict]:
    """List tasks, optionally filtered by stage."""
    stages = [stage] if stage else ["pending", "claimed", "completed"]
    results = []
    for s in stages:
        tasks = _rc.list_tasks(s)
        for t in tasks:
            if "data" in t:
                data = json.loads(t["data"])
                data["state"] = t.get("state", s)
                results.append(data)
            else:
                results.append(t)
    return results


def get_matching_tasks(capabilities: list[str] | None = None) -> list[dict]:
    """Find pending tasks matching capabilities."""
    pending = list_tasks("pending")
    if not capabilities:
        return pending
    matched = []
    for task in pending:
        requires = task.get("requires", [])
        if not requires or all(r in capabilities for r in requires):
            matched.append(task)
    return matched


# -----------------------------------------------------------------------
# Status operations (replaces status JSON files)
# -----------------------------------------------------------------------


def update_status(
    state: str = "idle",
    task_id: str = "",
    capabilities: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    """Update this node's status in Redis."""
    host = hostname()
    status = {
        "hostname": host,
        "state": state,
        "task_id": task_id,
        "capabilities": json.dumps(capabilities or []),
        "last_heartbeat": now_iso(),
        "pid": str(os.getpid()),
    }
    if extra:
        status.update(
            {
                k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                for k, v in extra.items()
            }
        )
    _rc.update_status(host, status)
    return status


def get_all_status() -> list[dict]:
    """Get status for all nodes (returns list for parity with swarm_lib)."""
    r = _rc.get_client()
    keys = r.keys("status:*")
    result = []
    if keys:
        pipe = r.pipeline()
        for k in keys:
            pipe.hgetall(k)
        for k, data in zip(keys, pipe.execute()):
            if data:
                result.append(data)
    return result


def get_status(host: str) -> dict | None:
    """Get status for a specific node."""
    return _rc.get_status(host)


def health_check() -> dict:
    """Full health check of swarm via Redis.

    Returns the same structure as swarm_lib.health_check() so the CLI
    display code works regardless of backend.
    """
    from datetime import datetime, timezone

    status_list = get_all_status()
    stale_threshold = 300  # seconds

    now = datetime.now(timezone.utc)
    nodes: dict[str, dict] = {}
    stale_nodes: list[str] = []

    for s in status_list:
        hostname = s.get("host", s.get("hostname", "unknown"))
        updated = s.get("updated_at", "")
        age = -1
        if updated:
            try:
                updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age = int((now - updated_dt).total_seconds())
            except ValueError:
                pass
        is_stale = age > stale_threshold if age >= 0 else True
        nodes[hostname] = {
            "state": s.get("state", "unknown"),
            "age_seconds": age,
            "stale": is_stale,
            "current_task": s.get("current_task", ""),
        }
        if is_stale and s.get("state") not in ("offline", "idle"):
            stale_nodes.append(hostname)

    pending = list_tasks("pending")
    claimed = list_tasks("claimed")
    completed = list_tasks("completed")

    return {
        "swarm_root": "/opt/swarm",
        "nfs_available": __import__("pathlib").Path("/opt/swarm").is_dir(),
        "config_loaded": True,
        "timestamp": now.isoformat(),
        "nodes": nodes,
        "stale_nodes": stale_nodes,
        "pending_tasks": len(pending),
        "claimed_tasks": len(claimed),
        "completed_tasks": len(completed),
    }


# -----------------------------------------------------------------------
# Messaging (replaces inbox filesystem)
# -----------------------------------------------------------------------


def send_message(target_host: str, message: dict) -> int:
    """Send a message to a host's inbox."""
    return _rc.send_message(target_host, message)


def read_inbox(host: str, pop: bool = False) -> list[dict]:
    """Read messages from a host's inbox."""
    return _rc.read_inbox(host, pop=pop)


def broadcast_message(msg: dict) -> None:
    """Send a message to broadcast inbox."""
    _rc.send_message("broadcast", msg)


def archive_message(msg_id: str) -> None:
    """Archive a message (Redis: just delete from inbox, events persist in stream)."""
    # In Redis, messages are already consumed via rpop — no archive needed
    pass


# -----------------------------------------------------------------------
# Artifacts & Summaries (keep filesystem — these are large files)
# -----------------------------------------------------------------------


def share_artifact(source_path: str, name: str = "") -> Path:
    """Copy a file to shared artifacts. Keeps filesystem for large files."""
    import shutil

    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"Artifact source not found: {source_path}")
    artifact_name = name or src.name
    dst_dir = Path("/opt/swarm/artifacts")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / artifact_name
    shutil.copy2(src, dst)

    # Index in Redis for discovery
    r = _rc.get_client()
    r.hset(
        "artifact:" + artifact_name,
        mapping={
            "path": str(dst),
            "size": str(dst.stat().st_size),
            "created_at": now_iso(),
            "source": source_path,
        },
    )
    return dst


def list_artifacts() -> list[dict]:
    """List artifacts — checks Redis index first, falls back to filesystem."""
    r = _rc.get_client()
    keys = r.keys("artifact:*")
    if keys:
        pipe = r.pipeline()
        for k in keys:
            pipe.hgetall(k)
        return [a for a in pipe.execute() if a]
    # Fallback: scan filesystem
    artifacts_dir = Path("/opt/swarm/artifacts")
    if not artifacts_dir.exists():
        return []
    return [
        {
            "name": f.name,
            "path": str(f),
            "size": str(f.stat().st_size),
            "modified": datetime.fromtimestamp(
                f.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        }
        for f in artifacts_dir.iterdir()
        if f.is_file()
    ]


def share_session_summary(summary: dict) -> Path:
    """Write session summary. Keeps filesystem + indexes in Redis."""
    from util import atomic_write_yaml

    summaries_dir = Path("/opt/swarm/artifacts/summaries")
    summaries_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    host = hostname()
    filename = f"{host}-{ts}.yaml"
    path = summaries_dir / filename
    atomic_write_yaml(path, summary)

    # Index in Redis
    r = _rc.get_client()
    r.zadd("summaries", {filename: time.time()})
    r.hset(
        f"summary:{filename}",
        mapping={
            "hostname": host,
            "project": summary.get("project", ""),
            "context_for_next": summary.get("context_for_next", ""),
            "timestamp": now_iso(),
        },
    )
    return path


def get_relevant_summaries(project: str = "", limit: int = 5) -> list[dict]:
    """Get recent session summaries for a project."""
    import yaml as _yaml

    summaries_dir = Path("/opt/swarm/artifacts/summaries")
    if not summaries_dir.exists():
        return []
    files = sorted(
        summaries_dir.glob("*.yaml"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    results = []
    for f in files:
        if len(results) >= limit:
            break
        try:
            data = _yaml.safe_load(f.read_text()) or {}
            if project and data.get("project", "") != project:
                continue
            results.append(data)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
            continue
    return results


def get_latest_summary_context(project: str = "") -> str:
    """Get context_for_next from most recent summary."""
    summaries = get_relevant_summaries(project, limit=1)
    if summaries:
        return summaries[0].get("context_for_next", "")
    return ""


# -----------------------------------------------------------------------
# Decomposition (simplified for Redis)
# -----------------------------------------------------------------------


def decompose_task(task_id: str, subtasks: list[dict]) -> list[dict]:
    """Decompose a task into subtasks."""
    r = _rc.get_client()

    # Move parent to decomposed state
    r.zrem("tasks:pending", task_id)
    r.hset(
        f"task:{task_id}",
        mapping={
            "state": "decomposed",
            "decomposed_at": now_iso(),
            "subtask_count": str(len(subtasks)),
        },
    )

    created = []
    for i, sub in enumerate(subtasks):
        sub_id = f"{task_id}-{chr(97 + i)}"  # task-001-a, task-001-b, etc.
        sub_data = {
            "id": sub_id,
            "parent_id": task_id,
            "title": sub.get("title", f"Subtask {i + 1}"),
            "description": sub.get("description", ""),
            "project": sub.get("project", ""),
            "priority": sub.get("priority", "medium"),
            "requires": sub.get("requires", []),
            "created_by": hostname(),
            "created_at": now_iso(),
        }
        priority_score = {"critical": 1, "high": 3, "medium": 5, "low": 7}.get(
            sub_data["priority"], 5
        )
        _rc.create_task(sub_id, sub_data, priority=priority_score)
        created.append(sub_data)

    return created


def check_parent_completion(parent_id: str) -> bool:
    """Check if all subtasks of a parent are complete."""
    r = _rc.get_client()
    parent = _rc.get_task(parent_id)
    if not parent:
        return False
    subtask_count = int(parent.get("subtask_count", "0"))
    if subtask_count == 0:
        return False

    completed = 0
    for i in range(subtask_count):
        sub_id = f"{parent_id}-{chr(97 + i)}"
        sub = _rc.get_task(sub_id)
        if sub and sub.get("state") == "completed":
            completed += 1

    if completed >= subtask_count:
        # Auto-complete parent
        r.zadd("tasks:completed", {parent_id: int(time.time())})
        r.hset(
            f"task:{parent_id}",
            mapping={
                "state": "completed",
                "completed_at": now_iso(),
                "auto_completed": "true",
            },
        )
        return True
    return False


# -----------------------------------------------------------------------
# Worktrees (keep subprocess-based — no filesystem state to migrate)
# -----------------------------------------------------------------------


def create_worktree(task_id: str, project_path: str, branch_name: str = "") -> str:
    """Create a git worktree for a task. Returns worktree path."""
    import subprocess

    project = Path(project_path)
    if not (project / ".git").exists():
        raise ValueError(f"Not a git repository: {project_path}")

    base = Path("/tmp/swarm-worktrees")
    base.mkdir(parents=True, exist_ok=True)
    wt_path = base / task_id
    branch = branch_name or f"swarm/{task_id}"

    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(wt_path)],
        cwd=str(project),
        check=True,
        capture_output=True,
    )

    # Record in Redis
    r = _rc.get_client()
    r.hset(
        f"worktree:{task_id}",
        mapping={
            "path": str(wt_path),
            "branch": branch,
            "project": project_path,
            "created_at": now_iso(),
        },
    )

    return str(wt_path)


def verify_stale_pids(nodes: list[dict]) -> list[dict]:
    """Verify PIDs on stale nodes and correct state in-place (Redis backend).

    Mirrors swarm_lib.verify_stale_pids for backend parity.
    """
    import subprocess
    from datetime import datetime, timezone

    now_utc = datetime.now(timezone.utc)
    local_host = hostname()

    for node in nodes:
        state = node.get("state", "")
        if state not in ("active", "busy"):
            continue

        updated = node.get("updated_at", "")
        if updated:
            try:
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                if (now_utc - dt).total_seconds() < 300:
                    continue
            except (ValueError, TypeError):
                pass

        pid = node.get("pid")
        node_host = node.get("hostname", "")
        if not pid:
            continue

        if node_host == local_host:
            try:
                os.kill(int(pid), 0)
            except (OSError, ValueError):
                node["state"] = "idle"
                node["updated_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            ip = node.get("ip", "")
            target = ip or node_host
            if not target or ("." not in target and target not in ("node_gpu", "node_reserve2", "node_primary", "node_miner", "mega")):
                continue
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
                     "-o", "BatchMode=yes", target,
                     f"kill -0 {pid} 2>/dev/null && echo alive || echo dead"],
                    capture_output=True, text=True, timeout=3,
                )
                if result.stdout.strip() == "dead":
                    node["state"] = "idle"
                    node["updated_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass

    return nodes


def list_worktrees(project_path: str) -> list[dict]:
    """List active git worktrees."""
    import subprocess

    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project_path,
        capture_output=True,
        text=True,
    )
    worktrees = []
    current = {}
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line.split(" ", 1)[1]}
        elif line.startswith("HEAD "):
            current["head"] = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1]
    if current:
        worktrees.append(current)
    return worktrees
