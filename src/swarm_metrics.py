#!/usr/bin/env python3
"""claude-swarm Prometheus Metrics Exporter — exposes swarm state on port 9191.

Metrics:
    swarm_nodes_total            Gauge   — by state: active/idle/offline
    swarm_tasks_total            Gauge   — by state: pending/claimed/completed
    swarm_health_events_total    Counter — cumulative from SQLite event log
    swarm_agent_sessions_total   Gauge   — active sessions from status files
    swarm_last_heartbeat_age_seconds  Gauge — seconds since last heartbeat per hostname

Run:
    python3 /opt/claude-swarm/src/swarm_metrics.py

The server listens on 0.0.0.0:9191. Prometheus should scrape
http://<miniboss>:9191/metrics.
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/ imports resolve when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prometheus_client import Gauge, Counter, start_http_server

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 9191
POLL_INTERVAL_SECONDS = 30

STATUS_DIR = Path("/opt/swarm/status")
TASKS_DIR = Path("/opt/swarm/tasks")
DB_PATH = Path("/opt/claude-swarm/data/health-events.db")

# Node states tracked in status JSON files
NODE_STATES = ("active", "idle", "offline")

# Task subdirectory names map directly to states
TASK_STATES = ("pending", "claimed", "completed")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("swarm.metrics")

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

NODES_TOTAL = Gauge(
    "swarm_nodes_total",
    "Number of swarm nodes by state",
    ["state"],
)

TASKS_TOTAL = Gauge(
    "swarm_tasks_total",
    "Number of swarm tasks by state",
    ["state"],
)

HEALTH_EVENTS_TOTAL = Counter(
    "swarm_health_events_total",
    "Cumulative health events recorded in the SQLite event log",
)

AGENT_SESSIONS_TOTAL = Gauge(
    "swarm_agent_sessions_total",
    "Number of nodes with an active Claude agent session",
)

LAST_HEARTBEAT_AGE = Gauge(
    "swarm_last_heartbeat_age_seconds",
    "Seconds since the last status heartbeat for each node",
    ["hostname"],
)

GPU_SLOTS_USED = Gauge(
    "swarm_gpu_slots_used",
    "Number of GPU slots claimed, by GPU ID",
    ["gpu_id"],
)

DISPATCH_COST_TOTAL = Counter(
    "swarm_dispatch_cost_usd_total",
    "Total estimated cost (USD) of all dispatches",
)

DISPATCH_COST_BY_HOST = Counter(
    "swarm_dispatch_cost_by_host_usd_total",
    "Total estimated dispatch cost by host",
    ["hostname"],
)

# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------


def _collect_node_metrics() -> tuple[dict[str, int], int, list[tuple[str, float]]]:
    """Read all *.json files in STATUS_DIR.

    Returns:
        state_counts   — {state: count} for all NODE_STATES
        active_sessions — count of nodes with a non-empty session_id
        heartbeat_ages  — [(hostname, age_seconds), ...]
    """
    state_counts: dict[str, int] = {s: 0 for s in NODE_STATES}
    active_sessions = 0
    heartbeat_ages: list[tuple[str, float]] = []

    if not STATUS_DIR.exists():
        log.warning("STATUS_DIR does not exist: %s", STATUS_DIR)
        return state_counts, active_sessions, heartbeat_ages

    now = datetime.now(timezone.utc)

    for path in STATUS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read %s: %s", path, exc)
            continue

        state = data.get("state", "offline").lower()
        if state not in state_counts:
            state = "offline"
        state_counts[state] += 1

        if data.get("session_id"):
            active_sessions += 1

        hostname = data.get("hostname", path.stem)
        updated_at = data.get("updated_at", "")
        if updated_at:
            try:
                ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                age = (now - ts).total_seconds()
                heartbeat_ages.append((hostname, max(0.0, age)))
            except ValueError:
                pass

    return state_counts, active_sessions, heartbeat_ages


def _collect_task_metrics() -> dict[str, int]:
    """Count *.yaml files in each TASKS_DIR/<state>/ subdirectory.

    Uses os.listdir for efficiency — avoids glob + parse overhead.

    Returns:
        {state: count} for all TASK_STATES
    """
    counts: dict[str, int] = {s: 0 for s in TASK_STATES}

    if not TASKS_DIR.exists():
        log.warning("TASKS_DIR does not exist: %s", TASKS_DIR)
        return counts

    for state in TASK_STATES:
        state_dir = TASKS_DIR / state
        if state_dir.is_dir():
            try:
                counts[state] = sum(
                    1 for name in os.listdir(state_dir) if name.endswith(".yaml")
                )
            except OSError:
                counts[state] = 0

    return counts


def _collect_event_count() -> int:
    """Return the total number of rows in the health_events SQLite table."""
    if not DB_PATH.exists():
        return 0
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        try:
            row = conn.execute("SELECT COUNT(*) FROM health_events").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("SQLite query failed: %s", exc)
        return 0


_DISPATCH_COST_CACHE_TTL = 60  # seconds
_dispatch_cost_cache: tuple[float, dict[str, float]] | None = None
_dispatch_cost_cache_ts: float = 0.0


def _collect_dispatch_costs() -> tuple[float, dict[str, float]]:
    """Collect total and per-host dispatch costs from plan.yaml files.

    Results are cached for _DISPATCH_COST_CACHE_TTL seconds to avoid
    re-reading all .plan.yaml files on every Prometheus scrape.

    Returns:
        (total_cost, {hostname: cost, ...})
    """
    global _dispatch_cost_cache, _dispatch_cost_cache_ts

    now = time.monotonic()
    if (
        _dispatch_cost_cache is not None
        and (now - _dispatch_cost_cache_ts) < _DISPATCH_COST_CACHE_TTL
    ):
        return _dispatch_cost_cache

    dispatches_dir = Path("/opt/swarm/artifacts/dispatches")
    if not dispatches_dir.exists():
        result: tuple[float, dict[str, float]] = (0.0, {})
        _dispatch_cost_cache = result
        _dispatch_cost_cache_ts = now
        return result

    total_cost = 0.0
    host_costs: dict[str, float] = {}

    for plan_file in dispatches_dir.glob("*.plan.yaml"):
        try:
            import yaml

            with open(plan_file) as f:
                data = yaml.safe_load(f) or {}
            cost = float(data.get("estimated_cost_usd", 0.0))
            host = data.get("host", "unknown")

            total_cost += cost
            host_costs[host] = host_costs.get(host, 0.0) + cost
        except (OSError, ValueError, yaml.YAMLError):
            pass

    _dispatch_cost_cache = (total_cost, host_costs)
    _dispatch_cost_cache_ts = now
    return _dispatch_cost_cache


# ---------------------------------------------------------------------------
# State for HEALTH_EVENTS_TOTAL counter (Prometheus counters are cumulative
# but we read a total from SQLite each cycle — we track what we've already
# incremented to avoid double-counting across restarts by pinning to the
# value at startup and adding deltas).
# ---------------------------------------------------------------------------

_last_event_count: int = 0
_last_dispatch_cost: float = 0.0
_host_dispatch_costs: dict[str, float] = {}


def _collect_gpu_slot_metrics() -> dict[int, bool]:
    """Collect GPU slot status.

    Returns:
        {gpu_id: claimed} for all slots
    """
    try:
        try:
            from gpu_slots_redis import get_slot_status
        except (ImportError, Exception):
            from gpu_slots import get_slot_status
        slots = get_slot_status()
        return {s["gpu_id"]: s["claimed"] for s in slots}
    except ImportError:
        return {}


def _update_all_metrics() -> None:
    global _last_event_count, _last_dispatch_cost, _host_dispatch_costs

    # --- nodes ---
    state_counts, active_sessions, heartbeat_ages = _collect_node_metrics()
    for state in NODE_STATES:
        NODES_TOTAL.labels(state=state).set(state_counts[state])

    AGENT_SESSIONS_TOTAL.set(active_sessions)

    for hostname, age in heartbeat_ages:
        LAST_HEARTBEAT_AGE.labels(hostname=hostname).set(age)

    # --- tasks ---
    task_counts = _collect_task_metrics()
    for state in TASK_STATES:
        TASKS_TOTAL.labels(state=state).set(task_counts[state])

    # --- gpu slots ---
    gpu_status = _collect_gpu_slot_metrics()
    for gpu_id, claimed in gpu_status.items():
        GPU_SLOTS_USED.labels(gpu_id=str(gpu_id)).set(1 if claimed else 0)

    # --- health events (counter delta) ---
    current_count = _collect_event_count()
    if current_count > _last_event_count:
        HEALTH_EVENTS_TOTAL.inc(current_count - _last_event_count)
        _last_event_count = current_count
    elif current_count < _last_event_count:
        # DB was pruned or replaced — reset baseline without emitting negative delta
        log.info(
            "Event count decreased (%d → %d), resetting baseline",
            _last_event_count,
            current_count,
        )
        _last_event_count = current_count

    # --- dispatch costs ---
    total_cost, host_costs = _collect_dispatch_costs()
    if total_cost > _last_dispatch_cost:
        delta = total_cost - _last_dispatch_cost
        DISPATCH_COST_TOTAL.inc(delta)
        _last_dispatch_cost = total_cost

        # Update per-host costs
        for hostname, cost in host_costs.items():
            if hostname not in _host_dispatch_costs:
                _host_dispatch_costs[hostname] = 0.0
            cost_delta = cost - _host_dispatch_costs[hostname]
            if cost_delta > 0:
                DISPATCH_COST_BY_HOST.labels(hostname=hostname).inc(cost_delta)
                _host_dispatch_costs[hostname] = cost


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the swarm metrics exporter Prometheus server.

    Polls swarm state periodically and exports metrics via HTTP.
    """
    log.info("Starting swarm metrics exporter on port %d", PORT)
    log.info("Polling every %d seconds", POLL_INTERVAL_SECONDS)
    log.info("STATUS_DIR : %s", STATUS_DIR)
    log.info("TASKS_DIR  : %s", TASKS_DIR)
    log.info("DB_PATH    : %s", DB_PATH)

    # Seed the event counter baseline so we don't emit a huge spike on first
    # scrape if the DB already has many rows.
    global _last_event_count
    _last_event_count = _collect_event_count()
    log.info("Event log baseline: %d events", _last_event_count)

    start_http_server(PORT)
    log.info("Metrics available at http://0.0.0.0:%d/metrics", PORT)

    while True:
        try:
            _update_all_metrics()
        except Exception as exc:  # noqa: BLE001
            log.error("Metrics update failed: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
