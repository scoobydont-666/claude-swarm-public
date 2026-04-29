"""Session Protocol — lifecycle management for Claude Code instances.

Provides start/end functions that handle:
- Agent registration + heartbeat
- Event stream catchup
- Git sync
- Session summary generation
"""

from __future__ import annotations

import atexit
import logging
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

try:
    from registry_redis import (
        AgentInfo,
        HeartbeatThread,
        register,
        deregister,
        update_agent,
    )
except (ImportError, Exception):
    from registry import AgentInfo, HeartbeatThread, register, deregister, update_agent
try:
    from events_redis import emit, since_last_session, summarize_since, query
except (ImportError, Exception):
    from events import emit, since_last_session, summarize_since
from sync_engine import pull_all_projects, push_all_dirty, sync_config, get_dirty_repos
from crash_handler import install_crash_handlers

from util import now_iso as _now_iso

SWARM_ROOT = Path("/opt/swarm")
SUMMARIES_DIR = SWARM_ROOT / "artifacts" / "summaries"


class SwarmSession:
    """Manages the lifecycle of a Claude Code instance within the swarm."""

    def __init__(self):
        self.agent: AgentInfo | None = None
        self.heartbeat: HeartbeatThread | None = None
        self.start_time: str = ""
        self.events_since: str = ""

    def start(
        self,
        model: str = "",
        project: str = "",
        context: str = "",
    ) -> dict[str, Any]:
        """Start a swarm session.

        1. Register agent
        2. Start heartbeat
        3. Read events since last session
        4. Pull changed repos

        Returns catchup info.
        """
        self.start_time = _now_iso()

        # Register
        self.agent = register(model=model, project=project, session_context=context)

        # Start heartbeat
        self.heartbeat = HeartbeatThread(self.agent)
        self.heartbeat.start()

        # Register cleanup on exit + crash signal handlers
        atexit.register(self._cleanup)
        install_crash_handlers()

        # Emit session_start event
        emit(
            "session_start",
            project=project,
            agent_id=self.agent.agent_id,
            details={
                "model": model,
                "context": context,
            },
        )

        # Catchup: events since last session
        events = since_last_session()
        self.events_since = events[-1]["timestamp"] if events else self.start_time

        # Pull repos that changed
        pull_results = pull_all_projects()
        pulls = {
            k: v
            for k, v in pull_results.items()
            if v.get("status") == "ok"
            and "Already up to date" not in v.get("stdout", "")
        }

        return {
            "agent_id": self.agent.agent_id,
            "events_since_last_session": len(events),
            "repos_pulled": list(pulls.keys()),
            "catchup_summary": summarize_since(self.events_since) if events else {},
        }

    def update(self, **kwargs) -> None:
        """Update agent state (project, task_id, state, etc.)."""
        if self.agent:
            update_agent(self.agent, **kwargs)

    def end(self) -> dict[str, Any]:
        """End the session.

        1. Push all dirty repos
        2. Sync config
        3. Generate session summary from events
        4. Deregister agent
        """
        if not self.agent:
            return {"status": "no active session"}

        # Push dirty repos
        push_results = push_all_dirty()

        # Sync config
        config_result = sync_config()

        # Generate summary from events
        summary = summarize_since(self.start_time)

        # Write session summary
        summary_data = {
            "hostname": socket.gethostname(),
            "agent_id": self.agent.agent_id,
            "session_start": self.start_time,
            "session_end": _now_iso(),
            "model": self.agent.model,
            "commits": summary.get("commits", []),
            "tests": summary.get("tests", []),
            "tasks_completed": summary.get("tasks_completed", []),
            "blockers": summary.get("blockers", []),
            "projects_touched": summary.get("projects_touched", []),
            "dirty_repos": get_dirty_repos(),
        }

        SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        summary_path = SUMMARIES_DIR / f"{socket.gethostname()}-{ts}.yaml"

        import yaml

        summary_path.write_text(
            yaml.dump(summary_data, default_flow_style=False, sort_keys=False)
        )

        # Emit session_end event
        emit(
            "session_end",
            agent_id=self.agent.agent_id,
            details={
                "duration_seconds": self._elapsed(),
                "commits": len(summary.get("commits", [])),
                "tasks_completed": len(summary.get("tasks_completed", [])),
                "projects_touched": summary.get("projects_touched", []),
            },
        )

        # Stop heartbeat + deregister
        self._cleanup()

        return {
            "summary_path": str(summary_path),
            "pushed": list(push_results.keys()),
            "config_synced": config_result,
            "stats": summary,
        }

    def _elapsed(self) -> int:
        """Seconds since session start."""
        try:
            start = datetime.fromisoformat(self.start_time.replace("Z", "+00:00"))
            return int((datetime.now(timezone.utc) - start).total_seconds())
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
            return 0

    def _cleanup(self) -> None:
        """Stop heartbeat and deregister. Safe to call multiple times."""
        if self.heartbeat:
            self.heartbeat.stop()
            self.heartbeat = None
        if self.agent:
            deregister(self.agent)
            self.agent = None


# Module-level session for simple usage
_session: SwarmSession | None = None


def start_session(**kwargs: Any) -> dict[str, Any]:
    """Start the global swarm session.

    Args:
        **kwargs: Arguments to pass to SwarmSession.start()

    Returns:
        Status dictionary from session start
    """
    global _session
    _session = SwarmSession()
    return _session.start(**kwargs)


def end_session() -> dict[str, Any]:
    """End the global swarm session.

    Returns:
        Status dictionary from session end
    """
    global _session
    if _session:
        result = _session.end()
        _session = None
        return result
    return {"status": "no active session"}


def get_session() -> SwarmSession | None:
    """Get the current session, if any."""
    return _session
