"""
GPU Discovery — Dynamic fleet GPU inventory via SSH nvidia-smi probing.

Replaces hardcoded 2-slot GIGA model with dynamic discovery across all fleet hosts.
Stores inventory in SQLite for fast lookup by the GPU scheduler.
"""

import logging
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# nvidia-smi query fields
NVIDIA_SMI_CMD = [
    "nvidia-smi",
    "--query-gpu=index,name,memory.total,memory.free,memory.used,utilization.gpu",
    "--format=csv,noheader,nounits",
]

# Default fleet hosts with GPUs
DEFAULT_GPU_HOSTS = ["GIGA", "MEGA", "MECHA", "MONGO", "miniboss"]

# SQLite DB for GPU inventory
DEFAULT_DB_PATH = "/opt/swarm/gpu/inventory.db"


@dataclass
class GpuInfo:
    """Single GPU on a host."""

    host: str
    gpu_index: int
    gpu_model: str
    vram_total_mb: int
    vram_free_mb: int
    vram_used_mb: int
    utilization_pct: int
    discovered_at: float = field(default_factory=time.time)

    @property
    def vram_available_mb(self) -> int:
        return self.vram_free_mb

    def can_fit_model(self, required_vram_mb: int) -> bool:
        """Check if this GPU has enough free VRAM for a model."""
        return self.vram_free_mb >= required_vram_mb


@dataclass
class HostGpuInventory:
    """All GPUs on a single host."""

    host: str
    gpus: list[GpuInfo] = field(default_factory=list)
    reachable: bool = True
    error: str | None = None
    discovered_at: float = field(default_factory=time.time)

    @property
    def gpu_count(self) -> int:
        return len(self.gpus)

    @property
    def total_vram_mb(self) -> int:
        return sum(g.vram_total_mb for g in self.gpus)

    @property
    def free_vram_mb(self) -> int:
        return sum(g.vram_free_mb for g in self.gpus)


def probe_host(host: str, timeout: int = 10) -> HostGpuInventory:
    """Probe a single host via SSH nvidia-smi and return GPU inventory."""
    try:
        import socket

        if host.lower() in ("localhost", socket.gethostname().lower()):
            # Local host — no SSH needed
            result = subprocess.run(
                NVIDIA_SMI_CMD,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    f"josh@{host}",
                ]
                + NVIDIA_SMI_CMD,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

        if result.returncode != 0:
            return HostGpuInventory(
                host=host, reachable=True, error=f"nvidia-smi failed: {result.stderr.strip()}"
            )

        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append(
                    GpuInfo(
                        host=host,
                        gpu_index=int(parts[0]),
                        gpu_model=parts[1],
                        vram_total_mb=int(parts[2]),
                        vram_free_mb=int(parts[3]),
                        vram_used_mb=int(parts[4]),
                        utilization_pct=int(parts[5]),
                    )
                )

        return HostGpuInventory(host=host, gpus=gpus)

    except subprocess.TimeoutExpired:
        return HostGpuInventory(host=host, reachable=False, error="SSH timeout")
    except Exception as e:
        return HostGpuInventory(host=host, reachable=False, error=str(e))


def discover_fleet(
    hosts: list[str] | None = None,
    exclude_hosts: list[str] | None = None,
) -> list[HostGpuInventory]:
    """Discover GPUs across the entire fleet."""
    hosts = hosts or DEFAULT_GPU_HOSTS
    exclude = set(h.upper() for h in (exclude_hosts or []))

    inventories = []
    for host in hosts:
        if host.upper() in exclude:
            logger.info(f"Skipping {host} (excluded)")
            continue
        logger.info(f"Probing {host}...")
        inv = probe_host(host)
        inventories.append(inv)
        if inv.reachable and not inv.error:
            logger.info(f"  {host}: {inv.gpu_count} GPUs, {inv.total_vram_mb}MB total VRAM")
        elif inv.error:
            logger.warning(f"  {host}: {inv.error}")
        else:
            logger.warning(f"  {host}: unreachable")

    return inventories


def init_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Initialize the GPU inventory database."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpu_inventory (
            host TEXT NOT NULL,
            gpu_index INTEGER NOT NULL,
            gpu_model TEXT NOT NULL,
            vram_total_mb INTEGER NOT NULL,
            vram_free_mb INTEGER NOT NULL,
            vram_used_mb INTEGER NOT NULL,
            utilization_pct INTEGER NOT NULL,
            discovered_at REAL NOT NULL,
            PRIMARY KEY (host, gpu_index)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpu_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT NOT NULL,
            gpu_index INTEGER NOT NULL,
            task_id TEXT NOT NULL,
            model_name TEXT,
            vram_required_mb INTEGER NOT NULL,
            allocated_at REAL NOT NULL,
            released_at REAL,
            FOREIGN KEY (host, gpu_index) REFERENCES gpu_inventory(host, gpu_index)
        )
    """)
    conn.commit()
    return conn


def save_inventory(inventories: list[HostGpuInventory], db_path: str = DEFAULT_DB_PATH):
    """Save discovered GPU inventory to SQLite."""
    conn = init_db(db_path)
    try:
        for inv in inventories:
            if not inv.reachable or inv.error:
                continue
            for gpu in inv.gpus:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO gpu_inventory
                    (host, gpu_index, gpu_model, vram_total_mb, vram_free_mb,
                     vram_used_mb, utilization_pct, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        gpu.host,
                        gpu.gpu_index,
                        gpu.gpu_model,
                        gpu.vram_total_mb,
                        gpu.vram_free_mb,
                        gpu.vram_used_mb,
                        gpu.utilization_pct,
                        gpu.discovered_at,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def get_available_gpus(
    min_vram_mb: int = 0,
    exclude_hosts: list[str] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> list[GpuInfo]:
    """Get all GPUs with at least min_vram_mb free, excluding allocated ones."""
    conn = init_db(db_path)
    exclude = set(h.upper() for h in (exclude_hosts or []))

    try:
        rows = conn.execute(
            """
            SELECT g.host, g.gpu_index, g.gpu_model, g.vram_total_mb,
                   g.vram_free_mb, g.vram_used_mb, g.utilization_pct, g.discovered_at
            FROM gpu_inventory g
            LEFT JOIN gpu_allocations a ON g.host = a.host AND g.gpu_index = a.gpu_index
                AND a.released_at IS NULL
            WHERE a.id IS NULL
            AND g.vram_free_mb >= ?
            ORDER BY g.vram_free_mb DESC
        """,
            (min_vram_mb,),
        ).fetchall()

        gpus = []
        for row in rows:
            if row[0].upper() in exclude:
                continue
            gpus.append(
                GpuInfo(
                    host=row[0],
                    gpu_index=row[1],
                    gpu_model=row[2],
                    vram_total_mb=row[3],
                    vram_free_mb=row[4],
                    vram_used_mb=row[5],
                    utilization_pct=row[6],
                    discovered_at=row[7],
                )
            )
        return gpus
    finally:
        conn.close()


def allocate_gpu(
    host: str,
    gpu_index: int,
    task_id: str,
    model_name: str = "",
    vram_required_mb: int = 0,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Allocate a GPU for a task. Returns True if successful."""
    conn = init_db(db_path)
    try:
        # Check not already allocated
        existing = conn.execute(
            """
            SELECT id FROM gpu_allocations
            WHERE host = ? AND gpu_index = ? AND released_at IS NULL
        """,
            (host, gpu_index),
        ).fetchone()

        if existing:
            return False

        conn.execute(
            """
            INSERT INTO gpu_allocations (host, gpu_index, task_id, model_name, vram_required_mb, allocated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (host, gpu_index, task_id, model_name, vram_required_mb, time.time()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def release_gpu(host: str, gpu_index: int, db_path: str = DEFAULT_DB_PATH):
    """Release a GPU allocation."""
    conn = init_db(db_path)
    try:
        conn.execute(
            """
            UPDATE gpu_allocations SET released_at = ?
            WHERE host = ? AND gpu_index = ? AND released_at IS NULL
        """,
            (time.time(), host, gpu_index),
        )
        conn.commit()
    finally:
        conn.close()


# Model VRAM requirements (approximate, in MB)
MODEL_VRAM_REQUIREMENTS = {
    "qwen3:8b": 6000,
    "qwen3:14b": 10000,
    "qwen3:32b": 20000,
    "devstral:latest": 14000,
    "deepseek-r1:32b": 20000,
    "llama3.3:70b": 42000,
    "christi-14b": 10000,
    "nomic-embed-text": 1000,
}


def find_best_gpu_for_model(
    model_name: str,
    exclude_hosts: list[str] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> GpuInfo | None:
    """Find the best available GPU for a given model based on VRAM requirements."""
    required_vram = MODEL_VRAM_REQUIREMENTS.get(model_name, 8000)  # default 8GB
    available = get_available_gpus(
        min_vram_mb=required_vram, exclude_hosts=exclude_hosts, db_path=db_path
    )

    if not available:
        return None

    # Prefer GPU with least excess VRAM (tight packing)
    available.sort(key=lambda g: g.vram_free_mb)
    return available[0]
