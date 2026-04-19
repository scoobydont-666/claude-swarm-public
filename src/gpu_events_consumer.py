"""Phase 4: gpu-events consumer for claude-swarm workers.

Subscribes to the `gpu:events` Redis stream published by
hydra-project/libs/gpu_events/ and maintains an in-memory fleet GPU
state view. Workers query this view before claiming slots so scheduling
decisions are informed by live pod + VRAM state — not stale SQLite.

Read-only consumer. Publishing is exclusively the producer/watcher's job.

Usage:
    from gpu_events_consumer import FleetGpuView
    view = FleetGpuView.start()       # spawns daemon thread
    if view.can_schedule(host="giga", vram_mib_required=8192):
        ...
    view.stop()
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import redis

log = logging.getLogger("gpu_events_consumer")

STREAM_KEY = "gpu:events"
READ_BLOCK_MS = 2000
STALE_HEARTBEAT_S = float(os.environ.get("GPU_EVENTS_STALE_S", "120"))
VRAM_HIGH_THRESHOLD_PCT = 90.0


def _dec(v: Any) -> Any:
    if isinstance(v, bytes):
        v = v.decode()
    if v == "" or v is None:
        return v
    try:
        return json.loads(v)
    except (TypeError, json.JSONDecodeError):
        return v


@dataclass
class HostState:
    host: str
    ready_pods: set[str] = field(default_factory=set)
    crashloop_pods: set[str] = field(default_factory=set)
    vram_high_gpus: set[int] = field(default_factory=set)
    last_event_ts: float = 0.0
    last_heartbeat_ts: float = 0.0
    restart_counts: dict[str, int] = field(default_factory=dict)


class FleetGpuView:
    """In-memory fleet GPU state view for claude-swarm schedulers.

    Thread-safe read API. Backed by a daemon thread doing blocking XREAD
    on gpu:events. Starts with all state empty — full hydration happens
    as events flow.
    """

    def __init__(self, client: redis.Redis | None = None):
        self.client = client or self._build_client()
        self.state: dict[str, HostState] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_id = "$"  # only read new events after we start

    @staticmethod
    def _build_client() -> redis.Redis:
        host = os.environ.get("REDIS_HOST", "127.0.0.1")
        port = int(os.environ.get("REDIS_PORT", "6379"))
        password = os.environ.get("REDIS_PASSWORD", "")
        return redis.Redis(
            host=host,
            port=port,
            password=password or None,
            decode_responses=False,
            socket_timeout=5,
        )

    @classmethod
    def start(cls, client: redis.Redis | None = None) -> "FleetGpuView":
        """Create + start the background consumer. Returns the view."""
        v = cls(client=client)
        v._thread = threading.Thread(target=v._run, daemon=True, name="gpu-events-consumer")
        v._thread.start()
        return v

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout_s)

    def _run(self) -> None:
        log.info("gpu-events consumer started, stream=%s", STREAM_KEY)
        while not self._stop.is_set():
            try:
                streams = self.client.xread({STREAM_KEY: self._last_id}, count=100, block=READ_BLOCK_MS)
            except redis.RedisError as e:
                log.warning("xread failed: %s — retrying in 5s", e)
                time.sleep(5)
                continue
            if not streams:
                continue
            for _stream_name, entries in streams:
                for msg_id, fields in entries:
                    self._apply(fields)
                    self._last_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id

    def _apply(self, fields: dict) -> None:
        """Update in-memory state from one stream event."""
        ev_type = str(_dec(fields.get(b"event") or fields.get("event") or ""))
        host = str(_dec(fields.get(b"host") or fields.get("host") or ""))
        pod = str(_dec(fields.get(b"pod_name") or fields.get("pod_name") or ""))
        ns = str(_dec(fields.get(b"namespace") or fields.get("namespace") or ""))
        key = f"{ns}/{pod}" if pod else ""
        gpu_idx_raw = _dec(fields.get(b"gpu_index") or fields.get("gpu_index") or -1)
        gpu_idx = int(gpu_idx_raw) if gpu_idx_raw not in ("", None) else -1
        restarts_raw = _dec(fields.get(b"restart_count") or fields.get("restart_count") or 0)
        restarts = int(restarts_raw) if restarts_raw not in ("", None) else 0
        ts_raw = _dec(fields.get(b"ts") or fields.get("ts") or time.time())
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            ts = time.time()

        with self._lock:
            hs = self.state.setdefault(host, HostState(host=host))
            hs.last_event_ts = ts
            if ev_type == "heartbeat":
                hs.last_heartbeat_ts = ts
            elif ev_type == "pod_ready" and key:
                hs.ready_pods.add(key)
                hs.crashloop_pods.discard(key)
            elif ev_type == "pod_notready" and key:
                hs.ready_pods.discard(key)
            elif ev_type == "pod_restart" and key:
                hs.restart_counts[key] = restarts
            elif ev_type == "pod_crashloop" and key:
                hs.crashloop_pods.add(key)
                hs.ready_pods.discard(key)
            elif ev_type == "vram_high" and gpu_idx >= 0:
                hs.vram_high_gpus.add(gpu_idx)
            elif ev_type == "vram_normal" and gpu_idx >= 0:
                hs.vram_high_gpus.discard(gpu_idx)

    # -------------------- Query API --------------------

    def snapshot(self) -> dict[str, HostState]:
        with self._lock:
            return {k: HostState(
                host=v.host,
                ready_pods=set(v.ready_pods),
                crashloop_pods=set(v.crashloop_pods),
                vram_high_gpus=set(v.vram_high_gpus),
                last_event_ts=v.last_event_ts,
                last_heartbeat_ts=v.last_heartbeat_ts,
                restart_counts=dict(v.restart_counts),
            ) for k, v in self.state.items()}

    def is_host_healthy(self, host: str) -> bool:
        """True if recent heartbeat + no crashlooping pods."""
        with self._lock:
            hs = self.state.get(host)
            if not hs:
                return False
            fresh = (time.time() - hs.last_heartbeat_ts) < STALE_HEARTBEAT_S
            return fresh and not hs.crashloop_pods

    def is_gpu_busy(self, host: str, gpu_index: int) -> bool:
        """True if this physical GPU has crossed the VRAM_HIGH threshold."""
        with self._lock:
            hs = self.state.get(host)
            if not hs:
                return False
            return gpu_index in hs.vram_high_gpus

    def can_schedule(self, host: str, vram_mib_required: int = 0, gpu_index: int | None = None) -> bool:
        """Advisory scheduling check.

        Returns False if the host is unhealthy, any crashloop is pending,
        or the target GPU is VRAM_HIGH. `vram_mib_required` is accepted for
        future use (would consult DCGM-backed free-VRAM once available).
        """
        with self._lock:
            hs = self.state.get(host)
            if not hs:
                return True  # no data = don't block, defer to legacy scheduler
            fresh = (time.time() - hs.last_heartbeat_ts) < STALE_HEARTBEAT_S
            if not fresh:
                return True
            if hs.crashloop_pods:
                return False
            if gpu_index is not None and gpu_index in hs.vram_high_gpus:
                return False
            if gpu_index is None and hs.vram_high_gpus:
                return False
            return True
