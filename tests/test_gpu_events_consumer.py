"""Unit tests for gpu_events_consumer.FleetGpuView — no Redis required."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.gpu_events_consumer import FleetGpuView, HostState


@pytest.fixture
def view():
    client = MagicMock()
    return FleetGpuView(client=client)


def _fields(**kw) -> dict:
    """Build a fake XREAD fields dict (bytes keys like real redis-py)."""
    out = {}
    for k, v in kw.items():
        out[k.encode()] = str(v).encode()
    return out


def test_apply_heartbeat_updates_last_heartbeat(view):
    view._apply(_fields(host="giga", event="heartbeat", ts=1000.0))
    assert view.state["giga"].last_heartbeat_ts == 1000.0


def test_apply_pod_ready_adds_pod(view):
    view._apply(
        _fields(host="giga", event="pod_ready", pod_name="vllm-giga-0", namespace="ai-cluster")
    )
    assert "ai-cluster/vllm-giga-0" in view.state["giga"].ready_pods


def test_apply_pod_notready_removes_pod(view):
    view._apply(_fields(host="giga", event="pod_ready", pod_name="p1", namespace="ai-cluster"))
    view._apply(_fields(host="giga", event="pod_notready", pod_name="p1", namespace="ai-cluster"))
    assert "ai-cluster/p1" not in view.state["giga"].ready_pods


def test_apply_pod_crashloop_tracked(view):
    view._apply(_fields(host="mega", event="pod_crashloop", pod_name="p1", namespace="ai-cluster"))
    assert "ai-cluster/p1" in view.state["mega"].crashloop_pods


def test_apply_vram_high_then_normal(view):
    view._apply(_fields(host="mega", event="vram_high", gpu_index=0))
    assert 0 in view.state["mega"].vram_high_gpus
    view._apply(_fields(host="mega", event="vram_normal", gpu_index=0))
    assert 0 not in view.state["mega"].vram_high_gpus


def test_apply_pod_restart_tracks_count(view):
    view._apply(
        _fields(
            host="giga", event="pod_restart", pod_name="p1", namespace="ai-cluster", restart_count=5
        )
    )
    assert view.state["giga"].restart_counts["ai-cluster/p1"] == 5


def test_is_host_healthy_fresh_heartbeat(view):
    view._apply(_fields(host="giga", event="heartbeat", ts=time.time()))
    assert view.is_host_healthy("giga") is True


def test_is_host_healthy_stale_heartbeat(view):
    view._apply(_fields(host="giga", event="heartbeat", ts=time.time() - 10_000))
    assert view.is_host_healthy("giga") is False


def test_is_host_healthy_crashloop_blocks(view):
    view._apply(_fields(host="giga", event="heartbeat", ts=time.time()))
    view._apply(_fields(host="giga", event="pod_crashloop", pod_name="p1", namespace="ai-cluster"))
    assert view.is_host_healthy("giga") is False


def test_can_schedule_unknown_host_defers(view):
    assert view.can_schedule("nowhere") is True


def test_can_schedule_crashloop_returns_false(view):
    view._apply(_fields(host="giga", event="heartbeat", ts=time.time()))
    view._apply(_fields(host="giga", event="pod_crashloop", pod_name="p1", namespace="ai-cluster"))
    assert view.can_schedule("giga") is False


def test_can_schedule_vram_high_blocks_specific_gpu(view):
    view._apply(_fields(host="mega", event="heartbeat", ts=time.time()))
    view._apply(_fields(host="mega", event="vram_high", gpu_index=0))
    assert view.can_schedule("mega", gpu_index=0) is False
    assert view.can_schedule("mega", gpu_index=1) is True


def test_can_schedule_vram_high_blocks_any_when_unspecified(view):
    view._apply(_fields(host="mega", event="heartbeat", ts=time.time()))
    view._apply(_fields(host="mega", event="vram_high", gpu_index=1))
    assert view.can_schedule("mega") is False


def test_snapshot_returns_deep_copy(view):
    view._apply(_fields(host="giga", event="pod_ready", pod_name="p1", namespace="ai-cluster"))
    snap = view.snapshot()
    assert isinstance(snap["giga"], HostState)
    snap["giga"].ready_pods.add("ai-cluster/fake")
    assert "ai-cluster/fake" not in view.state["giga"].ready_pods


def test_is_gpu_busy(view):
    view._apply(_fields(host="giga", event="vram_high", gpu_index=0))
    assert view.is_gpu_busy("giga", 0) is True
    assert view.is_gpu_busy("giga", 1) is False
