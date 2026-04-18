#!/usr/bin/env python3
"""claude-swarm web dashboard — FastAPI app serving fleet status at 127.0.0.1:9192.

Endpoints:
  GET / — serve the dashboard HTML
  GET /api/status — all node statuses from status JSON files
  GET /api/tasks — all tasks (pending, claimed, completed) with counts
  GET /api/dispatches — recent dispatches with status
  GET /api/health — latest health check results
  GET /api/metrics — current Prometheus metric values
  GET /api/events — recent health events from SQLite

Run:
  python3 /opt/claude-swarm/src/dashboard.py [--host 127.0.0.1] [--port 9192]
"""

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# Add parent to path so swarm_lib is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from backend import lib
except ImportError:
    import swarm_lib as lib
from util import relative_time as _relative_time

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 9192
HOST = "127.0.0.1"

DB_PATH = Path("/opt/claude-swarm/data/health-events.db")
DISPATCHES_DIR = Path("/opt/swarm/artifacts/dispatches")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("swarm.dashboard")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="claude-swarm Dashboard")


def _read_dispatch_yaml(path: Path) -> dict[str, Any]:
    """Read a dispatch YAML file safely."""
    try:
        import yaml

        with open(path) as f:
            return yaml.safe_load(f) or {}
    except (OSError, Exception):
        return {}


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def get_dashboard_html() -> str:
    """Serve the dashboard HTML."""
    return DASHBOARD_HTML


@app.get("/health")
def get_liveness_health() -> dict[str, Any]:
    """K8s-style liveness probe. degradation_reason pattern (P1 backport #4).

    Returns HTTP 200 always; status field signals health. Kubernetes probes
    and the Dockerfile healthcheck hit this endpoint.
    """
    import os

    checks: dict[str, Any] = {}
    degradation_reasons: list[str] = []

    # NFS mount check (primary coordination surface)
    swarm_nfs = os.path.ismount("/opt/swarm") or os.path.isdir("/opt/swarm/artifacts")
    checks["nfs_swarm"] = swarm_nfs
    if not swarm_nfs:
        degradation_reasons.append("nfs /opt/swarm not mounted")

    # Redis ping (Celery broker + IPC substrate)
    try:
        from redis_client import get_client

        get_client().ping()
        checks["redis"] = True
    except Exception as e:
        checks["redis"] = False
        degradation_reasons.append(f"redis unreachable: {type(e).__name__}")

    degraded = bool(degradation_reasons)
    return {
        "status": "degraded" if degraded else "ok",
        "degradation_reason": "; ".join(degradation_reasons) if degraded else None,
        "checks": checks,
    }


@app.get("/api/status")
def get_status_api() -> dict[str, Any]:
    """Return all node statuses from status JSON files."""
    nodes = lib.get_all_status()

    # Add color codes for UI
    for node in nodes:
        state = node.get("state", "unknown")
        if state == "active":
            node["_color"] = "green"
            node["_dot"] = "●"
        elif state == "idle":
            node["_color"] = "yellow"
            node["_dot"] = "◐"
        elif state == "offline":
            node["_color"] = "red"
            node["_dot"] = "○"
        elif state == "busy":
            node["_color"] = "cyan"
            node["_dot"] = "◉"
        else:
            node["_color"] = "gray"
            node["_dot"] = "?"

        # Add heartbeat age
        updated = node.get("updated_at", "")
        node["_heartbeat_age"] = _relative_time(updated)

    return {"nodes": nodes}


@app.get("/api/tasks")
def get_tasks_api() -> dict[str, Any]:
    """Return all tasks with stage counts."""
    pending = lib.list_tasks(stage="pending") or []
    claimed = lib.list_tasks(stage="claimed") or []
    completed = lib.list_tasks(stage="completed") or []

    # Add age to each task
    for tasks_list in [pending, claimed, completed]:
        for task in tasks_list:
            created = task.get("created_at", "")
            task["_age"] = _relative_time(created)

    return {
        "pending": {
            "count": len(pending),
            "tasks": pending,
        },
        "claimed": {
            "count": len(claimed),
            "tasks": claimed,
        },
        "completed": {
            "count": len(completed),
            "tasks": completed,
        },
        "total": {
            "count": len(pending) + len(claimed) + len(completed),
        },
    }


@app.get("/api/dispatches")
def get_dispatches_api() -> dict[str, Any]:
    """Return recent dispatches — reads from Redis IPC stream first, NFS fallback."""
    dispatches = []

    # Primary: Redis IPC dispatch-events stream
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from ipc_bridge import get_client, is_available

        if is_available():
            client = get_client()
            raw = client.xrevrange("hydra:ipc:dispatch-events", "+", "-", count=30)
            for msg_id, fields in raw:
                try:
                    import json as _json

                    env = _json.loads(fields.get("envelope", "{}"))
                    data = env.get("data", {})
                    dispatches.append(
                        {
                            "dispatch_id": data.get("dispatch_id", msg_id),
                            "host": data.get("host", ""),
                            "model": data.get("model", ""),
                            "status": data.get("status", env.get("type", "").split(".")[-1]),
                            "task": data.get("task", ""),
                            "_started_ago": _relative_time(env.get("ts", 0)),
                            "_icon": "✓" if "completed" in env.get("type", "") else "▶",
                        }
                    )
                except Exception:
                    continue
            if dispatches:
                return {"dispatches": dispatches, "count": len(dispatches)}
    except Exception:
        pass

    # Fallback: NFS YAML files
    if DISPATCHES_DIR.exists():
        for yaml_file in sorted(DISPATCHES_DIR.glob("*.yaml"), reverse=True)[:30]:
            data = _read_dispatch_yaml(yaml_file)
            if data:
                data["_started_ago"] = _relative_time(data.get("started_at", ""))
                data["_icon"] = "✓" if data.get("status") == "completed" else "▶"
                dispatches.append(data)

    return {"dispatches": dispatches, "count": len(dispatches)}


@app.get("/api/health")
def get_health_api() -> dict[str, Any]:
    """Return latest health/infra events — Redis IPC first, SQLite fallback."""
    events = []

    # Primary: Redis IPC infra-events stream (read-only, no ACK)
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from ipc_bridge import get_client, is_available

        if is_available():
            client = get_client()
            raw = client.xrevrange("hydra:ipc:infra-events", "+", "-", count=20)
            for msg_id, fields in raw:
                try:
                    import json as _json

                    env = _json.loads(fields.get("envelope", "{}"))
                    data = env.get("data", {})
                    event = {
                        "rule_name": data.get("type", env.get("type", "unknown")),
                        "host": data.get("hostname", ""),
                        "severity": "medium",
                        "description": str(data),
                        "_age": _relative_time(env.get("ts", 0)),
                        "_icon": "🟡",
                        "_color": "yellow",
                    }
                    events.append(event)
                except Exception:
                    continue
            if events:
                return {"events": events, "count": len(events)}
    except Exception:
        pass

    # Fallback: SQLite health events DB
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM health_events ORDER BY timestamp DESC LIMIT 20"
            ).fetchall()
            events = [dict(row) for row in rows]
            conn.close()
            for event in events:
                event["_age"] = _relative_time(event.get("timestamp", ""))
                severity = event.get("severity", "medium")
                event["_icon"] = {"high": "🔴", "medium": "🟡"}.get(severity, "🟢")
                event["_color"] = {"high": "red", "medium": "yellow"}.get(severity, "green")
        except Exception:
            pass

    return {"events": events, "count": len(events)}


@app.get("/api/metrics")
def get_metrics_api() -> dict[str, Any]:
    """Return current metrics from swarm state."""
    all_nodes = lib.get_all_status()
    all_tasks = {
        "pending": lib.list_tasks(stage="pending") or [],
        "claimed": lib.list_tasks(stage="claimed") or [],
        "completed": lib.list_tasks(stage="completed") or [],
    }

    # Count nodes by state
    node_counts = {}
    for node in all_nodes:
        state = node.get("state", "unknown")
        node_counts[state] = node_counts.get(state, 0) + 1

    # Count tasks by state
    task_counts = {
        "pending": len(all_tasks["pending"]),
        "claimed": len(all_tasks["claimed"]),
        "completed": len(all_tasks["completed"]),
    }

    # Real cache hit rate from Context Bridge /stats endpoint
    # (falls through to 0.0 if CB unreachable — handled in the try block below).
    try:
        import subprocess as _sp

        _cb = _sp.run(
            ["curl", "-sf", "http://127.0.0.1:8520/stats"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if _cb.returncode == 0:
            _stats = __import__("json").loads(_cb.stdout)
            cache_hit_rate = float(_stats.get("utilization_pct", "0")) / 100.0
        else:
            cache_hit_rate = 0.0
    except Exception:
        cache_hit_rate = 0.0  # CB not available

    # Count dispatches today — from Redis IPC stream length
    today_dispatches = 0
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from ipc_bridge import get_client as _ipc_client
        from ipc_bridge import is_available as _ipc_avail

        if _ipc_avail():
            today_dispatches = _ipc_client().xlen("hydra:ipc:dispatch-events") or 0
    except Exception:
        pass

    return {
        "nodes": {
            "total": len(all_nodes),
            "by_state": node_counts,
        },
        "tasks": {
            "total": sum(task_counts.values()),
            "by_state": task_counts,
        },
        "cache_hit_rate": cache_hit_rate,
        "dispatches_today": today_dispatches,
    }


@app.get("/api/events")
def get_events_api() -> dict[str, Any]:
    """Return recent health events from SQLite."""
    if not DB_PATH.exists():
        return {"events": [], "count": 0}

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Get the 50 most recent events
        cur.execute("""
            SELECT id, timestamp, rule_name, host, severity, description, action_taken
            FROM health_events
            ORDER BY timestamp DESC
            LIMIT 50
        """)

        rows = cur.fetchall()
        events = [dict(row) for row in rows]
        conn.close()

        # Add relative time
        for event in events:
            event["_age"] = _relative_time(event.get("timestamp", ""))

        return {"events": events, "count": len(events)}
    except (sqlite3.Error, OSError) as e:
        log.error(f"Error reading health events: {e}")
        return {"events": [], "count": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# v3 API endpoints: GPU status, warm models, IPC events, costs
# ---------------------------------------------------------------------------


@app.get("/api/gpu")
def get_gpu_api() -> dict[str, Any]:
    """Return GPU allocation status across the fleet."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from gpu_scheduler_v2 import GpuScheduler

        scheduler = GpuScheduler()
        return scheduler.get_status()
    except Exception as e:
        return {"error": str(e), "total_gpus": 0}


@app.get("/api/warm_models")
def get_warm_models_api() -> dict[str, Any]:
    """Return warm (loaded) Ollama models across the fleet."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from ipc_bridge import discover_fleet_warm_models

        warm = discover_fleet_warm_models()
        return {"hosts": warm, "total_models": sum(len(v) for v in warm.values())}
    except Exception as e:
        return {"error": str(e), "hosts": {}}


@app.get("/api/ipc_events")
def get_ipc_events_api() -> dict[str, Any]:
    """Return recent IPC events from Redis Streams (read-only, no ACK)."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from ipc_bridge import ALL_CHANNELS, get_client, is_available

        if not is_available():
            return {"available": False, "events": []}

        client = get_client()
        all_events = []
        for ch in ALL_CHANNELS:
            stream = f"hydra:ipc:{ch}"
            try:
                # XREVRANGE is read-only — doesn't consume/ACK
                raw = client.xrevrange(stream, "+", "-", count=10)
                for msg_id, fields in raw:
                    try:
                        import json as _json

                        env = _json.loads(fields.get("envelope", "{}"))
                        all_events.append(
                            {
                                "id": msg_id,
                                "channel": ch,
                                "type": env.get("type", ""),
                                "data": env.get("data", {}),
                                "sender": env.get("sender", ""),
                                "timestamp": env.get("ts", 0),
                            }
                        )
                    except Exception:
                        continue
            except Exception:
                continue

        all_events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        return {"available": True, "events": all_events[:50]}
    except Exception as e:
        return {"available": False, "error": str(e), "events": []}


@app.get("/api/costs")
def get_costs_api() -> dict[str, Any]:
    """Return dispatch cost data from Hydra Pulse."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from cost_tracker import get_daily_costs

        return get_daily_costs() or {"note": "No cost data available"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/routing")
def get_routing_api() -> dict[str, Any]:
    """Return model routing rules and recent decisions."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from model_router import get_router

        router = get_router()
        return {
            "rules": [
                {"name": r.name, "pattern": r.pattern, "tier": r.tier, "model": r.model}
                for r in router.rules
            ],
            "total_rules": len(router.rules),
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Dashboard HTML (embedded, single file)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>claude-swarm Dashboard</title>
    <meta http-equiv="refresh" content="10">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            background-color: #0d1117;
            color: #c9d1d9;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            font-size: 14px;
            line-height: 1.5;
        }

        header {
            background-color: #161b22;
            border-bottom: 1px solid #30363d;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
        }

        h1 {
            font-size: 28px;
            font-weight: 600;
            color: #f0f6fc;
        }

        .subtitle {
            color: #8b949e;
            font-size: 12px;
            margin-top: 4px;
        }

        main {
            padding: 20px;
            max-width: 1400px;
            margin: 0 auto;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }

        .card {
            background-color: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 16px;
        }

        .card h2 {
            font-size: 16px;
            font-weight: 600;
            color: #f0f6fc;
            margin-bottom: 12px;
            border-bottom: 1px solid #30363d;
            padding-bottom: 8px;
        }

        .node-card {
            background-color: #0d1117;
            border: 1px solid #30363d;
            border-radius: 4px;
            padding: 12px;
            margin-bottom: 8px;
            font-size: 13px;
        }

        .node-header {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 6px;
        }

        .node-dot {
            font-size: 18px;
            line-height: 1;
        }

        .node-dot.green { color: #3fb950; }
        .node-dot.yellow { color: #d29922; }
        .node-dot.red { color: #f85149; }
        .node-dot.cyan { color: #58a6ff; }
        .node-dot.gray { color: #6e7681; }

        .node-hostname {
            font-weight: 600;
            color: #f0f6fc;
            flex: 1;
        }

        .node-state {
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 11px;
            font-weight: 500;
            text-transform: uppercase;
        }

        .state-active { background-color: #238636; color: #fff; }
        .state-idle { background-color: #9e6a03; color: #fff; }
        .state-offline { background-color: #da3633; color: #fff; }
        .state-busy { background-color: #0969da; color: #fff; }

        .node-details {
            color: #8b949e;
            font-size: 12px;
        }

        .node-details div {
            margin: 3px 0;
        }

        .label {
            color: #6e7681;
            font-weight: 500;
        }

        .task-column {
            display: inline-block;
            width: 32%;
            vertical-align: top;
            margin-right: 1%;
        }

        .task-column h3 {
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 8px;
            padding: 6px 8px;
            border-radius: 4px;
        }

        .task-column.pending h3 {
            background-color: #9e6a03;
            color: #fff;
        }

        .task-column.claimed h3 {
            background-color: #0969da;
            color: #fff;
        }

        .task-column.completed h3 {
            background-color: #238636;
            color: #fff;
        }

        .task-item {
            background-color: #0d1117;
            border: 1px solid #30363d;
            border-radius: 4px;
            padding: 8px;
            margin-bottom: 6px;
            font-size: 12px;
        }

        .task-id {
            font-weight: 600;
            color: #58a6ff;
            word-break: break-all;
        }

        .task-meta {
            color: #8b949e;
            font-size: 11px;
            margin-top: 4px;
        }

        .dispatch-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }

        .dispatch-table th {
            background-color: #0d1117;
            border-bottom: 2px solid #30363d;
            padding: 8px;
            text-align: left;
            font-weight: 600;
            color: #f0f6fc;
        }

        .dispatch-table td {
            border-bottom: 1px solid #30363d;
            padding: 8px;
        }

        .dispatch-table tr:hover {
            background-color: rgba(88, 166, 255, 0.1);
        }

        .status-running {
            color: #58a6ff;
        }

        .status-completed {
            color: #3fb950;
        }

        .status-failed {
            color: #f85149;
        }

        .health-event {
            background-color: #0d1117;
            border-left: 3px solid #30363d;
            border-radius: 4px;
            padding: 10px;
            margin-bottom: 8px;
            font-size: 12px;
        }

        .health-event.high {
            border-left-color: #f85149;
        }

        .health-event.medium {
            border-left-color: #d29922;
        }

        .health-event.low {
            border-left-color: #3fb950;
        }

        .health-rule {
            font-weight: 600;
            color: #f0f6fc;
        }

        .health-desc {
            color: #8b949e;
            font-size: 11px;
            margin-top: 4px;
        }

        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
        }

        .metric-box {
            background-color: #0d1117;
            border: 1px solid #30363d;
            border-radius: 4px;
            padding: 12px;
            text-align: center;
        }

        .metric-value {
            font-size: 24px;
            font-weight: 600;
            color: #58a6ff;
        }

        .metric-label {
            color: #8b949e;
            font-size: 11px;
            margin-top: 4px;
            text-transform: uppercase;
        }

        .spinner {
            display: inline-block;
            width: 12px;
            height: 12px;
            border: 2px solid #30363d;
            border-top-color: #58a6ff;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .error {
            background-color: #161b22;
            border: 1px solid #f85149;
            border-radius: 4px;
            padding: 10px;
            color: #f85149;
            font-size: 12px;
        }

        .loading {
            text-align: center;
            color: #8b949e;
            padding: 20px;
        }

        footer {
            text-align: center;
            padding: 20px;
            color: #6e7681;
            font-size: 12px;
            border-top: 1px solid #30363d;
            margin-top: 40px;
        }
    </style>
</head>
<body>
    <header>
        <h1>⚙️ claude-swarm Dashboard</h1>
        <div class="subtitle">Real-time fleet status and task monitoring</div>
    </header>

    <main>
        <!-- Metrics Summary -->
        <div class="card">
            <h2>📊 Metrics Summary</h2>
            <div class="metrics-grid" id="metrics-container">
                <div class="metric-box">
                    <div class="metric-value" id="metric-nodes">-</div>
                    <div class="metric-label">Total Nodes</div>
                </div>
                <div class="metric-box">
                    <div class="metric-value" id="metric-tasks">-</div>
                    <div class="metric-label">Pending Tasks</div>
                </div>
                <div class="metric-box">
                    <div class="metric-value" id="metric-cache">-</div>
                    <div class="metric-label">Cache Hit Rate</div>
                </div>
                <div class="metric-box">
                    <div class="metric-value" id="metric-dispatches">-</div>
                    <div class="metric-label">Dispatches Today</div>
                </div>
                <div class="metric-box">
                    <div class="metric-value" id="metric-gpus">-</div>
                    <div class="metric-label">GPUs Online</div>
                </div>
                <div class="metric-box">
                    <div class="metric-value" id="metric-warm-models">-</div>
                    <div class="metric-label">Warm Models</div>
                </div>
            </div>
        </div>

        <!-- Fleet Status -->
        <div class="grid">
            <div class="card">
                <h2>🖥️ Fleet Status</h2>
                <div id="fleet-status-container" class="loading">Loading...</div>
            </div>

            <!-- Task Queue -->
            <div class="card">
                <h2>📋 Task Queue</h2>
                <div id="tasks-container" class="loading">Loading...</div>
            </div>
        </div>

        <!-- Dispatch Monitor -->
        <div class="card">
            <h2>🚀 Dispatch Monitor</h2>
            <div id="dispatches-container" class="loading">Loading...</div>
        </div>

        <!-- GPU & Warm Models -->
        <div class="grid">
            <div class="card">
                <h2>🎮 GPU Utilization</h2>
                <div id="gpu-container" class="loading">Loading...</div>
            </div>

            <div class="card">
                <h2>🔥 Warm Models</h2>
                <div id="warm-models-container" class="loading">Loading...</div>
            </div>
        </div>

        <!-- IPC Events -->
        <div class="card">
            <h2>📡 IPC Events</h2>
            <div id="ipc-container" class="loading">Loading...</div>
        </div>

        <!-- Health Alerts -->
        <div class="card">
            <h2>🚨 Health Alerts</h2>
            <div id="health-container" class="loading">Loading...</div>
        </div>
    </main>

    <footer>
        Auto-refreshing every 10 seconds • Last updated: <span id="last-update">—</span>
    </footer>

    <script>
        async function updateDashboard() {
            try {
                // Update metrics
                const metrics = await fetch('/api/metrics').then(r => r.json());
                document.getElementById('metric-nodes').textContent = metrics.nodes.total;
                document.getElementById('metric-tasks').textContent = metrics.tasks.by_state.pending;
                document.getElementById('metric-cache').textContent = (metrics.cache_hit_rate * 100).toFixed(0) + '%';
                document.getElementById('metric-dispatches').textContent = metrics.dispatches_today;

                // Update fleet status
                const status = await fetch('/api/status').then(r => r.json());
                const fleetHtml = status.nodes.map(node => {
                    const stateClass = 'state-' + node.state;
                    return `
                        <div class="node-card">
                            <div class="node-header">
                                <div class="node-dot ${node._color}">${node._dot}</div>
                                <div class="node-hostname">${node.hostname}</div>
                                <div class="node-state ${stateClass}">${node.state}</div>
                            </div>
                            <div class="node-details">
                                <div><span class="label">Task:</span> ${node.current_task || '—'}</div>
                                <div><span class="label">Model:</span> ${node.model || '—'}</div>
                                <div><span class="label">Heartbeat:</span> ${node._heartbeat_age}</div>
                            </div>
                        </div>
                    `;
                }).join('');
                document.getElementById('fleet-status-container').innerHTML = fleetHtml || '<p style="color: #8b949e;">No nodes registered yet.</p>';

                // Update tasks
                const tasks = await fetch('/api/tasks').then(r => r.json());
                const tasksHtml = `
                    <div class="task-column pending">
                        <h3>⏳ Pending (${tasks.pending.count})</h3>
                        ${tasks.pending.tasks.slice(0, 5).map(t => `
                            <div class="task-item">
                                <div class="task-id">${t.id}</div>
                                <div class="task-meta">
                                    <div>Priority: ${t.priority || 'normal'}</div>
                                    <div>Age: ${t._age}</div>
                                </div>
                            </div>
                        `).join('') || '<p style="color: #8b949e; font-size: 11px;">No pending tasks</p>'}
                    </div>
                    <div class="task-column claimed">
                        <h3>🔒 Claimed (${tasks.claimed.count})</h3>
                        ${tasks.claimed.tasks.slice(0, 5).map(t => `
                            <div class="task-item">
                                <div class="task-id">${t.id}</div>
                                <div class="task-meta">
                                    <div>By: ${t.claimed_by || '?'}</div>
                                    <div>Age: ${t._age}</div>
                                </div>
                            </div>
                        `).join('') || '<p style="color: #8b949e; font-size: 11px;">No claimed tasks</p>'}
                    </div>
                    <div class="task-column completed">
                        <h3>✓ Completed (${tasks.completed.count})</h3>
                        ${tasks.completed.tasks.slice(0, 5).map(t => `
                            <div class="task-item">
                                <div class="task-id">${t.id}</div>
                                <div class="task-meta">
                                    <div>Age: ${t._age}</div>
                                </div>
                            </div>
                        `).join('') || '<p style="color: #8b949e; font-size: 11px;">No completed tasks</p>'}
                    </div>
                `;
                document.getElementById('tasks-container').innerHTML = tasksHtml;

                // Update dispatches
                const dispatches = await fetch('/api/dispatches').then(r => r.json());
                const dispatchesHtml = `
                    <table class="dispatch-table">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Host</th>
                                <th>Model</th>
                                <th>Status</th>
                                <th>Duration</th>
                                <th>Started</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${dispatches.dispatches.slice(0, 10).map(d => {
                                const statusClass = d.status === 'running' ? 'status-running' : d.status === 'completed' ? 'status-completed' : 'status-failed';
                                const icon = d.status === 'running' ? '<span class="spinner"></span>' : d._icon;
                                return `
                                    <tr>
                                        <td><code>${d.dispatch_id.substring(0, 20)}...</code></td>
                                        <td>${d.host}</td>
                                        <td>${d.model}</td>
                                        <td class="${statusClass}">${icon} ${d.status}</td>
                                        <td>${d._duration || '—'}</td>
                                        <td>${d._started_ago}</td>
                                    </tr>
                                `;
                            }).join('') || '<tr><td colspan="6" style="text-align: center; color: #8b949e;">No dispatches</td></tr>'}
                        </tbody>
                    </table>
                `;
                document.getElementById('dispatches-container').innerHTML = dispatchesHtml;

                // Update health events
                const health = await fetch('/api/health').then(r => r.json());
                const healthHtml = health.events.slice(0, 15).map(evt => {
                    const severityClass = evt.severity || 'medium';
                    return `
                        <div class="health-event ${severityClass}">
                            <div class="health-rule">${evt._icon} ${evt.rule_name}</div>
                            <div style="color: #8b949e; font-size: 11px; margin-top: 2px;">
                                <strong>${evt.host || '?'}</strong> • ${evt._age}
                            </div>
                            <div class="health-desc">${evt.description || '—'}</div>
                            ${evt.action_taken ? `<div class="health-desc">Action: ${evt.action_taken}</div>` : ''}
                        </div>
                    `;
                }).join('') || '<p style="color: #8b949e;">No recent health events</p>';
                document.getElementById('health-container').innerHTML = healthHtml;

                // Update GPU status
                const gpu = await fetch('/api/gpu').then(r => r.json());
                if (gpu.error) {
                    document.getElementById('gpu-container').innerHTML = `<p style="color: #8b949e;">${gpu.error}</p>`;
                    document.getElementById('metric-gpus').textContent = '?';
                } else {
                    const gpus = gpu.inventory || [];
                    document.getElementById('metric-gpus').textContent = gpu.total_gpus || gpus.length;
                    const gpuHtml = gpus.map(g => {
                        const total = g.vram_total_mb || 1;
                        const free = g.vram_free_mb || 0;
                        const used = total - free;
                        const usedPct = Math.round(used / total * 100);
                        const barColor = usedPct > 80 ? '#f85149' : usedPct > 50 ? '#d29922' : '#3fb950';
                        return `
                            <div class="node-card">
                                <div class="node-header">
                                    <div class="node-hostname">${g.host || '?'}:${g.gpu_index || 0}</div>
                                    <div class="node-state state-active" style="font-size:10px;">${g.model || 'GPU'}</div>
                                </div>
                                <div style="background:#30363d;border-radius:3px;height:8px;margin:6px 0;">
                                    <div style="background:${barColor};height:8px;border-radius:3px;width:${usedPct}%;"></div>
                                </div>
                                <div class="node-details">
                                    <div><span class="label">VRAM:</span> ${(used/1024).toFixed(1)}/${(total/1024).toFixed(1)} GB (${usedPct}%)</div>
                                    <div><span class="label">Free:</span> ${(free/1024).toFixed(1)} GB</div>
                                </div>
                            </div>
                        `;
                    }).join('') || '<p style="color: #8b949e;">No GPU data available</p>';
                    document.getElementById('gpu-container').innerHTML = gpuHtml;
                }

                // Update warm models
                const warm = await fetch('/api/warm_models').then(r => r.json());
                document.getElementById('metric-warm-models').textContent = warm.total_models || 0;
                if (warm.error) {
                    document.getElementById('warm-models-container').innerHTML = `<p style="color: #8b949e;">${warm.error}</p>`;
                } else {
                    const hosts = warm.hosts || {};
                    const warmHtml = Object.entries(hosts).map(([host, models]) => `
                        <div class="node-card">
                            <div class="node-header">
                                <div class="node-dot green">●</div>
                                <div class="node-hostname">${host}</div>
                                <div class="node-state state-active">${models.length} model${models.length !== 1 ? 's' : ''}</div>
                            </div>
                            <div class="node-details">
                                ${models.map(m => {
                                    const name = typeof m === 'string' ? m : (m.name || m.model || '?');
                                    const vram = m.vram_mb ? ` (${(m.vram_mb/1024).toFixed(1)}GB)` : '';
                                    return `<div>• ${name}${vram}</div>`;
                                }).join('')}
                            </div>
                        </div>
                    `).join('') || '<p style="color: #8b949e;">No warm models detected</p>';
                    document.getElementById('warm-models-container').innerHTML = warmHtml;
                }

                // Update IPC events
                const ipc = await fetch('/api/ipc_events').then(r => r.json());
                if (!ipc.available) {
                    document.getElementById('ipc-container').innerHTML = '<p style="color: #8b949e;">Redis IPC not available</p>';
                } else {
                    const ipcHtml = ipc.events.slice(0, 20).map(evt => {
                        const ts = evt.timestamp ? new Date(evt.timestamp * 1000).toLocaleTimeString() : '?';
                        const chColor = {'heartbeat':'#3fb950','task':'#58a6ff','dispatch':'#d29922','alert':'#f85149'}[evt.channel] || '#8b949e';
                        return `
                            <div class="health-event" style="border-left-color: ${chColor};">
                                <div class="health-rule">${evt.channel}:${evt.type}</div>
                                <div style="color: #8b949e; font-size: 11px; margin-top: 2px;">
                                    <strong>${evt.sender || '?'}</strong> • ${ts}
                                </div>
                                <div class="health-desc">${JSON.stringify(evt.data).substring(0, 120)}</div>
                            </div>
                        `;
                    }).join('') || '<p style="color: #8b949e;">No recent IPC events</p>';
                    document.getElementById('ipc-container').innerHTML = ipcHtml;
                }

                // Update timestamp
                document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
            } catch (error) {
                console.error('Dashboard update failed:', error);
                const msg = `<div class="error">Failed to load dashboard: ${error.message}</div>`;
                document.getElementById('fleet-status-container').innerHTML = msg;
            }
        }

        // Initial load
        updateDashboard();

        // Auto-refresh every 10 seconds
        setInterval(updateDashboard, 10000);
    </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def run_dashboard(host: str = HOST, port: int = PORT) -> None:
    """Start the web dashboard."""
    log.info(f"Starting claude-swarm dashboard on {host}:{port}")
    log.info(f"Open http://{host}:{port}/ in your browser")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="claude-swarm web dashboard")
    parser.add_argument("--host", default=HOST, help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=PORT, help="Port to bind to (default: 9192)")

    args = parser.parse_args()
    run_dashboard(host=args.host, port=args.port)
