"""Redis-backed event bus. Drop-in replacement for events.py filesystem ops."""

import json
import os

try:
    import redis_client as _rc
except ImportError:
    from src import redis_client as _rc

try:
    from util import hostname, now_iso
except ImportError:
    from src.util import hostname

if os.environ.get("SWARM_REDIS_SKIP_CHECK") != "1" and not _rc.health_check():
    raise ImportError("Redis not available — falling back to NFS events")


def emit(
    event_type: str, project: str = "", details: dict | None = None, agent_id: str = ""
) -> str:
    """Emit an event to Redis stream."""
    data = {
        "hostname": hostname(),
        "pid": str(os.getpid()),
        "project": project,
        "agent_id": agent_id,
    }
    if details:
        data.update(details)
    return _rc.emit_event(event_type, data)


def query(
    since: str = "",
    event_type: str | None = None,
    project: str | None = None,
    hostname_filter: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query events from Redis stream.

    Args:
        since: ISO timestamp — return events after this time
        event_type: Filter by event type
        project: Filter by project path
        hostname_filter: Filter by originating hostname
        limit: Maximum events to return

    Returns:
        List of event dictionaries, newest first
    """
    # Convert ISO timestamp to Redis stream ID if provided
    start = "-"
    if since:
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            start = str(int(dt.timestamp() * 1000))
        except (ValueError, AttributeError):
            pass

    raw = _rc.query_events(start=start, end="+", count=limit, event_type=event_type)

    events = []
    for evt in raw:
        data = (
            json.loads(evt.get("data", "{}"))
            if isinstance(evt.get("data"), str)
            else evt.get("data", {})
        )
        entry = {
            "type": evt.get("type", ""),
            "timestamp": evt.get("timestamp", ""),
            "stream_id": evt.get("stream_id", ""),
            **data,
        }
        # Apply filters
        if project and entry.get("project") != project:
            continue
        if hostname_filter and entry.get("hostname") != hostname_filter:
            continue
        events.append(entry)

    return events


def since_last_session(host: str = "") -> list[dict]:
    """Get all events since last session_end."""
    host = host or hostname()
    all_events = query(event_type="session_end", hostname_filter=host, limit=1)
    if all_events:
        last_ts = all_events[-1].get("timestamp", "")
        if last_ts:
            return query(since=last_ts)
    return query(limit=50)


def emit_commit(
    project: str, commit_hash: str, message: str, files_changed: int = 0
) -> str:
    """Emit a commit event."""
    return emit(
        "commit",
        project=project,
        details={
            "commit_hash": commit_hash,
            "message": message,
            "files_changed": files_changed,
        },
    )


def emit_test_result(project: str, passed: int, failed: int, total: int) -> str:
    """Emit a test result event."""
    return emit(
        "test_result",
        project=project,
        details={
            "passed": passed,
            "failed": failed,
            "total": total,
        },
    )


def emit_task_complete(task_id: str, project: str = "", result: str = "") -> str:
    """Emit a task completion event."""
    return emit(
        "task_completed",
        project=project,
        details={
            "task_id": task_id,
            "result": result,
        },
    )


def emit_rate_limit(
    profile: str = "", limit_type: str = "", reset_hint: str = ""
) -> str:
    """Emit a rate-limit event."""
    return emit(
        "rate_limit",
        details={
            "profile": profile,
            "limit_type": limit_type,
            "reset_hint": reset_hint,
        },
    )


def summarize_since(since: str = "") -> dict:
    """Generate summary of all events since a timestamp.

    Returns a structured dict matching events.py's format for compatibility.
    """
    events = query(since=since, limit=1000)
    summary: dict = {
        "event_count": len(events),
        "commits": [],
        "tests": [],
        "tasks_completed": [],
        "blockers": [],
        "projects_touched": set(),
    }
    for evt in events:
        evt_type = evt.get("type", "unknown")
        project = evt.get("project", "")
        if project:
            summary["projects_touched"].add(project)
        if evt_type == "commit":
            summary["commits"].append(
                {
                    "project": project,
                    "message": evt.get("message", ""),
                    "host": evt.get("hostname", ""),
                }
            )
        elif evt_type == "test_result":
            summary["tests"].append(
                {
                    "project": project,
                    "passed": int(evt.get("passed", 0)),
                    "failed": int(evt.get("failed", 0)),
                }
            )
        elif evt_type == "task_completed":
            summary["tasks_completed"].append(evt.get("task_id", ""))
        elif evt_type == "blocker_found":
            summary["blockers"].append(
                {
                    "project": project,
                    "description": evt.get("description", ""),
                }
            )
    summary["projects_touched"] = sorted(summary["projects_touched"])
    return summary


def rotate(max_age_days: int = 30, max_files: int = 10000) -> int:
    """Trim event stream (Redis equivalent of file rotation)."""
    return _rc.trim_events(max_len=max_files)


def prune_archive(max_age_days: int = 90) -> int:
    """No-op for Redis — XTRIM handles cleanup."""
    return 0
