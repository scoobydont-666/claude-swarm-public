"""Tests for routing_metrics — Prometheus textfile exposition."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HOOKS_LIB = Path.home() / ".claude" / "hooks" / "lib"
if str(_HOOKS_LIB) not in sys.path:
    sys.path.insert(0, str(_HOOKS_LIB))


@pytest.fixture
def clean_modules():
    """Drop cached module state so env overrides take effect."""
    for m in ("routing_state_db", "routing_metrics"):
        sys.modules.pop(m, None)
    yield
    for m in ("routing_state_db", "routing_metrics"):
        sys.modules.pop(m, None)


@pytest.fixture
def metrics_env(tmp_path, monkeypatch, clean_modules):
    """Sandbox the DB to a temp file; ensure emit() picks a fallback path."""
    db = tmp_path / "routing.db"
    monkeypatch.setenv("CLAUDE_ROUTING_DB", str(db))
    import routing_metrics as rm
    import routing_state_db as rsdb

    rsdb.init_db()
    return rsdb, rm, tmp_path


def test_build_exposition_includes_all_metric_families(metrics_env):
    _, rm, _ = metrics_env
    text = rm.build_exposition()
    # All required gauges present.
    for name in [
        "routing_fp_blocks_total",
        "routing_dlq_depth",
        "routing_dispatches_per_minute",
        "routing_hook_fires_total",
        "routing_phase_commit_seconds_last",
        "routing_mode",
        "routing_metrics_last_refresh_timestamp_seconds",
    ]:
        assert name in text, f"missing metric: {name}"
    # Help + Type lines.
    assert text.count("# HELP") >= 7
    assert text.count("# TYPE") >= 7


def test_emit_writes_atomic_file(metrics_env):
    _, rm, tmp_path = metrics_env
    out = tmp_path / "routing_protocol.prom"
    result = rm.emit(path=out)
    assert result == out
    assert out.exists()
    content = out.read_text()
    assert "routing_fp_blocks_total" in content
    # No stray tempfiles left behind.
    leftover = list(tmp_path.glob(".routing_protocol_*.prom"))
    assert leftover == []


def test_emit_reflects_state_changes(metrics_env):
    rsdb, rm, tmp_path = metrics_env
    # Seed some state.
    rsdb.record_fp_block("hook-a")
    rsdb.record_fp_block("hook-a")
    rsdb.enqueue_dlq("fp-block", "hook-a", {"ts": 1.0})
    rsdb.record_hook_fire("parallel-detector", "enforce", "warn")

    out = tmp_path / "routing_protocol.prom"
    rm.emit(path=out)
    text = out.read_text()
    # fp_blocks_total reflects 2 records.
    assert 'routing_fp_blocks_total{window="1h"} 2' in text
    # dlq_depth shows kind=fp-block line with value 1.
    assert 'routing_dlq_depth{kind="fp-block"} 1' in text
    # hook_fires recorded.
    assert 'hook="parallel-detector"' in text


def test_emit_handles_empty_state(metrics_env):
    _, rm, tmp_path = metrics_env
    out = tmp_path / "routing_protocol.prom"
    rm.emit(path=out)
    text = out.read_text()
    # Placeholder lines when no data yet.
    assert 'routing_dlq_depth{kind="none"} 0' in text
    assert 'routing_hook_fires_total{hook="none",action="none"} 0' in text


def test_mode_value_mapping(metrics_env, monkeypatch, tmp_path):
    _, rm, _ = metrics_env
    # Point settings.json to a temp file with warn-only.
    settings = tmp_path / "settings.json"
    settings.write_text('{"routing_protocol_mode": "warn-only"}')
    monkeypatch.setattr(rm, "_SETTINGS", settings)
    text = rm.build_exposition()
    assert 'routing_mode{mode="warn-only"} 1' in text
