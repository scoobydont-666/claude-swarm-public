"""Redis client for claude-swarm task orchestration.

Replaces NFS + fcntl locks with atomic Redis operations.
Redis runs on orchestration-node (10.0.0.5:6379).

Data model:
  tasks:pending     — Sorted set (score = priority×1000 + timestamp)
  tasks:claimed     — Sorted set
  tasks:completed   — Sorted set
  task:{id}         — Hash (full task data + state)
  agent:{host}:{pid} — Hash with TTL (auto-expire = stale detection)
  status:{host}     — Hash with TTL
  events            — Redis stream
  gpu:slot:{N}      — String with NX + EX (atomic lock)
  gpu:queue:{N}     — Sorted set
  inbox:{host}      — List
"""

import json
import os
import time

import redis

# Default connection config — override via environment
REDIS_HOST = os.environ.get("SWARM_REDIS_HOST", "10.0.0.5")
REDIS_PORT = int(os.environ.get("SWARM_REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("SWARM_REDIS_PASSWORD", "your-redis-password")
REDIS_DB = int(os.environ.get("SWARM_REDIS_DB", "0"))

# TTLs
AGENT_TTL = 300  # 5 minutes — heartbeat refreshes
STATUS_TTL = 120  # 2 minutes
GPU_SLOT_TTL = 300  # 5 minutes


def get_pool() -> redis.ConnectionPool:
    """Get or create the global connection pool."""
    if not hasattr(get_pool, "_pool"):
        get_pool._pool = redis.ConnectionPool(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD or None,
            db=REDIS_DB,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
    return get_pool._pool


def get_client() -> redis.Redis:
    """Get a Redis client from the pool."""
    return redis.Redis(connection_pool=get_pool())


def health_check() -> bool:
    """Check Redis connectivity. Returns True if healthy."""
    try:
        return get_client().ping()
    except (
        redis.ConnectionError,
        redis.AuthenticationError,
        redis.TimeoutError,
        Exception,
    ):
        return False


# -----------------------------------------------------------------------
# Task operations (replaces swarm_lib.py filesystem ops)
# -----------------------------------------------------------------------

# Lua script for atomic task claim: ZPOPMIN from pending, ZADD to claimed
CLAIM_SCRIPT = """
local result = redis.call('ZPOPMIN', KEYS[1], 1)
if #result == 0 then
    return nil
end
local task_id = result[1]
local score = result[2]
redis.call('ZADD', KEYS[2], score, task_id)
redis.call('HSET', 'task:' .. task_id, 'state', 'claimed', 'claimed_at', ARGV[1], 'claimed_by', ARGV[2])
return task_id
"""


def create_task(task_id: str, data: dict, priority: int = 5) -> bool:
    """Create a new task. Returns True on success."""
    r = get_client()
    score = priority * 1000 + int(time.time())
    pipe = r.pipeline()
    pipe.hset(
        f"task:{task_id}",
        mapping={
            "id": task_id,
            "state": "pending",
            "priority": str(priority),
            "created_at": str(time.time()),
            "data": json.dumps(data),
        },
    )
    pipe.zadd("tasks:pending", {task_id: score})
    pipe.execute()
    return True


def claim_task(claimer: str) -> str | None:
    """Atomically claim the highest-priority pending task. Returns task_id or None."""
    r = get_client()
    now = str(time.time())
    result = r.eval(CLAIM_SCRIPT, 2, "tasks:pending", "tasks:claimed", now, claimer)
    return result


def complete_task(task_id: str, result_data: dict | None = None) -> bool:
    """Mark a task as completed."""
    r = get_client()
    pipe = r.pipeline()
    pipe.zrem("tasks:claimed", task_id)
    score = int(time.time())
    pipe.zadd("tasks:completed", {task_id: score})
    update = {"state": "completed", "completed_at": str(time.time())}
    if result_data:
        update["result"] = json.dumps(result_data)
    pipe.hset(f"task:{task_id}", mapping=update)
    pipe.execute()
    return True


def get_task(task_id: str) -> dict | None:
    """Get full task data."""
    r = get_client()
    data = r.hgetall(f"task:{task_id}")
    return data if data else None


def list_tasks(state: str = "pending", limit: int = 50) -> list[dict]:
    """List tasks by state."""
    r = get_client()
    task_ids = r.zrange(f"tasks:{state}", 0, limit - 1)
    if not task_ids:
        return []
    pipe = r.pipeline()
    for tid in task_ids:
        pipe.hgetall(f"task:{tid}")
    return [t for t in pipe.execute() if t]


# -----------------------------------------------------------------------
# Agent registry (replaces registry.py)
# -----------------------------------------------------------------------


def register_agent(host: str, pid: int, capabilities: dict | None = None) -> bool:
    """Register an agent with auto-expiring TTL."""
    r = get_client()
    key = f"agent:{host}:{pid}"
    data = {
        "host": host,
        "pid": str(pid),
        "registered_at": str(time.time()),
        "last_heartbeat": str(time.time()),
    }
    if capabilities:
        data["capabilities"] = json.dumps(capabilities)
    r.hset(key, mapping=data)
    r.expire(key, AGENT_TTL)
    return True


def heartbeat(host: str, pid: int) -> bool:
    """Refresh agent TTL. Returns False if agent not registered."""
    r = get_client()
    key = f"agent:{host}:{pid}"
    if not r.exists(key):
        return False
    r.hset(key, "last_heartbeat", str(time.time()))
    r.expire(key, AGENT_TTL)
    return True


def unregister_agent(host: str, pid: int) -> bool:
    """Remove agent registration."""
    r = get_client()
    return bool(r.delete(f"agent:{host}:{pid}"))


def list_agents() -> list[dict]:
    """List all live agents (those whose TTL hasn't expired)."""
    r = get_client()
    keys = r.keys("agent:*")
    if not keys:
        return []
    pipe = r.pipeline()
    for k in keys:
        pipe.hgetall(k)
    return [a for a in pipe.execute() if a]


# -----------------------------------------------------------------------
# Events (replaces events.py filesystem)
# -----------------------------------------------------------------------


def emit_event(event_type: str, data: dict) -> str:
    """Emit an event to the Redis stream. Returns stream ID."""
    r = get_client()
    fields = {
        "type": event_type,
        "timestamp": str(time.time()),
        "data": json.dumps(data),
    }
    return r.xadd("events", fields)


def query_events(
    start: str = "-", end: str = "+", count: int = 100, event_type: str | None = None
) -> list[dict]:
    """Query events from the stream."""
    r = get_client()
    raw = r.xrange("events", start, end, count=count)
    events = []
    for stream_id, fields in raw:
        if event_type and fields.get("type") != event_type:
            continue
        fields["stream_id"] = stream_id
        events.append(fields)
    return events


def trim_events(max_len: int = 10000) -> int:
    """Trim event stream to max length."""
    r = get_client()
    return r.xtrim("events", maxlen=max_len, approximate=True)


# -----------------------------------------------------------------------
# GPU slots (replaces gpu_slots.py)
# -----------------------------------------------------------------------


def claim_gpu_slot(slot: int, holder: str) -> bool:
    """Atomically claim a GPU slot. Returns True if claimed."""
    r = get_client()
    return bool(r.set(f"gpu:slot:{slot}", holder, nx=True, ex=GPU_SLOT_TTL))


def release_gpu_slot(slot: int, holder: str) -> bool:
    """Release a GPU slot (only if we hold it)."""
    r = get_client()
    current = r.get(f"gpu:slot:{slot}")
    if current == holder:
        return bool(r.delete(f"gpu:slot:{slot}"))
    return False


def gpu_slot_holder(slot: int) -> str | None:
    """Get current holder of a GPU slot."""
    return get_client().get(f"gpu:slot:{slot}")


# -----------------------------------------------------------------------
# Messaging (replaces inbox filesystem)
# -----------------------------------------------------------------------


def send_message(target_host: str, message: dict) -> int:
    """Send a message to a host's inbox. Returns inbox length."""
    r = get_client()
    return r.lpush(f"inbox:{target_host}", json.dumps(message))


def read_inbox(host: str, pop: bool = False) -> list[dict]:
    """Read messages from a host's inbox."""
    r = get_client()
    key = f"inbox:{host}"
    if pop:
        messages = []
        while True:
            msg = r.rpop(key)
            if msg is None:
                break
            messages.append(json.loads(msg))
        return messages
    else:
        raw = r.lrange(key, 0, -1)
        return [json.loads(m) for m in raw]


# -----------------------------------------------------------------------
# Status (replaces status JSON files)
# -----------------------------------------------------------------------


def update_status(host: str, status: dict) -> bool:
    """Update host status with TTL."""
    r = get_client()
    key = f"status:{host}"
    r.hset(
        key,
        mapping={
            k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            for k, v in status.items()
        },
    )
    r.expire(key, STATUS_TTL)
    return True


def get_status(host: str) -> dict | None:
    """Get host status."""
    r = get_client()
    data = r.hgetall(f"status:{host}")
    return data if data else None
