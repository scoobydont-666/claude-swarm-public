"""SQLite task backend — atomic task claiming with BEGIN IMMEDIATE.

NAI Swarm backport: replaces YAML file-based task storage with SQLite
for atomic claiming (no double-booking) and faster queries.

Opt-in via config: set task_backend: sqlite in swarm.yaml.

Usage:
    from sqlite_backend import SQLiteTaskBackend

    backend = SQLiteTaskBackend("/var/lib/swarm/tasks/tasks.db")
    task = backend.create("Fix auth bug", priority=1)
    claimed = backend.claim("agent-1")
    backend.complete(claimed.id, result="Fixed in auth.py:42")
"""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

LOG = logging.getLogger(__name__)

# Schema version for migrations
SCHEMA_VERSION = 1

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    project TEXT DEFAULT '',
    priority INTEGER DEFAULT 3,
    requires TEXT DEFAULT '[]',
    state TEXT DEFAULT 'pending',
    created_by TEXT DEFAULT '',
    created_at REAL DEFAULT 0,
    claimed_by TEXT DEFAULT '',
    claimed_at REAL DEFAULT 0,
    completed_at REAL DEFAULT 0,
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    estimated_minutes INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_state_priority ON tasks(state, priority, created_at);
"""


class SQLiteTaskBackend:
    """SQLite-backed task queue with atomic claiming via BEGIN IMMEDIATE."""

    def __init__(self, db_path: str = "/var/lib/swarm/tasks/tasks.db") -> None:
        """Initialize SQLite backend.

        Args:
            db_path: Path to SQLite database file.
        """
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new connection with WAL mode and busy timeout."""
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            conn.executescript(CREATE_TABLE_SQL)
            conn.commit()
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Create
    # -----------------------------------------------------------------------

    def create(
        self,
        title: str,
        description: str = "",
        project: str = "",
        priority: int = 3,
        requires: list[str] | None = None,
        estimated_minutes: int = 0,
        created_by: str = "",
    ) -> dict:
        """Create a new pending task.

        Returns:
            Task dict with all fields.
        """
        task_id = f"task-{uuid4().hex[:12]}"
        requires_json = json.dumps(requires or [])
        created_at = time.time()
        created_by = created_by or os.uname().nodename

        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO tasks (id, title, description, project, priority,
                   requires, state, created_by, created_at, estimated_minutes)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (task_id, title, description, project, priority,
                 requires_json, created_by, created_at, estimated_minutes),
            )
            conn.commit()
            LOG.info("SQLite: task created %s (P%d)", task_id, priority)
            return self.get(task_id)  # type: ignore[return-value]
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Claim — atomic via BEGIN IMMEDIATE
    # -----------------------------------------------------------------------

    def claim(self, claimer: str, task_id: str | None = None) -> dict | None:
        """Claim a specific task or the highest-priority pending task.

        Uses BEGIN IMMEDIATE for write-lock atomicity — no double-booking.

        Args:
            claimer: Agent identifier.
            task_id: Specific task ID to claim, or None for next available.

        Returns:
            Claimed task dict, or None if nothing available.
        """
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if task_id:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE id = ? AND state = 'pending'",
                    (task_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT id FROM tasks WHERE state = 'pending'
                       ORDER BY priority ASC, created_at ASC LIMIT 1""",
                ).fetchone()

            if not row:
                conn.rollback()
                return None

            now = time.time()
            conn.execute(
                """UPDATE tasks SET state = 'claimed', claimed_by = ?,
                   claimed_at = ? WHERE id = ?""",
                (claimer, now, row["id"]),
            )
            conn.commit()
            LOG.info("SQLite: task claimed %s by %s", row["id"], claimer)
            return self.get(row["id"])
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_matching(
        self, capabilities: list[str], claimer: str
    ) -> dict | None:
        """Claim highest-priority task matching capabilities.

        Args:
            capabilities: List of capability names the claimer has.
            claimer: Agent identifier.

        Returns:
            Claimed task dict, or None.
        """
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT id, requires FROM tasks WHERE state = 'pending'
                   ORDER BY priority ASC, created_at ASC""",
            ).fetchall()

            cap_set = set(capabilities)
            for row in rows:
                requires = json.loads(row["requires"]) if row["requires"] else []
                if not requires or all(r in cap_set for r in requires):
                    now = time.time()
                    conn.execute(
                        """UPDATE tasks SET state = 'claimed', claimed_by = ?,
                           claimed_at = ? WHERE id = ?""",
                        (claimer, now, row["id"]),
                    )
                    conn.commit()
                    return self.get(row["id"])

            conn.rollback()
            return None
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # State transitions
    # -----------------------------------------------------------------------

    def complete(self, task_id: str, result: str = "") -> dict | None:
        """Mark task as completed."""
        return self._transition(task_id, "completed", ("claimed", "running"),
                                result=result, completed_at=time.time())

    def fail(self, task_id: str, error: str = "") -> dict | None:
        """Mark task as failed."""
        return self._transition(task_id, "failed", ("claimed", "running"),
                                error=error, completed_at=time.time())

    def start(self, task_id: str) -> dict | None:
        """Transition claimed → running."""
        return self._transition(task_id, "running", ("claimed",))

    def requeue(self, task_id: str) -> dict | None:
        """Return task to pending."""
        conn = self._get_conn()
        try:
            conn.execute(
                """UPDATE tasks SET state = 'pending', claimed_by = '',
                   claimed_at = 0, error = '' WHERE id = ? AND state != 'completed'""",
                (task_id,),
            )
            conn.commit()
            return self.get(task_id)
        finally:
            conn.close()

    def _transition(self, task_id: str, new_state: str,
                    valid_from: tuple[str, ...], **kwargs: object) -> dict | None:
        """Generic state transition with validation."""
        values: list = [new_state]
        set_clause = "state = ?"
        if kwargs:
            extra = ", ".join(f"{k} = ?" for k in kwargs)
            set_clause += f", {extra}"
            values.extend(kwargs.values())
        from_clause = " OR ".join(f"state = '{s}'" for s in valid_from)
        values.append(task_id)

        conn = self._get_conn()
        try:
            cursor = conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ? AND ({from_clause})",
                values,
            )
            conn.commit()
            if cursor.rowcount == 0:
                return None
            return self.get(task_id)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Query
    # -----------------------------------------------------------------------

    def get(self, task_id: str) -> dict | None:
        """Get task by ID."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            return self._row_to_dict(row)
        finally:
            conn.close()

    def list_by_state(self, state: str, limit: int = 50) -> list[dict]:
        """List tasks in a given state, sorted by priority then created_at."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE state = ? ORDER BY priority ASC, created_at ASC LIMIT ?",
                (state, limit),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def list_pending(self, limit: int = 50) -> list[dict]:
        return self.list_by_state("pending", limit)

    def list_claimed(self, limit: int = 50) -> list[dict]:
        return self.list_by_state("claimed", limit)

    def list_all(self, limit: int = 100) -> list[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY priority ASC, created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def requeue_stale(self, ttl: int = 600) -> list[str]:
        """Requeue tasks claimed longer than TTL seconds."""
        cutoff = time.time() - ttl
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE state = 'claimed' AND claimed_at > 0 AND claimed_at < ?",
                (cutoff,),
            ).fetchall()
            requeued = []
            for row in rows:
                conn.execute(
                    "UPDATE tasks SET state = 'pending', claimed_by = '', claimed_at = 0 WHERE id = ?",
                    (row["id"],),
                )
                requeued.append(row["id"])
                LOG.warning("SQLite: auto-requeued stale task %s", row["id"])
            conn.commit()
            return requeued
        finally:
            conn.close()

    def count_by_state(self) -> dict[str, int]:
        """Get task counts by state."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT state, COUNT(*) as cnt FROM tasks GROUP BY state"
            ).fetchall()
            return {row["state"]: row["cnt"] for row in rows}
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        """Convert sqlite3.Row to a plain dict, parsing JSON fields."""
        d = dict(row)
        if "requires" in d and isinstance(d["requires"], str):
            try:
                d["requires"] = json.loads(d["requires"])
            except (json.JSONDecodeError, TypeError):
                d["requires"] = []
        return d
