"""Tests for GPU discovery module."""

import pytest

from src.gpu_discovery import (
    MODEL_VRAM_REQUIREMENTS,
    GpuInfo,
    HostGpuInventory,
    allocate_gpu,
    find_best_gpu_for_model,
    get_available_gpus,
    init_db,
    release_gpu,
    save_inventory,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_gpu.db")


@pytest.fixture
def sample_inventory():
    return [
        HostGpuInventory(
            host="node_reserve1",
            gpus=[
                GpuInfo(
                    host="node_reserve1",
                    gpu_index=0,
                    gpu_model="RTX 5080",
                    vram_total_mb=16303,
                    vram_free_mb=12000,
                    vram_used_mb=4303,
                    utilization_pct=10,
                ),
                GpuInfo(
                    host="node_reserve1",
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
            host="node_reserve2",
            gpus=[
                GpuInfo(
                    host="node_reserve2",
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
            host="node_mongo",
            gpus=[
                GpuInfo(
                    host="node_mongo",
                    gpu_index=0,
                    gpu_model="RTX 5080",
                    vram_total_mb=16303,
                    vram_free_mb=8000,
                    vram_used_mb=8303,
                    utilization_pct=50,
                ),
            ],
        ),
    ]


class TestGpuInfo:
    def test_vram_available(self):
        gpu = GpuInfo(
            host="TEST",
            gpu_index=0,
            gpu_model="RTX 5080",
            vram_total_mb=16000,
            vram_free_mb=10000,
            vram_used_mb=6000,
            utilization_pct=0,
        )
        assert gpu.vram_available_mb == 10000

    def test_can_fit_model(self):
        gpu = GpuInfo(
            host="TEST",
            gpu_index=0,
            gpu_model="RTX 5080",
            vram_total_mb=16000,
            vram_free_mb=10000,
            vram_used_mb=6000,
            utilization_pct=0,
        )
        assert gpu.can_fit_model(8000) is True
        assert gpu.can_fit_model(12000) is False


class TestHostGpuInventory:
    def test_gpu_count(self, sample_inventory):
        assert sample_inventory[0].gpu_count == 2
        assert sample_inventory[1].gpu_count == 1

    def test_total_vram(self, sample_inventory):
        assert sample_inventory[0].total_vram_mb == 32606

    def test_free_vram(self, sample_inventory):
        assert sample_inventory[0].free_vram_mb == 26000


class TestDatabase:
    def test_init_db(self, db_path):
        conn = init_db(db_path)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = {t[0] for t in tables}
        assert "gpu_inventory" in table_names
        assert "gpu_allocations" in table_names
        conn.close()

    def test_save_and_retrieve(self, db_path, sample_inventory):
        save_inventory(sample_inventory, db_path)
        gpus = get_available_gpus(db_path=db_path)
        assert len(gpus) == 4  # 2 on node_reserve1 + 1 node_reserve2 + 1 node_mongo

    def test_exclude_hosts(self, db_path, sample_inventory):
        save_inventory(sample_inventory, db_path)
        gpus = get_available_gpus(exclude_hosts=["node_gpu", "node_reserve1"], db_path=db_path)
        assert all(g.host != "node_reserve1" for g in gpus)
        assert len(gpus) == 2  # node_reserve2 + node_mongo only

    def test_min_vram_filter(self, db_path, sample_inventory):
        save_inventory(sample_inventory, db_path)
        gpus = get_available_gpus(min_vram_mb=11000, db_path=db_path)
        assert all(g.vram_free_mb >= 11000 for g in gpus)
        assert len(gpus) == 2  # node_reserve1 GPU 0 (12000) + node_reserve1 GPU 1 (14000)


class TestAllocation:
    def test_allocate_and_release(self, db_path, sample_inventory):
        save_inventory(sample_inventory, db_path)
        assert allocate_gpu("node_reserve1", 0, "task-1", "qwen3:14b", 10000, db_path) is True
        # GPU 0 should now be unavailable
        gpus = get_available_gpus(db_path=db_path)
        assert not any(g.host == "node_reserve1" and g.gpu_index == 0 for g in gpus)
        # Release
        release_gpu("node_reserve1", 0, db_path)
        gpus = get_available_gpus(db_path=db_path)
        assert any(g.host == "node_reserve1" and g.gpu_index == 0 for g in gpus)

    def test_double_allocate_fails(self, db_path, sample_inventory):
        save_inventory(sample_inventory, db_path)
        assert allocate_gpu("node_reserve1", 0, "task-1", db_path=db_path) is True
        assert allocate_gpu("node_reserve1", 0, "task-2", db_path=db_path) is False

    def test_find_best_gpu(self, db_path, sample_inventory):
        save_inventory(sample_inventory, db_path)
        gpu = find_best_gpu_for_model("qwen3:8b", db_path=db_path)
        assert gpu is not None
        assert gpu.vram_free_mb >= MODEL_VRAM_REQUIREMENTS["qwen3:8b"]

    def test_find_best_gpu_excludes_hosts(self, db_path, sample_inventory):
        save_inventory(sample_inventory, db_path)
        gpu = find_best_gpu_for_model(
            "qwen3:8b", exclude_hosts=["node_reserve1", "node_reserve2", "node_mongo"], db_path=db_path
        )
        assert gpu is None  # No GPUs available after excluding all hosts
