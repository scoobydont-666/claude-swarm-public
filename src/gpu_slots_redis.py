"""Redis-backed GPU slot management. Drop-in replacement for gpu_slots.py."""

import os
import time

try:
    import redis_client as _rc
except ImportError:
    from src import redis_client as _rc

try:
    from util import hostname
except ImportError:
    from src.util import hostname

import os as _os

if _os.environ.get("SWARM_REDIS_SKIP_CHECK") != "1" and not _rc.health_check():
    raise ImportError("Redis not available — falling back to NFS gpu_slots")


def claim_slot(gpu_id: int = 0, timeout_seconds: int = 0) -> bool:
    """Claim a GPU slot atomically via Redis SET NX EX."""
    holder = f"{hostname()}-{os.getpid()}"

    if timeout_seconds <= 0:
        return _rc.claim_gpu_slot(gpu_id, holder)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _rc.claim_gpu_slot(gpu_id, holder):
            return True
        time.sleep(1)
    return False


def release_slot(gpu_id: int = 0) -> bool:
    """Release a GPU slot (only if we hold it)."""
    holder = f"{hostname()}-{os.getpid()}"
    return _rc.release_gpu_slot(gpu_id, holder)


def is_slot_available(gpu_id: int = 0) -> bool:
    """Check if a GPU slot is available."""
    return _rc.gpu_slot_holder(gpu_id) is None


def get_slot_status() -> list[dict]:
    """Get status of all GPU slots."""
    r = _rc.get_client()
    slots = []
    for key in r.keys("gpu:slot:*"):
        gpu_id = int(key.split(":")[-1])
        holder = r.get(key)
        ttl = r.ttl(key)
        slots.append(
            {
                "gpu_id": gpu_id,
                "holder": holder or "",
                "available": holder is None,
                "ttl": ttl,
            }
        )
    # If no slots in Redis, report GPU 0 as available
    if not slots:
        slots.append({"gpu_id": 0, "holder": "", "available": True, "ttl": -1})
    return sorted(slots, key=lambda s: s["gpu_id"])


def wait_for_slot(
    gpu_id: int = 0,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    priority: int = 5,
) -> bool:
    """Wait for a GPU slot with polling."""
    return claim_slot(gpu_id, timeout_seconds=timeout_seconds)


def get_queue_position(gpu_id: int = 0) -> int:
    """Get queue position (always 0 in Redis — no explicit queue)."""
    return 0


def setup_ollama_slot() -> bool:
    """Permanently claim GPU 0 for Ollama."""
    return _rc.claim_gpu_slot(0, "ollama-permanent")
