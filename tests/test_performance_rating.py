"""Tests for performance_rating module."""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Use temp DB for testing
_test_db = None


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    """Use a temporary database for each test."""
    global _test_db
    db_path = tmp_path / "test_agents.db"
    _test_db = db_path

    import performance_rating

    performance_rating.DB_PATH = db_path
    performance_rating._ensure_tables()
    yield db_path


class TestTaskMetricRecording:
    """Test recording dispatch metrics."""

    def test_record_metric(self, temp_db):
        from performance_rating import record_metric, TaskMetric, get_metrics_for_host

        metric = TaskMetric(
            task_id="task-001",
            hostname="node_gpu",
            started_at="2026-03-27T10:00:00Z",
            completed_at="2026-03-27T10:05:00Z",
            duration_seconds=300.0,
            success=True,
            model_used="sonnet",
            task_complexity="moderate",
            estimated_minutes=10.0,
        )
        record_metric(metric)

        metrics = get_metrics_for_host("node_gpu")
        assert len(metrics) == 1
        assert metrics[0]["task_id"] == "task-001"
        assert metrics[0]["duration_seconds"] == 300.0
        assert metrics[0]["success"] == 1

    def test_record_dispatch_start_end(self, temp_db):
        from performance_rating import (
            record_dispatch_start,
            record_dispatch_end,
            get_metrics_for_host,
        )

        ts = record_dispatch_start("task-002", "node_primary", model="haiku")
        assert ts  # returns timestamp

        record_dispatch_end("task-002", "node_primary", success=True)

        metrics = get_metrics_for_host("node_primary")
        assert len(metrics) == 1
        assert metrics[0]["success"] == 1
        assert metrics[0]["completed_at"] is not None
        assert metrics[0]["duration_seconds"] >= 0

    def test_record_dispatch_end_without_start(self, temp_db):
        from performance_rating import record_dispatch_end, get_metrics_for_host

        record_dispatch_end("task-orphan", "node_gpu", success=False, error_type="timeout")

        metrics = get_metrics_for_host("node_gpu")
        assert len(metrics) == 1
        assert metrics[0]["success"] == 0
        assert metrics[0]["error_type"] == "timeout"

    def test_record_dispatch_end_with_error(self, temp_db):
        from performance_rating import (
            record_dispatch_start,
            record_dispatch_end,
            get_metrics_for_host,
        )

        record_dispatch_start("task-fail", "node_gpu")
        record_dispatch_end("task-fail", "node_gpu", success=False, error_type="rate_limit")

        metrics = get_metrics_for_host("node_gpu")
        assert len(metrics) == 1
        assert metrics[0]["error_type"] == "rate_limit"


class TestRatingComputation:
    """Test composite rating computation."""

    def test_new_host_gets_neutral_rating(self, temp_db):
        from performance_rating import compute_rating, NEUTRAL_SCORE

        rating = compute_rating("new_host")
        assert rating.composite_score == NEUTRAL_SCORE
        assert rating.task_count == 0

    def test_perfect_host_gets_high_rating(self, temp_db):
        from performance_rating import record_metric, TaskMetric, compute_rating

        # Record 10 successful tasks
        for i in range(10):
            record_metric(
                TaskMetric(
                    task_id=f"task-{i}",
                    hostname="node_gpu",
                    started_at=datetime.now(timezone.utc).isoformat(),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    duration_seconds=300.0,
                    success=True,
                    estimated_minutes=10.0,  # Finished in 5 min vs 10 min estimate
                )
            )

        rating = compute_rating("node_gpu")
        assert rating.composite_score > 600
        assert rating.completion_rate == 1.0
        assert rating.error_rate == 0.0
        assert rating.task_count == 10

    def test_failing_host_gets_low_rating(self, temp_db):
        from performance_rating import record_metric, TaskMetric, compute_rating

        # Record 10 failed tasks
        for i in range(10):
            record_metric(
                TaskMetric(
                    task_id=f"task-{i}",
                    hostname="BAD_HOST",
                    started_at=datetime.now(timezone.utc).isoformat(),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    duration_seconds=300.0,
                    success=False,
                    error_type="crash",
                )
            )

        rating = compute_rating("BAD_HOST")
        assert rating.composite_score < 400
        assert rating.completion_rate == 0.0
        assert rating.error_rate == 1.0

    def test_mixed_performance_moderate_rating(self, temp_db):
        from performance_rating import record_metric, TaskMetric, compute_rating

        now = datetime.now(timezone.utc)
        # 7 successes, 3 failures
        for i in range(7):
            record_metric(
                TaskMetric(
                    task_id=f"ok-{i}",
                    hostname="mixed",
                    started_at=now.isoformat(),
                    completed_at=now.isoformat(),
                    duration_seconds=300.0,
                    success=True,
                    estimated_minutes=5.0,
                )
            )
        for i in range(3):
            record_metric(
                TaskMetric(
                    task_id=f"fail-{i}",
                    hostname="mixed",
                    started_at=now.isoformat(),
                    completed_at=now.isoformat(),
                    duration_seconds=300.0,
                    success=False,
                )
            )

        rating = compute_rating("mixed")
        assert 200 < rating.composite_score < 900
        assert 0.65 < rating.completion_rate < 0.75
        assert rating.task_count == 10

    def test_rating_cached_for_one_hour(self, temp_db):
        from performance_rating import (
            record_metric,
            TaskMetric,
            compute_rating,
            get_rating,
        )

        record_metric(
            TaskMetric(
                task_id="task-1",
                hostname="cached_host",
                started_at=datetime.now(timezone.utc).isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                duration_seconds=60.0,
                success=True,
            )
        )
        compute_rating("cached_host")

        # Second call should use cache (not recompute)
        rating = get_rating("cached_host")
        assert rating.task_count == 1


class TestDecayWeight:
    """Test exponential decay function."""

    def test_recent_data_full_weight(self):
        from performance_rating import _decay_weight

        assert _decay_weight(0) == pytest.approx(1.0)

    def test_half_life_decay(self):
        from performance_rating import _decay_weight, DECAY_HALF_LIFE_DAYS

        assert _decay_weight(DECAY_HALF_LIFE_DAYS) == pytest.approx(0.5, abs=0.01)

    def test_old_data_low_weight(self):
        from performance_rating import _decay_weight

        assert _decay_weight(30) < 0.15

    def test_very_old_data_near_zero(self):
        from performance_rating import _decay_weight

        assert _decay_weight(100) < 0.001


class TestScoredHostSelection:
    """Test scored host selection for dispatch routing."""

    def test_single_capable_host(self, temp_db):
        from performance_rating import scored_host_selection

        fleet = {
            "node_gpu": {"capabilities": ["gpu", "docker", "ollama"]},
            "node_primary": {"capabilities": ["docker"]},
        }
        result = scored_host_selection(fleet, requires=["gpu"])
        assert len(result) == 1
        assert result[0][0] == "node_gpu"

    def test_no_capable_hosts(self, temp_db):
        from performance_rating import scored_host_selection

        fleet = {
            "node_primary": {"capabilities": ["docker"]},
        }
        result = scored_host_selection(fleet, requires=["gpu"])
        assert len(result) == 0

    def test_multiple_capable_hosts_scored(self, temp_db):
        from performance_rating import (
            scored_host_selection,
            record_metric,
            TaskMetric,
            compute_rating,
        )

        fleet = {
            "node_gpu": {"capabilities": ["gpu", "docker"]},
            "node_reserve2": {"capabilities": ["gpu", "docker", "ollama"]},
        }

        # Make node_reserve2 have better rating
        now = datetime.now(timezone.utc)
        for i in range(5):
            record_metric(
                TaskMetric(
                    task_id=f"mecha-{i}",
                    hostname="node_reserve2",
                    started_at=now.isoformat(),
                    completed_at=now.isoformat(),
                    duration_seconds=60.0,
                    success=True,
                    estimated_minutes=2.0,
                )
            )
        for i in range(5):
            record_metric(
                TaskMetric(
                    task_id=f"giga-{i}",
                    hostname="node_gpu",
                    started_at=now.isoformat(),
                    completed_at=now.isoformat(),
                    duration_seconds=60.0,
                    success=(i < 3),  # 3/5 success
                )
            )
        compute_rating("node_reserve2")
        compute_rating("node_gpu")

        result = scored_host_selection(fleet, requires=["gpu"])
        assert len(result) == 2
        assert result[0][0] == "node_reserve2"  # Higher rated
        assert result[0][1] > result[1][1]  # Higher score

    def test_empty_requires_matches_all(self, temp_db):
        from performance_rating import scored_host_selection

        fleet = {
            "node_gpu": {"capabilities": ["gpu"]},
            "node_primary": {"capabilities": ["docker"]},
        }
        result = scored_host_selection(fleet, requires=[])
        assert len(result) == 2

    def test_task_complexity_influences_scoring(self, temp_db):
        from performance_rating import scored_host_selection

        fleet = {
            "node_gpu": {"capabilities": ["gpu", "docker"]},
            "node_primary": {"capabilities": ["docker"]},
        }
        # Complex task should prefer GPU host
        result = scored_host_selection(
            fleet, requires=["docker"], task_complexity="complex"
        )
        assert len(result) == 2
        # node_gpu should score higher for complex tasks (has GPU)
        giga_score = next(s for h, s in result if h == "node_gpu")
        mini_score = next(s for h, s in result if h == "node_primary")
        assert giga_score > mini_score

    def test_simple_task_prefers_non_gpu(self, temp_db):
        from performance_rating import scored_host_selection

        fleet = {
            "node_gpu": {"capabilities": ["gpu", "docker"]},
            "node_primary": {"capabilities": ["docker"]},
        }
        # Simple task should prefer non-GPU (don't waste GPU)
        result = scored_host_selection(
            fleet, requires=["docker"], task_complexity="simple"
        )
        assert len(result) == 2
        mini_score = next(s for h, s in result if h == "node_primary")
        giga_score = next(s for h, s in result if h == "node_gpu")
        assert mini_score > giga_score


class TestBenchmark:
    """Test host benchmarking."""

    def test_benchmark_unreachable_host(self, temp_db):
        from performance_rating import benchmark_host

        with patch("performance_rating.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("ssh", 10)
            result = benchmark_host("offline", "192.168.0.99")
            assert not result.reachable

    def test_benchmark_result_structure(self, temp_db):
        from performance_rating import BenchmarkResult

        result = BenchmarkResult(hostname="test")
        assert result.hostname == "test"
        assert not result.reachable
        assert result.ssh_latency_ms == 0.0
        assert not result.gpu_available

    def test_on_join_probe_structure(self, temp_db):
        from performance_rating import on_join_probe

        with patch("performance_rating.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="GPU_VAL=8192\nOLLAMA_VAL=200\n"
            )
            result = on_join_probe("node_gpu", "<primary-node-ip>")
            assert result.reachable
            assert result.gpu_available
            assert result.gpu_vram_free_mb == 8192
            assert result.ollama_healthy


class TestGetAllRatings:
    """Test aggregate rating queries."""

    def test_empty_fleet(self, temp_db):
        from performance_rating import get_all_ratings

        ratings = get_all_ratings()
        assert ratings == []

    def test_multiple_hosts(self, temp_db):
        from performance_rating import record_metric, TaskMetric, get_all_ratings

        now = datetime.now(timezone.utc)
        for host in ["node_gpu", "node_primary", "node_reserve2"]:
            record_metric(
                TaskMetric(
                    task_id=f"task-{host}",
                    hostname=host,
                    started_at=now.isoformat(),
                    completed_at=now.isoformat(),
                    duration_seconds=60.0,
                    success=True,
                )
            )

        ratings = get_all_ratings()
        assert len(ratings) == 3
        hostnames = {r.hostname for r in ratings}
        assert hostnames == {"node_gpu", "node_primary", "node_reserve2"}


class TestScoredRouting:
    """Test the integrated scored routing in hydra_dispatch."""

    def test_find_best_host_with_scoring(self, temp_db):
        """Test that _find_best_host uses scored selection when available."""
        from hydra_dispatch import _find_best_host

        with (
            patch(
                "hydra_dispatch.FLEET",
                {
                    "node_gpu": {"capabilities": ["gpu", "docker"]},
                    "node_primary": {"capabilities": ["docker"]},
                },
            ),
            patch("performance_rating.DB_PATH", temp_db),
        ):
            result = _find_best_host(["gpu"])
            assert result == "node_gpu"

    def test_find_best_host_fallback(self, temp_db):
        """Test fallback when performance_rating import fails."""
        from hydra_dispatch import _find_best_host

        with (
            patch(
                "hydra_dispatch.FLEET",
                {
                    "node_gpu": {"capabilities": ["gpu"]},
                    "node_primary": {"capabilities": ["docker"]},
                },
            ),
            patch("performance_rating.DB_PATH", temp_db),
        ):
            result = _find_best_host(["docker"])
            assert result is not None

    def test_find_best_host_no_match(self, temp_db):
        from hydra_dispatch import _find_best_host

        with (
            patch(
                "hydra_dispatch.FLEET",
                {
                    "node_primary": {"capabilities": ["docker"]},
                },
            ),
            patch("performance_rating.DB_PATH", temp_db),
        ):
            result = _find_best_host(["gpu", "chromadb"])
            assert result is None


class TestMetricsForHost:
    """Test metrics retrieval."""

    def test_no_metrics(self, temp_db):
        from performance_rating import get_metrics_for_host

        metrics = get_metrics_for_host("nonexistent")
        assert metrics == []

    def test_limit_respected(self, temp_db):
        from performance_rating import record_metric, TaskMetric, get_metrics_for_host

        now = datetime.now(timezone.utc)
        for i in range(20):
            record_metric(
                TaskMetric(
                    task_id=f"task-{i}",
                    hostname="node_gpu",
                    started_at=now.isoformat(),
                    completed_at=now.isoformat(),
                    duration_seconds=60.0,
                    success=True,
                )
            )

        metrics = get_metrics_for_host("node_gpu", limit=5)
        assert len(metrics) == 5

    def test_ordered_by_recency(self, temp_db):
        from performance_rating import record_metric, TaskMetric, get_metrics_for_host

        for i in range(3):
            ts = (datetime.now(timezone.utc) + timedelta(hours=i)).isoformat()
            record_metric(
                TaskMetric(
                    task_id=f"task-{i}",
                    hostname="node_gpu",
                    started_at=ts,
                    completed_at=ts,
                    duration_seconds=60.0,
                    success=True,
                )
            )

        metrics = get_metrics_for_host("node_gpu")
        # Should be newest first
        assert metrics[0]["task_id"] == "task-2"
        assert metrics[2]["task_id"] == "task-0"


import subprocess  # needed for TestBenchmark patch
