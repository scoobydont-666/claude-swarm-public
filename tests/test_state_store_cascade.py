#!/usr/bin/env python3
"""Tests for state_store_cascade.StateStoreCascade.

All cascade scenarios use mocks — no real Redis, CB, or permanent SQLite needed.

Coverage:
  - Cascade: Redis fail → CB; Redis+CB fail → SQLite; all fail → degraded
  - Write-behind: SQLite gets row even when Redis succeeds
  - active_backend / backend_status reporting
  - Basic slot claim/release/list round-trip (Redis path)
  - Ledger write/read/list round-trip (Redis path)
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("HYDRA_ENV", "dev")
os.environ.setdefault("SWARM_REDIS_SKIP_CHECK", "1")

from state_store_cascade import StateStoreCascade, _SQLiteStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cascade(tmp_path, redis_up=True, cb_up=True, redis_retry_window_s=0):
    """Build a StateStoreCascade with mocked Redis and CB."""
    db_path = str(tmp_path / "cascade.db")
    cascade = StateStoreCascade(redis_retry_window_s=redis_retry_window_s, db_path=db_path)

    # Patch _try_redis to return a mock or None
    if redis_up:
        fake_redis = _build_fake_redis_module()
        cascade._try_redis = lambda: fake_redis
        cascade._get_redis = lambda: fake_redis
    else:
        cascade._try_redis = lambda: None
        dead_rc = MagicMock()
        dead_rc.health_check.return_value = False
        cascade._get_redis = lambda: dead_rc

    # Patch CB
    if not cb_up:
        cascade._cb.assert_fact = MagicMock(return_value=False)
        cascade._cb.query_fact = MagicMock(return_value=None)
        cascade._cb.health = MagicMock(return_value=False)
    else:
        cascade._cb.assert_fact = MagicMock(return_value=True)
        cascade._cb.query_fact = MagicMock(return_value=None)
        cascade._cb.health = MagicMock(return_value=True)

    return cascade


def _build_fake_redis_module():
    """Build a minimal in-memory redis_client mock."""
    store: dict = {}

    def _set(key, value, nx=False, ex=None):
        if nx and key in store:
            return False
        store[key] = value
        return True

    def _get(key):
        return store.get(key)

    def _delete(*keys):
        count = 0
        for k in keys:
            if k in store:
                del store[k]
                count += 1
        return count

    def _keys(pattern):
        import fnmatch

        return [k for k in store if fnmatch.fnmatch(k, pattern)]

    def _ttl(key):
        return -1 if key in store else -2

    def _hset(key, mapping=None, **kwargs):
        if key not in store:
            store[key] = {}
        if mapping:
            store[key].update(mapping)

    def _hgetall(key):
        return dict(store.get(key, {}))

    def _sadd(key, *members):
        if key not in store:
            store[key] = set()
        store[key].update(members)

    def _smembers(key):
        return store.get(key, set())

    class _FakePipeline:
        def __init__(self):
            self._cmds = []

        def hset(self, key, mapping=None, **kw):
            self._cmds.append(("hset", key, mapping or kw))
            return self

        def sadd(self, key, *members):
            self._cmds.append(("sadd", key, members))
            return self

        def execute(self):
            for cmd, *args in self._cmds:
                if cmd == "hset":
                    _hset(args[0], mapping=args[1])
                elif cmd == "sadd":
                    _sadd(args[0], *args[1])
            return [True] * len(self._cmds)

    fake_r = MagicMock()
    fake_r.set = _set
    fake_r.get = _get
    fake_r.delete = _delete
    fake_r.keys = _keys
    fake_r.ttl = _ttl
    fake_r.hset = _hset
    fake_r.hgetall = _hgetall
    fake_r.sadd = _sadd
    fake_r.smembers = _smembers
    fake_r.pipeline = lambda: _FakePipeline()

    rc = MagicMock()
    rc.health_check.return_value = True
    rc.get_client.return_value = fake_r
    # Expose internal store for write-behind assertions
    rc._store = store
    return rc


# ---------------------------------------------------------------------------
# Slot round-trip tests (Redis path)
# ---------------------------------------------------------------------------


class TestSlotRoundTrip:
    def test_claim_slot_redis(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        assert c.claim_slot("giga:0:7b", "worker-1", ttl_s=60) is True

    def test_release_slot_redis(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        c.claim_slot("giga:0:7b", "worker-1", ttl_s=60)
        assert c.release_slot("giga:0:7b", "worker-1") is True

    def test_release_wrong_holder(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        c.claim_slot("giga:0:7b", "worker-1", ttl_s=60)
        assert c.release_slot("giga:0:7b", "worker-2") is False

    def test_double_claim_blocked(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        assert c.claim_slot("giga:0:7b", "worker-1", ttl_s=60) is True
        assert c.claim_slot("giga:0:7b", "worker-2", ttl_s=60) is False

    def test_slot_holder(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        c.claim_slot("giga:0:7b", "worker-1", ttl_s=60)
        assert c.slot_holder("giga:0:7b") == "worker-1"

    def test_list_slots_redis(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        c.claim_slot("giga:0:7b", "worker-1", ttl_s=60)
        c.claim_slot("giga:1:14b", "worker-2", ttl_s=60)
        slots = c.list_slots()
        keys = {s["slot_key"] for s in slots}
        assert "giga:0:7b" in keys
        assert "giga:1:14b" in keys


# ---------------------------------------------------------------------------
# Ledger round-trip tests (Redis path)
# ---------------------------------------------------------------------------


class TestLedgerRoundTrip:
    def test_write_and_read(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        task = {"state": "running", "worker": "giga", "priority": 1}
        assert c.ledger_write("task-abc", task) is True
        result = c.ledger_read("task-abc")
        assert result is not None
        # Values stored as strings in Redis hash
        assert result.get("state") in ("running", task["state"])

    def test_list_by_state(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        c.ledger_write("task-001", {"state": "pending", "name": "build"})
        c.ledger_write("task-002", {"state": "pending", "name": "test"})
        c.ledger_write("task-003", {"state": "running", "name": "deploy"})
        pending = c.ledger_list_by_state("pending")
        assert len(pending) == 2
        running = c.ledger_list_by_state("running")
        assert len(running) == 1

    def test_read_missing_returns_none(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        assert c.ledger_read("nonexistent-task") is None


# ---------------------------------------------------------------------------
# Cascade: Redis fail → CB
# ---------------------------------------------------------------------------


class TestCascadeRedisToCB:
    def test_claim_slot_falls_to_cb(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=True)
        assert c.claim_slot("giga:0:7b", "worker-1", ttl_s=60) is True
        c._cb.assert_fact.assert_called_once()

    def test_ledger_write_falls_to_cb(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=True)
        assert c.ledger_write("task-x", {"state": "pending"}) is True
        c._cb.assert_fact.assert_called_once()

    def test_slot_holder_falls_to_cb(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=True)
        # CB returns a fact with holder
        c._cb.query_fact = MagicMock(
            return_value={"holder": "worker-1", "ttl_expires": int(time.time()) + 100}
        )
        assert c.slot_holder("giga:0:7b") == "worker-1"


# ---------------------------------------------------------------------------
# Cascade: Redis+CB fail → SQLite
# ---------------------------------------------------------------------------


class TestCascadeToSQLite:
    def test_claim_slot_falls_to_sqlite(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=False)
        assert c.claim_slot("giga:0:7b", "worker-1", ttl_s=60) is True
        # Verify it's in SQLite
        assert c._sqlite.slot_holder("giga:0:7b") == "worker-1"

    def test_release_slot_falls_to_sqlite(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=False)
        c.claim_slot("giga:0:7b", "worker-1", ttl_s=60)
        assert c.release_slot("giga:0:7b", "worker-1") is True
        assert c._sqlite.slot_holder("giga:0:7b") is None

    def test_ledger_write_falls_to_sqlite(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=False)
        state = {"state": "dispatched", "tier": 2}
        assert c.ledger_write("task-y", state) is True
        assert c._sqlite.task_read("task-y") == state

    def test_ledger_read_falls_to_sqlite(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=False)
        state = {"state": "completed", "result": "ok"}
        c._sqlite.task_write("task-z", state)
        result = c.ledger_read("task-z")
        assert result == state

    def test_list_by_state_sqlite(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=False)
        c.ledger_write("t1", {"state": "pending"})
        c.ledger_write("t2", {"state": "pending"})
        c.ledger_write("t3", {"state": "running"})
        assert len(c.ledger_list_by_state("pending")) == 2
        assert len(c.ledger_list_by_state("running")) == 1


# ---------------------------------------------------------------------------
# Cascade: all backends fail → degraded fail-closed
# ---------------------------------------------------------------------------


class TestDegradedFailClosed:
    def _make_all_dead(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=False)
        # Sabotage SQLite
        c._sqlite.slot_claim = MagicMock(return_value=False)
        c._sqlite.slot_release = MagicMock(return_value=False)
        c._sqlite.slot_holder = MagicMock(return_value=None)
        c._sqlite.slot_list = MagicMock(return_value=[])
        c._sqlite.task_write = MagicMock(return_value=False)
        c._sqlite.task_read = MagicMock(return_value=None)
        c._sqlite.task_list_by_state = MagicMock(return_value=[])
        c._sqlite.health = MagicMock(return_value=False)
        return c

    def test_claim_returns_false(self, tmp_path):
        c = self._make_all_dead(tmp_path)
        assert c.claim_slot("giga:0:7b", "worker-1") is False

    def test_ledger_write_returns_false(self, tmp_path):
        c = self._make_all_dead(tmp_path)
        assert c.ledger_write("task-dead", {"state": "pending"}) is False

    def test_ledger_read_returns_none(self, tmp_path):
        c = self._make_all_dead(tmp_path)
        assert c.ledger_read("task-dead") is None

    def test_active_backend_is_failed(self, tmp_path):
        c = self._make_all_dead(tmp_path)
        assert c.active_backend() == "failed"


# ---------------------------------------------------------------------------
# Write-behind: SQLite gets row even when Redis succeeds
# ---------------------------------------------------------------------------


class TestWriteBehind:
    def test_slot_claim_mirrors_to_sqlite(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        assert c.claim_slot("giga:0:7b", "worker-wb", ttl_s=120) is True
        # Write-behind is async — give the thread pool a moment
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            holder = c._sqlite.slot_holder("giga:0:7b")
            if holder == "worker-wb":
                break
            time.sleep(0.05)
        assert c._sqlite.slot_holder("giga:0:7b") == "worker-wb"

    def test_ledger_write_mirrors_to_sqlite(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True)
        state = {"state": "running", "tier": 2}
        assert c.ledger_write("task-wb", state) is True
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            row = c._sqlite.task_read("task-wb")
            if row is not None:
                break
            time.sleep(0.05)
        assert c._sqlite.task_read("task-wb") == state


# ---------------------------------------------------------------------------
# Backend status reporting
# ---------------------------------------------------------------------------


class TestBackendStatus:
    def test_all_up_active_is_redis(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=True, cb_up=True)
        # Patch health_check on the rc module returned by _get_redis
        rc_mock = c._get_redis()
        rc_mock.health_check.return_value = True
        c._cb.health = MagicMock(return_value=True)
        status = c.backend_status()
        assert status["redis"] == "ok"
        assert status["active"] == "redis"

    def test_redis_down_active_is_cb(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=True)
        c._cb.health = MagicMock(return_value=True)
        status = c.backend_status()
        assert status["redis"] == "down"
        assert status["active"] == "cb"

    def test_redis_and_cb_down_active_is_sqlite(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=False)
        c._cb.health = MagicMock(return_value=False)
        c._sqlite.health = MagicMock(return_value=True)
        status = c.backend_status()
        assert status["active"] == "sqlite"

    def test_all_down_active_is_failed(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=False)
        c._cb.health = MagicMock(return_value=False)
        c._sqlite.health = MagicMock(return_value=False)
        status = c.backend_status()
        assert status["active"] == "failed"

    def test_active_backend_convenience(self, tmp_path):
        c = _make_cascade(tmp_path, redis_up=False, cb_up=False)
        c._cb.health = MagicMock(return_value=False)
        c._sqlite.health = MagicMock(return_value=False)
        assert c.active_backend() == "failed"


# ---------------------------------------------------------------------------
# SQLiteStore unit tests (isolated)
# ---------------------------------------------------------------------------


class TestSQLiteStore:
    def test_slot_claim_and_release(self, tmp_path):
        s = _SQLiteStore(str(tmp_path / "test.db"))
        assert s.slot_claim("k1", "holder-a", 3600) is True
        assert s.slot_holder("k1") == "holder-a"
        assert s.slot_release("k1", "holder-a") is True
        assert s.slot_holder("k1") is None

    def test_slot_double_claim_blocked(self, tmp_path):
        s = _SQLiteStore(str(tmp_path / "test.db"))
        s.slot_claim("k1", "holder-a", 3600)
        assert s.slot_claim("k1", "holder-b", 3600) is False

    def test_slot_expired_returns_none(self, tmp_path):
        s = _SQLiteStore(str(tmp_path / "test.db"))
        # Claim with already-expired TTL by inserting directly
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute(
            "INSERT OR REPLACE INTO slots (key, holder, ttl_expires) VALUES (?, ?, ?)",
            ("expired-k", "old-holder", int(time.time()) - 10),
        )
        conn.commit()
        conn.close()
        assert s.slot_holder("expired-k") is None

    def test_task_write_and_read(self, tmp_path):
        s = _SQLiteStore(str(tmp_path / "test.db"))
        state = {"state": "running", "worker": "giga"}
        assert s.task_write("t1", state) is True
        assert s.task_read("t1") == state

    def test_task_list_by_state(self, tmp_path):
        s = _SQLiteStore(str(tmp_path / "test.db"))
        s.task_write("t1", {"state": "pending"})
        s.task_write("t2", {"state": "pending"})
        s.task_write("t3", {"state": "running"})
        assert len(s.task_list_by_state("pending")) == 2
        assert len(s.task_list_by_state("running")) == 1

    def test_health_ok(self, tmp_path):
        s = _SQLiteStore(str(tmp_path / "test.db"))
        assert s.health() is True
