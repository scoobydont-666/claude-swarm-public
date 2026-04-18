"""
Cost Tracker — Per-task cost tracking via Hydra Pulse integration.

Correlates SWARM_TASK_ID with Claude Code session costs tracked by
hydra-pulse-hook. Supports budget caps and cost aggregation.
"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Hydra Pulse database path (default on all fleet hosts)
PULSE_DB_PATHS = [
    Path.home() / ".local/share/hydra-pulse/pulse.db",
    Path("/opt/hydra-pulse/pulse.db"),
]

HYDRA_PULSE_BIN = "hydra-pulse"


@dataclass
class TaskCost:
    """Cost data for a single task."""

    task_id: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    session_count: int = 0
    model: str = ""
    duration_seconds: float = 0.0
    retrieved_at: float = 0.0


def get_task_cost(task_id: str) -> TaskCost | None:
    """Query Hydra Pulse for cost data associated with a SWARM_TASK_ID.

    Tries the CLI first, falls back to direct SQLite query.
    """
    # Method 1: CLI (preferred — doesn't depend on DB path)
    try:
        result = subprocess.run(
            [HYDRA_PULSE_BIN, "task-costs", "--task-id", task_id, "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return TaskCost(
                task_id=task_id,
                total_input_tokens=data.get("input_tokens", 0),
                total_output_tokens=data.get("output_tokens", 0),
                total_cost_usd=data.get("cost_usd", 0.0),
                session_count=data.get("sessions", 0),
                model=data.get("model", ""),
                duration_seconds=data.get("duration_seconds", 0.0),
                retrieved_at=time.time(),
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass

    # Method 2: Direct SQLite query (fallback)
    try:
        import sqlite3

        for db_path in PULSE_DB_PATHS:
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                try:
                    row = conn.execute(
                        """
                        SELECT
                            COALESCE(SUM(input_tokens), 0),
                            COALESCE(SUM(output_tokens), 0),
                            COALESCE(SUM(cost_usd), 0.0),
                            COUNT(*),
                            MAX(model),
                            COALESCE(SUM(duration_seconds), 0.0)
                        FROM sessions
                        WHERE task_id = ?
                    """,
                        (task_id,),
                    ).fetchone()

                    if row and row[0] > 0:
                        return TaskCost(
                            task_id=task_id,
                            total_input_tokens=row[0],
                            total_output_tokens=row[1],
                            total_cost_usd=row[2],
                            session_count=row[3],
                            model=row[4] or "",
                            duration_seconds=row[5],
                            retrieved_at=time.time(),
                        )
                finally:
                    conn.close()
    except Exception as e:
        logger.debug(f"Direct DB query failed: {e}")

    return None


def check_budget(task_id: str, budget_usd: float) -> tuple[bool, float]:
    """Check if a task has exceeded its budget.

    Args:
        task_id: SWARM_TASK_ID
        budget_usd: Maximum allowed cost in USD (0 = unlimited)

    Returns:
        (within_budget: bool, actual_cost: float)
    """
    if budget_usd <= 0:
        return True, 0.0

    cost = get_task_cost(task_id)
    if cost is None:
        return True, 0.0  # No data yet = within budget

    return cost.total_cost_usd <= budget_usd, cost.total_cost_usd


def get_daily_costs() -> dict:
    """Get aggregated costs for today across all tasks."""
    try:
        result = subprocess.run(
            [HYDRA_PULSE_BIN, "stats", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def format_cost(cost_usd: float) -> str:
    """Format cost for display."""
    if cost_usd < 0.01:
        return f"${cost_usd:.4f}"
    elif cost_usd < 1.0:
        return f"${cost_usd:.2f}"
    else:
        return f"${cost_usd:.2f}"
