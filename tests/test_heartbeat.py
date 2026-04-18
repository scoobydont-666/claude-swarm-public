"""Tests for worker heartbeat protocol (routing-protocol-v1 §7,§10)."""

import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ["SWARM_REDIS_SKIP_CHECK"] = "1"

import heartbeat as hb_mod  # noqa: E402


@pytest.fixture(autouse=True)
def fs_tmpdir(monkeypatch, tmp_path):
    monkeypatch.setattr(hb_mod, "FS_FALLBACK_DIR", tmp_path / "hb")
    # Force filesystem fallback (pretend Redis is down)
    monkeypatch.setattr(hb_mod, "_redis_up", lambda: False)
    yield


def test_write_and_read_roundtrip():
    h = hb_mod.Heartbeat(
        task_id="t1",
        worker_id="w1",
        last_ping=time.time(),
        state_hash="s0",
        started_at=time.time(),
    )
    assert hb_mod.write_heartbeat(h) is True
    got = hb_mod.read_heartbeat("t1")
    assert got is not None
    assert got.task_id == "t1"
    assert got.worker_id == "w1"
    assert got.state_hash == "s0"


def test_is_alive_fresh():
    h = hb_mod.Heartbeat("t2", "w2", time.time(), "s0", time.time())
    hb_mod.write_heartbeat(h)
    assert hb_mod.is_alive("t2") is True


def test_is_alive_stale():
    old_time = time.time() - (hb_mod.HEARTBEAT_TIMEOUT_S + 10)
    h = hb_mod.Heartbeat("t3", "w3", old_time, "s0", old_time)
    hb_mod.write_heartbeat(h)
    assert hb_mod.is_alive("t3") is False


def test_is_alive_no_record():
    assert hb_mod.is_alive("nonexistent") is False


def test_reap_dead_workers():
    fresh = hb_mod.Heartbeat("fresh", "w", time.time(), "s0", time.time())
    old_t = time.time() - (hb_mod.HEARTBEAT_TIMEOUT_S + 10)
    stale = hb_mod.Heartbeat("stale", "w", old_t, "s0", old_t)
    hb_mod.write_heartbeat(fresh)
    hb_mod.write_heartbeat(stale)

    dead = hb_mod.reap_dead_workers(["fresh", "stale", "nonexistent"])
    assert "stale" in dead
    assert "nonexistent" in dead
    assert "fresh" not in dead


def test_heartbeat_thread_pings():
    thread = hb_mod.HeartbeatThread(task_id="threaded", worker_id="w")
    # Monkeypatch interval to something fast
    orig_interval = hb_mod.HEARTBEAT_INTERVAL_S
    hb_mod.HEARTBEAT_INTERVAL_S = 0.1
    try:
        thread.start()
        time.sleep(0.3)
        got = hb_mod.read_heartbeat("threaded")
        assert got is not None
        assert got.task_id == "threaded"
    finally:
        thread.stop()
        hb_mod.HEARTBEAT_INTERVAL_S = orig_interval


def test_heartbeat_thread_update_progress():
    thread = hb_mod.HeartbeatThread(task_id="progress", worker_id="w")
    hb_mod.HEARTBEAT_INTERVAL_S = 0.1
    try:
        thread.start()
        time.sleep(0.15)
        thread.update("step-1")
        got = hb_mod.read_heartbeat("progress")
        assert got.state_hash == "step-1"
    finally:
        thread.stop()
        hb_mod.HEARTBEAT_INTERVAL_S = 30


def test_is_stuck_returns_false_for_dead():
    """A dead worker is not 'stuck' — is_alive is False."""
    old_t = time.time() - 1000
    h = hb_mod.Heartbeat("dead", "w", old_t, "s0", old_t)
    hb_mod.write_heartbeat(h)
    assert hb_mod.is_stuck("dead") is False


def test_is_stuck_returns_false_for_fresh_young_worker():
    """Worker that just started can't be stuck yet."""
    h = hb_mod.Heartbeat("young", "w", time.time(), "s0", time.time())
    hb_mod.write_heartbeat(h)
    assert hb_mod.is_stuck("young") is False
