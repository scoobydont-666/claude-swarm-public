"""
VRAM-Aware GPU Scheduler — SQLite-backed GPU allocation with model-size routing.

Replaces the lockfile-based gpu_slots.py with a proper scheduler that:
- Knows VRAM requirements per model
- Allocates GPUs based on available VRAM
- Supports multi-GPU allocation for large models (70B+)
- Tracks allocations in SQLite (not lockfiles)
- Supports host exclusion (e.g., gpu-server-1 training)
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from gpu_discovery import (
    GpuInfo, discover_fleet, save_inventory,
    get_available_gpus, allocate_gpu, release_gpu,
    find_best_gpu_for_model, init_db, MODEL_VRAM_REQUIREMENTS,
    DEFAULT_DB_PATH,
)

logger = logging.getLogger(__name__)

# Model-size tiers for tensor parallelism decisions
MODEL_SIZE_TIERS = {
    "small": {"max_params": "14B", "gpus_needed": 1, "min_vram_mb": 10000},
    "medium": {"max_params": "32B", "gpus_needed": 1, "min_vram_mb": 20000},
    "large": {"max_params": "70B", "gpus_needed": 2, "min_vram_mb": 40000},
    "xlarge": {"max_params": "405B", "gpus_needed": 4, "min_vram_mb": 80000},
}


@dataclass
class ScheduleResult:
    """Result of a GPU scheduling request."""
    success: bool
    host: Optional[str] = None
    gpu_indices: list[int] = None
    model_name: Optional[str] = None
    vram_allocated_mb: int = 0
    reason: str = ""

    def __post_init__(self):
        if self.gpu_indices is None:
            self.gpu_indices = []


class GpuScheduler:
    """VRAM-aware GPU scheduler with SQLite-backed state."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        exclude_hosts: list[str] | None = None,
        auto_refresh_interval: int = 300,  # 5 minutes
    ):
        self.db_path = db_path
        self.exclude_hosts = exclude_hosts or []
        self.auto_refresh_interval = auto_refresh_interval
        self._last_refresh = 0.0

    def refresh_inventory(self, force: bool = False):
        """Refresh GPU inventory from fleet if stale."""
        now = time.time()
        if not force and (now - self._last_refresh) < self.auto_refresh_interval:
            return

        logger.info("Refreshing GPU inventory from fleet...")
        inventories = discover_fleet(exclude_hosts=self.exclude_hosts)
        save_inventory(inventories, self.db_path)
        self._last_refresh = now
        total_gpus = sum(inv.gpu_count for inv in inventories if inv.reachable and not inv.error)
        logger.info(f"GPU inventory refreshed: {total_gpus} GPUs across {len(inventories)} hosts")

    def schedule(
        self,
        task_id: str,
        model_name: str = "",
        required_vram_mb: int = 0,
        prefer_host: str = "",
    ) -> ScheduleResult:
        """
        Schedule a GPU for a task.

        Args:
            task_id: Unique task identifier
            model_name: Ollama model name (used to look up VRAM requirements)
            required_vram_mb: Override VRAM requirement (0 = auto from model_name)
            prefer_host: Prefer this host if available

        Returns:
            ScheduleResult with allocation details
        """
        self.refresh_inventory()

        # Determine VRAM requirement
        if required_vram_mb <= 0 and model_name:
            required_vram_mb = MODEL_VRAM_REQUIREMENTS.get(model_name, 8000)

        if required_vram_mb <= 0:
            required_vram_mb = 8000  # safe default

        # Try preferred host first
        if prefer_host:
            available = get_available_gpus(
                min_vram_mb=required_vram_mb,
                exclude_hosts=self.exclude_hosts,
                db_path=self.db_path,
            )
            preferred = [g for g in available if g.host.upper() == prefer_host.upper()]
            if preferred:
                gpu = preferred[0]
                if allocate_gpu(gpu.host, gpu.gpu_index, task_id, model_name, required_vram_mb, self.db_path):
                    return ScheduleResult(
                        success=True, host=gpu.host, gpu_indices=[gpu.gpu_index],
                        model_name=model_name, vram_allocated_mb=required_vram_mb,
                        reason=f"Allocated on preferred host {gpu.host}",
                    )

        # Find best GPU across fleet (use explicit VRAM requirement)
        available = get_available_gpus(min_vram_mb=required_vram_mb, exclude_hosts=self.exclude_hosts, db_path=self.db_path)
        # Sort by least excess VRAM (tight packing)
        available.sort(key=lambda g: g.vram_free_mb)
        gpu = available[0] if available else None
        if gpu and allocate_gpu(gpu.host, gpu.gpu_index, task_id, model_name, required_vram_mb, self.db_path):
            return ScheduleResult(
                success=True, host=gpu.host, gpu_indices=[gpu.gpu_index],
                model_name=model_name, vram_allocated_mb=required_vram_mb,
                reason=f"Best-fit allocation on {gpu.host} GPU {gpu.gpu_index} ({gpu.vram_free_mb}MB free)",
            )

        # Check if multi-GPU is needed
        if required_vram_mb > 16000:
            result = self._try_multi_gpu(task_id, model_name, required_vram_mb)
            if result.success:
                return result

        return ScheduleResult(
            success=False,
            reason=f"No GPU available with {required_vram_mb}MB VRAM (excluded: {self.exclude_hosts})",
        )

    def _try_multi_gpu(self, task_id: str, model_name: str, required_vram_mb: int) -> ScheduleResult:
        """Try to allocate multiple GPUs on the same host for tensor parallelism."""
        conn = init_db(self.db_path)
        try:
            # Group available GPUs by host
            all_gpus = get_available_gpus(min_vram_mb=0, exclude_hosts=self.exclude_hosts, db_path=self.db_path)
            by_host: dict[str, list[GpuInfo]] = {}
            for gpu in all_gpus:
                by_host.setdefault(gpu.host, []).append(gpu)

            # Find a host where combined VRAM meets requirement
            for host, gpus in by_host.items():
                combined_vram = sum(g.vram_free_mb for g in gpus)
                if combined_vram >= required_vram_mb and len(gpus) >= 2:
                    # Allocate all GPUs on this host
                    allocated_indices = []
                    for gpu in gpus:
                        if allocate_gpu(host, gpu.gpu_index, task_id, model_name,
                                       required_vram_mb // len(gpus), self.db_path):
                            allocated_indices.append(gpu.gpu_index)

                    if len(allocated_indices) >= 2:
                        return ScheduleResult(
                            success=True, host=host, gpu_indices=allocated_indices,
                            model_name=model_name, vram_allocated_mb=combined_vram,
                            reason=f"Multi-GPU on {host}: {len(allocated_indices)} GPUs, {combined_vram}MB combined",
                        )
                    else:
                        # Rollback partial allocation
                        for idx in allocated_indices:
                            release_gpu(host, idx, self.db_path)

            return ScheduleResult(success=False, reason="No host has enough combined VRAM for multi-GPU")
        finally:
            conn.close()

    def release(self, host: str, gpu_indices: list[int]):
        """Release GPU allocation(s)."""
        for idx in gpu_indices:
            release_gpu(host, idx, self.db_path)
            logger.info(f"Released GPU {host}:{idx}")

    def get_status(self) -> dict:
        """Get current GPU allocation status."""
        conn = init_db(self.db_path)
        try:
            inventory = conn.execute("SELECT host, gpu_index, gpu_model, vram_total_mb, vram_free_mb FROM gpu_inventory").fetchall()
            allocations = conn.execute(
                "SELECT host, gpu_index, task_id, model_name, vram_required_mb, allocated_at FROM gpu_allocations WHERE released_at IS NULL"
            ).fetchall()

            return {
                "total_gpus": len(inventory),
                "allocated_gpus": len(allocations),
                "available_gpus": len(inventory) - len(allocations),
                "inventory": [
                    {"host": r[0], "gpu_index": r[1], "model": r[2], "vram_total_mb": r[3], "vram_free_mb": r[4]}
                    for r in inventory
                ],
                "allocations": [
                    {"host": r[0], "gpu_index": r[1], "task_id": r[2], "model": r[3], "vram_mb": r[4], "since": r[5]}
                    for r in allocations
                ],
                "excluded_hosts": self.exclude_hosts,
            }
        finally:
            conn.close()
