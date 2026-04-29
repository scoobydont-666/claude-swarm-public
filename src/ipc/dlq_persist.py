"""SQLite write-through persistence for the IPC dead-letter queue.

Redis streams hold the live DLQ (XADD with MAXLEN=5000). This module mirrors
each DLQ event into a local SQLite file so that:

1. A Redis restart without AOF/RDB doesn't silently drop DLQ history — the
   `_warn_if_no_persistence()` check in `dlq.py` warns, but operators need
   an actual durable record, not just a log line.
2. The false-positive rate (requeued ÷ (requeued + expired)) is computable
   from a rolling window without loading the live stream.
3. Long-horizon triage (days/weeks after an incident) has a queryable record.

Design notes
------------
- Best-effort. If SQLite I/O fails for any reason, we log + swallow; Redis
  remains the source of truth for live operations.
- Write-through. `persist_entry` is called alongside `XADD` in `sweep_pending`
  and any other producer; `mark_resolved` is called by `requeue` / `purge` /
  `prune_old_messages`.
- Low volume. Write rate is bounded by DLQ rate (measured: single-digit/hour
  in steady state). SQLite + WAL handles this comfortably.
- One row per stream_id. We rely on the upstream Redis stream ID as the
  primary key; duplicate XADD calls would be de-duplicated here too.

Why SQLite and not PostgreSQL
-----------------------------
See <hydra-project-path>/docs/db-choice-matrix.md. Short version: this workload
is single-writer, low-volume, and locally interesting — exactly the CB shape.
Adding a PG dependency for the DLQ would gate claude-swarm on a service that
isn't needed for its hot path.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DLQ_PERSIST_DB", "/opt/claude-swarm/data/dlq.db"))

# Resolution values. Anything outside this set is rejected at write time to
# keep queries stable.
RESOLUTION_REQUEUED = "requeued"
RESOLUTION_PURGED = "purged"
RESOLUTION_EXPIRED = "expired"
_VALID_RESOLUTIONS = {RESOLUTION_REQUEUED, RESOLUTION_PURGED, RESOLUTION_EXPIRED}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dlq_entries (
    stream_id        TEXT PRIMARY KEY,
    reason           TEXT NOT NULL,
    envelope_json    TEXT NOT NULL,
    inserted_at_ms   INTEGER NOT NULL,
    resolution       TEXT,
    resolved_at_ms   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_dlq_inserted_at ON dlq_entries (inserted_at_ms);
CREATE INDEX IF NOT EXISTS idx_dlq_resolved_at ON dlq_entries (resolved_at_ms)
    WHERE resolved_at_ms IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dlq_resolution ON dlq_entries (resolution)
    WHERE resolution IS NOT NULL;
"""

_init_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    """Open a SQLite connection with WAL and the expected row-factory.

    Caller is responsible for closing. Parent directory is created on demand.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        try:
            with _connect() as conn:
                conn.executescript(_SCHEMA)
            _initialized = True
        except sqlite3.Error as exc:  # pragma: no cover — init failure is op-visible
            log.warning("dlq-persist: schema init failed: %s", exc)


def reset_for_test() -> None:
    """Test-only: drop memoization so a fresh DB is re-initialized."""
    global _initialized
    _initialized = False


def persist_entry(stream_id: str, reason: str, envelope_fields: dict[str, Any]) -> None:
    """Mirror a DLQ XADD into SQLite.

    Silently no-ops on any SQLite error — Redis remains the source of truth.
    """
    _ensure_schema()
    try:
        envelope_json = json.dumps(envelope_fields, default=str)
        now_ms = int(time.time() * 1000)
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO dlq_entries "
                "(stream_id, reason, envelope_json, inserted_at_ms) "
                "VALUES (?, ?, ?, ?)",
                (stream_id, reason, envelope_json, now_ms),
            )
    except sqlite3.Error as exc:
        log.debug("dlq-persist: insert failed for %s: %s", stream_id, exc)


def mark_resolved(stream_id: str, resolution: str) -> None:
    """Mark a persisted DLQ entry as resolved (requeued / purged / expired)."""
    if resolution not in _VALID_RESOLUTIONS:
        raise ValueError(f"invalid resolution {resolution!r}; expected one of {_VALID_RESOLUTIONS}")
    _ensure_schema()
    try:
        now_ms = int(time.time() * 1000)
        with _connect() as conn:
            conn.execute(
                "UPDATE dlq_entries SET resolution=?, resolved_at_ms=? WHERE stream_id=? AND resolution IS NULL",
                (resolution, now_ms, stream_id),
            )
    except sqlite3.Error as exc:
        log.debug("dlq-persist: resolve failed for %s: %s", stream_id, exc)


def mark_resolved_bulk(stream_ids: list[str], resolution: str) -> int:
    """Mark many entries resolved in one round trip. Returns row count."""
    if not stream_ids:
        return 0
    if resolution not in _VALID_RESOLUTIONS:
        raise ValueError(f"invalid resolution {resolution!r}")
    _ensure_schema()
    try:
        now_ms = int(time.time() * 1000)
        placeholders = ",".join("?" for _ in stream_ids)
        with _connect() as conn:
            cur = conn.execute(
                f"UPDATE dlq_entries SET resolution=?, resolved_at_ms=? "
                f"WHERE stream_id IN ({placeholders}) AND resolution IS NULL",
                (resolution, now_ms, *stream_ids),
            )
            return cur.rowcount or 0
    except sqlite3.Error as exc:
        log.debug("dlq-persist: bulk resolve failed: %s", exc)
        return 0


def false_positive_rate(window_seconds: int = 3600) -> tuple[float, int, int]:
    """Return (fp_rate, requeued_count, resolved_total) over a rolling window.

    False-positive rate = requeued / (requeued + expired). Purged entries are
    excluded because purge is operator-initiated cleanup, not a signal of
    DLQ-er quality.

    Returns (0.0, 0, 0) if there are no resolved entries in the window or if
    the DB is unavailable — callers can treat that as "no signal yet".
    """
    _ensure_schema()
    try:
        cutoff_ms = int((time.time() - window_seconds) * 1000)
        with _connect() as conn:
            row = conn.execute(
                "SELECT "
                "  SUM(CASE WHEN resolution=? THEN 1 ELSE 0 END) AS requeued, "
                "  SUM(CASE WHEN resolution=? THEN 1 ELSE 0 END) AS expired "
                "FROM dlq_entries "
                "WHERE resolved_at_ms >= ?",
                (RESOLUTION_REQUEUED, RESOLUTION_EXPIRED, cutoff_ms),
            ).fetchone()
        requeued = int(row["requeued"] or 0)
        expired = int(row["expired"] or 0)
        total = requeued + expired
        if total == 0:
            return (0.0, 0, 0)
        return (requeued / total, requeued, total)
    except sqlite3.Error as exc:
        log.debug("dlq-persist: fp_rate query failed: %s", exc)
        return (0.0, 0, 0)


def depth_persisted() -> int:
    """Number of unresolved (still-in-DLQ) entries per the persisted store."""
    _ensure_schema()
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM dlq_entries WHERE resolution IS NULL"
            ).fetchone()
            return int(row["c"])
    except sqlite3.Error as exc:
        log.debug("dlq-persist: depth query failed: %s", exc)
        return 0
