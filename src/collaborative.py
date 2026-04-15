"""Collaborative mode — two Claude Code sessions exchange context mid-flight.

Orchestrator writes initial context, worker reads and updates progress/blockers,
orchestrator polls and resolves blockers, then writes resolution back.

Files in /var/lib/swarm/collaborative/{session_id}/:
- context.yaml — orchestrator writes, worker reads
- progress.yaml — worker writes periodically
- blockers.yaml — worker writes when stuck
"""

from __future__ import annotations

import fcntl
import logging
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from util import now_iso as _now_iso

logger = logging.getLogger(__name__)


@contextmanager
def _locked_file(path: Path, mode: str = "r+"):
    """Context manager for file operations with exclusive advisory lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    f = open(path, mode)
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield f
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


COLLAB_ROOT = Path("/var/lib/swarm/collaborative")


@dataclass
class CollaborativeSession:
    """A collaborative session between orchestrator and worker."""

    session_id: str
    orchestrator_host: str
    worker_host: str
    task: str
    status: str = "active"  # active, blocked, completed, failed
    context_dir: Path = field(default_factory=Path)
    created_at: str = ""
    updated_at: str = ""
    project_dir: str = ""
    model: str = ""

    def __post_init__(self):
        if not self.context_dir or self.context_dir == Path():
            self.context_dir = COLLAB_ROOT / self.session_id
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = _now_iso()


@dataclass
class Blocker:
    """A blocker reported by the worker."""

    blocker_id: str
    reported_at: str
    description: str
    context: dict = field(default_factory=dict)
    resolution: dict = field(default_factory=dict)  # Populated by orchestrator
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert blocker to a dictionary."""
        return asdict(self)


def start_collaborative(
    task: str,
    worker_host: str,
    orchestrator_host: str = "",
    project_dir: str = "",
    model: str = "sonnet",
) -> CollaborativeSession:
    """Start a collaborative session with a worker.

    Args:
        task: Task description for the worker
        worker_host: Hostname where worker will run (e.g. 'gpu-server-1')
        orchestrator_host: Hostname of orchestrator (auto-detected if empty)
        project_dir: Project directory context
        model: Claude model to use

    Returns:
        CollaborativeSession object with session_id set
    """
    import socket

    orchestrator_host = orchestrator_host or socket.gethostname()

    # Generate session ID (use microseconds for uniqueness on fast systems)
    ts = int(time.time() * 1_000_000)  # microseconds
    session_id = f"collab-{ts}-{orchestrator_host}-{worker_host}"

    session = CollaborativeSession(
        session_id=session_id,
        orchestrator_host=orchestrator_host,
        worker_host=worker_host,
        task=task,
        status="active",
        project_dir=project_dir,
        model=model,
    )
    session.context_dir.mkdir(parents=True, exist_ok=True)

    # Write initial context
    context = {
        "session_id": session_id,
        "orchestrator_host": orchestrator_host,
        "worker_host": worker_host,
        "task": task,
        "project_dir": project_dir,
        "model": model,
        "status": "active",
        "started_at": session.created_at,
        "worker_instructions": (
            "1. Read context.yaml at start\n"
            "2. Write progress.yaml after each major step\n"
            "3. If blocked, write to blockers.yaml and poll for resolution\n"
            "4. When resolved, continue from where you left off\n"
        ),
    }
    write_context(session_id, context)

    logger.info(
        "Started collaborative session %s (worker: %s, project: %s)",
        session_id,
        worker_host,
        project_dir,
    )
    return session


def write_context(session_id: str, context: dict) -> None:
    """Orchestrator writes context for worker to consume.

    Args:
        session_id: Session identifier
        context: Context dictionary (will be written as YAML)
    """
    context_dir = COLLAB_ROOT / session_id
    context_dir.mkdir(parents=True, exist_ok=True)
    context_file = context_dir / "context.yaml"

    # Update timestamp
    context["updated_at"] = _now_iso()

    with open(context_file, "w") as f:
        yaml.dump(context, f, default_flow_style=False, sort_keys=False)

    logger.debug("Wrote context for session %s", session_id)


def read_context(session_id: str) -> dict | None:
    """Worker reads orchestrator's context.

    Args:
        session_id: Session identifier

    Returns:
        Context dictionary or None if not found
    """
    context_file = COLLAB_ROOT / session_id / "context.yaml"
    if not context_file.exists():
        return None

    try:
        with open(context_file) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error("Error reading context for %s: %s", session_id, e)
        return None


def write_progress(session_id: str, progress: dict) -> None:
    """Worker reports progress.

    Args:
        session_id: Session identifier
        progress: Progress dictionary (steps completed, current state, etc.)
    """
    progress_dir = COLLAB_ROOT / session_id
    progress_dir.mkdir(parents=True, exist_ok=True)
    progress_file = progress_dir / "progress.yaml"

    progress["updated_at"] = _now_iso()

    with open(progress_file, "w") as f:
        yaml.dump(progress, f, default_flow_style=False, sort_keys=False)

    logger.debug("Wrote progress for session %s", session_id)


def read_progress(session_id: str) -> dict | None:
    """Orchestrator reads worker's progress updates.

    Args:
        session_id: Session identifier

    Returns:
        Progress dictionary or None if not found
    """
    progress_file = COLLAB_ROOT / session_id / "progress.yaml"
    if not progress_file.exists():
        return None

    try:
        with open(progress_file) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error("Error reading progress for %s: %s", session_id, e)
        return None


def write_blocker(session_id: str, blocker: Blocker) -> None:
    """Worker reports a blocker.

    Args:
        session_id: Session identifier
        blocker: Blocker object with description and context
    """
    blockers_file = COLLAB_ROOT / session_id / "blockers.yaml"

    with _locked_file(blockers_file, "r+") as f:
        content = f.read()
        existing = yaml.safe_load(content) if content.strip() else []
        if not isinstance(existing, list):
            existing = []
        existing.append(blocker.to_dict())
        f.seek(0)
        f.truncate()
        yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

    logger.warning(
        "Worker wrote blocker %s for session %s: %s",
        blocker.blocker_id,
        session_id,
        blocker.description,
    )


def read_blockers(session_id: str) -> list[dict]:
    """Orchestrator reads worker's blocker reports.

    Args:
        session_id: Session identifier

    Returns:
        List of blocker dictionaries
    """
    blockers_file = COLLAB_ROOT / session_id / "blockers.yaml"
    if not blockers_file.exists():
        return []

    try:
        with open(blockers_file) as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("Error reading blockers for %s: %s", session_id, e)
        return []


def resolve_blocker(
    session_id: str,
    blocker_id: str,
    resolution: dict,
) -> None:
    """Orchestrator provides resolution for a blocker.

    Args:
        session_id: Session identifier
        blocker_id: Blocker ID to resolve
        resolution: Resolution dictionary (may contain updated context, guidance, etc.)
    """
    blockers_file = COLLAB_ROOT / session_id / "blockers.yaml"
    if not blockers_file.exists():
        logger.warning("Blockers file not found for session %s", session_id)
        return

    with _locked_file(blockers_file, "r+") as f:
        content = f.read()
        blockers = yaml.safe_load(content) if content.strip() else []
        if not isinstance(blockers, list):
            blockers = []
        for blocker in blockers:
            if blocker.get("blocker_id") == blocker_id:
                blocker["resolution"] = resolution
                blocker["resolved"] = True
                break
        f.seek(0)
        f.truncate()
        yaml.dump(blockers, f, default_flow_style=False, sort_keys=False)

    logger.info("Resolved blocker %s for session %s", blocker_id, session_id)


def poll_for_resolution(
    session_id: str, blocker_id: str, timeout_seconds: int = 300
) -> dict | None:
    """Worker polls for resolution of a reported blocker.

    Args:
        session_id: Session identifier
        blocker_id: Blocker ID to poll for
        timeout_seconds: How long to wait before giving up

    Returns:
        Resolution dictionary if resolved, None if timeout
    """
    start = time.time()
    while time.time() - start < timeout_seconds:
        blockers = read_blockers(session_id)
        for blocker in blockers:
            if blocker.get("blocker_id") == blocker_id and blocker.get("resolved"):
                return blocker.get("resolution", {})
        time.sleep(5)  # Poll every 5 seconds
    return None


def update_session_status(session_id: str, status: str) -> None:
    """Update session status.

    Args:
        session_id: Session identifier
        status: New status (active, blocked, completed, failed)
    """
    context = read_context(session_id)
    if context:
        context["status"] = status
        context["updated_at"] = _now_iso()
        write_context(session_id, context)


def get_session_status(session_id: str) -> str | None:
    """Get current session status.

    Args:
        session_id: Session identifier

    Returns:
        Status string or None if session not found
    """
    context = read_context(session_id)
    return context.get("status") if context else None


def list_sessions() -> list[dict]:
    """List all collaborative sessions.

    Returns:
        List of session info dictionaries
    """
    sessions = []
    if not COLLAB_ROOT.exists():
        return sessions

    for session_dir in COLLAB_ROOT.iterdir():
        if not session_dir.is_dir():
            continue

        context_file = session_dir / "context.yaml"
        if context_file.exists():
            try:
                with open(context_file) as f:
                    context = yaml.safe_load(f) or {}
                    sessions.append(
                        {
                            "session_id": session_dir.name,
                            "status": context.get("status", "unknown"),
                            "started_at": context.get("started_at", ""),
                            "updated_at": context.get("updated_at", ""),
                            "orchestrator": context.get("orchestrator_host", ""),
                            "worker": context.get("worker_host", ""),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Suppressed: %s", exc)
                continue

    return sessions


def cleanup_session(session_id: str) -> None:
    """Clean up a completed or failed session.

    Args:
        session_id: Session identifier
    """
    session_dir = COLLAB_ROOT / session_id
    if session_dir.exists():
        import shutil

        shutil.rmtree(session_dir)
        logger.info("Cleaned up session %s", session_id)
