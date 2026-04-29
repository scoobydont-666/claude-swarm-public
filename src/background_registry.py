"""Background Task Registry — Track async dispatches and auto-complete tasks.

Provides persistent tracking of background dispatches (tasks sent to fleet
members via hydra_dispatch). Polls PID files to detect completion, updates
dispatch records, and optionally transitions swarm tasks.

Usage:
    from background_registry import BackgroundRegistry

    registry = BackgroundRegistry()
    registry.register(dispatch_id, task_id="task-001", host="node_gpu")
    active = registry.active()
    completed = registry.poll()  # Check PIDs, return newly completed
"""

import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

DISPATCH_DIR = Path("/opt/swarm/artifacts/dispatches")
REGISTRY_PATH = Path("/opt/swarm/artifacts/background-registry.yaml")


@dataclass
class BackgroundTask:
    """A tracked background dispatch.

    Attributes:
        dispatch_id: Unique dispatch identifier (from hydra_dispatch)
        task_id: Optional swarm task ID to auto-transition on completion
        host: Fleet member hostname
        model: Claude model used
        description: Human-readable task description
        status: Current status (running, completed, failed, unknown)
        pid: OS process ID of the SSH process
        started_at: ISO timestamp
        completed_at: ISO timestamp (set on completion)
        exit_code: Process exit code (-1 if still running)
        output_file: Path to dispatch output
    """

    dispatch_id: str
    host: str
    description: str = ""
    task_id: Optional[str] = None
    model: str = "sonnet"
    status: str = "running"
    pid: int = -1
    started_at: str = ""
    completed_at: str = ""
    exit_code: int = -1
    output_file: str = ""


class BackgroundRegistry:
    """Persistent registry for tracking background dispatches.

    Stores state in a YAML file and polls PID files to detect completion.
    Thread-safe for single-process use (file-level atomicity).
    """

    def __init__(self, registry_path: Path = REGISTRY_PATH) -> None:
        self._path = registry_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, BackgroundTask] = {}
        self._load()

    def _load(self) -> None:
        """Load registry from disk."""
        if self._path.exists():
            try:
                with open(self._path) as f:
                    data = yaml.safe_load(f) or {}
                for dispatch_id, entry in data.items():
                    self._tasks[dispatch_id] = BackgroundTask(**entry)
            except Exception as e:
                logger.warning("Failed to load background registry: %s", e)

    def _save(self) -> None:
        """Persist registry to disk (atomic write)."""
        tmp = self._path.with_suffix(".tmp")
        data = {did: asdict(task) for did, task in self._tasks.items()}
        with open(tmp, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
        tmp.rename(self._path)

    def register(
        self,
        dispatch_id: str,
        host: str,
        description: str = "",
        task_id: Optional[str] = None,
        model: str = "sonnet",
        pid: int = -1,
        output_file: str = "",
    ) -> BackgroundTask:
        """Register a new background dispatch for tracking.

        Args:
            dispatch_id: Unique dispatch ID from hydra_dispatch
            host: Target fleet member
            description: Human-readable task description
            task_id: Optional swarm task ID for auto-transition
            model: Claude model used
            pid: OS process ID
            output_file: Path to output file

        Returns:
            The registered BackgroundTask
        """
        # Try to read PID from dispatch artifacts if not provided
        if pid == -1:
            pid_path = DISPATCH_DIR / f"{dispatch_id}.pid"
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text().strip())
                except ValueError:
                    pass

        task = BackgroundTask(
            dispatch_id=dispatch_id,
            host=host,
            description=description,
            task_id=task_id,
            model=model,
            status="running",
            pid=pid,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            output_file=output_file or str(DISPATCH_DIR / f"{dispatch_id}.output"),
        )
        self._tasks[dispatch_id] = task
        self._save()
        logger.info(
            "Registered background task: %s on %s (pid=%d)", dispatch_id, host, pid
        )
        return task

    def active(self) -> list[BackgroundTask]:
        """Return all tasks with status 'running'."""
        return [t for t in self._tasks.values() if t.status == "running"]

    def completed(self) -> list[BackgroundTask]:
        """Return all tasks with status 'completed'."""
        return [t for t in self._tasks.values() if t.status == "completed"]

    def failed(self) -> list[BackgroundTask]:
        """Return all tasks with status 'failed'."""
        return [t for t in self._tasks.values() if t.status == "failed"]

    def get(self, dispatch_id: str) -> Optional[BackgroundTask]:
        """Get a specific task by dispatch ID."""
        return self._tasks.get(dispatch_id)

    def _check_pid(self, pid: int) -> bool:
        """Check if a process is still alive."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Process exists but we can't signal it

    def poll(self) -> list[BackgroundTask]:
        """Poll all running tasks, detect completions. Returns newly completed tasks.

        Checks PID files and process status. Updates dispatch records on disk.
        Optionally transitions linked swarm tasks.
        """
        newly_completed: list[BackgroundTask] = []

        for task in list(self._tasks.values()):
            if task.status != "running":
                continue

            # Check if PID is still alive
            if task.pid > 0 and not self._check_pid(task.pid):
                task.status = "completed"
                task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                # Try to read exit code from dispatch record
                record_path = DISPATCH_DIR / f"{task.dispatch_id}.yaml"
                if record_path.exists():
                    try:
                        with open(record_path) as f:
                            record = yaml.safe_load(f)
                        task.exit_code = record.get("exit_code", 0)
                        if task.exit_code != 0:
                            task.status = "failed"
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Suppressed: %s", exc)
                        pass

                    # Update dispatch record
                    try:
                        with open(record_path) as f:
                            record = yaml.safe_load(f)
                        record["status"] = task.status
                        record["completed_at"] = task.completed_at
                        with open(record_path, "w") as f:
                            yaml.dump(record, f)
                    except Exception as e:
                        logger.warning("Failed to update dispatch record: %s", e)

                newly_completed.append(task)
                logger.info(
                    "Background task %s on %s: %s (pid=%d)",
                    task.status,
                    task.host,
                    task.dispatch_id,
                    task.pid,
                )

            # Handle tasks with no PID — try reading from file, then check
            elif task.pid <= 0:
                pid_path = DISPATCH_DIR / f"{task.dispatch_id}.pid"
                if pid_path.exists():
                    try:
                        task.pid = int(pid_path.read_text().strip())
                        # Now check if the newly-discovered PID is alive
                        if not self._check_pid(task.pid):
                            task.status = "completed"
                            task.completed_at = time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            )
                            newly_completed.append(task)
                    except ValueError:
                        task.status = "unknown"

        if newly_completed:
            self._save()

        return newly_completed

    def summary(self) -> dict:
        """Return a summary of all tracked tasks."""
        active = self.active()
        done = self.completed()
        fails = self.failed()
        return {
            "total": len(self._tasks),
            "running": len(active),
            "completed": len(done),
            "failed": len(fails),
            "active_hosts": list({t.host for t in active}),
            "tasks": [
                {
                    "dispatch_id": t.dispatch_id,
                    "host": t.host,
                    "status": t.status,
                    "description": t.description[:80],
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                }
                for t in self._tasks.values()
            ],
        }

    def cleanup(self, max_age_hours: int = 72) -> int:
        """Remove completed/failed tasks older than max_age_hours.

        Returns the number of tasks removed.
        """
        cutoff = time.time() - (max_age_hours * 3600)
        to_remove = []
        for did, task in self._tasks.items():
            if task.status in ("completed", "failed") and task.completed_at:
                try:
                    completed_ts = time.mktime(
                        time.strptime(task.completed_at, "%Y-%m-%dT%H:%M:%SZ")
                    )
                    if completed_ts < cutoff:
                        to_remove.append(did)
                except ValueError:
                    pass

        for did in to_remove:
            del self._tasks[did]

        if to_remove:
            self._save()

        return len(to_remove)
