"""Tests for VRAM-aware GPU scheduler."""

import pytest

from src.gpu_discovery import (
    MODEL_VRAM_REQUIREMENTS,
    GpuInfo,
    HostGpuInventory,
    save_inventory,
)
from src.gpu_scheduler_v2 import GpuScheduler


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_scheduler.db")


@pytest.fixture
def scheduler(db_path):
    # Pre-populate inventory
    inventories = [
        HostGpuInventory(
            host="MEGA",
            gpus=[
                GpuInfo(
                    host="MEGA",
                    gpu_index=0,
                    gpu_model="RTX 5080",
                    vram_total_mb=16303,
                    vram_free_mb=14000,
                    vram_used_mb=2303,
                    utilization_pct=5,
                ),
                GpuInfo(
                    host="MEGA",
                    gpu_index=1,
                    gpu_model="RTX 5080",
                    vram_total_mb=16303,
                    vram_free_mb=14000,
                    vram_used_mb=2303,
                    utilization_pct=5,
                ),
            ],
        ),
        HostGpuInventory(
            host="MECHA",
            gpus=[
                GpuInfo(
                    host="MECHA",
                    gpu_index=0,
                    gpu_model="RTX 5060 Ti",
                    vram_total_mb=16311,
                    vram_free_mb=10000,
                    vram_used_mb=6311,
                    utilization_pct=25,
                ),
            ],
        ),
        HostGpuInventory(
            host="MONGO",
            gpus=[
                GpuInfo(
                    host="MONGO",
                    gpu_index=0,
                    gpu_model="RTX 5080",
                    vram_total_mb=16303,
                    vram_free_mb=12000,
                    vram_used_mb=4303,
                    utilization_pct=10,
                ),
            ],
        ),
    ]
    save_inventory(inventories, db_path)

    sched = GpuScheduler(db_path=db_path, exclude_hosts=["GIGA"])
    sched._last_refresh = 9999999999.0  # prevent auto-refresh (no SSH in tests)
    return sched


class TestScheduleBasic:
    def test_schedule_small_model(self, scheduler):
        result = scheduler.schedule("task-1", model_name="qwen3:8b")
        assert result.success is True
        assert result.host is not None
        assert len(result.gpu_indices) == 1

    def test_schedule_respects_vram(self, scheduler):
        result = scheduler.schedule("task-2", model_name="qwen3:14b")
        assert result.success is True
        assert result.vram_allocated_mb >= MODEL_VRAM_REQUIREMENTS["qwen3:14b"]

    def test_schedule_preferred_host(self, scheduler):
        result = scheduler.schedule("task-3", model_name="qwen3:8b", prefer_host="MECHA")
        assert result.success is True
        assert result.host == "MECHA"

    def test_schedule_excludes_giga(self, scheduler):
        # Schedule all 4 GPUs (2 MEGA + 1 MECHA + 1 MONGO)
        results = []
        for i in range(4):
            r = scheduler.schedule(f"task-fill-{i}", model_name="qwen3:8b")
            results.append(r)
        assert all(r.host != "GIGA" for r in results if r.success)

    def test_schedule_fails_when_full(self, scheduler):
        # Allocate all GPUs
        for i in range(4):
            scheduler.schedule(f"task-saturate-{i}", model_name="qwen3:8b")
        # Next should fail
        result = scheduler.schedule("task-overflow", model_name="qwen3:8b")
        assert result.success is False


class TestMultiGpu:
    def test_multi_gpu_allocation(self, scheduler):
        # Request more VRAM than single GPU has
        result = scheduler.schedule("task-big", required_vram_mb=25000)
        assert result.success is True
        assert len(result.gpu_indices) >= 2
        assert result.host == "MEGA"  # only host with 2 GPUs

    def test_multi_gpu_fails_on_single_gpu_host(self, scheduler):
        # Allocate MEGA's GPUs first
        scheduler.schedule("task-mega-0", model_name="qwen3:8b", prefer_host="MEGA")
        scheduler.schedule("task-mega-1", model_name="qwen3:8b", prefer_host="MEGA")
        # Now try multi-GPU — should fail (only single-GPU hosts left)
        result = scheduler.schedule("task-multi-fail", required_vram_mb=25000)
        assert result.success is False


class TestRelease:
    def test_release_makes_gpu_available(self, scheduler):
        r1 = scheduler.schedule("task-release-1", model_name="qwen3:8b", prefer_host="MECHA")
        assert r1.success is True
        # MECHA should be full now
        r2 = scheduler.schedule("task-release-2", model_name="qwen3:8b", prefer_host="MECHA")
        assert r2.success is False or r2.host != "MECHA"
        # Release
        scheduler.release(r1.host, r1.gpu_indices)
        # Should be available again
        r3 = scheduler.schedule("task-release-3", model_name="qwen3:8b", prefer_host="MECHA")
        assert r3.success is True
        assert r3.host == "MECHA"


class TestStatus:
    def test_status_format(self, scheduler):
        scheduler.schedule("task-status", model_name="qwen3:8b")
        status = scheduler.get_status()
        assert "total_gpus" in status
        assert "allocated_gpus" in status
        assert "available_gpus" in status
        assert "inventory" in status
        assert "allocations" in status
        assert status["allocated_gpus"] == 1
        assert status["total_gpus"] == 4

    def test_status_shows_exclusions(self, scheduler):
        status = scheduler.get_status()
        assert "GIGA" in status["excluded_hosts"]
