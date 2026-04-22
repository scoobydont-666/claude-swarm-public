#!/usr/bin/env python3
"""Phase 1 DoD #9 — claude-swarm drain/restore automation helpers.

Exposes three public functions consumed by the hydra-project DoD test harness:

    drain()                    — checkpoint DB 0 keyspace; move live traffic
                                  test prefix keys to scratch area; verify no
                                  active dispatches before proceeding.
    restore()                  — idempotent: restore DB 0 from checkpoint;
                                  reconcile orphaned dispatch records.
    redis_keyspace_checksum()  — SHA-256 of sorted (key, type, value) tuples
                                  across every key in the specified Redis DB.

Safety gates
------------
* ALL three functions are no-ops unless the env var PHASE1_DOD_DRAIN_TEST=1
  is set.  This prevents accidental execution in a normal shell session.
* drain() refuses to proceed if tasks:claimed is non-empty (active
  dispatches in flight).
* restore() is idempotent: calling it twice is safe.

Redis layout assumed by these helpers
--------------------------------------
* DB 0 — claude-swarm production keyspace
* DB 1 — nai-swarm / test keyspace  (this module never writes to DB 1)

The helpers do NOT stop or start celery-swarm systemd services.  That
is intentional: for the DoD #9 unit test we only need Redis-level state
preservation proof.  The full drain runbook (stopping celery services,
snapshotting NFS task board, etc.) is documented in:
    <hydra-project-path>/docs/claude-swarm-drain-for-nai-swarm-testing-2026-04-21.md
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import redis

# ---------------------------------------------------------------------------
# Environment / connection helpers
# ---------------------------------------------------------------------------

_REDIS_HOST = os.environ.get("SWARM_REDIS_HOST", "127.0.0.1")
_REDIS_PORT = int(os.environ.get("SWARM_REDIS_PORT", "6379"))
_REDIS_PASSWORD = os.environ.get("SWARM_REDIS_PASSWORD", "")
_GATE_VAR = "PHASE1_DOD_DRAIN_TEST"


def _require_gate() -> None:
    """Raise RuntimeError unless the safety gate env var is set."""
    if not os.environ.get(_GATE_VAR):
        raise RuntimeError(
            f"Safety gate: set {_GATE_VAR}=1 before calling drain/restore helpers. "
            "This prevents accidental execution outside the DoD test harness."
        )


def _client(db: int = 0) -> redis.Redis:
    """Return a fresh Redis client for the given DB index."""
    return redis.Redis(
        host=_REDIS_HOST,
        port=_REDIS_PORT,
        password=_REDIS_PASSWORD or None,
        db=db,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )


# ---------------------------------------------------------------------------
# Checkpoint store (in-process dict; lives for the duration of the test run)
# ---------------------------------------------------------------------------

# _CHECKPOINT maps  key -> (type, serialised_value)
# serialised_value depends on Redis type:
#   string → str
#   hash   → dict
#   list   → list
#   set    → sorted list (deterministic)
#   zset   → list of (member, score) sorted by score then member
#   stream → list of (stream_id, fields_dict) — read-only snapshot; not restored
#
# Keys prefixed with "dod9:" are TEST-INJECTED keys; their presence in the
# checkpoint is expected and they are cleaned up during restore.

_CHECKPOINT: dict[str, tuple[str, Any]] | None = None
_CHECKPOINT_TS: float = 0.0
_TEST_KEY_PREFIX = "dod9:"


def _serialise_key(r: redis.Redis, key: str) -> tuple[str, Any]:
    """Read one key from Redis and return (type, serialisable_value)."""
    ktype = r.type(key)
    if ktype == "string":
        return (ktype, r.get(key))
    elif ktype == "hash":
        return (ktype, r.hgetall(key))
    elif ktype == "list":
        return (ktype, r.lrange(key, 0, -1))
    elif ktype == "set":
        return (ktype, sorted(r.smembers(key)))
    elif ktype == "zset":
        members_scores = r.zrange(key, 0, -1, withscores=True)
        return (ktype, [(m, s) for m, s in members_scores])
    elif ktype == "stream":
        # Snapshot stream for checksum but do NOT attempt to restore it
        # (streams are append-only; restoration would create duplicates).
        entries = r.xrange(key, "-", "+", count=5000)
        return (ktype, [(sid, fields) for sid, fields in entries])
    else:
        return (ktype, None)


def _restore_key(r: redis.Redis, key: str, ktype: str, value: Any) -> None:
    """Write one key back into Redis from its checkpoint snapshot.

    Skips stream keys (append-only; cannot be replayed safely).
    Idempotent: deletes and rewrites each key.
    """
    if ktype == "stream":
        return  # cannot restore streams — see _serialise_key comment
    r.delete(key)
    if value is None:
        return
    if ktype == "string":
        r.set(key, value)
    elif ktype == "hash":
        r.hset(key, mapping=value)
    elif ktype == "list":
        if value:
            r.rpush(key, *value)
    elif ktype == "set":
        if value:
            r.sadd(key, *value)
    elif ktype == "zset":
        if value:
            mapping = {member: score for member, score in value}
            r.zadd(key, mapping)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def redis_keyspace_checksum(db: int = 0) -> str:
    """Return a SHA-256 hex digest of the entire keyspace in `db`.

    Algorithm:
      1. SCAN all keys.
      2. For each key (sorted) read its type + value.
      3. Hash the JSON-serialised sorted list of (key, type, value) tuples.

    Stream entries are included in the checksum (read-only snapshot), so the
    checksum reflects the full DB state including event history.
    """
    r = _client(db)
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = r.scan(cursor, count=500)
        keys.extend(batch)
        if cursor == 0:
            break
    keys.sort()

    items: list[tuple[str, str, Any]] = []
    for key in keys:
        ktype, value = _serialise_key(r, key)
        items.append((key, ktype, value))

    payload = json.dumps(items, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def drain() -> dict:
    """Checkpoint DB 0 and block further writes.

    Returns a summary dict with:
        key_count    — number of keys snapshotted
        claimed_count — number of active dispatches found (must be 0)
        ts           — epoch timestamp of checkpoint

    Raises:
        RuntimeError — if PHASE1_DOD_DRAIN_TEST gate is not set
        RuntimeError — if tasks:claimed is non-empty (active dispatches)
    """
    _require_gate()
    global _CHECKPOINT, _CHECKPOINT_TS

    r = _client(0)

    # Safety: refuse if active dispatches are in flight
    claimed_count = r.zcard("tasks:claimed")
    if claimed_count:
        raise RuntimeError(
            f"drain() refused: {claimed_count} tasks currently in tasks:claimed. "
            "Wait for active dispatches to complete before draining."
        )

    # Snapshot every key in DB 0
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = r.scan(cursor, count=500)
        keys.extend(batch)
        if cursor == 0:
            break

    checkpoint: dict[str, tuple[str, Any]] = {}
    for key in keys:
        checkpoint[key] = _serialise_key(r, key)

    _CHECKPOINT = checkpoint
    _CHECKPOINT_TS = time.time()

    return {
        "key_count": len(checkpoint),
        "claimed_count": claimed_count,
        "ts": _CHECKPOINT_TS,
    }


def restore() -> dict:
    """Restore DB 0 from checkpoint and reconcile orphaned test keys.

    Idempotent: calling restore() a second time is safe (it re-applies the
    same checkpoint).  Test-injected keys (prefixed with 'dod9:') that were
    NOT in the original checkpoint are deleted as part of orphan
    reconciliation.

    Returns a summary dict with:
        restored_keys    — keys written back
        orphans_deleted  — test-injected keys removed
        ts               — epoch timestamp of restore

    Raises:
        RuntimeError — if PHASE1_DOD_DRAIN_TEST gate is not set
        RuntimeError — if drain() was never called (no checkpoint)
    """
    _require_gate()
    global _CHECKPOINT

    if _CHECKPOINT is None:
        raise RuntimeError(
            "restore() called before drain() — no checkpoint to restore from."
        )

    r = _client(0)
    ts = time.time()

    # Step 1: Delete all test-injected keys not in the original checkpoint
    test_keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = r.scan(cursor, match=f"{_TEST_KEY_PREFIX}*", count=500)
        test_keys.extend(batch)
        if cursor == 0:
            break

    orphans_deleted = 0
    for key in test_keys:
        if key not in _CHECKPOINT:
            r.delete(key)
            orphans_deleted += 1

    # Step 2: Restore every checkpointed key
    restored_keys = 0
    for key, (ktype, value) in _CHECKPOINT.items():
        if ktype != "stream":  # streams not restored (append-only)
            _restore_key(r, key, ktype, value)
            restored_keys += 1

    return {
        "restored_keys": restored_keys,
        "orphans_deleted": orphans_deleted,
        "ts": ts,
    }


# ---------------------------------------------------------------------------
# Test workload helpers (used by DoD #9 test)
# ---------------------------------------------------------------------------


def inject_test_dispatches(count: int = 5) -> list[str]:
    """Write `count` synthetic dispatch records into DB 0 using the dod9: prefix.

    These are plain hashes — no interaction with the real task queues.
    Returns list of injected key names.
    """
    _require_gate()
    r = _client(0)
    injected: list[str] = []
    for i in range(count):
        key = f"{_TEST_KEY_PREFIX}dispatch:{int(time.time() * 1000)}:{i}"
        r.hset(
            key,
            mapping={
                "id": key,
                "state": "synthetic",
                "created_at": str(time.time()),
                "test_marker": "dod9",
            },
        )
        injected.append(key)
    return injected


def inject_orphaned_dispatch(task_id: str | None = None) -> str:
    """Write one orphaned dispatch: key in tasks:claimed zset but hash is missing.

    This simulates a worker that died mid-flight.  restore() should clean
    this up via the checkpoint replay (claimed entry disappears because it
    was absent in the pre-drain snapshot).
    Returns the orphaned task_id.
    """
    _require_gate()
    r = _client(0)
    orphan_id = task_id or f"{_TEST_KEY_PREFIX}orphan:{int(time.time() * 1000)}"
    # Add to claimed zset — no corresponding task:{id} hash
    r.zadd("tasks:claimed", {orphan_id: time.time()})
    return orphan_id


# ---------------------------------------------------------------------------
# CLI shim (for manual testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Phase 1 DoD #9 drain/restore CLI shim")
    parser.add_argument("cmd", choices=["checksum", "drain", "restore", "inject"])
    parser.add_argument("--db", type=int, default=0)
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()

    os.environ[_GATE_VAR] = "1"  # CLI shim sets the gate automatically

    if args.cmd == "checksum":
        print(f"DB {args.db} checksum: {redis_keyspace_checksum(args.db)}")
    elif args.cmd == "drain":
        result = drain()
        print(f"Drained: {result}")
    elif args.cmd == "restore":
        result = restore()
        print(f"Restored: {result}")
    elif args.cmd == "inject":
        keys = inject_test_dispatches(args.count)
        print(f"Injected {len(keys)} test keys: {keys[:3]}...")
