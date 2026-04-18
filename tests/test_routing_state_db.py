"""Tests for routing_state_db — SQLite persistence for routing protocol v1."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Add hooks/lib to sys.path so we can import routing_state_db.
_HOOKS_LIB = Path.home() / ".claude" / "hooks" / "lib"
if str(_HOOKS_LIB) not in sys.path:
    sys.path.insert(0, str(_HOOKS_LIB))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Use a per-test SQLite DB via CLAUDE_ROUTING_DB env override."""
    db = tmp_path / "routing.db"
    monkeypatch.setenv("CLAUDE_ROUTING_DB", str(db))
    # Force re-import so module-level constants re-resolve
    if "routing_state_db" in sys.modules:
        del sys.modules["routing_state_db"]
    return db


@pytest.fixture
def db(db_path):
    """Initialized DB module with clean schema."""
    import routing_state_db as m
    m.init_db()
    return m


def test_init_db_idempotent(db):
    # Calling init_db twice must not error.
    db.init_db()
    db.init_db()


def test_record_fp_block_counts_window(db):
    assert db.record_fp_block("hook-a") == 1
    assert db.record_fp_block("hook-a") == 2
    assert db.record_fp_block("hook-b") == 3
    assert db.recent_fp_count(window_s=3600) == 3


def test_recent_fp_count_respects_window(db):
    # Insert an "old" timestamp directly.
    with db._get_conn() as conn:
        conn.execute("INSERT INTO fp_blocks(ts, hook) VALUES (?, ?)",
                     (time.time() - 7200, "ancient"))
    # Add a fresh one
    db.record_fp_block("fresh")
    assert db.recent_fp_count(window_s=3600) == 1  # old one outside window


def test_record_and_recent_dispatches(db):
    for i in range(3):
        db.record_dispatch(f"target-{i}")
    ts_list = db.recent_dispatches(window_s=60)
    assert len(ts_list) == 3
    assert all(isinstance(t, float) for t in ts_list)


def test_record_edit_and_recent_edits(db):
    db.record_edit("/opt/foo/a.py")
    db.record_edit("/opt/bar/b.py")
    edits = db.recent_edits(window_s=60)
    assert len(edits) == 2
    paths = {e["path"] for e in edits}
    assert paths == {"/opt/foo/a.py", "/opt/bar/b.py"}


def test_hook_fires_counts(db):
    db.record_hook_fire("parallel-detector", "enforce", "warn")
    db.record_hook_fire("parallel-detector", "enforce", "warn")
    db.record_hook_fire("pause-ask-scanner", "enforce", "block", "matched halt")
    counts = db.hook_fire_counts(window_s=300)
    assert counts.get(("parallel-detector", "warn")) == 2
    assert counts.get(("pause-ask-scanner", "block")) == 1


def test_dlq_enqueue_and_resolve(db):
    eid = db.enqueue_dlq("fp-block", "parallel-detector", {"ts": 1.0})
    assert eid > 0
    assert db.dlq_depth() == 1
    assert db.dlq_depth(kind="fp-block") == 1
    db.resolve_dlq(eid, "false positive confirmed")
    assert db.dlq_depth() == 0


def test_dlq_depths_by_kind(db):
    db.enqueue_dlq("fp-block", "h1", {})
    db.enqueue_dlq("fp-block", "h2", {})
    db.enqueue_dlq("pause-ask-block", "h1", {})
    depths = db.dlq_depths_by_kind()
    assert depths.get("fp-block") == 2
    assert depths.get("pause-ask-block") == 1


def test_phase_commit_recording(db):
    db.phase_commit_record("P0A", "abc1234", 42.7)
    dur = db.last_phase_commit_duration()
    assert dur == pytest.approx(42.7)


def test_last_phase_commit_duration_empty_db(db):
    # Freshly-initialized DB has no commits.
    assert db.last_phase_commit_duration() == 0.0


def test_prune_expired(db):
    # Insert a mix of old and fresh
    with db._get_conn() as conn:
        conn.execute("INSERT INTO fp_blocks(ts, hook) VALUES (?, ?)",
                     (time.time() - 7200, "ancient"))
    db.record_fp_block("fresh")
    db.prune_expired("fp_blocks", window_s=3600)
    # Fresh should survive; ancient should be gone.
    assert db.recent_fp_count(window_s=86400) == 1


def test_prune_rejects_unknown_table(db):
    # Must not drop unauthorized tables.
    db.prune_expired("dlq", window_s=0)  # should be a no-op per our spec
    # dlq table must still exist
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dlq'"
        ).fetchone()
        assert row is not None


def test_metadata_get_set(db):
    db.set_meta("foo", "bar")
    assert db.get_meta("foo") == "bar"
    db.set_meta("foo", "baz")  # upsert
    assert db.get_meta("foo") == "baz"
    # Default for unknown key
    assert db.get_meta("missing", default="default-val") == "default-val"


def test_resilience_against_db_error(tmp_path, monkeypatch):
    """If the DB file is unwritable, API must return defaults, not raise."""
    # Point to a path that cannot be created.
    monkeypatch.setenv("CLAUDE_ROUTING_DB", "/nonexistent/readonly/routing.db")
    if "routing_state_db" in sys.modules:
        del sys.modules["routing_state_db"]
    import routing_state_db as m
    # These should degrade gracefully.
    assert m.record_fp_block("hook-x") == 0
    assert m.recent_fp_count() == 0
    assert m.dlq_depth() == 0
