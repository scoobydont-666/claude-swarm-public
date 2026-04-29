"""Redis-backed agent registry. Drop-in replacement for registry.py filesystem ops."""

import json
import logging
import os
from dataclasses import dataclass, field

LOG = logging.getLogger(__name__)

try:
    import redis_client as _rc
except ImportError:
    from src import redis_client as _rc

try:
    from util import hostname, now_iso, swarm_root
except ImportError:
    from src.util import hostname, now_iso, swarm_root

# Fail fast if Redis is not available — callers catch Exception and fall back to NFS
# Only check in production (not when SWARM_BACKEND is unset or testing)
if os.environ.get("SWARM_REDIS_SKIP_CHECK") != "1" and not _rc.health_check():
    raise ImportError("Redis not available — falling back to NFS registry")

# Compatibility: some callers import SWARM_ROOT from registry
SWARM_ROOT = swarm_root()


@dataclass
class AgentInfo:
    """Agent registration info (compatible with registry.py)."""

    hostname: str
    pid: int
    state: str = "idle"  # idle | working | blocked | shutting_down
    project: str = ""
    task_id: str = ""
    model: str = ""
    session_context: str = ""
    started_at: str = ""
    last_heartbeat: str = ""
    registered_at: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)

    @property
    def agent_id(self) -> str:
        """Return unique agent identifier as hostname-pid."""
        return f"{self.hostname}-{self.pid}"


def register(
    model: str = "", project: str = "", session_context: str = ""
) -> AgentInfo:
    """Register this agent in Redis."""
    host = hostname()
    pid = os.getpid()
    caps = _detect_capabilities()
    now = now_iso()
    agent = AgentInfo(
        hostname=host,
        pid=pid,
        model=model,
        project=project,
        capabilities=caps,
        state="idle",
        started_at=now,
        registered_at=now,
        last_heartbeat=now,
        session_context=session_context,
    )
    _rc.register_agent(
        host,
        pid,
        {
            "model": model,
            "project": project,
            "capabilities": caps,
            "session_context": session_context,
        },
    )
    return agent


def deregister(agent: AgentInfo) -> None:
    """Remove agent from Redis."""
    _rc.unregister_agent(agent.hostname, agent.pid)


def heartbeat(agent: AgentInfo) -> None:
    """Refresh agent TTL in Redis."""
    _rc.heartbeat(agent.hostname, agent.pid)


def update_agent(agent: AgentInfo, **kwargs) -> None:
    """Update agent fields in Redis."""
    for k, v in kwargs.items():
        setattr(agent, k, v)
    r = _rc.get_client()
    key = f"agent:{agent.hostname}:{agent.pid}"
    update = {
        k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        for k, v in kwargs.items()
    }
    if update:
        r.hset(key, mapping=update)
        r.expire(key, _rc.AGENT_TTL)


def list_agents() -> list[AgentInfo]:
    """List all live agents from Redis."""
    raw = _rc.list_agents()
    agents = []
    for data in raw:
        raw_caps = data.get("capabilities", "{}")
        if isinstance(raw_caps, str):
            caps = json.loads(raw_caps)
        else:
            caps = raw_caps
        # Normalize list format to dict format for compatibility
        if isinstance(caps, list):
            caps = {c: True for c in caps}
        agents.append(
            AgentInfo(
                hostname=data.get("host", ""),
                pid=int(data.get("pid", 0)),
                model=data.get("model", ""),
                project=data.get("project", ""),
                capabilities=caps,
                state=data.get("state", "idle"),
                task_id=data.get("task_id", ""),
                started_at=data.get("started_at", data.get("registered_at", "")),
                registered_at=data.get("registered_at", ""),
                last_heartbeat=data.get("last_heartbeat", ""),
            )
        )
    return agents


def get_live_agents() -> list[AgentInfo]:
    """Get all agents with recent heartbeats. Redis TTL handles staleness."""
    return list_agents()


def get_stale_agents() -> list[AgentInfo]:
    """No stale agents in Redis — TTL auto-expires them."""
    return []


def cleanup_stale() -> list[str]:
    """No cleanup needed — Redis TTL handles expiration."""
    return []


def _detect_capabilities() -> dict[str, bool]:
    """Auto-detect host capabilities (matches registry.py format)."""
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

        r = httpx.get("http://127.0.0.1:8000/api/v1/heartbeat", timeout=2.0)
        caps["chromadb"] = r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Suppressed: %s", exc)
        pass

    return caps


class HeartbeatThread:
    """Heartbeat thread that refreshes agent TTL in Redis."""

    def __init__(self, agent: AgentInfo, interval: int = 60):
        import threading

        self.agent = agent
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        """Start the heartbeat background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop the heartbeat background thread."""
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                heartbeat(self.agent)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Suppressed: %s", exc)
                pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
