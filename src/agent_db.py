"""Persistent Agent State — SQLite database for agent presence and task history.

Database at /opt/claude-swarm/data/agents.db with two tables:
- agents: Current agent state (hostname, state, task, capabilities, etc.)
- task_history: History of all task actions (claimed, completed, failed, preempted)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from util import now_iso as _now_iso

logger = logging.getLogger(__name__)

DB_PATH = Path("/opt/claude-swarm/data/agents.db")


def _init_db() -> None:
    """Initialize database schema if not exists."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Agents table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            hostname TEXT PRIMARY KEY,
            ip TEXT,
            pid INTEGER,
            state TEXT NOT NULL DEFAULT 'idle',
            current_task TEXT DEFAULT '',
            project TEXT DEFAULT '',
            model TEXT DEFAULT '',
            session_id TEXT DEFAULT '',
            capabilities TEXT DEFAULT '{}',
            last_heartbeat TEXT,
            registered_at TEXT,
            total_tasks_completed INTEGER DEFAULT 0,
            total_session_minutes REAL DEFAULT 0.0
        )
    """)

    # Task history table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            hostname TEXT NOT NULL,
            action TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            details TEXT DEFAULT ''
        )
    """)

    # Index for efficient queries
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_history_task_id
        ON task_history(task_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_history_hostname
        ON task_history(hostname)
    """)

    conn.commit()
    conn.close()


@dataclass
class AgentStats:
    """Statistics for an agent."""

    hostname: str
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    preempted_tasks: int
    completion_rate: float
    avg_session_minutes: float
    last_heartbeat: str


class AgentDB:
    """SQLite database interface for agent state and history."""

    def __init__(self):
        _init_db()

    def upsert_agent(
        self,
        hostname: str,
        ip: str = "",
        pid: int = 0,
        state: str = "idle",
        current_task: str = "",
        project: str = "",
        model: str = "",
        session_id: str = "",
        capabilities: Optional[dict] = None,
    ) -> None:
        """Insert or update an agent record.

        Args:
            hostname: Agent hostname
            ip: Agent IP address
            pid: Process ID
            state: Agent state (idle, working, blocked, etc.)
            current_task: Current task ID being worked on
            project: Project directory
            model: Claude model
            session_id: Session ID
            capabilities: Capabilities dict (json-serialized)
        """
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        caps_json = json.dumps(capabilities or {})
        now = _now_iso()

        cursor.execute(
            """
            INSERT INTO agents
            (hostname, ip, pid, state, current_task, project, model, session_id,
             capabilities, last_heartbeat, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hostname) DO UPDATE SET
                ip=excluded.ip,
                pid=excluded.pid,
                state=excluded.state,
                current_task=excluded.current_task,
                project=excluded.project,
                model=excluded.model,
                session_id=excluded.session_id,
                capabilities=excluded.capabilities,
                last_heartbeat=excluded.last_heartbeat
        """,
            (
                hostname,
                ip,
                pid,
                state,
                current_task,
                project,
                model,
                session_id,
                caps_json,
                now,
                now,
            ),
        )

        conn.commit()
        conn.close()

    def get_agent(self, hostname: str) -> dict | None:
        """Get agent by hostname.

        Args:
            hostname: Agent hostname

        Returns:
            Agent dict or None if not found
        """
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM agents WHERE hostname = ?", (hostname,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        agent_dict = dict(row)
        # Parse JSON fields
        agent_dict["capabilities"] = json.loads(agent_dict.get("capabilities", "{}"))
        return agent_dict

    def list_agents(self) -> list[dict]:
        """List all agents.

        Returns:
            List of agent dictionaries
        """
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM agents ORDER BY hostname")
        rows = cursor.fetchall()
        conn.close()

        agents = []
        for row in rows:
            agent_dict = dict(row)
            agent_dict["capabilities"] = json.loads(
                agent_dict.get("capabilities", "{}")
            )
            agents.append(agent_dict)
        return agents

    def record_task_action(
        self,
        task_id: str,
        hostname: str,
        action: str,
        details: Optional[dict] = None,
    ) -> None:
        """Record a task action in history.

        Args:
            task_id: Task ID
            hostname: Agent hostname
            action: Action (claimed, completed, failed, preempted, requeued)
            details: Optional details dict (json-serialized)
        """
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        details_json = json.dumps(details or {})
        now = _now_iso()

        cursor.execute(
            """
            INSERT INTO task_history (task_id, hostname, action, timestamp, details)
            VALUES (?, ?, ?, ?, ?)
        """,
            (task_id, hostname, action, now, details_json),
        )

        conn.commit()
        conn.close()

    def get_agent_stats(self, hostname: str) -> AgentStats | None:
        """Get statistics for an agent.

        Args:
            hostname: Agent hostname

        Returns:
            AgentStats object or None if agent not found
        """
        agent = self.get_agent(hostname)
        if not agent:
            return None

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Count task actions
        cursor.execute(
            "SELECT action, COUNT(*) FROM task_history WHERE hostname = ? GROUP BY action",
            (hostname,),
        )
        action_counts = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()

        completed = action_counts.get("completed", 0)
        failed = action_counts.get("failed", 0)
        preempted = action_counts.get("preempted", 0)
        total = sum(action_counts.values())

        completion_rate = completed / total if total > 0 else 0.0

        return AgentStats(
            hostname=hostname,
            total_tasks=total,
            completed_tasks=completed,
            failed_tasks=failed,
            preempted_tasks=preempted,
            completion_rate=completion_rate,
            avg_session_minutes=agent.get("total_session_minutes", 0.0),
            last_heartbeat=agent.get("last_heartbeat", ""),
        )

    def get_fleet_stats(self) -> dict[str, Any]:
        """Get aggregate statistics across all agents.

        Returns:
            Dictionary with fleet-wide statistics
        """
        agents = self.list_agents()
        if not agents:
            return {
                "total_agents": 0,
                "active_agents": 0,
                "idle_agents": 0,
                "total_tasks": 0,
                "completion_rate": 0.0,
            }

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Count task actions
        cursor.execute("""
            SELECT action, COUNT(*) FROM task_history GROUP BY action
        """)
        action_counts = {row[0]: row[1] for row in cursor.fetchall()}

        # Count agent states
        cursor.execute("""
            SELECT state, COUNT(*) FROM agents GROUP BY state
        """)
        state_counts = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()

        completed = action_counts.get("completed", 0)
        total = sum(action_counts.values())
        completion_rate = completed / total if total > 0 else 0.0

        return {
            "total_agents": len(agents),
            "active_agents": state_counts.get("working", 0)
            + state_counts.get("blocked", 0),
            "idle_agents": state_counts.get("idle", 0),
            "total_tasks": total,
            "completed_tasks": completed,
            "failed_tasks": action_counts.get("failed", 0),
            "preempted_tasks": action_counts.get("preempted", 0),
            "completion_rate": completion_rate,
        }

    def task_history(self, task_id: str) -> list[dict]:
        """Get history of all actions for a task.

        Args:
            task_id: Task ID

        Returns:
            List of action dictionaries in chronological order
        """
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT * FROM task_history
            WHERE task_id = ?
            ORDER BY timestamp ASC
        """,
            (task_id,),
        )

        rows = cursor.fetchall()
        conn.close()

        history = []
        for row in rows:
            action_dict = dict(row)
            action_dict["details"] = json.loads(action_dict.get("details", "{}"))
            history.append(action_dict)
        return history

    def delete_agent(self, hostname: str) -> None:
        """Delete an agent record.

        Args:
            hostname: Agent hostname
        """
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM agents WHERE hostname = ?", (hostname,))
        conn.commit()
        conn.close()

    def cleanup_old_records(self, days: int = 30) -> int:
        """Clean up task history older than specified days.

        Args:
            days: Days of history to keep

        Returns:
            Number of records deleted
        """
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Calculate cutoff date
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        cursor.execute("DELETE FROM task_history WHERE timestamp < ?", (cutoff,))
        deleted = cursor.rowcount

        conn.commit()
        conn.close()

        return deleted
