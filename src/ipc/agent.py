"""IPC agent registration, heartbeat, and presence."""

from __future__ import annotations

import atexit
import os
import signal
import socket
import threading
import time

from . import transport

# TTLs
AGENT_TTL = 120  # seconds — heartbeat must refresh before this
HEARTBEAT_INTERVAL = 30  # seconds between heartbeats

# Key prefixes
_K_AGENT = "ipc:agent:"
_K_INDEX = "ipc:agents:index"
_K_PROJECT = "ipc:agents:project:"
_K_INBOX = "ipc:inbox:"
_K_INBOX_GROUP = "reader"


def _make_agent_id(hostname: str | None = None, pid: int | None = None) -> str:
    """Build agent ID from hostname, PID, and session short hash."""
    hostname = hostname or socket.gethostname()
    pid = pid or os.getpid()
    session_id = os.environ.get("CLAUDE_SESSION_ID", "0000")
    short = session_id[:4] if len(session_id) >= 4 else session_id.ljust(4, "0")
    return f"{hostname}:{pid}:{short}"


class _HeartbeatThread(threading.Thread):
    """Daemon thread that refreshes agent TTL."""

    def __init__(self, agent_id: str, interval: int = HEARTBEAT_INTERVAL) -> None:
        super().__init__(daemon=True, name=f"ipc-heartbeat-{agent_id}")
        self.agent_id = agent_id
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                refresh_heartbeat(self.agent_id)
            except Exception:
                pass  # Best-effort — don't crash the host process

    def stop(self) -> None:
        self._stop_event.set()


# Module-level state for the registered agent
_current_agent_id: str | None = None
_heartbeat_thread: _HeartbeatThread | None = None


def get_current_agent_id() -> str | None:
    """Get the agent ID for this process, or None if not registered."""
    return _current_agent_id


def register(
    project: str = "",
    model: str = "",
    hostname: str | None = None,
    pid: int | None = None,
    auto_heartbeat: bool = True,
) -> str:
    """Register this Claude Code instance as an IPC agent.

    Returns the agent_id. Idempotent — re-registering updates state.
    """
    global _current_agent_id, _heartbeat_thread

    agent_id = _make_agent_id(hostname, pid)
    r = transport.get_client()

    # Write agent hash
    data = {
        "hostname": hostname or socket.gethostname(),
        "pid": str(pid or os.getpid()),
        "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
        "project": project,
        "model": model,
        "status": "online",
        "registered_at": str(time.time()),
        "last_heartbeat": str(time.time()),
    }
    r.hset(f"{_K_AGENT}{agent_id}", mapping=data)
    r.expire(f"{_K_AGENT}{agent_id}", AGENT_TTL)

    # Add to index
    r.sadd(_K_INDEX, agent_id)

    # Add to project index
    if project:
        r.sadd(f"{_K_PROJECT}{project}", agent_id)

    # Ensure inbox stream + consumer group exist
    transport.ensure_consumer_group(f"{_K_INBOX}{agent_id}", _K_INBOX_GROUP)

    # Start heartbeat thread
    if auto_heartbeat:
        if _heartbeat_thread is not None:
            _heartbeat_thread.stop()
        _heartbeat_thread = _HeartbeatThread(agent_id)
        _heartbeat_thread.start()

    _current_agent_id = agent_id

    # Register cleanup on exit
    atexit.register(_cleanup_on_exit)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _signal_handler)
        except (OSError, ValueError):
            pass  # Can't set signal handlers in non-main threads

    return agent_id


def deregister(agent_id: str | None = None) -> bool:
    """Deregister an IPC agent. Stops heartbeat, removes from indexes.

    Does NOT delete the inbox stream (messages persist for reconnection).
    """
    global _current_agent_id, _heartbeat_thread

    agent_id = agent_id or _current_agent_id
    if not agent_id:
        return False

    r = transport.get_client()

    # Get agent data for project cleanup
    data = r.hgetall(f"{_K_AGENT}{agent_id}")
    project = data.get("project", "") if data else ""

    # Remove from indexes
    r.srem(_K_INDEX, agent_id)
    if project:
        r.srem(f"{_K_PROJECT}{project}", agent_id)

    # Delete agent hash
    r.delete(f"{_K_AGENT}{agent_id}")

    # Stop heartbeat
    if _heartbeat_thread is not None:
        _heartbeat_thread.stop()
        _heartbeat_thread = None

    if agent_id == _current_agent_id:
        _current_agent_id = None

    return True


def refresh_heartbeat(agent_id: str | None = None) -> bool:
    """Refresh agent TTL. Returns False if agent not registered."""
    agent_id = agent_id or _current_agent_id
    if not agent_id:
        return False

    r = transport.get_client()
    key = f"{_K_AGENT}{agent_id}"
    if not r.exists(key):
        return False
    r.hset(key, "last_heartbeat", str(time.time()))
    r.expire(key, AGENT_TTL)
    return True


def update_status(agent_id: str | None = None, **fields: str) -> bool:
    """Update agent metadata fields (project, model, status, etc.)."""
    agent_id = agent_id or _current_agent_id
    if not agent_id:
        return False

    r = transport.get_client()
    key = f"{_K_AGENT}{agent_id}"
    if not r.exists(key):
        return False
    if fields:
        r.hset(key, mapping=fields)
    return True


def get_agent(agent_id: str) -> dict | None:
    """Get agent metadata."""
    r = transport.get_client()
    data = r.hgetall(f"{_K_AGENT}{agent_id}")
    if not data:
        return None
    data["agent_id"] = agent_id
    return data


def list_agents(project: str | None = None) -> list[dict]:
    """List all registered agents, optionally filtered by project."""
    r = transport.get_client()

    if project:
        agent_ids = r.smembers(f"{_K_PROJECT}{project}")
    else:
        agent_ids = r.smembers(_K_INDEX)

    if not agent_ids:
        return []

    pipe = r.pipeline()
    for aid in sorted(agent_ids):
        pipe.hgetall(f"{_K_AGENT}{aid}")
    results = pipe.execute()

    agents = []
    stale_ids = []
    for aid, data in zip(sorted(agent_ids), results):
        if data:
            data["agent_id"] = aid
            agents.append(data)
        else:
            # Agent hash expired (TTL) but still in index — clean up
            stale_ids.append(aid)

    # Lazy cleanup of stale index entries
    if stale_ids:
        pipe = r.pipeline()
        for sid in stale_ids:
            pipe.srem(_K_INDEX, sid)
        pipe.execute()

    return agents


def cleanup_stale() -> list[str]:
    """Remove agent IDs from indexes whose hashes have expired.

    Returns list of cleaned agent IDs.
    """
    r = transport.get_client()
    all_ids = r.smembers(_K_INDEX)
    stale = []
    for aid in all_ids:
        if not r.exists(f"{_K_AGENT}{aid}"):
            stale.append(aid)

    if stale:
        pipe = r.pipeline()
        for sid in stale:
            pipe.srem(_K_INDEX, sid)
        pipe.execute()

    return stale


def _cleanup_on_exit() -> None:
    """atexit handler."""
    try:
        deregister()
    except Exception:
        pass


def _signal_handler(signum: int, frame: object) -> None:
    """Signal handler for graceful shutdown."""
    try:
        deregister()
    except Exception:
        pass
    # Re-raise with default handler
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
