"""Persistent SQLite event log for health monitor events and remediations."""

import fcntl
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DB_PATH = Path("/opt/claude-swarm/data/health-events.db")

# DDL — executed once on first connect
_SCHEMA = """
CREATE TABLE IF NOT EXISTS health_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    rule_name       TEXT    NOT NULL,
    host            TEXT    NOT NULL DEFAULT '',
    severity        TEXT    NOT NULL DEFAULT 'medium',
    description     TEXT    NOT NULL DEFAULT '',
    action_taken    TEXT    NOT NULL DEFAULT '',
    action_result   TEXT    NOT NULL DEFAULT '',
    escalated_to    TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_he_timestamp  ON health_events (timestamp);
CREATE INDEX IF NOT EXISTS idx_he_rule       ON health_events (rule_name);
CREATE INDEX IF NOT EXISTS idx_he_host       ON health_events (host);
"""


from util import now_iso as _now_iso


def _get_conn() -> sqlite3.Connection:
    """Open (and initialise if needed) the SQLite database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


class EventLog:
    """Persistent log of all health events, remediations, and alerts.

    All writes use a file-level flock so concurrent processes don't corrupt
    the WAL.  Reads are lock-free (SQLite handles concurrent readers).
    """

    # Path to the advisory lock file used for exclusive writes
    _LOCK_PATH = DB_PATH.parent / "health-events.lock"

    def _write_lock(self):
        """Return a context manager that holds an exclusive flock."""
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            self._LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._LOCK_PATH.touch(exist_ok=True)
            with open(self._LOCK_PATH, "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)

        return _ctx()

    def record(
        self,
        rule_name: str,
        host: str = "",
        severity: str = "medium",
        description: str = "",
        action_taken: str = "",
        action_result: str = "",
        escalated_to: str = "",
        timestamp: str | None = None,
    ) -> int:
        """Insert one event row. Returns the new row id."""
        ts = timestamp or _now_iso()
        with self._write_lock():
            conn = _get_conn()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO health_events
                        (timestamp, rule_name, host, severity,
                         description, action_taken, action_result, escalated_to)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        rule_name,
                        host,
                        severity,
                        description,
                        action_taken,
                        action_result,
                        escalated_to,
                    ),
                )
                conn.commit()
                return cur.lastrowid or 0
            finally:
                conn.close()

    def query(
        self,
        rule_name: str | None = None,
        host: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query events with optional filters.

        Args:
            rule_name: Filter by exact rule name.
            host:      Filter by host.
            since:     ISO timestamp lower bound (inclusive).
            until:     ISO timestamp upper bound (inclusive).
            limit:     Maximum rows returned.

        Returns:
            List of dicts with all column names as keys.
        """
        clauses: list[str] = []
        params: list = []

        if rule_name:
            clauses.append("rule_name = ?")
            params.append(rule_name)
        if host:
            clauses.append("host = ?")
            params.append(host)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        if until:
            clauses.append("timestamp <= ?")
            params.append(until)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT * FROM health_events
            {where}
            ORDER BY timestamp DESC
            LIMIT ?
        """
        params.append(limit)

        conn = _get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def last_action_time(self, rule_name: str, host: str = "") -> str | None:
        """Return the timestamp of the most recent action for rule+host, or None."""
        conn = _get_conn()
        try:
            if host:
                row = conn.execute(
                    """
                    SELECT timestamp FROM health_events
                    WHERE rule_name = ? AND host = ? AND action_taken != ''
                    ORDER BY timestamp DESC LIMIT 1
                    """,
                    (rule_name, host),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT timestamp FROM health_events
                    WHERE rule_name = ? AND action_taken != ''
                    ORDER BY timestamp DESC LIMIT 1
                    """,
                    (rule_name,),
                ).fetchone()
            return row["timestamp"] if row else None
        finally:
            conn.close()

    def recent_events(self, limit: int = 50) -> list[dict]:
        """Return the most recent events, newest first."""
        return self.query(limit=limit)

    def prune(self, days: int = 30) -> int:
        """Delete events older than N days. Returns count of deleted rows."""
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._write_lock():
            conn = _get_conn()
            try:
                cur = conn.execute("DELETE FROM health_events WHERE timestamp < ?", (cutoff,))
                deleted = cur.rowcount
                conn.commit()
                if deleted > 0:
                    conn.execute("VACUUM")
                return deleted
            finally:
                conn.close()

    def count(self) -> int:
        """Return total number of events in the log."""
        conn = _get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) as cnt FROM health_events").fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def rule_summary(self) -> list[dict]:
        """Return per-rule last-trigger summary for the `health --rules` display."""
        conn = _get_conn()
        try:
            rows = conn.execute(
                """
                SELECT rule_name,
                       COUNT(*) AS total,
                       MAX(timestamp) AS last_seen,
                       SUM(CASE WHEN action_taken != '' THEN 1 ELSE 0 END) AS actions_taken
                FROM health_events
                GROUP BY rule_name
                ORDER BY last_seen DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
