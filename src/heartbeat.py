#!/usr/bin/env python3
"""Worker heartbeat protocol — routing-protocol-v1 §7, §10.

Workers ping every 30s; coordinator reaps workers that miss >90s.
Stuck workers (heartbeating but no state change for 5 min) get killed.

Storage: Redis primary (`routing:hb:<task_id>`), filesystem fallback
(`/opt/swarm/artifacts/heartbeats/<task_id>.json`).
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 30
HEARTBEAT_TIMEOUT_S = 90
STUCK_WORKER_TIMEOUT_S = 300  # 5 min

FS_FALLBACK_DIR = Path("/opt/swarm/artifacts/heartbeats")

try:
    import redis_client as _rc
except ImportError:
    try:
        from src import redis_client as _rc
    except ImportError:
        _rc = None


@dataclass
class Heartbeat:
    task_id: str
    worker_id: str
    last_ping: float
    state_hash: str  # hash of task state; changes = progress
    started_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "Heartbeat":
        return cls(**json.loads(data))


def _redis_up() -> bool:
    if _rc is None:
        return False
    try:
        return _rc.health_check()
    except Exception:
        return False


def _key(task_id: str) -> str:
    return f"routing:hb:{task_id}"


def _fs_path(task_id: str) -> Path:
    FS_FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    return FS_FALLBACK_DIR / f"{task_id}.json"


def write_heartbeat(hb: Heartbeat) -> bool:
    """Store a heartbeat record. Redis primary, filesystem fallback."""
    data = hb.to_json()

    if _redis_up():
        try:
            r = _rc.get_client()
            r.set(_key(hb.task_id), data, ex=HEARTBEAT_TIMEOUT_S * 2)
            return True
        except Exception as e:
            log.warning("redis write failed: %s; falling back to fs", e)

    try:
        _fs_path(hb.task_id).write_text(data)
        return True
    except Exception as e:
        log.error("fs write failed: %s", e)
        return False


def read_heartbeat(task_id: str) -> Heartbeat | None:
    """Fetch a heartbeat record."""
    if _redis_up():
        try:
            r = _rc.get_client()
            data = r.get(_key(task_id))
            if data:
                return Heartbeat.from_json(data)
        except Exception as e:
            log.warning("redis read failed: %s; falling back to fs", e)

    p = _fs_path(task_id)
    if p.exists():
        try:
            return Heartbeat.from_json(p.read_text())
        except Exception as e:
            log.error("fs read failed: %s", e)
    return None


def is_alive(task_id: str, now: float | None = None) -> bool:
    """True if task has a fresh heartbeat (< HEARTBEAT_TIMEOUT_S)."""
    hb = read_heartbeat(task_id)
    if hb is None:
        return False
    if now is None:
        now = time.time()
    return (now - hb.last_ping) < HEARTBEAT_TIMEOUT_S


def is_stuck(task_id: str, now: float | None = None) -> bool:
    """True if worker heartbeat is alive BUT state has not changed for > STUCK_WORKER_TIMEOUT_S."""
    hb = read_heartbeat(task_id)
    if hb is None:
        return False
    if not is_alive(task_id, now):
        return False  # not stuck, just dead
    if now is None:
        now = time.time()
    # The first heartbeat sets started_at; if the task has been running long
    # enough that the stuck timeout would've elapsed, check whether state_hash
    # has been updated recently enough via last_ping minus a stored "last_progress_at".
    # Simpler v1 heuristic: compare last_ping vs started_at. If worker has been
    # pinging for > STUCK_WORKER_TIMEOUT_S without the caller updating state_hash
    # via update_progress(), treat as stuck.
    # Note: the caller is responsible for invoking update_progress() on real work.
    return (hb.last_ping - hb.started_at) > STUCK_WORKER_TIMEOUT_S and _progress_stale(task_id, now)


def _progress_stale(task_id: str, now: float) -> bool:
    """Returns True if state_hash has not changed in the last STUCK_WORKER_TIMEOUT_S."""
    if _redis_up():
        try:
            r = _rc.get_client()
            last = r.get(f"routing:hb:progress:{task_id}")
            if last:
                return (now - float(last)) > STUCK_WORKER_TIMEOUT_S
        except Exception:
            pass
    # Fallback: assume not stuck if we can't determine
    return False


def update_progress(task_id: str, new_state_hash: str) -> None:
    """Worker calls this when it makes real progress (file write, API call, etc.).
    Updates state_hash and a separate last_progress timestamp so is_stuck can detect
    heartbeat-alive-but-no-progress workers."""
    hb = read_heartbeat(task_id)
    if hb is None:
        return
    hb.state_hash = new_state_hash
    hb.last_ping = time.time()
    write_heartbeat(hb)
    if _redis_up():
        try:
            _rc.get_client().set(
                f"routing:hb:progress:{task_id}",
                str(hb.last_ping),
                ex=STUCK_WORKER_TIMEOUT_S * 2,
            )
        except Exception:
            pass


class HeartbeatThread(threading.Thread):
    """Background thread that pings every HEARTBEAT_INTERVAL_S.

    Usage:
        hb = HeartbeatThread(task_id, worker_id)
        hb.start()
        try:
            do_work()
        finally:
            hb.stop()
    """

    def __init__(self, task_id: str, worker_id: str):
        super().__init__(daemon=True)
        self.task_id = task_id
        self.worker_id = worker_id
        self._stop_event = threading.Event()
        self._hb = Heartbeat(
            task_id=task_id,
            worker_id=worker_id,
            last_ping=time.time(),
            state_hash="init",
            started_at=time.time(),
        )

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._hb.last_ping = time.time()
            write_heartbeat(self._hb)
            self._stop_event.wait(HEARTBEAT_INTERVAL_S)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)

    def update(self, state_hash: str) -> None:
        """Call from worker when real progress is made."""
        self._hb.state_hash = state_hash
        update_progress(self.task_id, state_hash)


def reap_dead_workers(task_ids: list[str], now: float | None = None) -> list[str]:
    """Return list of task_ids whose heartbeat has timed out.
    Caller is responsible for re-dispatching those tasks."""
    dead = []
    for tid in task_ids:
        if not is_alive(tid, now):
            dead.append(tid)
    return dead


def reap_stuck_workers(task_ids: list[str], now: float | None = None) -> list[str]:
    """Return list of task_ids whose worker is alive but stuck (no progress > 5min)."""
    return [tid for tid in task_ids if is_stuck(tid, now)]


if __name__ == "__main__":
    # Smoke test
    import sys

    hb = HeartbeatThread(task_id="smoke-test", worker_id=f"pid-{os.getpid()}")
    hb.start()
    try:
        for i in range(3):
            time.sleep(2)
            hb.update(f"step-{i}")
            print(f"alive: {is_alive('smoke-test')}, stuck: {is_stuck('smoke-test')}")
    finally:
        hb.stop()
    sys.exit(0)
