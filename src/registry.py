"""Agent Registry — presence, heartbeat, capability advertisement.

Each Claude Code instance registers as an agent. Heartbeats keep it alive.
Stale agents (no heartbeat for 5 min) are marked dead and their tasks released.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

SWARM_ROOT = Path("/opt/swarm")
AGENTS_DIR = SWARM_ROOT / "agents"
HEARTBEAT_INTERVAL = 60  # seconds
STALE_THRESHOLD = 300  # 5 minutes
STALE_MISS_COUNT = 3  # require 3 consecutive stale observations before marking dead
_STALE_TRACKER_FILE = AGENTS_DIR / ".stale-tracker.json"


@dataclass
class AgentInfo:
    """Information about a registered Claude Code agent.

    Attributes:
        hostname: Hostname where agent is running
        pid: Process ID of the agent
        state: Agent state (idle, working, blocked, shutting_down)
        project: Current project directory
        task_id: Currently claimed task ID if any
        model: Claude model being used
        session_context: Session context information
        started_at: ISO timestamp when agent started
        last_heartbeat: ISO timestamp of last heartbeat
        capabilities: Dict of capability flags (gpu, ollama, docker, chromadb)
    """

    hostname: str
    pid: int
    state: str = "idle"  # idle | working | blocked | shutting_down
    project: str = ""
    task_id: str = ""
    model: str = ""
    session_context: str = ""
    started_at: str = ""
    last_heartbeat: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)

    @property
    def agent_id(self) -> str:
        """Return unique agent identifier as hostname-pid."""
        return f"{self.hostname}-{self.pid}"

    @property
    def agent_file(self) -> Path:
        """Return path to this agent's state file in AGENTS_DIR."""
        return AGENTS_DIR / f"{self.agent_id}.json"

    def to_dict(self) -> dict[str, Any]:
        """Convert agent info to a dictionary."""
        return asdict(self)


from util import now_iso as _now_iso


def _detect_capabilities() -> dict[str, bool]:
    """Auto-detect host capabilities."""
    hostname = socket.gethostname()
    caps = {
        "gpu": False,
        "ollama": False,
        "docker": False,
        "chromadb": False,
    }

    # GPU: check nvidia-smi
    if os.path.exists("/usr/bin/nvidia-smi"):
        try:
            result = (
                os.popen(
                    "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null"
                )
                .read()
                .strip()
            )
            caps["gpu"] = bool(result)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
            pass

    # Ollama: check if responding
    try:
        import httpx

        r = httpx.get("http://127.0.0.1:11434/api/version", timeout=2.0)
        caps["ollama"] = r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Suppressed: %s", exc)
        pass

    # Docker
    caps["docker"] = os.path.exists("/usr/bin/docker")

    # ChromaDB
    try:
        import httpx

        r = httpx.get("http://127.0.0.1:8100/api/v2/heartbeat", timeout=2.0)
        caps["chromadb"] = r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Suppressed: %s", exc)
        pass

    return caps


def register(
    model: str = "",
    project: str = "",
    session_context: str = "",
) -> AgentInfo:
    """Register this Claude Code instance as an active agent."""
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    agent = AgentInfo(
        hostname=socket.gethostname(),
        pid=os.getpid(),
        state="idle",
        model=model,
        project=project,
        session_context=session_context,
        started_at=now,
        last_heartbeat=now,
        capabilities=_detect_capabilities(),
    )

    agent.agent_file.write_text(json.dumps(agent.to_dict(), indent=2))
    return agent


def deregister(agent: AgentInfo) -> None:
    """Remove this agent from the registry."""
    try:
        agent.agent_file.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Suppressed: %s", exc)
        pass


def heartbeat(agent: AgentInfo) -> None:
    """Update heartbeat timestamp. Call every 60s."""
    agent.last_heartbeat = _now_iso()
    try:
        agent.agent_file.write_text(json.dumps(agent.to_dict(), indent=2))
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Suppressed: %s", exc)
        pass


def update_agent(agent: AgentInfo, **kwargs) -> None:
    """Update agent fields and write to disk."""
    for k, v in kwargs.items():
        if hasattr(agent, k):
            setattr(agent, k, v)
    agent.last_heartbeat = _now_iso()
    agent.agent_file.write_text(json.dumps(agent.to_dict(), indent=2))


def list_agents() -> list[AgentInfo]:
    """List all registered agents."""
    agents = []
    if not AGENTS_DIR.exists():
        return agents
    for f in AGENTS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            agents.append(AgentInfo(**data))
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
            continue
    return agents


def get_live_agents() -> list[AgentInfo]:
    """List agents with recent heartbeats (not stale)."""
    now = datetime.now(timezone.utc)
    live = []
    for agent in list_agents():
        try:
            last = datetime.fromisoformat(agent.last_heartbeat.replace("Z", "+00:00"))
            age = (now - last).total_seconds()
            if age < STALE_THRESHOLD:
                live.append(agent)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
            continue
    return live


def _load_stale_tracker() -> dict[str, int]:
    """Load stale observation counts from disk."""
    try:
        return json.loads(_STALE_TRACKER_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_stale_tracker(tracker: dict[str, int]) -> None:
    """Persist stale observation counts to disk."""
    try:
        _STALE_TRACKER_FILE.write_text(json.dumps(tracker))
    except OSError:
        pass


def get_stale_agents() -> list[AgentInfo]:
    """List agents whose heartbeats have expired with hysteresis.

    An agent must be observed as stale for STALE_MISS_COUNT consecutive checks
    before being reported as stale. This prevents network jitter from causing
    false dead-node detection and duplicate task execution.
    """
    now = datetime.now(timezone.utc)
    tracker = _load_stale_tracker()
    stale = []
    changed = False

    for agent in list_agents():
        agent_id = agent.agent_id
        try:
            last = datetime.fromisoformat(agent.last_heartbeat.replace("Z", "+00:00"))
            age = (now - last).total_seconds()
            if age >= STALE_THRESHOLD:
                prev_count = tracker.get(agent_id, 0)
                tracker[agent_id] = prev_count + 1
                changed = True
                if tracker[agent_id] >= STALE_MISS_COUNT:
                    stale.append(agent)
            else:
                # Fresh heartbeat — reset counter
                if agent_id in tracker:
                    del tracker[agent_id]
                    changed = True
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
            # Unparseable heartbeat — count as stale observation
            prev_count = tracker.get(agent_id, 0)
            tracker[agent_id] = prev_count + 1
            changed = True
            if tracker[agent_id] >= STALE_MISS_COUNT:
                stale.append(agent)

    if changed:
        _save_stale_tracker(tracker)

    return stale


def cleanup_stale() -> list[str]:
    """Remove stale agent registrations. Returns list of cleaned agent IDs."""
    cleaned = []
    tracker = _load_stale_tracker()
    for agent in get_stale_agents():
        deregister(agent)
        cleaned.append(agent.agent_id)
        # Clean tracker entry
        tracker.pop(agent.agent_id, None)
    if cleaned:
        _save_stale_tracker(tracker)
    return cleaned


class HeartbeatThread:
    """Background thread that sends heartbeats every HEARTBEAT_INTERVAL seconds."""

    def __init__(self, agent: AgentInfo):
        self.agent = agent
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background heartbeat thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background heartbeat thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            heartbeat(self.agent)
