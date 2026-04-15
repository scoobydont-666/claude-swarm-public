"""claude-swarm core library — status, tasks, artifacts, messages, decomposition, worktrees, summaries."""

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger(__name__)

import yaml

from util import (
    now_iso as _now_iso_util,
    hostname as _hostname_util,
    swarm_root,
    atomic_write_json,
    atomic_write_yaml,
)


# Backward-compatible aliases
def _now_iso() -> str:
    return _now_iso_util()


def _hostname() -> str:
    return _hostname_util()


def _swarm_root() -> Path:
    return swarm_root()


def _config_path() -> Path:
    """Return path to swarm.yaml config."""
    swarm_cfg = _swarm_root() / "config" / "swarm.yaml"
    if swarm_cfg.exists():
        return swarm_cfg
    project_cfg = Path("/opt/claude-swarm/config/swarm.yaml")
    if project_cfg.exists():
        return project_cfg
    raise FileNotFoundError("No swarm.yaml found")


def load_config() -> dict:
    """Load swarm configuration."""
    with open(_config_path()) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Atomic file operations — delegate to util
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, data: dict) -> None:
    atomic_write_json(path, data)


def _atomic_write_yaml(path: Path, data: dict) -> None:
    atomic_write_yaml(path, data)


def _locked_read_yaml(path: Path) -> dict:
    """Read a YAML file with shared lock."""
    with open(path) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return yaml.safe_load(f) or {}
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _locked_write_yaml(path: Path, data: dict) -> None:
    """Write a YAML file with exclusive lock, then atomic rename."""
    lock_path = path.with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            _atomic_write_yaml(path, data)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Status operations
# ---------------------------------------------------------------------------


def _is_nfs_healthy() -> bool:
    """Check if NFS is responding. Fast health check."""
    try:
        nfs_path = Path("/var/lib/swarm")
        if not nfs_path.is_dir():
            return False

        # Quick check: can we write to .nfs-health?
        test_file = nfs_path / ".nfs-health"
        try:
            import signal

            def timeout_handler(signum: int, frame: object) -> None:
                """Signal handler for NFS write timeout."""
                raise TimeoutError("NFS write timeout")

            # Set 2-second timeout
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(2)

            try:
                test_file.write_text("ok")
                test_file.unlink()
                signal.alarm(0)  # Cancel alarm
                return True
            finally:
                signal.alarm(0)  # Ensure alarm is cancelled
        except (OSError, TimeoutError):
            return False
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Suppressed: %s", exc)
        return False


def _status_dir() -> Path:
    d = _swarm_root() / "status"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_all_status() -> list[dict]:
    """Return status for all nodes.

    Cross-references /var/lib/swarm/status/ (per-host status files) with
    /var/lib/swarm/agents/ (per-agent heartbeat files) to fix stale displays.
    If an agent has a recent heartbeat on a host whose status file is stale,
    the status is updated from the agent's heartbeat timestamp.
    """
    results = []
    status_dir = _status_dir()
    for f in sorted(status_dir.glob("*.json")):
        try:
            with open(f) as fh:
                results.append(json.load(fh))
        except (json.JSONDecodeError, OSError):
            continue

    # Cross-reference with agent registry heartbeats
    agents_dir = Path(_swarm_root()) / "agents"
    if agents_dir.exists():
        # Build map: hostname → most recent agent heartbeat
        agent_heartbeats: dict[
            str, tuple[str, dict]
        ] = {}  # hostname → (timestamp, agent_data)
        for af in agents_dir.glob("*.json"):
            try:
                agent = json.loads(af.read_text())
                host = agent.get("hostname", "")
                hb = agent.get("last_heartbeat", "")
                if host and hb:
                    existing = agent_heartbeats.get(host)
                    if existing is None or hb > existing[0]:
                        agent_heartbeats[host] = (hb, agent)
            except (json.JSONDecodeError, OSError):
                continue

        # Enrich status entries with agent heartbeat data
        for node in results:
            hostname = node.get("hostname", "")
            if hostname not in agent_heartbeats:
                continue

            hb_time, agent_data = agent_heartbeats[hostname]
            status_updated = node.get("updated_at", "")

            # If agent heartbeat is newer than status file, use it
            if hb_time > status_updated:
                node["updated_at"] = hb_time
                # Update state from agent if status was stale
                agent_state = agent_data.get("state", "")
                if agent_state:
                    node["state"] = agent_state
                if agent_data.get("project"):
                    node["project"] = agent_data["project"]
                if agent_data.get("model"):
                    node["model"] = agent_data["model"]
                if agent_data.get("pid"):
                    node["pid"] = agent_data["pid"]

    return results


def verify_stale_pids(nodes: list[dict]) -> list[dict]:
    """Verify PIDs on stale nodes and correct state in-place.

    Separated from get_all_status() so cleanup_stale_nodes() gets raw data
    while the CLI display gets corrected state. Call this from the display layer.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    local_hostname = _hostname()

    for node in nodes:
        state = node.get("state", "")
        if state not in ("active", "busy"):
            continue

        updated = node.get("updated_at", "")
        if updated:
            try:
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                if (now - dt).total_seconds() < 300:
                    continue  # Recent heartbeat, trust it
            except (ValueError, TypeError):
                pass

        pid = node.get("pid")
        hostname = node.get("hostname", "")
        if not pid:
            continue

        if hostname == local_hostname:
            try:
                os.kill(int(pid), 0)
            except (OSError, ValueError):
                node["state"] = "idle"
                node["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            ip = node.get("ip", "")
            target = ip or hostname
            # Skip test fixtures / unreachable hosts
            if not target or (
                "." not in target
                and target
                not in (
                    "gpu-server-1",
                    "gpu-server-2",
                    "orchestration-node",
                    "rainbow",
                )
            ):
                continue
            try:
                result = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "ConnectTimeout=2",
                        "-o",
                        "StrictHostKeyChecking=no",
                        "-o",
                        "BatchMode=yes",
                        target,
                        f"kill -0 {pid} 2>/dev/null && echo alive || echo dead",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if result.stdout.strip() == "dead":
                    node["state"] = "idle"
                    node["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Suppressed: %s", exc)
                pass

    return nodes


def get_status(hostname: Optional[str] = None) -> Optional[dict]:
    """Return status for a specific node."""
    hostname = hostname or _hostname()
    path = _status_dir() / f"{hostname}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def update_status(
    state: str = "active",
    current_task: str = "",
    project: str = "",
    session_id: str = "",
    model: str = "",
    pid: Optional[int] = None,
    capabilities: Optional[dict] = None,
    skills_loaded: int = 0,
    uptime_seconds: int = 0,
) -> dict:
    """Write/update this node's status file."""
    hostname = _hostname()
    config = load_config()
    node_cfg = config.get("nodes", {}).get(hostname, {})

    caps = capabilities or {
        "gpu": "gpu" in node_cfg.get("capabilities", []),
        "docker": "docker" in node_cfg.get("capabilities", []),
        "ollama": "ollama" in node_cfg.get("capabilities", []),
        "tailscale": "tailscale" in node_cfg.get("capabilities", []),
    }

    status = {
        "hostname": hostname,
        "ip": node_cfg.get("ip", "unknown"),
        "state": state,
        "session_id": session_id,
        "model": model,
        "current_task": current_task,
        "project": project,
        "skills_loaded": skills_loaded,
        "uptime_seconds": uptime_seconds,
        "updated_at": _now_iso(),
        "pid": pid or os.getpid(),
        "capabilities": caps,
    }

    # Graceful NFS degradation: try to write status, fall back to local if NFS unhealthy
    try:
        path = _status_dir() / f"{hostname}.json"
        _atomic_write_json(path, status)
    except OSError as exc:
        # NFS may be unhealthy — try local fallback
        if not _is_nfs_healthy():
            local_fallback = Path.home() / ".swarm-status" / f"{hostname}.json"
            local_fallback.parent.mkdir(parents=True, exist_ok=True)
            try:
                _atomic_write_json(local_fallback, status)
                import logging

                logging.getLogger("swarm").warning(
                    "NFS unhealthy; wrote status to local fallback: %s", local_fallback
                )
            except Exception as exc2:
                import logging

                logging.getLogger("swarm").error(
                    "Failed to write status file (NFS and local): %s, %s", exc, exc2
                )
        else:
            raise

    # Also update SQLite database for agent tracking
    try:
        from agent_db import AgentDB

        db = AgentDB()
        db.upsert_agent(
            hostname=hostname,
            ip=status.get("ip", ""),
            pid=status.get("pid", 0),
            state=state,
            current_task=current_task,
            project=project,
            model=model,
            session_id=session_id,
            capabilities=caps,
        )
    except ImportError:
        pass  # agent_db not available

    # Reconcile any locally-cached task files back to NFS on heartbeat
    try:
        _reconcile_local_tasks()
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Suppressed: %s", exc)
        pass

    return status


def mark_stale_nodes(threshold_seconds: int = 300) -> list[str]:
    """Mark active nodes as offline if their status is older than threshold. Returns list of stale hostnames.
    Only marks 'active' or 'busy' nodes as stale — 'idle' is a valid resting state."""
    stale = []
    now = datetime.now(timezone.utc)
    for status in get_all_status():
        state = status.get("state", "")
        if state in ("offline", "idle"):
            continue
        updated = status.get("updated_at", "")
        if not updated:
            continue
        try:
            updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            age = (now - updated_dt).total_seconds()
            if age > threshold_seconds:
                # Mark offline
                hostname = status["hostname"]
                path = _status_dir() / f"{hostname}.json"
                status["state"] = "offline"
                status["updated_at"] = _now_iso()
                _atomic_write_json(path, status)
                stale.append(hostname)
        except (ValueError, KeyError):
            continue
    return stale


def cleanup_stale_nodes(threshold_seconds: int = 300, verify_pid: bool = True) -> dict:
    """Detect and clean up stale nodes. Optionally verify PID via SSH.

    Returns dict with 'cleaned' (list of hostnames reset to idle) and
    'orphaned_tasks' (list of task IDs requeued to pending).
    """
    cleaned = []
    orphaned_tasks = []
    now = datetime.now(timezone.utc)

    for status in get_all_status():
        state = status.get("state", "")
        if state not in ("active", "busy"):
            continue
        updated = status.get("updated_at", "")
        if not updated:
            continue
        try:
            updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            age = (now - updated_dt).total_seconds()
        except (ValueError, KeyError):
            continue

        hostname = status.get("hostname", "")
        pid = status.get("pid")
        is_stale = age > threshold_seconds

        # If within threshold, skip
        if not is_stale:
            continue

        # Optionally verify PID is actually dead via SSH
        pid_alive = False
        if verify_pid and pid and hostname and hostname != _hostname():
            try:
                result = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "ConnectTimeout=5",
                        "-o",
                        "BatchMode=yes",
                        hostname,
                        f"ps -p {pid} -o pid= 2>/dev/null",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                pid_alive = result.returncode == 0 and str(pid) in result.stdout
            except (subprocess.TimeoutExpired, OSError):
                # Can't reach host — treat as stale
                pid_alive = False
        elif verify_pid and pid and hostname == _hostname():
            # Local PID check
            try:
                os.kill(pid, 0)
                pid_alive = True
            except (ProcessLookupError, PermissionError):
                pid_alive = False

        if pid_alive:
            # PID is alive — update timestamp so it's not stale anymore
            continue

        # Reset node to idle
        path = _status_dir() / f"{hostname}.json"
        status["state"] = "idle"
        status["current_task"] = ""
        status["project"] = ""
        status["session_id"] = ""
        status["model"] = ""
        status["updated_at"] = _now_iso()
        _atomic_write_json(path, status)
        cleaned.append(hostname)

    # Find orphaned claimed tasks (claimed by cleaned hosts)
    if cleaned:
        claimed_dir = _tasks_dir("claimed")
        for f in claimed_dir.glob("task-*.yaml"):
            task = _locked_read_yaml(f)
            if task.get("claimed_by") in cleaned:
                # Move back to pending
                task.pop("claimed_by", None)
                task.pop("claimed_at", None)
                retries = task.get("_retries", 0) + 1
                task["_retries"] = retries
                if retries <= 3:
                    dst = _tasks_dir("pending") / f.name
                    _locked_write_yaml(dst, task)
                    os.remove(f)
                    orphaned_tasks.append(task.get("id", f.stem))

    return {"cleaned": cleaned, "orphaned_tasks": orphaned_tasks}


# ---------------------------------------------------------------------------
# Task operations
# ---------------------------------------------------------------------------


def _tasks_dir(stage: str = "pending") -> Path:
    d = _swarm_root() / "tasks" / stage
    d.mkdir(parents=True, exist_ok=True)
    return d


def _next_task_id() -> str:
    """Generate next task ID by scanning all task directories."""
    max_id = 0
    for stage in ("pending", "claimed", "completed"):
        task_dir = _tasks_dir(stage)
        for f in task_dir.glob("task-*.yaml"):
            try:
                num = int(f.stem.split("-")[1])
                max_id = max(max_id, num)
            except (IndexError, ValueError):
                continue
    return f"task-{max_id + 1:03d}"


class TaskIndex:
    """In-memory cache for task listings, invalidated by directory mtime."""

    def __init__(self) -> None:
        self._cache: dict[str, list[dict]] = {}  # stage -> list of task dicts
        self._mtimes: dict[str, float] = {}  # stage -> last known mtime
        self._lock = threading.Lock()

    def list_tasks(self, stage: str) -> list[dict]:
        """Return cached task list for stage, refreshing if directory changed."""
        stage_dir = _tasks_dir(stage)
        if not stage_dir.is_dir():
            return []
        current_mtime = stage_dir.stat().st_mtime
        with self._lock:
            if stage in self._mtimes and self._mtimes[stage] == current_mtime:
                return list(self._cache.get(stage, []))
            # Re-scan
            tasks = []
            for f in sorted(stage_dir.glob("task-*.yaml")):
                try:
                    task = _locked_read_yaml(f)
                    task["_stage"] = stage
                    task["_file"] = str(f)
                    tasks.append(task)
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("Suppressed: %s", exc)
                    continue
            self._cache[stage] = tasks
            self._mtimes[stage] = current_mtime
            return list(tasks)

    def invalidate(self, stage: Optional[str] = None) -> None:
        """Force cache invalidation. If stage given, invalidate only that stage."""
        with self._lock:
            if stage:
                self._mtimes.pop(stage, None)
                self._cache.pop(stage, None)
            else:
                self._mtimes.clear()
                self._cache.clear()


_task_index = TaskIndex()  # Module-level singleton


def list_tasks(stage: Optional[str] = None) -> list[dict]:
    """List tasks, optionally filtered by stage."""
    stages = [stage] if stage else ["pending", "claimed", "completed", "decomposed"]
    results = []
    for s in stages:
        results.extend(_task_index.list_tasks(s))
    return results


def create_task(
    title: str,
    description: str = "",
    project: str = "",
    priority: str = "medium",
    requires: Optional[list[str]] = None,
    estimated_minutes: int = 0,
) -> dict:
    """Create a new pending task."""
    task_id = _next_task_id()
    task = {
        "id": task_id,
        "title": title,
        "description": description,
        "project": project,
        "priority": priority,
        "requires": requires or [],
        "created_by": _hostname(),
        "created_at": _now_iso(),
        "estimated_minutes": estimated_minutes,
    }
    path = _tasks_dir("pending") / f"{task_id}.yaml"
    _locked_write_yaml(path, task)
    return task


def _local_tasks_dir(stage: str) -> Path:
    """Return local fallback task directory for a given stage (claimed/completed)."""
    d = Path.home() / ".swarm-tasks" / stage
    d.mkdir(parents=True, exist_ok=True)
    return d


def _reconcile_local_tasks() -> list[str]:
    """Move locally-cached task files back to NFS when NFS is healthy again.

    Called from update_status() (the heartbeat path). Returns list of task IDs moved.
    """
    import logging as _logging

    _log = _logging.getLogger("swarm")
    moved = []

    if not _is_nfs_healthy():
        return moved

    for stage in ("claimed", "completed"):
        local_dir = _local_tasks_dir(stage)
        if not local_dir.exists():
            continue
        task_files = list(local_dir.glob("*.yaml"))
        if not task_files:
            continue
        for task_file in task_files:
            try:
                nfs_dir = _tasks_dir(stage)
                dst = nfs_dir / task_file.name
                # Only move if not already on NFS
                if not dst.exists():
                    _atomic_write_yaml(dst, yaml.safe_load(task_file.read_text()) or {})
                task_file.unlink()
                moved.append(task_file.stem)
                _log.info(
                    "reconcile_local_tasks: moved %s/%s back to NFS",
                    stage,
                    task_file.stem,
                )
            except (OSError, yaml.YAMLError) as exc:
                _log.warning(
                    "reconcile_local_tasks: failed to move %s: %s", task_file, exc
                )

    return moved


def claim_task(task_id: str) -> dict:
    """Claim a pending task for this host. Moves from pending/ to claimed/.

    Falls back to ~/.swarm-tasks/claimed/ if NFS is unavailable.
    """
    import logging as _logging

    _log = _logging.getLogger("swarm")

    src = _tasks_dir("pending") / f"{task_id}.yaml"
    if not src.exists():
        raise FileNotFoundError(f"Task {task_id} not found in pending/")

    lock_path = src.with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            if not src.exists():
                raise FileNotFoundError(
                    f"Task {task_id} already claimed by another node"
                )
            task = yaml.safe_load(open(src)) or {}
            hostname = _hostname()
            task["claimed_by"] = hostname
            task["claimed_at"] = _now_iso()
            try:
                dst = _tasks_dir("claimed") / f"{task_id}.yaml"
                _atomic_write_yaml(dst, task)
                os.remove(src)
            except OSError as nfs_exc:
                if not _is_nfs_healthy():
                    # NFS is down — write to local fallback
                    local_dst = _local_tasks_dir("claimed") / f"{task_id}.yaml"
                    _atomic_write_yaml(local_dst, task)
                    try:
                        os.remove(src)
                    except OSError:
                        pass
                    _log.warning(
                        "claim_task: NFS unhealthy, wrote %s to local fallback: %s",
                        task_id,
                        local_dst,
                    )
                else:
                    raise nfs_exc

            # Record action in agent_db
            try:
                from agent_db import AgentDB

                db = AgentDB()
                db.record_task_action(
                    task_id,
                    hostname,
                    "claimed",
                    {
                        "priority": task.get("priority", ""),
                        "requires": task.get("requires", []),
                    },
                )
            except ImportError:
                pass
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
    # Clean up lock file
    lock_path.unlink(missing_ok=True)
    return task


def complete_task(task_id: str, result_artifact: str = "") -> dict:
    """Complete a claimed task. Moves from claimed/ to completed/.

    Falls back to ~/.swarm-tasks/completed/ if NFS is unavailable.
    """
    import logging as _logging

    _log = _logging.getLogger("swarm")

    src = _tasks_dir("claimed") / f"{task_id}.yaml"
    # Also check local fallback if not on NFS
    local_src = _local_tasks_dir("claimed") / f"{task_id}.yaml"
    if not src.exists() and local_src.exists():
        src = local_src

    if not src.exists():
        raise FileNotFoundError(f"Task {task_id} not found in claimed/")

    task = _locked_read_yaml(src)
    hostname = _hostname()
    task["completed_by"] = hostname
    task["completed_at"] = _now_iso()
    if result_artifact:
        task["result_artifact"] = result_artifact

    try:
        dst = _tasks_dir("completed") / f"{task_id}.yaml"
        _locked_write_yaml(dst, task)
        os.remove(src)
    except OSError as nfs_exc:
        if not _is_nfs_healthy():
            # NFS is down — write to local fallback
            local_dst = _local_tasks_dir("completed") / f"{task_id}.yaml"
            _atomic_write_yaml(local_dst, task)
            try:
                os.remove(src)
            except OSError:
                pass
            _log.warning(
                "complete_task: NFS unhealthy, wrote %s to local fallback: %s",
                task_id,
                local_dst,
            )
        else:
            raise nfs_exc

    # Record action in agent_db
    try:
        from agent_db import AgentDB

        db = AgentDB()
        db.record_task_action(
            task_id,
            hostname,
            "completed",
            {
                "result_artifact": result_artifact,
            },
        )
    except ImportError:
        pass

    return task


def get_matching_tasks(capabilities: Optional[dict] = None) -> list[dict]:
    """Find pending tasks that match this node's capabilities."""
    if capabilities is None:
        status = get_status()
        capabilities = status.get("capabilities", {}) if status else {}

    matching = []
    for task in list_tasks("pending"):
        requires = task.get("requires", [])
        if not requires:
            matching.append(task)
            continue
        # Check all requirements are met
        if all(capabilities.get(req, False) for req in requires):
            matching.append(task)
    return matching


# ---------------------------------------------------------------------------
# Message operations
# ---------------------------------------------------------------------------


def _inbox_dir(target: Optional[str] = None) -> Path:
    target = target or _hostname()
    d = _swarm_root() / "messages" / "inbox" / target
    d.mkdir(parents=True, exist_ok=True)
    return d


def _archive_dir() -> Path:
    d = _swarm_root() / "messages" / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def send_message(target: str, text: str, sender: Optional[str] = None) -> Path:
    """Send a message to a specific host or 'broadcast'."""
    sender = sender or _hostname()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    filename = f"{ts}-from-{sender}.yaml"
    msg = {
        "from": sender,
        "to": target,
        "text": text,
        "sent_at": _now_iso(),
        "read": False,
    }
    path = _inbox_dir(target) / filename
    _atomic_write_yaml(path, msg)
    return path


def broadcast_message(text: str, sender: Optional[str] = None) -> list[Path]:
    """Send a message to broadcast inbox."""
    return [send_message("broadcast", text, sender)]


def read_inbox(hostname: Optional[str] = None) -> list[dict]:
    """Read all messages in this node's inbox + broadcast."""
    hostname = hostname or _hostname()
    messages = []

    for inbox in [_inbox_dir(hostname), _inbox_dir("broadcast")]:
        for f in sorted(inbox.glob("*.yaml")):
            try:
                msg = yaml.safe_load(open(f)) or {}
                msg["_file"] = str(f)
                msg["_source"] = "broadcast" if "broadcast" in str(f) else "direct"
                messages.append(msg)
            except (yaml.YAMLError, OSError):
                continue
    return messages


def archive_message(msg_path: str) -> None:
    """Move a message to the archive."""
    src = Path(msg_path)
    if not src.exists():
        return
    dst = _archive_dir() / src.name
    shutil.move(str(src), str(dst))


# ---------------------------------------------------------------------------
# Artifact operations
# ---------------------------------------------------------------------------


def _artifacts_dir() -> Path:
    d = _swarm_root() / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_artifacts() -> list[dict]:
    """List all shared artifacts."""
    results = []
    for f in sorted(_artifacts_dir().iterdir()):
        if f.is_file() and not f.name.startswith("."):
            results.append(
                {
                    "name": f.name,
                    "path": str(f),
                    "size_bytes": f.stat().st_size,
                    "modified_at": datetime.fromtimestamp(
                        f.stat().st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
    return results


def share_artifact(source_path: str, name: Optional[str] = None) -> Path:
    """Copy a file to the shared artifacts directory."""
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")
    dest_name = name or src.name
    dst = _artifacts_dir() / dest_name
    shutil.copy2(str(src), str(dst))
    return dst


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def health_check() -> dict:
    """Run a full health check of the swarm."""
    root = _swarm_root()
    config = load_config()
    stale_threshold = config.get("heartbeat", {}).get("stale_threshold_seconds", 300)

    result: dict[str, Any] = {
        "swarm_root": str(root),
        "nfs_available": Path("/var/lib/swarm").is_dir(),
        "config_loaded": True,
        "timestamp": _now_iso(),
        "nodes": {},
        "stale_nodes": [],
        "pending_tasks": 0,
        "claimed_tasks": 0,
        "completed_tasks": 0,
    }

    # Node health
    now = datetime.now(timezone.utc)
    for status in get_all_status():
        hostname = status.get("hostname", "unknown")
        updated = status.get("updated_at", "")
        age = -1
        if updated:
            try:
                updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age = int((now - updated_dt).total_seconds())
            except ValueError:
                pass
        is_stale = age > stale_threshold if age >= 0 else True
        result["nodes"][hostname] = {
            "state": status.get("state", "unknown"),
            "age_seconds": age,
            "stale": is_stale,
            "current_task": status.get("current_task", ""),
        }
        if is_stale and status.get("state") not in ("offline", "idle"):
            result["stale_nodes"].append(hostname)

    # Task counts
    result["pending_tasks"] = len(list_tasks("pending"))
    result["claimed_tasks"] = len(list_tasks("claimed"))
    result["completed_tasks"] = len(list_tasks("completed"))

    return result


# ---------------------------------------------------------------------------
# Improvement 1: Task Decomposition
# ---------------------------------------------------------------------------


class TaskDecomposer:
    """Rule-based task decomposition suggester."""

    # CPA exam sections for "all sections" decomposition
    CPA_SECTIONS = ["FAR", "AUD", "REG", "BAR"]

    # Patterns that trigger decomposition suggestions
    SPLIT_PATTERNS = {
        "all_sections": re.compile(r"\ball\s+sections?\b", re.IGNORECASE),
        "across": re.compile(r"\bacross\b", re.IGNORECASE),
        "generate_and_validate": re.compile(
            r"\bgenerate\b.*\bvalidate\b", re.IGNORECASE
        ),
        "multiple_projects": re.compile(
            r"\b(christi|monero|str[- ]intel|taxprep|examforge|audit[- ]sentinel|hashrate|clausehound|prompt[- ]forge|documint)\b",
            re.IGNORECASE,
        ),
    }

    # Capability inference from task text
    CAPABILITY_KEYWORDS = {
        "ollama": re.compile(
            r"\b(ollama|llm|inference|generate|embed)\b", re.IGNORECASE
        ),
        "gpu": re.compile(r"\b(gpu|cuda|tensor|train|comfyui)\b", re.IGNORECASE),
        "docker": re.compile(r"\b(docker|container|swarm|deploy)\b", re.IGNORECASE),
    }

    # Project routing hints
    PROJECT_ROUTING = {
        "monero": "monero-farm",
        "mining": "monero-farm",
        "p2pool": "monero-farm",
        "xmrig": "monero-farm",
        "christi": "christi-project",
        "tax advisor": "christi-project",
        "str": "str-project",
        "rental": "str-project",
        "taxprep": "taxprep-project",
        "examforge": "examforge",
    }

    @classmethod
    def _infer_capabilities(cls, text: str) -> list[str]:
        """Infer required capabilities from task text."""
        caps = []
        for cap, pattern in cls.CAPABILITY_KEYWORDS.items():
            if pattern.search(text):
                caps.append(cap)
        return caps

    @classmethod
    def suggest(cls, task: dict) -> list[dict]:
        """Suggest subtask decomposition for a task.

        Args:
            task: Task dictionary with title, description, project, etc.

        Returns:
            List of suggested subtask dictionaries
        """
        title = task.get("title", "")
        description = task.get("description", "")
        combined = f"{title} {description}"
        project = task.get("project", "")
        suggestions: list[dict] = []

        # Pattern: "all sections" or "across" → split per CPA section
        if cls.SPLIT_PATTERNS["all_sections"].search(combined) or (
            cls.SPLIT_PATTERNS["across"].search(combined)
            and any(s.lower() in combined.lower() for s in cls.CPA_SECTIONS)
        ):
            base_caps = cls._infer_capabilities(combined)
            for section in cls.CPA_SECTIONS:
                suggestions.append(
                    {
                        "title": f"{title} — {section}",
                        "description": f"Section-specific subtask for {section}",
                        "project": project,
                        "requires": base_caps,
                    }
                )
            # Add validation subtask
            suggestions.append(
                {
                    "title": f"Validate {title}",
                    "description": "Validate all generated outputs",
                    "project": project,
                    "requires": base_caps,
                }
            )
            return suggestions

        # Pattern: "generate AND validate" → split into two phases
        if cls.SPLIT_PATTERNS["generate_and_validate"].search(combined):
            gen_caps = cls._infer_capabilities(combined)
            suggestions.append(
                {
                    "title": f"Generate: {title}",
                    "description": "Generation phase",
                    "project": project,
                    "requires": gen_caps,
                }
            )
            suggestions.append(
                {
                    "title": f"Validate: {title}",
                    "description": "Validation phase",
                    "project": project,
                    "requires": gen_caps,
                }
            )
            return suggestions

        # Pattern: multiple projects mentioned → split per project
        found_projects: list[str] = []
        for keyword, proj in cls.PROJECT_ROUTING.items():
            if keyword.lower() in combined.lower() and proj not in found_projects:
                found_projects.append(proj)
        if len(found_projects) > 1:
            for proj in found_projects:
                proj_caps = cls._infer_capabilities(combined)
                suggestions.append(
                    {
                        "title": f"{title} — {proj}",
                        "description": f"Subtask scoped to {proj}",
                        "project": f"/opt/{proj}/",
                        "requires": proj_caps,
                    }
                )
            return suggestions

        return suggestions


def decompose_task(task_id: str, subtasks: list[dict]) -> dict:
    """Decompose a parent task into subtasks.

    Moves parent to 'decomposed' state and creates subtask files in pending/.
    Each subtask gets a parent_id linking back and inherits project from parent.
    Returns updated parent task dict.
    """
    # Find parent in pending
    parent_path = _tasks_dir("pending") / f"{task_id}.yaml"
    if not parent_path.exists():
        raise FileNotFoundError(f"Task {task_id} not found in pending/")

    lock_path = parent_path.with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            if not parent_path.exists():
                raise FileNotFoundError(f"Task {task_id} already moved by another node")
            parent = yaml.safe_load(open(parent_path)) or {}

            subtask_ids = []
            for i, sub in enumerate(subtasks):
                sub_id = f"{task_id}-{chr(97 + i)}"  # task-001-a, task-001-b, etc.
                subtask_data = {
                    "id": sub_id,
                    "parent_id": task_id,
                    "title": sub.get("title", f"Subtask {i + 1}"),
                    "description": sub.get("description", ""),
                    "project": sub.get("project", parent.get("project", "")),
                    "priority": sub.get("priority", parent.get("priority", "medium")),
                    "requires": sub.get("requires", []),
                    "created_by": _hostname(),
                    "created_at": _now_iso(),
                    "estimated_minutes": sub.get("estimated_minutes", 0),
                }
                sub_path = _tasks_dir("pending") / f"{sub_id}.yaml"
                _atomic_write_yaml(sub_path, subtask_data)
                subtask_ids.append(sub_id)

            # Move parent to decomposed state
            parent["state"] = "decomposed"
            parent["subtasks"] = subtask_ids
            parent["decomposed_at"] = _now_iso()
            parent["decomposed_by"] = _hostname()

            # Write parent to decomposed dir
            decomposed_dir = _tasks_dir("decomposed")
            dst = decomposed_dir / f"{task_id}.yaml"
            _atomic_write_yaml(dst, parent)
            os.remove(parent_path)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
    lock_path.unlink(missing_ok=True)
    return parent


def check_parent_completion(parent_id: str) -> bool:
    """Check if all subtasks of a decomposed parent are complete. If so, auto-complete the parent.

    Returns True if parent was auto-completed, False otherwise.
    """
    decomposed_dir = _tasks_dir("decomposed")
    parent_path = decomposed_dir / f"{parent_id}.yaml"
    if not parent_path.exists():
        return False

    parent = _locked_read_yaml(parent_path)
    subtask_ids = parent.get("subtasks", [])
    if not subtask_ids:
        return False

    completed_dir = _tasks_dir("completed")
    for sub_id in subtask_ids:
        if not (completed_dir / f"{sub_id}.yaml").exists():
            return False

    # All subtasks complete — auto-complete parent
    parent["completed_at"] = _now_iso()
    parent["completed_by"] = "auto"
    parent["state"] = "completed"
    dst = completed_dir / f"{parent_id}.yaml"
    _locked_write_yaml(dst, parent)
    os.remove(parent_path)
    return True


# Override complete_task to check parent completion after subtask completes
_original_complete_task = complete_task


def complete_task(task_id: str, result_artifact: str = "") -> dict:
    """Complete a claimed task. If it has a parent_id, check parent auto-completion."""
    task = _original_complete_task(task_id, result_artifact)

    parent_id = task.get("parent_id")
    if parent_id:
        check_parent_completion(parent_id)

    return task


# ---------------------------------------------------------------------------
# Improvement 2: Worktree Isolation
# ---------------------------------------------------------------------------


def _validate_git_repo(project_path: str) -> bool:
    """Check that project_path is a valid git repo."""
    git_dir = Path(project_path) / ".git"
    return git_dir.exists() or (Path(project_path) / "HEAD").exists()


def _safe_subprocess(
    cmd: list[str], cwd: Optional[str] = None
) -> subprocess.CompletedProcess:
    """Run a subprocess with validated inputs — no shell injection."""
    # Validate: no shell metacharacters in any argument
    for arg in cmd:
        if any(c in arg for c in (";", "|", "&", "$", "`", "\n", "\r")):
            raise ValueError(f"Invalid character in command argument: {arg!r}")
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def _is_path_within_base(base: str, candidate: str) -> bool:
    """Check that candidate path resolves within base — prevents traversal attacks.

    Guards against task_id containing '../' or other path traversal sequences.
    Uses trailing separator to prevent partial directory name matches
    (e.g., /tmp/worktrees-evil should not match /tmp/worktrees).
    """
    base_resolved = str(Path(base).resolve())
    candidate_resolved = str(Path(candidate).resolve())
    # Exact match (base itself) or within base (with separator)
    return candidate_resolved == base_resolved or candidate_resolved.startswith(
        base_resolved + "/"
    )


def create_worktree(project_path: str, task_id: str) -> str:
    """Create a git worktree for a task.

    Creates branch swarm/<hostname>/<task_id> and worktree at the configured base path.
    Records worktree path in task YAML.
    Returns the worktree path.
    """
    if not _validate_git_repo(project_path):
        raise ValueError(f"Not a git repository: {project_path}")

    config = load_config()
    base_path = config.get("worktrees", {}).get("base_path", "/tmp/swarm-worktrees")
    branch_prefix = config.get("worktrees", {}).get("branch_prefix", "swarm")
    hostname = _hostname()

    worktree_path = f"{base_path}/{task_id}"

    # Path traversal guard — prevent task_id like "../../etc/passwd"
    if not _is_path_within_base(base_path, worktree_path):
        raise ValueError(f"Invalid task_id — path traversal detected: {task_id}")
    branch_name = f"{branch_prefix}/{hostname}/{task_id}"

    # Create base dir if needed
    Path(base_path).mkdir(parents=True, exist_ok=True)

    result = _safe_subprocess(
        ["git", "worktree", "add", worktree_path, "-b", branch_name],
        cwd=project_path,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    # Record in task YAML if claimed
    claimed_path = _tasks_dir("claimed") / f"{task_id}.yaml"
    if claimed_path.exists():
        task = _locked_read_yaml(claimed_path)
        task["worktree"] = worktree_path
        task["branch"] = branch_name
        _locked_write_yaml(claimed_path, task)

    return worktree_path


def complete_worktree(project_path: str, task_id: str, merge: bool = False) -> dict:
    """Complete and clean up a worktree.

    If merge=True: merge the worktree branch back to main.
    If merge=False: push the branch, record as artifact.
    Always: remove the worktree and record the branch name.

    Returns dict with branch name and action taken.
    """
    if not _validate_git_repo(project_path):
        raise ValueError(f"Not a git repository: {project_path}")

    config = load_config()
    base_path = config.get("worktrees", {}).get("base_path", "/tmp/swarm-worktrees")
    branch_prefix = config.get("worktrees", {}).get("branch_prefix", "swarm")
    hostname = _hostname()

    worktree_path = f"{base_path}/{task_id}"
    branch_name = f"{branch_prefix}/{hostname}/{task_id}"
    action = ""

    if merge:
        # Merge branch into current HEAD (main)
        result = _safe_subprocess(
            [
                "git",
                "merge",
                branch_name,
                "--no-ff",
                "-m",
                f"Merge swarm task {task_id}",
            ],
            cwd=project_path,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git merge failed: {result.stderr.strip()}")
        action = "merged"
    else:
        # Push branch to remote
        result = _safe_subprocess(
            ["git", "push", "-u", "origin", branch_name],
            cwd=project_path,
        )
        if result.returncode != 0:
            # Push failure is non-fatal — branch still exists locally
            action = "branch-only-local"
        else:
            action = "branch-pushed"

    # Remove worktree
    if Path(worktree_path).exists():
        _safe_subprocess(
            ["git", "worktree", "remove", worktree_path, "--force"],
            cwd=project_path,
        )

    # Record branch as artifact
    artifact_data = {
        "task_id": task_id,
        "branch": branch_name,
        "project": project_path,
        "action": action,
        "completed_at": _now_iso(),
        "completed_by": hostname,
    }
    artifact_dir = _swarm_root() / "artifacts" / "branches"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{task_id}.yaml"
    _atomic_write_yaml(artifact_path, artifact_data)

    return artifact_data


def list_worktrees(project_path: str) -> list[dict]:
    """List active git worktrees for a project."""
    if not _validate_git_repo(project_path):
        return []

    result = _safe_subprocess(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project_path,
    )
    if result.returncode != 0:
        return []

    worktrees = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line.split(" ", 1)[1]}
        elif line.startswith("HEAD "):
            current["head"] = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1]
        elif line == "detached":
            current["detached"] = "true"
    if current:
        worktrees.append(current)

    return worktrees


# ---------------------------------------------------------------------------
# Improvement 3: Session Summary Sharing
# ---------------------------------------------------------------------------


@dataclass
class SessionSummary:
    """Summary of a Claude Code session for cross-instance context sharing."""

    hostname: str
    session_id: str
    timestamp: str
    project: str
    task_id: Optional[str] = None
    duration_minutes: int = 0
    key_decisions: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    issues_found: list[str] = field(default_factory=list)
    artifacts_produced: list[str] = field(default_factory=list)
    context_for_next: str = ""


def _summaries_dir() -> Path:
    d = _swarm_root() / "artifacts" / "summaries"
    d.mkdir(parents=True, exist_ok=True)
    return d


def share_session_summary(summary: SessionSummary) -> Path:
    """Write a session summary to swarm/artifacts/summaries/."""
    ts = summary.timestamp.replace(":", "").replace("-", "")
    filename = f"{summary.hostname}-{ts}.yaml"
    path = _summaries_dir() / filename
    data = asdict(summary)
    _atomic_write_yaml(path, data)
    return path


def get_relevant_summaries(project: str, limit: int = 5) -> list[dict]:
    """Return recent session summaries for a given project, sorted by recency."""
    summaries_dir = _summaries_dir()
    if not summaries_dir.exists():
        return []

    results = []
    for f in summaries_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(open(f)) or {}
            if data.get("project", "") == project:
                data["_file"] = str(f)
                results.append(data)
        except (yaml.YAMLError, OSError):
            continue

    # Sort by timestamp descending
    results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return results[:limit]


def get_latest_summary_context(project: str) -> str:
    """Get context_for_next from the most recent summary for a project.

    Used by session-start hook to inject context into systemMessage.
    """
    summaries = get_relevant_summaries(project, limit=1)
    if not summaries:
        return ""
    s = summaries[0]
    hostname = s.get("hostname", "unknown")
    ts = s.get("timestamp", "unknown")
    context = s.get("context_for_next", "")
    duration = s.get("duration_minutes", 0)
    if not context:
        return ""
    dur_str = f"{duration}m ago" if duration else ts
    return f"Last session on this project (by {hostname}, {dur_str}): {context}"


def generate_session_summary(
    project: str,
    session_id: str = "",
    task_id: Optional[str] = None,
    duration_minutes: int = 0,
    key_decisions: Optional[list[str]] = None,
    issues_found: Optional[list[str]] = None,
    artifacts_produced: Optional[list[str]] = None,
    context_for_next: str = "",
) -> SessionSummary:
    """Generate a session summary, auto-detecting files changed from git diff."""
    hostname = _hostname()

    # Auto-detect files changed via git
    files_changed: list[str] = []
    if project and Path(project).is_dir():
        result = _safe_subprocess(["git", "diff", "--name-only", "HEAD"], cwd=project)
        if result.returncode == 0 and result.stdout.strip():
            files_changed = [
                line.strip()
                for line in result.stdout.strip().splitlines()
                if line.strip()
            ]
        # Also check staged
        result2 = _safe_subprocess(
            ["git", "diff", "--name-only", "--cached"], cwd=project
        )
        if result2.returncode == 0 and result2.stdout.strip():
            for line in result2.stdout.strip().splitlines():
                if line.strip() and line.strip() not in files_changed:
                    files_changed.append(line.strip())

    # Respect max_files from config
    try:
        config = load_config()
        max_files = config.get("summaries", {}).get("max_files", 20)
        max_decisions = config.get("summaries", {}).get("max_decisions", 10)
    except FileNotFoundError:
        max_files = 20
        max_decisions = 10

    files_changed = files_changed[:max_files]
    decisions = (key_decisions or [])[:max_decisions]

    summary = SessionSummary(
        hostname=hostname,
        session_id=session_id,
        timestamp=_now_iso(),
        project=project,
        task_id=task_id,
        duration_minutes=duration_minutes,
        key_decisions=decisions,
        files_changed=files_changed,
        issues_found=issues_found or [],
        artifacts_produced=artifacts_produced or [],
        context_for_next=context_for_next,
    )
    return summary
