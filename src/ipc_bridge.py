"""
IPC Bridge — Redis Streams event bus for Hydra Swarm v3.

Primary coordination layer (replaces NFS polling):
  Redis Streams → real-time events (sub-second)
  NFS files     → secondary (fallback, state persistence)
  Git sync      → tertiary (durability, cross-session)

Uses nai-ipc-py for wire-compatible messaging with Rust services.

Channels:
  task-events       — task lifecycle (created, claimed, completed, failed)
  gpu-events        — GPU allocation/release/discovery
  dispatch-events   — dispatch started/completed/failed
  infra-events      — node health changes, service up/down
  routing-events    — model routing decisions
"""

import json
import logging
import os
import time
from typing import Optional, Callable

import redis

logger = logging.getLogger(__name__)

# Redis connection config
REDIS_HOST = os.environ.get("SWARM_REDIS_HOST", "10.0.0.5")  # orchestration-node
REDIS_PORT = int(os.environ.get("SWARM_REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("SWARM_REDIS_PASSWORD", "your-redis-password")
REDIS_DB = int(os.environ.get("SWARM_REDIS_DB", "0"))

# Channel names
TASK_EVENTS = "task-events"
GPU_EVENTS = "gpu-events"
DISPATCH_EVENTS = "dispatch-events"
INFRA_EVENTS = "infra-events"
ROUTING_EVENTS = "routing-events"

ALL_CHANNELS = [TASK_EVENTS, GPU_EVENTS, DISPATCH_EVENTS, INFRA_EVENTS, ROUTING_EVENTS]

# Singleton client
_client: Optional[redis.Redis] = None
_agent_id: Optional[str] = None


def get_client() -> redis.Redis:
    """Get or create the Redis client."""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
    return _client


def is_available() -> bool:
    """Check if Redis IPC is reachable."""
    try:
        return get_client().ping()
    except Exception:
        return False


def register(hostname: str = "") -> str:
    """Register this process as an IPC agent via Redis hash."""
    global _agent_id
    try:
        client = get_client()
        _agent_id = hostname or os.environ.get("HOSTNAME", "unknown")
        client.hset(f"hydra:ipc:agents:{_agent_id}", mapping={
            "role": "swarm", "pid": str(os.getpid()), "registered_at": str(time.time()),
        })
        client.expire(f"hydra:ipc:agents:{_agent_id}", 3600)  # 1hr TTL

        # Ensure consumer groups exist for all channels
        for ch in ALL_CHANNELS:
            stream = f"hydra:ipc:{ch}"
            try:
                client.xgroup_create(stream, f"cg:{_agent_id}", id="0", mkstream=True)
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise

        logger.info(f"IPC registered: {_agent_id} on {REDIS_HOST}:{REDIS_PORT}")
        return _agent_id
    except Exception as e:
        logger.warning(f"IPC registration failed (NFS fallback active): {e}")
        return ""


def publish(channel: str, event_type: str, data: dict, priority: int = 3) -> Optional[str]:
    """Publish an event to a Redis Stream.

    Wire-compatible with nai-ipc (Rust) envelope format.
    """
    try:
        client = get_client()
        stream = f"hydra:ipc:{channel}"
        envelope = json.dumps({
            "type": event_type,
            "data": data,
            "sender": _agent_id or "unknown",
            "ts": time.time(),
            "priority": priority,
        })
        msg_id = client.xadd(stream, {"envelope": envelope}, maxlen=10000)
        logger.debug(f"IPC published: {channel}/{event_type} -> {msg_id}")
        return str(msg_id)
    except Exception as e:
        logger.debug(f"IPC publish failed ({channel}/{event_type}): {e}")
        return None


def consume(channel: str, count: int = 10, block_ms: int = 1000) -> list[dict]:
    """Consume events from a Redis Stream via consumer group.

    Returns:
        List of event dicts with {id, type, data, sender, timestamp}
    """
    try:
        client = get_client()
        stream = f"hydra:ipc:{channel}"
        group = f"cg:{_agent_id or 'default'}"

        # Ensure group exists
        try:
            client.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError:
            pass

        results = client.xreadgroup(group, _agent_id or "worker", {stream: ">"}, count=count, block=block_ms)
        events = []
        for _stream_name, messages in results:
            for msg_id, fields in messages:
                try:
                    env = json.loads(fields.get("envelope", "{}"))
                    events.append({
                        "id": msg_id,
                        "type": env.get("type", ""),
                        "data": env.get("data", {}),
                        "sender": env.get("sender", ""),
                        "timestamp": env.get("ts", 0),
                    })
                    # ACK the message
                    client.xack(stream, group, msg_id)
                except (json.JSONDecodeError, KeyError):
                    pass
        return events
    except Exception as e:
        logger.debug(f"IPC consume failed ({channel}): {e}")
        return []


# ── Convenience publishers ────────────────────────────────────────────────────

def emit_task_created(task_id: str, title: str, priority: int = 3, **kwargs):
    """Emit a task.created event."""
    publish(TASK_EVENTS, "task.created", {"task_id": task_id, "title": title, "priority": priority, **kwargs})


def emit_task_claimed(task_id: str, claimed_by: str, **kwargs):
    """Emit a task.claimed event."""
    publish(TASK_EVENTS, "task.claimed", {"task_id": task_id, "claimed_by": claimed_by, **kwargs})


def emit_task_completed(task_id: str, result: str = "", cost_usd: float = 0.0, **kwargs):
    """Emit a task.completed event."""
    publish(TASK_EVENTS, "task.completed", {"task_id": task_id, "result": result, "cost_usd": cost_usd, **kwargs})


def emit_task_failed(task_id: str, error: str = "", **kwargs):
    """Emit a task.failed event."""
    publish(TASK_EVENTS, "task.failed", {"task_id": task_id, "error": error, **kwargs})


def emit_gpu_allocated(host: str, gpu_index: int, task_id: str, model: str = "", vram_mb: int = 0):
    """Emit a gpu.allocated event."""
    publish(GPU_EVENTS, "gpu.allocated", {
        "host": host, "gpu_index": gpu_index, "task_id": task_id,
        "model": model, "vram_mb": vram_mb,
    })


def emit_gpu_released(host: str, gpu_index: int, task_id: str = ""):
    """Emit a gpu.released event."""
    publish(GPU_EVENTS, "gpu.released", {"host": host, "gpu_index": gpu_index, "task_id": task_id})


def emit_dispatch_started(dispatch_id: str, host: str, model: str, task: str):
    """Emit a dispatch.started event."""
    publish(DISPATCH_EVENTS, "dispatch.started", {
        "dispatch_id": dispatch_id, "host": host, "model": model, "task": task[:200],
    })


def emit_dispatch_completed(dispatch_id: str, host: str, status: str, cost_usd: float = 0.0):
    """Emit a dispatch.completed event."""
    publish(DISPATCH_EVENTS, "dispatch.completed", {
        "dispatch_id": dispatch_id, "host": host, "status": status, "cost_usd": cost_usd,
    })


def emit_node_health(hostname: str, status: str, gpu_count: int = 0, details: dict = None):
    """Emit an infra.node_health event."""
    publish(INFRA_EVENTS, "infra.node_health", {
        "hostname": hostname, "status": status, "gpu_count": gpu_count, "details": details or {},
    })


def emit_routing_decision(task_description: str, tier: str, model: str, rule: str):
    """Emit a routing.decision event."""
    publish(ROUTING_EVENTS, "routing.decision", {
        "task": task_description[:200], "tier": tier, "model": model, "rule": rule,
    })


# ── Event listener (for dashboard / reactive components) ──────────────────────

def listen(channels: list[str], callback: Callable[[str, dict], None], block_ms: int = 2000):
    """Blocking event listener loop. Calls callback(channel, event) for each message.

    Use in a background thread for the dashboard or reactive components.
    """
    while True:
        for ch in channels:
            events = consume(ch, count=50, block_ms=block_ms)
            for event in events:
                try:
                    callback(ch, event)
                except Exception as e:
                    logger.warning(f"Event callback error ({ch}): {e}")


# ── KV-Cache / Warm Model Awareness ──────────────────────────────────────────

def query_warm_models(host_ip: str, ollama_port: int = 11434, timeout: int = 3) -> list[dict]:
    """Query Ollama /api/ps on a host to discover warm (loaded) models.

    Returns list of {name, vram_mb, context_length} for loaded models.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["curl", "-sf", "--max-time", str(timeout),
             f"http://{host_ip}:{ollama_port}/api/ps"],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            models = []
            for m in data.get("models", []):
                models.append({
                    "name": m.get("name", ""),
                    "vram_mb": m.get("size_vram", 0) // (1024 * 1024),
                    "context_length": m.get("context_length", 0),
                })
            return models
    except Exception:
        pass
    return []


def discover_fleet_warm_models(fleet_ips: dict[str, str] | None = None) -> dict[str, list[dict]]:
    """Discover warm models across the entire fleet.

    Args:
        fleet_ips: Dict of hostname → IP. Uses defaults if None.

    Returns:
        Dict of hostname → list of warm model dicts
    """
    if fleet_ips is None:
        fleet_ips = {
            "gpu-server-1": "10.0.0.1",
            "gpu-server-2": "10.0.0.2",
            "gpu-server-3": "10.0.0.3",
            "gpu-server-4": "10.0.0.4",
        }

    result = {}
    for hostname, ip in fleet_ips.items():
        models = query_warm_models(ip)
        if models:
            result[hostname] = models
            # Publish to IPC
            for m in models:
                publish(GPU_EVENTS, "gpu.model_warm", {
                    "host": hostname, "model": m["name"], "vram_mb": m["vram_mb"],
                })
    return result


def find_host_with_warm_model(model_name: str, fleet_ips: dict[str, str] | None = None) -> str | None:
    """Find a fleet host that already has a specific model loaded in VRAM.

    Returns hostname if found, None otherwise. Useful for KV-cache-aware routing.
    """
    warm = discover_fleet_warm_models(fleet_ips)
    for hostname, models in warm.items():
        if any(m["name"] == model_name or model_name in m["name"] for m in models):
            return hostname
    return None
