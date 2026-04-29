#!/usr/bin/env python3
"""State-store cascade wrapper — routing protocol §6.

Cascade order: Redis (hot) → Context Bridge (fact-assert) → SQLite (durable) → degraded fail-closed.

Every Redis write is async-mirrored to SQLite (write-behind, fire-and-forget) so SQLite
has a durable resume snapshot even while Redis is healthy.

Key conventions:
    Redis:   routing:slot:<slot_key>        (string)
             routing:task:<task_id>         (hash)
             routing:tasks:by_state:<state> (set)
    CB:      namespace=routing-protocol, subject=slot_key | task:<task_id>
    SQLite:  slots(key, holder, ttl_expires)
             tasks(task_id, state, data, updated_at)
             tasks_by_state(state, task_id)

Env:
    SWARM_CASCADE_DB  — SQLite path (default /opt/swarm/artifacts/cascade.db)
"""

import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

LOG = logging.getLogger(__name__)

_CASCADE_LOG = "/opt/swarm/artifacts/cascade.log"
_DEFAULT_DB = "/opt/swarm/artifacts/cascade.db"
_CB_BASE = "http://127.0.0.1:8518/mcp"
_CB_TIMEOUT = 2.0  # seconds

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cascade-wb")


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log_cascade(level: str, msg: str) -> None:
    """Append a line to the cascade log file AND Python logger."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts} [{level}] {msg}\n"
    try:
        os.makedirs(os.path.dirname(_CASCADE_LOG), exist_ok=True)
        with open(_CASCADE_LOG, "a") as fh:
            fh.write(line)
    except OSError:
        pass
    getattr(LOG, level.lower(), LOG.warning)(msg)


def _decode_redis_hash(data: dict) -> dict:
    """Attempt JSON decode on each value in a Redis hash dict."""
    out = {}
    for k, v in data.items():
        try:
            out[k] = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# SQLite primitives
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    key         TEXT PRIMARY KEY,
    holder      TEXT NOT NULL DEFAULT '',
    ttl_expires INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id    TEXT PRIMARY KEY,
    state      TEXT NOT NULL DEFAULT '',
    data       TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks_by_state (
    state   TEXT NOT NULL,
    task_id TEXT NOT NULL,
    PRIMARY KEY (state, task_id)
);
"""


class _SQLiteStore:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SQLITE_SCHEMA)

    # -- slots --

    def slot_claim(self, key: str, holder: str, ttl_s: int) -> bool:
        expires = int(time.time()) + ttl_s
        try:
            with self._conn() as conn:
                existing = conn.execute(
                    "SELECT holder, ttl_expires FROM slots WHERE key = ?", (key,)
                ).fetchone()
                if existing:
                    # Allow reclaim only if expired or same holder
                    if existing["ttl_expires"] > int(time.time()) and existing["holder"] != holder:
                        return False
                conn.execute(
                    "INSERT OR REPLACE INTO slots (key, holder, ttl_expires) VALUES (?, ?, ?)",
                    (key, holder, expires),
                )
            return True
        except sqlite3.Error as exc:
            LOG.error("SQLite slot_claim error: %s", exc)
            return False

    def slot_release(self, key: str, holder: str) -> bool:
        try:
            with self._conn() as conn:
                row = conn.execute("SELECT holder FROM slots WHERE key = ?", (key,)).fetchone()
                if row and row["holder"] == holder:
                    conn.execute("DELETE FROM slots WHERE key = ?", (key,))
                    return True
            return False
        except sqlite3.Error as exc:
            LOG.error("SQLite slot_release error: %s", exc)
            return False

    def slot_holder(self, key: str) -> str | None:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT holder, ttl_expires FROM slots WHERE key = ?", (key,)
                ).fetchone()
                if not row:
                    return None
                if row["ttl_expires"] <= int(time.time()):
                    conn.execute("DELETE FROM slots WHERE key = ?", (key,))
                    return None
                return row["holder"]
        except sqlite3.Error:
            return None

    def slot_list(self) -> list[dict]:
        try:
            now = int(time.time())
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT key, holder, ttl_expires FROM slots WHERE ttl_expires > ?", (now,)
                ).fetchall()
                return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    # -- tasks --

    def task_write(self, task_id: str, state: dict) -> bool:
        state_name = state.get("state", "")
        data_json = json.dumps(state)
        now = int(time.time())
        try:
            with self._conn() as conn:
                # Remove from old state index
                old = conn.execute(
                    "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if old and old["state"] != state_name:
                    conn.execute(
                        "DELETE FROM tasks_by_state WHERE state = ? AND task_id = ?",
                        (old["state"], task_id),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO tasks (task_id, state, data, updated_at) VALUES (?, ?, ?, ?)",
                    (task_id, state_name, data_json, now),
                )
                if state_name:
                    conn.execute(
                        "INSERT OR IGNORE INTO tasks_by_state (state, task_id) VALUES (?, ?)",
                        (state_name, task_id),
                    )
            return True
        except sqlite3.Error as exc:
            LOG.error("SQLite task_write error: %s", exc)
            return False

    def task_read(self, task_id: str) -> dict | None:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT data FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if not row:
                    return None
                return json.loads(row["data"])
        except (sqlite3.Error, json.JSONDecodeError):
            return None

    def task_list_by_state(self, state_name: str) -> list[dict]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT t.data FROM tasks t
                       JOIN tasks_by_state s ON t.task_id = s.task_id
                       WHERE s.state = ?""",
                    (state_name,),
                ).fetchall()
                result = []
                for row in rows:
                    try:
                        result.append(json.loads(row["data"]))
                    except json.JSONDecodeError:
                        pass
                return result
        except sqlite3.Error:
            return []

    def health(self) -> bool:
        try:
            with self._conn() as conn:
                conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False


# ---------------------------------------------------------------------------
# Context Bridge primitives
# ---------------------------------------------------------------------------


class _CBStore:
    """Thin wrapper around CB fact-assert/fact-query MCP endpoints."""

    _NS = "routing-protocol"

    def _call(self, method: str, params: dict) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": method,
                "arguments": params,
            },
        }
        resp = requests.post(_CB_BASE, json=payload, timeout=_CB_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"CB error: {data['error']}")
        return data.get("result")

    def assert_fact(self, subject: str, obj: Any) -> bool:
        try:
            self._call(
                "cb_fact_assert",
                {
                    "namespace": self._NS,
                    "subject": subject,
                    "object": json.dumps(obj) if not isinstance(obj, str) else obj,
                },
            )
            return True
        except Exception as exc:
            LOG.debug("CB assert_fact failed: %s", exc)
            return False

    def query_fact(self, subject: str) -> Any:
        try:
            result = self._call(
                "cb_fact_query",
                {"namespace": self._NS, "subject": subject},
            )
            if result is None:
                return None
            # CB returns the fact object; decode if JSON string
            if isinstance(result, str):
                try:
                    return json.loads(result)
                except json.JSONDecodeError:
                    return result
            return result
        except Exception as exc:
            LOG.debug("CB query_fact failed: %s", exc)
            return None

    def health(self) -> bool:
        try:
            requests.get("http://127.0.0.1:8518/health", timeout=_CB_TIMEOUT)
            return True
        except Exception:
            # Fall back to a lightweight fact query ping
            try:
                self._call("cb_fact_query", {"namespace": self._NS, "subject": "__ping__"})
                return True
            except Exception:
                return False


# ---------------------------------------------------------------------------
# StateStoreCascade
# ---------------------------------------------------------------------------


class StateStoreCascade:
    """Cascade-aware state store: Redis → CB → SQLite → degraded fail-closed.

    Args:
        redis_retry_window_s: Seconds to retry Redis before failing over (default 5).
        db_path: Override SQLite path (default: SWARM_CASCADE_DB env or /opt/swarm/artifacts/cascade.db).
    """

    def __init__(
        self,
        redis_retry_window_s: int = 5,
        db_path: str | None = None,
    ) -> None:
        self._retry_window = float(redis_retry_window_s)
        _db = db_path or os.environ.get("SWARM_CASCADE_DB", _DEFAULT_DB)
        self._sqlite = _SQLiteStore(_db)
        self._cb = _CBStore()
        self._redis_ok: bool | None = None  # None = unchecked

    # ------------------------------------------------------------------
    # Internal backend resolution
    # ------------------------------------------------------------------

    def _get_redis(self):
        try:
            import redis_client as _rc

            return _rc
        except ImportError:
            from src import redis_client as _rc

            return _rc

    def _try_redis(self) -> Any | None:
        """Return redis_client module if healthy, else None."""
        rc = self._get_redis()
        try:
            if rc.health_check():
                return rc
        except Exception:
            pass
        # Retry window
        deadline = time.monotonic() + self._retry_window
        while time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
            try:
                if rc.health_check():
                    return rc
            except Exception:
                pass
        return None

    def _mirror_to_sqlite_async(self, fn, *args, **kwargs) -> None:
        """Fire-and-forget SQLite write — never blocks hot path."""

        def _run():
            try:
                fn(*args, **kwargs)
            except Exception as exc:
                LOG.debug("Write-behind mirror failed: %s", exc)

        _executor.submit(_run)

    # ------------------------------------------------------------------
    # Slot operations
    # ------------------------------------------------------------------

    def claim_slot(self, slot_key: str, holder: str, ttl_s: int = 3600) -> bool:
        """Claim a named slot atomically. Returns True on success."""
        redis_key = f"routing:slot:{slot_key}"

        rc = self._try_redis()
        if rc is not None:
            try:
                ok = bool(rc.get_client().set(redis_key, holder, nx=True, ex=ttl_s))
                if ok:
                    # Write-behind to SQLite
                    self._mirror_to_sqlite_async(self._sqlite.slot_claim, slot_key, holder, ttl_s)
                return ok
            except Exception as exc:
                LOG.warning("Redis claim_slot error: %s", exc)

        # Failover 1: CB
        _log_cascade("WARNING", f"Redis down — failing over to CB for claim_slot({slot_key})")
        obj = {"holder": holder, "ttl_expires": int(time.time()) + ttl_s}
        existing = self._cb.query_fact(slot_key)
        if existing and isinstance(existing, dict):
            if (
                existing.get("ttl_expires", 0) > int(time.time())
                and existing.get("holder") != holder
            ):
                return False
        if self._cb.assert_fact(slot_key, obj):
            return True

        # Failover 2: SQLite
        _log_cascade("WARNING", f"CB down — failing over to SQLite for claim_slot({slot_key})")
        return self._sqlite.slot_claim(slot_key, holder, ttl_s)

    def release_slot(self, slot_key: str, holder: str) -> bool:
        """Release a named slot (only if holder matches). Returns True on success."""
        redis_key = f"routing:slot:{slot_key}"

        rc = self._try_redis()
        if rc is not None:
            try:
                current = rc.get_client().get(redis_key)
                if current == holder:
                    ok = bool(rc.get_client().delete(redis_key))
                    if ok:
                        self._mirror_to_sqlite_async(self._sqlite.slot_release, slot_key, holder)
                    return ok
                return False
            except Exception as exc:
                LOG.warning("Redis release_slot error: %s", exc)

        _log_cascade("WARNING", f"Redis down — failing over to CB for release_slot({slot_key})")
        obj = self._cb.query_fact(slot_key)
        if obj and isinstance(obj, dict) and obj.get("holder") == holder:
            if self._cb.assert_fact(slot_key, {}):
                return True

        _log_cascade("WARNING", f"CB down — failing over to SQLite for release_slot({slot_key})")
        return self._sqlite.slot_release(slot_key, holder)

    def slot_holder(self, slot_key: str) -> str | None:
        """Return the current holder of a slot, or None if free/expired."""
        redis_key = f"routing:slot:{slot_key}"

        rc = self._try_redis()
        if rc is not None:
            try:
                return rc.get_client().get(redis_key)
            except Exception as exc:
                LOG.warning("Redis slot_holder error: %s", exc)

        _log_cascade("WARNING", f"Redis down — CB fallback for slot_holder({slot_key})")
        obj = self._cb.query_fact(slot_key)
        if obj and isinstance(obj, dict):
            if obj.get("ttl_expires", 0) > int(time.time()):
                return obj.get("holder")
            return None

        _log_cascade("WARNING", f"CB down — SQLite fallback for slot_holder({slot_key})")
        return self._sqlite.slot_holder(slot_key)

    def list_slots(self) -> list[dict]:
        """List all active (non-expired) slot records."""
        rc = self._try_redis()
        if rc is not None:
            try:
                r = rc.get_client()
                keys = r.keys("routing:slot:*")
                slots = []
                for k in keys:
                    holder = r.get(k)
                    ttl = r.ttl(k)
                    slot_key = k.replace("routing:slot:", "", 1)
                    slots.append({"slot_key": slot_key, "holder": holder or "", "ttl": ttl})
                return slots
            except Exception as exc:
                LOG.warning("Redis list_slots error: %s", exc)

        _log_cascade("WARNING", "Redis down — CB/SQLite fallback for list_slots")
        # CB has no list; fall through to SQLite
        return self._sqlite.slot_list()

    # ------------------------------------------------------------------
    # Task ledger operations
    # ------------------------------------------------------------------

    def ledger_write(self, task_id: str, state: dict) -> bool:
        """Write task state to the ledger. Returns True on success."""
        redis_key = f"routing:task:{task_id}"
        state_name = state.get("state", "")

        rc = self._try_redis()
        if rc is not None:
            try:
                r = rc.get_client()
                pipe = r.pipeline()
                flat = {
                    k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                    for k, v in state.items()
                }
                pipe.hset(redis_key, mapping=flat)
                if state_name:
                    pipe.sadd(f"routing:tasks:by_state:{state_name}", task_id)
                pipe.execute()
                # Write-behind to SQLite
                self._mirror_to_sqlite_async(self._sqlite.task_write, task_id, state)
                return True
            except Exception as exc:
                LOG.warning("Redis ledger_write error: %s", exc)

        _log_cascade("WARNING", f"Redis down — CB fallback for ledger_write({task_id})")
        if self._cb.assert_fact(f"task:{task_id}", state):
            return True

        _log_cascade("WARNING", f"CB down — SQLite fallback for ledger_write({task_id})")
        result = self._sqlite.task_write(task_id, state)
        if not result:
            _log_cascade("CRITICAL", f"All backends down — ledger_write({task_id}) FAILED")
        return result

    def ledger_read(self, task_id: str) -> dict | None:
        """Read task state from the ledger. Returns dict or None."""
        redis_key = f"routing:task:{task_id}"

        rc = self._try_redis()
        if rc is not None:
            try:
                data = rc.get_client().hgetall(redis_key)
                if data:
                    return _decode_redis_hash(data)
            except Exception as exc:
                LOG.warning("Redis ledger_read error: %s", exc)

        _log_cascade("WARNING", f"Redis down — CB fallback for ledger_read({task_id})")
        result = self._cb.query_fact(f"task:{task_id}")
        if result is not None:
            return result if isinstance(result, dict) else {"raw": result}

        _log_cascade("WARNING", f"CB down — SQLite fallback for ledger_read({task_id})")
        return self._sqlite.task_read(task_id)

    def ledger_list_by_state(self, state_name: str) -> list[dict]:
        """List all tasks in a given state."""
        rc = self._try_redis()
        if rc is not None:
            try:
                r = rc.get_client()
                task_ids = r.smembers(f"routing:tasks:by_state:{state_name}")
                if task_ids is not None:  # smembers returns empty set, not None
                    results = []
                    for tid in task_ids:
                        data = r.hgetall(f"routing:task:{tid}")
                        if data:
                            results.append(_decode_redis_hash(data))
                    return results
            except Exception as exc:
                LOG.warning("Redis ledger_list_by_state error: %s", exc)

        _log_cascade(
            "WARNING", f"Redis down — SQLite fallback for ledger_list_by_state({state_name})"
        )
        return self._sqlite.task_list_by_state(state_name)

    # ------------------------------------------------------------------
    # Backend health + reporting
    # ------------------------------------------------------------------

    def backend_status(self) -> dict:
        """Return health of all three backends and the active one."""
        rc_mod = self._get_redis()
        try:
            redis_ok = rc_mod.health_check()
        except Exception:
            redis_ok = False

        try:
            cb_ok = self._cb.health()
        except Exception:
            cb_ok = False

        sqlite_ok = self._sqlite.health()

        if redis_ok:
            active = "redis"
        elif cb_ok:
            active = "cb"
        elif sqlite_ok:
            active = "sqlite"
        else:
            active = "failed"

        return {
            "redis": "ok" if redis_ok else "down",
            "cb": "ok" if cb_ok else "down",
            "sqlite": "ok" if sqlite_ok else "down",
            "active": active,
        }

    def active_backend(self) -> str:
        """Return the name of the currently active backend."""
        return self.backend_status()["active"]
