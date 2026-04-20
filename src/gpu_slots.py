#!/usr/bin/env python3
"""GPU Slot Manager — atomic slot claiming via lockfiles.

Manages GPU slot allocation across the swarm using lockfiles at /opt/swarm/gpu/slot-{N}.lock.
Each slot can be claimed by a single hostname:pid:timestamp tuple.

GIGA has 2 GPUs:
  - GPU 0: Reserved for Ollama (permanently claimed)
  - GPU 1: Workload allocation

Example:
    from gpu_slots import claim_slot, release_slot, get_slot_status

    if claim_slot(1):
        try:
            # Use GPU 1
            pass
        finally:
            release_slot(1)
"""

import fcntl
import json
import logging
import os
import socket
import time
from pathlib import Path

LOG = logging.getLogger(__name__)

# Default deadline: auto-release slots held longer than this (seconds)
DEFAULT_DEADLINE_SECONDS = 3600  # 1 hour
# Heartbeat staleness threshold (seconds)
DEFAULT_STALE_THRESHOLD_SECONDS = 300  # 5 minutes


def _gpu_dir() -> Path:
    """Return GPU slot directory, creating it if needed."""
    d = Path("/opt/swarm/gpu")
    d.mkdir(parents=True, exist_ok=True)
    return d


from datetime import UTC

from util import now_iso as _now_iso


def _lock_path(gpu_id: int) -> Path:
    """Return path to lockfile for a GPU slot."""
    return _gpu_dir() / f"slot-{gpu_id}.lock"


def claim_slot(gpu_id: int, timeout_seconds: float = 5.0) -> bool:
    """Claim a GPU slot atomically.

    Args:
        gpu_id: GPU ID (0, 1, etc.)
        timeout_seconds: Max time to wait for lock acquisition (float for non-blocking: 0.0)

    Returns:
        True if claimed successfully, False if already held (even by same process).

    The lockfile contains: hostname:pid:timestamp
    Uses content-based ownership check (not just flock) to prevent same-process double-claiming.
    """
    lock_path = _lock_path(gpu_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Content-based check: if file has a non-empty holder, slot is claimed
    if lock_path.exists():
        try:
            content = lock_path.read_text().strip()
            if content:
                # Slot is already claimed — verify holder PID is alive
                parts = content.split(":")
                if len(parts) >= 2:
                    holder_host = parts[0]
                    try:
                        holder_pid = int(parts[1])
                    except (ValueError, IndexError):
                        holder_pid = 0
                    # If same host, check if PID alive
                    if holder_host == socket.gethostname() and holder_pid > 0:
                        try:
                            os.kill(holder_pid, 0)
                            return False  # PID alive, slot genuinely held
                        except ProcessLookupError:
                            pass  # PID dead, slot is stale — allow reclaim
                    else:
                        return False  # Different host holds it
        except OSError:
            pass

    try:
        lock_f = open(lock_path, "w")
    except OSError:
        return False

    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Write slot holder info (format: hostname:pid|claimed_at|heartbeat|deadline)
        # Uses | as delimiter for extended fields to avoid conflict with : in timestamps
        hostname = socket.gethostname()
        pid = os.getpid()
        timestamp = _now_iso()
        heartbeat = timestamp
        deadline_ts = str(int(time.time()) + DEFAULT_DEADLINE_SECONDS)
        lock_f.write(f"{hostname}:{pid}|{timestamp}|{heartbeat}|{deadline_ts}\n")
        lock_f.flush()

        return True

    except (OSError, BlockingIOError):
        try:
            lock_f.close()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
            pass
        return False


def release_slot(gpu_id: int) -> bool:
    """Release a claimed GPU slot by clearing the lockfile content.

    Args:
        gpu_id: GPU ID to release

    Returns:
        True if released successfully, False if not held or on error.
    """
    lock_path = _lock_path(gpu_id)

    if not lock_path.exists():
        return False

    try:
        # Verify we own this slot (same host)
        content = lock_path.read_text().strip()
        if content:
            parts = content.split(":")
            holder_host = parts[0] if parts else ""
            if holder_host and holder_host != socket.gethostname():
                return False  # Not ours to release

        # Clear the file to release
        lock_path.write_text("")
        return True
    except OSError:
        return False


def is_slot_available(gpu_id: int) -> bool:
    """Check if a GPU slot is available (not held by another process).

    Args:
        gpu_id: GPU ID to check

    Returns:
        True if available, False if held.
    """
    lock_path = _lock_path(gpu_id)

    # If lockfile doesn't exist, slot is available
    if not lock_path.exists():
        return True

    # If file exists but is empty, slot is available
    try:
        content = lock_path.read_text().strip()
        if not content:
            return True
        # File has content — slot is held
        return False
    except OSError:
        # Can't read file — assume held
        return False


def get_slot_status() -> list[dict]:
    """Get status of all GPU slots.

    Returns:
        List of dicts with keys:
            - gpu_id: GPU ID
            - claimed: bool - True if slot is held
            - holder: str - "hostname:pid:timestamp" or empty
    """
    gpu_dir = _gpu_dir()
    slots = []

    # Scan for all slot files in GPU directory
    existing_ids = set()
    if gpu_dir.exists():
        for f in gpu_dir.glob("slot-*.lock"):
            try:
                gpu_id = int(f.stem.split("-")[1])
                existing_ids.add(gpu_id)
            except (IndexError, ValueError):
                continue

    # Include GIGA's 2 GPUs explicitly
    for gpu_id in {0, 1} | existing_ids:
        available = is_slot_available(gpu_id)
        holder = ""

        if not available:
            lock_path = _lock_path(gpu_id)
            try:
                with open(lock_path) as f:
                    holder = f.read().strip()
            except OSError:
                holder = "unknown"

        slots.append(
            {
                "gpu_id": gpu_id,
                "claimed": not available,
                "holder": holder,
            }
        )

    return sorted(slots, key=lambda x: x["gpu_id"])


def _queue_path(gpu_id: int) -> Path:
    """Return path to queue file for a GPU slot."""
    return _gpu_dir() / f"queue-{gpu_id}.json"


def _load_queue(gpu_id: int) -> list[dict]:
    """Load the priority queue for a GPU slot, returning a list of entries."""
    path = _queue_path(gpu_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _save_queue(gpu_id: int, queue: list[dict]) -> None:
    """Atomically write the queue for a GPU slot."""
    path = _queue_path(gpu_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, indent=2))
    tmp.replace(path)


def _clean_queue(gpu_id: int) -> list[dict]:
    """Remove stale queue entries (dead PIDs on this host) and return cleaned queue."""
    queue = _load_queue(gpu_id)
    alive = []
    hostname = socket.gethostname()
    for entry in queue:
        if entry.get("hostname") != hostname:
            alive.append(entry)
            continue
        pid = entry.get("pid", 0)
        if pid <= 0:
            continue
        try:
            os.kill(pid, 0)
            alive.append(entry)
        except ProcessLookupError:
            pass  # PID dead — drop from queue
    if len(alive) != len(queue):
        _save_queue(gpu_id, alive)
    return alive


def _queue_key(entry: dict) -> tuple:
    """Sort key: lower priority number = higher priority, then FIFO by requested_at.

    Args:
        entry: Queue entry dict with 'priority' and 'requested_at' keys.

    Returns:
        Tuple of (priority, requested_at) for sorting.
    """
    return (entry.get("priority", 5), entry.get("requested_at", ""))


def wait_for_slot(
    gpu_id: int = 0,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    priority: int = 5,
) -> bool:
    """Wait for a GPU slot to become available, with priority queueing.

    Args:
        gpu_id: GPU ID to wait for (default 0).
        timeout_seconds: Max seconds to wait before giving up.
        poll_interval: Seconds between availability polls.
        priority: Lower numbers are higher priority (1 = highest, 10 = lowest).

    Returns:
        True if the slot was successfully claimed, False on timeout.

    Entries in /opt/swarm/gpu/queue-{N}.json:
        [{hostname, pid, priority, requested_at}, ...]
    Sorted by (priority asc, requested_at asc) — head of queue gets to claim.
    """
    hostname = socket.gethostname()
    pid = os.getpid()
    requested_at = _now_iso()

    my_entry = {
        "hostname": hostname,
        "pid": pid,
        "priority": priority,
        "requested_at": requested_at,
    }

    # Register in queue
    queue = _clean_queue(gpu_id)
    # Don't add duplicate (same host+pid)
    if not any(e["hostname"] == hostname and e["pid"] == pid for e in queue):
        queue.append(my_entry)
        queue.sort(key=_queue_key)
        _save_queue(gpu_id, queue)

    deadline = time.monotonic() + timeout_seconds

    try:
        while time.monotonic() < deadline:
            # Refresh and clean queue each iteration
            queue = _clean_queue(gpu_id)

            # Check if we're at the head
            if queue and queue[0]["hostname"] == hostname and queue[0]["pid"] == pid:
                # We're first — try to claim
                if is_slot_available(gpu_id):
                    if claim_slot(gpu_id):
                        # Remove ourselves from the queue
                        queue = [
                            e for e in queue if not (e["hostname"] == hostname and e["pid"] == pid)
                        ]
                        _save_queue(gpu_id, queue)
                        return True

            time.sleep(poll_interval)

        return False
    finally:
        # Always clean ourselves out of queue on exit (timeout or success already removes)
        queue = _load_queue(gpu_id)
        queue = [e for e in queue if not (e["hostname"] == hostname and e["pid"] == pid)]
        _save_queue(gpu_id, queue)


def get_queue_position(gpu_id: int) -> int:
    """Return this process's 1-based position in the GPU slot queue, or 0 if not queued.

    Args:
        gpu_id: GPU ID to check.

    Returns:
        1-based position (1 = head of queue), or 0 if not in queue.
    """
    hostname = socket.gethostname()
    pid = os.getpid()
    queue = _clean_queue(gpu_id)
    for i, entry in enumerate(queue):
        if entry["hostname"] == hostname and entry["pid"] == pid:
            return i + 1
    return 0


def heartbeat_slot(gpu_id: int) -> bool:
    """Update the heartbeat timestamp for a claimed GPU slot.

    Should be called periodically by the process holding the slot
    to prove liveness. If heartbeat is not updated within
    DEFAULT_STALE_THRESHOLD_SECONDS, the slot is considered stale.

    Args:
        gpu_id: GPU ID to heartbeat.

    Returns:
        True if heartbeat updated, False if not held by this host.
    """
    lock_path = _lock_path(gpu_id)
    if not lock_path.exists():
        return False

    try:
        content = lock_path.read_text().strip()
        if not content:
            return False

        info = _parse_slot_info(content)
        if not info:
            return False

        if info["hostname"] != socket.gethostname():
            return False

        new_heartbeat = _now_iso()
        claimed_at = info.get("claimed_at") or _now_iso()
        deadline_ts = info.get("deadline_ts") or int(time.time()) + DEFAULT_DEADLINE_SECONDS

        lock_path.write_text(
            f"{info['hostname']}:{info['pid']}|{claimed_at}|{new_heartbeat}|{deadline_ts}\n"
        )
        return True
    except OSError:
        return False


def _parse_slot_info(content: str) -> dict:
    """Parse lockfile content into a structured dict.

    Supports two formats:
    - New: hostname:pid|claimed_at|heartbeat|deadline_ts
    - Legacy: hostname:pid:claimed_at (no heartbeat/deadline)

    Args:
        content: Raw lockfile content string.

    Returns:
        Dict with keys: hostname, pid, claimed_at, heartbeat, deadline_ts.
        Empty dict if content is empty or unparseable.
    """
    content = content.strip()
    if not content:
        return {}

    # New format uses | for extended fields
    if "|" in content:
        # Split on first | to get "hostname:pid" and the rest
        base, _, extended = content.partition("|")
        ext_parts = extended.split("|")
        host_pid = base.split(":")
        if len(host_pid) < 2:
            return {}
        return {
            "hostname": host_pid[0],
            "pid": int(host_pid[1]) if host_pid[1].isdigit() else 0,
            "claimed_at": ext_parts[0] if len(ext_parts) > 0 else "",
            "heartbeat": ext_parts[1] if len(ext_parts) > 1 else "",
            "deadline_ts": int(ext_parts[2])
            if len(ext_parts) > 2 and ext_parts[2].isdigit()
            else 0,
        }

    # Legacy format: hostname:pid:timestamp
    parts = content.split(":")
    if len(parts) < 2:
        return {}

    # Rejoin timestamp parts (ISO timestamps have colons)
    pid_str = parts[1]
    claimed_at = ":".join(parts[2:]) if len(parts) > 2 else ""

    return {
        "hostname": parts[0],
        "pid": int(pid_str) if pid_str.isdigit() else 0,
        "claimed_at": claimed_at,
        "heartbeat": "",
        "deadline_ts": 0,
    }


def is_slot_stale(gpu_id: int, stale_threshold: int = DEFAULT_STALE_THRESHOLD_SECONDS) -> bool:
    """Check if a claimed GPU slot has a stale heartbeat.

    A slot is stale if:
    - It has content (is claimed)
    - The heartbeat timestamp is older than stale_threshold seconds
    - OR the holder PID is dead (same-host only)

    Args:
        gpu_id: GPU ID to check.
        stale_threshold: Seconds without heartbeat before considered stale.

    Returns:
        True if the slot is stale, False if healthy or unclaimed.
    """
    lock_path = _lock_path(gpu_id)
    if not lock_path.exists():
        return False

    try:
        content = lock_path.read_text().strip()
        if not content:
            return False

        info = _parse_slot_info(content)
        if not info:
            return False

        # Same-host PID check
        if info["hostname"] == socket.gethostname() and info["pid"] > 0:
            try:
                os.kill(info["pid"], 0)
            except ProcessLookupError:
                return True  # PID dead = stale

        # Heartbeat staleness check
        if info["heartbeat"]:
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(info["heartbeat"].replace("Z", "+00:00"))
                age = (datetime.now(UTC) - dt).total_seconds()
                if age > stale_threshold:
                    return True
            except (ValueError, TypeError):
                pass

        return False
    except OSError:
        return False


def is_slot_expired(gpu_id: int) -> bool:
    """Check if a claimed GPU slot has exceeded its deadline.

    Args:
        gpu_id: GPU ID to check.

    Returns:
        True if the slot deadline has passed, False otherwise.
    """
    lock_path = _lock_path(gpu_id)
    if not lock_path.exists():
        return False

    try:
        content = lock_path.read_text().strip()
        if not content:
            return False

        info = _parse_slot_info(content)
        if not info or not info.get("deadline_ts"):
            return False

        return time.time() > info["deadline_ts"]
    except OSError:
        return False


def release_stale_slots(stale_threshold: int = DEFAULT_STALE_THRESHOLD_SECONDS) -> list[int]:
    """Scan all GPU slots and release any that are stale or expired.

    A slot is released if:
    - Heartbeat is older than stale_threshold seconds
    - OR the deadline timestamp has passed
    - OR the holder PID is dead (same-host only)

    Args:
        stale_threshold: Seconds without heartbeat before considered stale.

    Returns:
        List of GPU IDs that were released.
    """
    released = []
    gpu_dir = _gpu_dir()
    if not gpu_dir.exists():
        return released

    for f in gpu_dir.glob("slot-*.lock"):
        try:
            gpu_id = int(f.stem.split("-")[1])
        except (IndexError, ValueError):
            continue

        content = ""
        try:
            content = f.read_text().strip()
        except OSError:
            continue

        if not content:
            continue

        should_release = False

        if is_slot_stale(gpu_id, stale_threshold):
            LOG.warning("Releasing stale GPU slot %d (heartbeat expired)", gpu_id)
            should_release = True
        elif is_slot_expired(gpu_id):
            LOG.warning("Releasing expired GPU slot %d (deadline passed)", gpu_id)
            should_release = True

        if should_release:
            try:
                # Force release: clear the lockfile regardless of hostname
                f.write_text("")
                released.append(gpu_id)
            except OSError:
                LOG.error("Failed to release GPU slot %d", gpu_id)

    return released


def claim_slot_with_deadline(gpu_id: int, deadline_seconds: int = DEFAULT_DEADLINE_SECONDS) -> bool:
    """Claim a GPU slot with a custom deadline.

    Args:
        gpu_id: GPU ID to claim.
        deadline_seconds: Seconds until auto-release.

    Returns:
        True if claimed, False otherwise.
    """
    lock_path = _lock_path(gpu_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Content-based check (same as claim_slot)
    if lock_path.exists():
        try:
            content = lock_path.read_text().strip()
            if content:
                parts = content.split(":")
                if len(parts) >= 2:
                    holder_host = parts[0]
                    try:
                        holder_pid = int(parts[1])
                    except (ValueError, IndexError):
                        holder_pid = 0
                    if holder_host == socket.gethostname() and holder_pid > 0:
                        try:
                            os.kill(holder_pid, 0)
                            return False  # PID alive
                        except ProcessLookupError:
                            pass  # PID dead, reclaim
                    else:
                        return False
        except OSError:
            pass

    try:
        lock_f = open(lock_path, "w")
    except OSError:
        return False

    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        hostname = socket.gethostname()
        pid = os.getpid()
        timestamp = _now_iso()
        deadline_ts = str(int(time.time()) + deadline_seconds)
        lock_f.write(f"{hostname}:{pid}|{timestamp}|{timestamp}|{deadline_ts}\n")
        lock_f.flush()
        return True
    except (OSError, BlockingIOError):
        try:
            lock_f.close()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Suppressed: %s", exc)
        return False


def setup_ollama_slot() -> bool:
    """Permanently claim GPU 0 for Ollama on startup.

    Should be called once when swarm initializes.
    Uses a very long deadline (30 days) since Ollama is permanent.

    Returns:
        True if successful, False otherwise.
    """
    return claim_slot_with_deadline(0, deadline_seconds=30 * 86400)
