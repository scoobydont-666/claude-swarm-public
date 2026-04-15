"""Performance Rating System — track and score cluster member performance.

Provides:
- Per-task metric recording (duration, success, error type, tokens)
- Composite host ratings (0-1000 scale, 500 = neutral)
- On-demand benchmarks (SSH probe, GPU, Ollama health)
- On-join lightweight probes
- Scored host selection for dispatch routing

Ratings use exponential decay (10-day half-life) so stale data loses influence.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from util import now_iso as _now_iso

logger = logging.getLogger(__name__)

DB_PATH = Path("/opt/claude-swarm/data/agents.db")

NEUTRAL_SCORE = 500.0
DECAY_HALF_LIFE_DAYS = 10.0

# Scoring weights for composite rating
WEIGHT_COMPLETION = 0.35
WEIGHT_DURATION = 0.25
WEIGHT_ERROR_RATE = 0.20
WEIGHT_THROUGHPUT = 0.20

# Scoring weights for host selection
SELECT_WEIGHT_CAPABILITY = 0.20
SELECT_WEIGHT_PERFORMANCE = 0.35
SELECT_WEIGHT_AVAILABILITY = 0.25
SELECT_WEIGHT_HW_SUITABILITY = 0.20


def _ensure_tables() -> None:
    """Create performance tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS performance_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            hostname TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_seconds REAL,
            success INTEGER DEFAULT 1,
            error_type TEXT DEFAULT '',
            token_count INTEGER DEFAULT 0,
            model_used TEXT DEFAULT '',
            task_complexity TEXT DEFAULT '',
            estimated_minutes REAL DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS host_ratings (
            hostname TEXT PRIMARY KEY,
            composite_score REAL DEFAULT 500.0,
            completion_rate REAL DEFAULT 1.0,
            avg_duration_ratio REAL DEFAULT 1.0,
            error_rate REAL DEFAULT 0.0,
            throughput_per_hour REAL DEFAULT 0.0,
            last_computed TEXT,
            task_count INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_perf_hostname
        ON performance_metrics(hostname)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_perf_started
        ON performance_metrics(started_at)
    """)

    conn.commit()
    conn.close()


_ensure_tables()


@dataclass
class TaskMetric:
    """A single task performance measurement."""

    task_id: str
    hostname: str
    started_at: str
    completed_at: str = ""
    duration_seconds: float = 0.0
    success: bool = True
    error_type: str = ""
    token_count: int = 0
    model_used: str = ""
    task_complexity: str = ""
    estimated_minutes: float = 0.0


@dataclass
class HostRating:
    """Composite performance rating for a host."""

    hostname: str
    composite_score: float = NEUTRAL_SCORE
    completion_rate: float = 1.0
    avg_duration_ratio: float = 1.0
    error_rate: float = 0.0
    throughput_per_hour: float = 0.0
    last_computed: str = ""
    task_count: int = 0


@dataclass
class BenchmarkResult:
    """Result of an on-demand host benchmark."""

    hostname: str
    reachable: bool = False
    ssh_latency_ms: float = 0.0
    disk_latency_ms: float = 0.0
    gpu_available: bool = False
    gpu_vram_free_mb: int = 0
    ollama_healthy: bool = False
    claude_available: bool = False
    timestamp: str = ""


def record_metric(metric: TaskMetric) -> None:
    """Record a task performance metric."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO performance_metrics
        (task_id, hostname, started_at, completed_at, duration_seconds,
         success, error_type, token_count, model_used, task_complexity,
         estimated_minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            metric.task_id,
            metric.hostname,
            metric.started_at,
            metric.completed_at,
            metric.duration_seconds,
            1 if metric.success else 0,
            metric.error_type,
            metric.token_count,
            metric.model_used,
            metric.task_complexity,
            metric.estimated_minutes,
        ),
    )

    conn.commit()
    conn.close()


def record_dispatch_start(
    task_id: str,
    hostname: str,
    model: str = "",
    complexity: str = "",
    estimated_minutes: float = 0,
) -> str:
    """Record when a dispatch starts. Returns the started_at timestamp."""
    ts = _now_iso()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO performance_metrics
        (task_id, hostname, started_at, model_used, task_complexity, estimated_minutes)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (task_id, hostname, ts, model, complexity, estimated_minutes),
    )

    conn.commit()
    conn.close()
    return ts


def record_dispatch_end(
    task_id: str,
    hostname: str,
    success: bool,
    error_type: str = "",
    token_count: int = 0,
) -> None:
    """Record when a dispatch completes."""
    now = _now_iso()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Find the matching start record
    cursor.execute(
        """
        SELECT id, started_at FROM performance_metrics
        WHERE task_id = ? AND hostname = ? AND completed_at IS NULL
        ORDER BY started_at DESC LIMIT 1
    """,
        (task_id, hostname),
    )
    row = cursor.fetchone()

    if row:
        metric_id, started_at = row
        # Calculate duration
        try:
            start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
            duration = (end_dt - start_dt).total_seconds()
        except (ValueError, TypeError):
            duration = 0.0

        cursor.execute(
            """
            UPDATE performance_metrics
            SET completed_at = ?, duration_seconds = ?, success = ?,
                error_type = ?, token_count = ?
            WHERE id = ?
        """,
            (now, duration, 1 if success else 0, error_type, token_count, metric_id),
        )
    else:
        # No matching start — insert a complete record
        record_metric(
            TaskMetric(
                task_id=task_id,
                hostname=hostname,
                started_at=now,
                completed_at=now,
                success=success,
                error_type=error_type,
                token_count=token_count,
            )
        )

    conn.commit()
    conn.close()


def _decay_weight(age_days: float) -> float:
    """Exponential decay weight. Returns 0-1."""
    return math.exp(-0.693 * age_days / DECAY_HALF_LIFE_DAYS)


def compute_rating(hostname: str) -> HostRating:
    """Compute the composite performance rating for a host.

    Uses exponentially-weighted metrics with a 10-day half-life.
    New hosts start at 500 (neutral).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get all metrics for this host
    cursor.execute(
        """
        SELECT * FROM performance_metrics
        WHERE hostname = ? AND completed_at IS NOT NULL
        ORDER BY started_at DESC
    """,
        (hostname,),
    )
    rows = cursor.fetchall()

    if not rows:
        rating = HostRating(hostname=hostname, last_computed=_now_iso())
        _save_rating(conn, rating)
        conn.close()
        return rating

    now = datetime.now(timezone.utc)
    weighted_success = 0.0
    weighted_total = 0.0
    weighted_duration_ratio = 0.0
    duration_weight_total = 0.0
    error_count = 0.0
    total_weight = 0.0
    total_duration_hours = 0.0

    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            age_days = (now - started).total_seconds() / 86400
        except (ValueError, TypeError):
            age_days = 30.0

        w = _decay_weight(age_days)

        # Completion rate (weighted)
        weighted_total += w
        if row["success"]:
            weighted_success += w
        else:
            error_count += w

        # Duration ratio (actual vs estimated)
        est = row["estimated_minutes"] or 0
        dur = row["duration_seconds"] or 0
        if est > 0 and dur > 0:
            ratio = (dur / 60.0) / est  # <1 = faster than estimated
            weighted_duration_ratio += ratio * w
            duration_weight_total += w

        total_weight += w
        total_duration_hours += (dur or 0) / 3600.0

    completion_rate = weighted_success / weighted_total if weighted_total > 0 else 1.0
    avg_duration_ratio = (
        weighted_duration_ratio / duration_weight_total
        if duration_weight_total > 0
        else 1.0
    )
    error_rate = error_count / weighted_total if weighted_total > 0 else 0.0

    # Throughput: completed tasks per hour of wall-clock time
    completed_count = sum(1 for r in rows if r["success"])
    if total_duration_hours > 0:
        throughput = completed_count / total_duration_hours
    else:
        throughput = 0.0

    # Composite score (0-1000)
    # completion_rate: 1.0 = perfect → 1000 points contribution
    completion_score = completion_rate * 1000
    # duration_ratio: 1.0 = on time, <1 = faster → bonus, >1 = slower → penalty
    duration_score = max(0, min(1000, 1000 * (2.0 - avg_duration_ratio)))
    # error_rate: 0.0 = perfect → 1000, 1.0 = all errors → 0
    error_score = (1.0 - error_rate) * 1000
    # throughput: normalize to 0-1000 (cap at 2 tasks/hour = 1000)
    throughput_score = min(1000, throughput * 500)

    composite = (
        WEIGHT_COMPLETION * completion_score
        + WEIGHT_DURATION * duration_score
        + WEIGHT_ERROR_RATE * error_score
        + WEIGHT_THROUGHPUT * throughput_score
    )

    rating = HostRating(
        hostname=hostname,
        composite_score=round(composite, 1),
        completion_rate=round(completion_rate, 3),
        avg_duration_ratio=round(avg_duration_ratio, 2),
        error_rate=round(error_rate, 3),
        throughput_per_hour=round(throughput, 2),
        last_computed=_now_iso(),
        task_count=len(rows),
    )

    _save_rating(conn, rating)
    conn.close()
    return rating


def _save_rating(conn: sqlite3.Connection, rating: HostRating) -> None:
    """Save a host rating to the database."""
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO host_ratings
        (hostname, composite_score, completion_rate, avg_duration_ratio,
         error_rate, throughput_per_hour, last_computed, task_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hostname) DO UPDATE SET
            composite_score=excluded.composite_score,
            completion_rate=excluded.completion_rate,
            avg_duration_ratio=excluded.avg_duration_ratio,
            error_rate=excluded.error_rate,
            throughput_per_hour=excluded.throughput_per_hour,
            last_computed=excluded.last_computed,
            task_count=excluded.task_count
    """,
        (
            rating.hostname,
            rating.composite_score,
            rating.completion_rate,
            rating.avg_duration_ratio,
            rating.error_rate,
            rating.throughput_per_hour,
            rating.last_computed,
            rating.task_count,
        ),
    )
    conn.commit()


def get_rating(hostname: str) -> HostRating:
    """Get the cached rating for a host, or compute if stale/missing."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM host_ratings WHERE hostname = ?", (hostname,))
    row = cursor.fetchone()
    conn.close()

    if row:
        last = row["last_computed"] or ""
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if age_hours < 1:  # Cache for 1 hour
                return HostRating(
                    hostname=row["hostname"],
                    composite_score=row["composite_score"],
                    completion_rate=row["completion_rate"],
                    avg_duration_ratio=row["avg_duration_ratio"],
                    error_rate=row["error_rate"],
                    throughput_per_hour=row["throughput_per_hour"],
                    last_computed=row["last_computed"],
                    task_count=row["task_count"],
                )
        except (ValueError, TypeError):
            pass

    return compute_rating(hostname)


def get_all_ratings() -> list[HostRating]:
    """Get ratings for all known hosts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT DISTINCT hostname FROM performance_metrics")
    hostnames = [row["hostname"] for row in cursor.fetchall()]

    # Also include hosts in agents table that may not have metrics yet
    try:
        cursor.execute("SELECT hostname FROM agents")
        for row in cursor.fetchall():
            if row["hostname"] not in hostnames:
                hostnames.append(row["hostname"])
    except sqlite3.OperationalError:
        pass  # agents table may not exist in test environments

    conn.close()

    return [get_rating(h) for h in sorted(hostnames)]


def benchmark_host(hostname: str, ip: str, ssh_user: str = "josh") -> BenchmarkResult:
    """Run an on-demand benchmark probe against a host.

    Tests: SSH connectivity, disk latency, GPU presence, Ollama health,
    Claude CLI availability.
    """
    result = BenchmarkResult(hostname=hostname, timestamp=_now_iso())

    # SSH connectivity + latency
    try:
        start = time.monotonic()
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                f"{ssh_user}@{ip}",
                "echo ok",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        elapsed = (time.monotonic() - start) * 1000
        result.reachable = proc.returncode == 0
        result.ssh_latency_ms = round(elapsed, 1)
    except (subprocess.TimeoutExpired, Exception):
        result.reachable = False
        return result

    if not result.reachable:
        return result

    # Batch probe: GPU, Ollama, Claude, disk
    probe_script = """
echo "DISK_START"
dd if=/dev/zero of=/tmp/.bench_test bs=4k count=256 oflag=dsync 2>&1 | tail -1
rm -f /tmp/.bench_test
echo "DISK_END"

echo "GPU_START"
nvidia-smi --query-gpu=memory.free,memory.total --format=csv,noheader,nounits 2>/dev/null || echo "NO_GPU"
echo "GPU_END"

echo "OLLAMA_START"
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:11434/api/tags 2>/dev/null || echo "NO_OLLAMA"
echo "OLLAMA_END"

echo "CLAUDE_START"
which claude 2>/dev/null && claude --version 2>/dev/null | head -1 || echo "NO_CLAUDE"
echo "CLAUDE_END"
"""
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                f"{ssh_user}@{ip}",
                probe_script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = proc.stdout

        # Parse GPU
        if "NO_GPU" not in output and "GPU_START" in output:
            gpu_section = output.split("GPU_START")[1].split("GPU_END")[0].strip()
            if gpu_section and "," in gpu_section:
                parts = gpu_section.split("\n")[0].split(",")
                result.gpu_available = True
                result.gpu_vram_free_mb = int(parts[0].strip())

        # Parse Ollama
        if "OLLAMA_START" in output:
            ollama_section = (
                output.split("OLLAMA_START")[1].split("OLLAMA_END")[0].strip()
            )
            result.ollama_healthy = ollama_section.strip() == "200"

        # Parse Claude
        if "CLAUDE_START" in output:
            claude_section = (
                output.split("CLAUDE_START")[1].split("CLAUDE_END")[0].strip()
            )
            result.claude_available = (
                "NO_CLAUDE" not in claude_section and claude_section != ""
            )

        # Parse disk latency (rough)
        if "DISK_START" in output:
            disk_section = output.split("DISK_START")[1].split("DISK_END")[0].strip()
            # dd output like "1048576 bytes (1.0 MB, 1.0 MiB) copied, 0.123 s, 8.5 MB/s"
            if "copied" in disk_section:
                try:
                    time_part = disk_section.split("copied,")[1].split("s,")[0].strip()
                    result.disk_latency_ms = round(float(time_part) * 1000, 1)
                except (IndexError, ValueError):
                    pass

    except (subprocess.TimeoutExpired, Exception) as e:
        logger.warning(f"Benchmark probe failed for {hostname}: {e}")

    return result


def on_join_probe(hostname: str, ip: str, ssh_user: str = "josh") -> BenchmarkResult:
    """Lightweight probe run when an agent joins the cluster.

    Only tests SSH + GPU + Ollama (skip disk benchmark for speed).
    """
    result = BenchmarkResult(hostname=hostname, timestamp=_now_iso())

    try:
        start = time.monotonic()
        probe = 'echo GPU_VAL=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null || echo NO); echo OLLAMA_VAL=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:11434/api/tags 2>/dev/null || echo NO)'
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                f"{ssh_user}@{ip}",
                probe,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        elapsed = (time.monotonic() - start) * 1000
        result.reachable = proc.returncode == 0
        result.ssh_latency_ms = round(elapsed, 1)

        output = proc.stdout
        for line in output.strip().split("\n"):
            line = line.strip()
            if line.startswith("GPU_VAL="):
                val = line[8:]
                if val != "NO" and val.isdigit():
                    result.gpu_available = True
                    result.gpu_vram_free_mb = int(val)
            elif line.startswith("OLLAMA_VAL="):
                val = line[11:]
                result.ollama_healthy = val == "200"

    except (subprocess.TimeoutExpired, Exception):
        result.reachable = False

    return result


def scored_host_selection(
    fleet: dict,
    requires: list[str],
    task_complexity: str = "",
) -> list[tuple[str, float]]:
    """Score all fleet hosts and return ranked list.

    Args:
        fleet: Fleet configuration dict (hostname -> config)
        requires: Required capabilities for the task
        task_complexity: Task complexity level (trivial/simple/moderate/complex/exploratory)

    Returns:
        List of (hostname, score) tuples, sorted by score descending.
        Empty list if no host matches required capabilities.
    """
    candidates = []

    for hostname, config in fleet.items():
        caps = set(config.get("capabilities", []))
        required = set(requires)

        # Must meet capability requirements
        if not required.issubset(caps):
            continue

        # Capability match score: bonus for extra capabilities
        cap_score = 500 + min(500, len(caps - required) * 100)

        # Performance rating
        rating = get_rating(hostname)
        perf_score = rating.composite_score

        # Availability: prefer hosts that aren't currently overloaded
        # (Simple proxy: if the host has a recent error, penalize)
        avail_score = 500.0
        if rating.error_rate > 0.3:
            avail_score = 200.0
        elif rating.error_rate < 0.05:
            avail_score = 800.0

        # Hardware suitability: match task complexity to host power
        hw_score = 500.0
        host_has_gpu = "gpu" in caps
        if task_complexity in ("complex", "exploratory") and host_has_gpu:
            hw_score = 800.0
        elif task_complexity in ("trivial", "simple") and not host_has_gpu:
            hw_score = 700.0  # Don't waste GPU on simple tasks

        # Composite selection score
        total = (
            SELECT_WEIGHT_CAPABILITY * cap_score
            + SELECT_WEIGHT_PERFORMANCE * perf_score
            + SELECT_WEIGHT_AVAILABILITY * avail_score
            + SELECT_WEIGHT_HW_SUITABILITY * hw_score
        )

        candidates.append((hostname, round(total, 1)))

    # Sort by score descending
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def get_metrics_for_host(hostname: str, limit: int = 50) -> list[dict]:
    """Get recent performance metrics for a host."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT * FROM performance_metrics
        WHERE hostname = ?
        ORDER BY started_at DESC
        LIMIT ?
    """,
        (hostname, limit),
    )

    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows
