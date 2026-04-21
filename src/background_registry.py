"""Background Task Registry — Track async dispatches and auto-complete tasks.

Provides persistent tracking of background dispatches (tasks sent to fleet
members via hydra_dispatch). Polls PID files to detect completion, updates
dispatch records, and optionally transitions swarm tasks.

Usage:
    from background_registry import BackgroundRegistry, start_task

    registry = BackgroundRegistry()
    # One-shot dispatch + track:
    task = start_task(registry, host="node_gpu", task="Kin index <project-a-path>")
    # Or register-after-dispatch:
    registry.register(dispatch_id, task_id="task-001", host="node_gpu")
    active = registry.active()
    completed = registry.poll()  # Check PIDs, return newly completed
    registry.cancel(dispatch_id)  # SIGTERM + mark canceled
"""

import logging
import os
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

if TYPE_CHECKING:  # avoid runtime dep cycles
    pass

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
    task_id: str | None = None
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
        task_id: str | None = None,
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
        logger.info("Registered background task: %s on %s (pid=%d)", dispatch_id, host, pid)
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

    def get(self, dispatch_id: str) -> BackgroundTask | None:
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
                            task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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

    def cancel(self, dispatch_id: str, sig: int = signal.SIGTERM) -> bool:
        """Cancel a running background task.

        Sends a signal (default SIGTERM) to the task's PID and marks the
        task as 'canceled' with a completion timestamp. Idempotent: if
        the task is already completed/failed/canceled, returns False.

        Args:
            dispatch_id: The dispatch to cancel
            sig: Signal to send (default SIGTERM; use SIGKILL for force)

        Returns:
            True if cancel succeeded (task was running + signal sent),
            False if task not found or already in a terminal state.
        """
        task = self._tasks.get(dispatch_id)
        if task is None:
            logger.warning("cancel: unknown dispatch_id %s", dispatch_id)
            return False
        if task.status != "running":
            logger.info("cancel: task %s already in status %s", dispatch_id, task.status)
            return False

        # Send signal if we have a PID; ignore ProcessLookupError (already dead)
        if task.pid > 0:
            try:
                os.kill(task.pid, sig)
            except ProcessLookupError:
                logger.info("cancel: pid %d already gone for %s", task.pid, dispatch_id)
            except PermissionError as exc:
                logger.warning("cancel: permission denied to signal pid %d: %s", task.pid, exc)
                return False

        task.status = "canceled"
        task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save()
        logger.info("Canceled background task %s on %s (pid=%d)", dispatch_id, task.host, task.pid)
        return True

    def cleanup(self, max_age_hours: int = 72) -> int:
        """Remove completed/failed tasks older than max_age_hours.

        Returns the number of tasks removed.
        """
        cutoff = time.time() - (max_age_hours * 3600)
        to_remove = []
        for did, task in self._tasks.items():
            if task.status in ("completed", "failed", "canceled") and task.completed_at:
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


def start_task(
    registry: BackgroundRegistry,
    host: str,
    task: str,
    *,
    description: str = "",
    task_id: Optional[str] = None,
    model: str = "sonnet",
    project_dir: Optional[str] = None,
    timeout_minutes: int = 30,
) -> BackgroundTask:
    """One-shot: dispatch a Claude Code session to a fleet member AND track it.

    Wraps ``hydra_dispatch.dispatch(..., background=True)`` + ``registry.register()``.
    Use this when you want a non-blocking dispatch whose completion you'll poll
    for later via ``registry.poll()`` or ``registry.get(dispatch_id)``.

    Args:
        registry: Target registry (usually a shared BackgroundRegistry instance).
        host: Fleet member hostname (e.g. "node_gpu"). See hydra_dispatch.FLEET.
        task: Natural-language task description / prompt for Claude Code.
        description: Human-readable short description (stored in registry).
                     Falls back to ``task[:80]`` if empty.
        task_id: Optional swarm task ID to auto-transition on completion.
        model: Claude model (haiku / sonnet / opus); passed through to dispatch.
        project_dir: Working directory on the remote host.
        timeout_minutes: Hard kill after this many minutes.

    Returns:
        The registered BackgroundTask.

    Raises:
        ValueError: If ``host`` is unknown (propagated from hydra_dispatch).
        RuntimeError: If dispatch fails to start.
    """
    # Local import to avoid circular deps at module-import time.
    from hydra_dispatch import dispatch as _dispatch

    result = _dispatch(
        host=host,
        task=task,
        model=model,
        project_dir=project_dir,
        timeout_minutes=timeout_minutes,
        background=True,
    )

    if result.status in ("failed", "pending") and result.error:
        raise RuntimeError(f"start_task: dispatch failed: {result.error}")

    # Extract PID from dispatch artifacts (hydra_dispatch writes <dispatch_id>.pid).
    pid_path = DISPATCH_DIR / f"{result.dispatch_id}.pid"
    pid = -1
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
        except ValueError:
            pass

    return registry.register(
        dispatch_id=result.dispatch_id,
        host=host,
        description=description or task[:80],
        task_id=task_id,
        model=model,
        pid=pid,
        output_file=result.output_file or str(DISPATCH_DIR / f"{result.dispatch_id}.output"),
    )
