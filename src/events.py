"""Event Bus — lightweight event system for cross-agent coordination.

Events are JSON files in /opt/swarm/events/ with timestamp-based names.
Any agent can emit events. Any agent can query the stream by time range.
Sequence numbers enable dedup during crash recovery.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

from util import now_iso as _now_iso

SWARM_ROOT = Path("/opt/swarm")
EVENTS_DIR = SWARM_ROOT / "events"

# Per-agent monotonic sequence counter for dedup
_sequence_lock = threading.Lock()
_sequence_counter: int = 0


def _next_sequence() -> int:
    """Return next monotonically increasing sequence number for this process."""
    global _sequence_counter
    with _sequence_lock:
        _sequence_counter += 1
        return _sequence_counter


def _event_filename() -> str:
    """Generate unique event filename: timestamp-hostname-pid."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{ts}-{socket.gethostname()}-{os.getpid()}.json"


def emit(
    event_type: str,
    project: str = "",
    details: dict[str, Any] | None = None,
    agent_id: str = "",
) -> Path:
    """Emit an event to the event bus.

    Event types:
      - session_start, session_end
      - task_claimed, task_completed, task_failed
      - commit (git commit + push)
      - test_result
      - blocker_found
      - context_handoff
      - config_sync
      - rate_limit
    """
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)

    aid = agent_id or f"{socket.gethostname()}-{os.getpid()}"
    event = {
        "type": event_type,
        "timestamp": _now_iso(),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "agent_id": aid,
        "sequence": _next_sequence(),
        "project": project,
        "details": details or {},
    }

    path = EVENTS_DIR / _event_filename()
    path.write_text(json.dumps(event, indent=2))
    return path


def query(
    since: str | None = None,
    event_type: str | None = None,
    project: str | None = None,
    hostname: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query events from the bus.

    Args:
        since: ISO timestamp — return events after this time.
        event_type: Filter by event type.
        project: Filter by project path.
        hostname: Filter by originating host.
        limit: Max events to return.

    Returns:
        List of event dicts, newest first.
    """
    if not EVENTS_DIR.exists():
        return []

    events = []
    for f in sorted(EVENTS_DIR.glob("*.json"), reverse=True):
        if len(events) >= limit:
            break
        try:
            event = json.loads(f.read_text())
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
            continue

        # Apply filters
        if since and event.get("timestamp", "") < since:
            break  # Files are sorted by time, so we can stop early

        if event_type and event.get("type") != event_type:
            continue
        if project and event.get("project") != project:
            continue
        if hostname and event.get("hostname") != hostname:
            continue

        events.append(event)

    return events


def since_last_session(hostname: str | None = None) -> list[dict]:
    """Get all events since the last session_end for this host."""
    host = hostname or socket.gethostname()

    # Find the last session_end event for this host
    last_end = None
    if EVENTS_DIR.exists():
        for f in sorted(EVENTS_DIR.glob("*.json"), reverse=True):
            try:
                event = json.loads(f.read_text())
                if event.get("type") == "session_end" and event.get("hostname") == host:
                    last_end = event.get("timestamp")
                    break
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Suppressed: %s", exc)
                continue

    return query(since=last_end)


def emit_commit(
    project: str, commit_hash: str, message: str, files_changed: int = 0
) -> Path:
    """Shorthand: emit a commit event."""
    return emit(
        "commit",
        project=project,
        details={
            "commit": commit_hash,
            "message": message,
            "files_changed": files_changed,
        },
    )


def emit_test_result(project: str, passed: int, failed: int, total: int) -> Path:
    """Shorthand: emit a test result event."""
    return emit(
        "test_result",
        project=project,
        details={
            "passed": passed,
            "failed": failed,
            "total": total,
            "all_green": failed == 0,
        },
    )


def emit_task_complete(
    task_id: str, project: str, result: dict[str, Any] | None = None
) -> Path:
    """Shorthand: emit a task completion event."""
    return emit(
        "task_completed",
        project=project,
        details={
            "task_id": task_id,
            "result": result or {},
        },
    )


def summarize_since(since: str) -> dict[str, Any]:
    """Generate a summary of all events since a timestamp.

    Returns a structured dict suitable for session summary generation.
    """
    events = query(since=since, limit=1000)

    summary: dict[str, Any] = {
        "event_count": len(events),
        "commits": [],
        "tests": [],
        "tasks_completed": [],
        "blockers": [],
        "projects_touched": set(),
    }

    for e in events:
        t = e.get("type", "")
        d = e.get("details", {})
        p = e.get("project", "")

        if p:
            summary["projects_touched"].add(p)

        if t == "commit":
            summary["commits"].append(
                {
                    "project": p,
                    "message": d.get("message", ""),
                    "host": e.get("hostname", ""),
                }
            )
        elif t == "test_result":
            summary["tests"].append(
                {
                    "project": p,
                    "passed": d.get("passed", 0),
                    "failed": d.get("failed", 0),
                }
            )
        elif t == "task_completed":
            summary["tasks_completed"].append(d.get("task_id", ""))
        elif t == "blocker_found":
            summary["blockers"].append(
                {
                    "project": p,
                    "description": d.get("description", ""),
                }
            )

    summary["projects_touched"] = sorted(summary["projects_touched"])
    return summary


def emit_rate_limit(profile: str, limit_type: str, reset_hint: str) -> Path:
    """Shorthand: emit a rate-limit event."""
    return emit(
        "rate_limit",
        details={
            "profile": profile,
            "limit_type": limit_type,
            "reset_hint": reset_hint,
        },
    )


def rotate(max_age_days: int = 7, max_files: int = 10000) -> int:
    """Move events older than max_age_days to events/archive/YYYY-MM/ subdirectory.

    Parses the timestamp prefix from each filename (format: 20260331T142001...).
    Files are moved to EVENTS_DIR/archive/YYYY-MM/ with directories created as needed.

    Args:
        max_age_days: Events older than this many days are archived.
        max_files: If total event count exceeds this, also archive oldest files.

    Returns:
        Count of rotated files.
    """
    if not EVENTS_DIR.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    rotated = 0
    all_files = sorted(EVENTS_DIR.glob("*.json"))

    # Determine which files to rotate: older than cutoff OR beyond max_files cap
    files_to_rotate: list[Path] = []
    for f in all_files:
        stem = f.name
        # Filename format: YYYYMMDDTHHMMSSffffff-hostname-pid.json
        ts_part = stem.split("-")[0]
        try:
            file_ts = datetime.strptime(ts_part[:15], "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue  # Skip files with unrecognisable timestamp prefix

        if file_ts < cutoff:
            files_to_rotate.append(f)

    # Also rotate oldest files if total count exceeds max_files
    remaining = [f for f in all_files if f not in set(files_to_rotate)]
    if len(remaining) > max_files:
        overflow = sorted(remaining)[: len(remaining) - max_files]
        files_to_rotate.extend(overflow)

    for f in files_to_rotate:
        stem = f.name
        ts_part = stem.split("-")[0]
        try:
            file_ts = datetime.strptime(ts_part[:15], "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

        archive_dir = EVENTS_DIR / "archive" / file_ts.strftime("%Y-%m")
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / f.name
        if not dest.exists():
            shutil.move(str(f), str(dest))
        else:
            f.unlink()  # Duplicate — discard
        rotated += 1

    return rotated


def prune_archive(max_age_days: int = 30) -> int:
    """Delete archived events older than max_age_days.

    Walks EVENTS_DIR/archive/YYYY-MM/ subdirectories and removes files whose
    timestamp prefix indicates they are older than max_age_days.

    Args:
        max_age_days: Archived events older than this many days are deleted.

    Returns:
        Count of deleted files.
    """
    archive_root = EVENTS_DIR / "archive"
    if not archive_root.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    pruned = 0

    for month_dir in sorted(archive_root.iterdir()):
        if not month_dir.is_dir():
            continue
        for f in sorted(month_dir.glob("*.json")):
            stem = f.name
            ts_part = stem.split("-")[0]
            try:
                file_ts = datetime.strptime(ts_part[:15], "%Y%m%dT%H%M%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

            if file_ts < cutoff:
                f.unlink()
                pruned += 1

        # Remove empty month directories
        try:
            month_dir.rmdir()
        except OSError:
            pass  # Not empty — leave it

    return pruned


class EventWatcher:
    """Background thread that watches for commit events and auto-pulls repos.

    Polls the event bus every `interval` seconds for commit events from
    other hosts. When found, pulls the affected repos locally.
    """

    def __init__(self, interval: float = 60.0) -> None:
        """Initialize ConfigWatcher.

        Args:
            interval: Seconds between sync checks (default 60).
        """
        self.interval = interval
        self._consumer = EventConsumer()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_check: str = _now_iso()
        self._pull_count: int = 0

    def start(self) -> None:
        """Start the watcher thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="event-watcher"
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the watcher thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None

    @property
    def pull_count(self) -> int:
        """Number of repos pulled since start."""
        return self._pull_count

    def _loop(self) -> None:
        """Main watch loop — runs in background thread."""
        while not self._stop_event.is_set():
            try:
                self._check_for_commits()
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Suppressed: %s", exc)
                pass  # never crash the watcher
            self._stop_event.wait(self.interval)

    def _check_for_commits(self) -> None:
        """Check for commit events from other hosts and pull affected repos."""
        from sync_engine import process_commit_events

        pulled = process_commit_events(self._last_check)
        self._last_check = _now_iso()
        self._pull_count += len(pulled)


class EventConsumer:
    """Consume events with per-agent sequence dedup.

    Tracks the last-seen sequence number per agent_id to prevent
    duplicate processing during crash recovery or NFS replay.
    """

    def __init__(self) -> None:
        """Initialize EventConsumer with empty dedup state."""
        self._last_seen: dict[str, int] = {}  # agent_id → last sequence

    def process(self, events: list[dict]) -> list[dict]:
        """Filter events, dropping those already seen by this consumer.

        Args:
            events: List of event dicts (must contain 'agent_id' and 'sequence').

        Returns:
            List of new (unseen) events.
        """
        new_events = []
        for event in events:
            agent_id = event.get("agent_id", "")
            seq = event.get("sequence", 0)

            if not agent_id or not seq:
                # Events without sequence pass through (backward compat)
                new_events.append(event)
                continue

            last = self._last_seen.get(agent_id, 0)
            if seq > last:
                self._last_seen[agent_id] = seq
                new_events.append(event)
            # else: duplicate — skip

        return new_events

    def reset(self, agent_id: str | None = None) -> None:
        """Reset sequence tracking. If agent_id given, reset only that agent."""
        if agent_id:
            self._last_seen.pop(agent_id, None)
        else:
            self._last_seen.clear()
