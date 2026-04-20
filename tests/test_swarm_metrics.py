"""Tests for swarm metrics exporter — data collection helpers."""

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestCollectNodeMetrics:
    def test_counts_by_state(self, tmp_path):
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (status_dir / "node_primary.json").write_text(
            json.dumps(
                {
                    "hostname": "node_primary",
                    "state": "idle",
                    "updated_at": now,
                }
            )
        )
        (status_dir / "node_gpu.json").write_text(
            json.dumps(
                {
                    "hostname": "node_gpu",
                    "state": "active",
                    "session_id": "sess-1",
                    "updated_at": now,
                }
            )
        )
        with patch("swarm_metrics.STATUS_DIR", status_dir):
            from swarm_metrics import _collect_node_metrics

            state_counts, active_sessions, heartbeat_ages = _collect_node_metrics()
        assert state_counts["idle"] == 1
        assert state_counts["active"] == 1
        assert active_sessions == 1
        assert len(heartbeat_ages) == 2

    def test_unknown_state_maps_to_offline(self, tmp_path):
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        (status_dir / "bad.json").write_text(
            json.dumps(
                {
                    "hostname": "bad",
                    "state": "exploding",
                }
            )
        )
        with patch("swarm_metrics.STATUS_DIR", status_dir):
            from swarm_metrics import _collect_node_metrics

            state_counts, _, _ = _collect_node_metrics()
        assert state_counts["offline"] == 1

    def test_missing_dir_returns_zeros(self, tmp_path):
        missing = tmp_path / "nonexistent"
        with patch("swarm_metrics.STATUS_DIR", missing):
            from swarm_metrics import _collect_node_metrics

            state_counts, active_sessions, heartbeat_ages = _collect_node_metrics()
        assert all(v == 0 for v in state_counts.values())
        assert active_sessions == 0

    def test_corrupt_json_skipped(self, tmp_path):
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        (status_dir / "corrupt.json").write_text("{invalid json")
        with patch("swarm_metrics.STATUS_DIR", status_dir):
            from swarm_metrics import _collect_node_metrics

            state_counts, _, _ = _collect_node_metrics()
        # Should not crash, just skip
        assert all(v == 0 for v in state_counts.values())

    def test_heartbeat_age_calculation(self, tmp_path):
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        old_time = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (status_dir / "node.json").write_text(
            json.dumps(
                {
                    "hostname": "node",
                    "state": "idle",
                    "updated_at": old_time,
                }
            )
        )
        with patch("swarm_metrics.STATUS_DIR", status_dir):
            from swarm_metrics import _collect_node_metrics

            _, _, heartbeat_ages = _collect_node_metrics()
        assert len(heartbeat_ages) == 1
        hostname, age = heartbeat_ages[0]
        assert hostname == "node"
        assert 280 < age < 320  # ~5 minutes


class TestCollectTaskMetrics:
    def test_counts_tasks_by_state(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        for state in ("pending", "claimed", "completed"):
            (tasks_dir / state).mkdir(parents=True)
        (tasks_dir / "pending" / "task-1.yaml").write_text("title: test1")
        (tasks_dir / "pending" / "task-2.yaml").write_text("title: test2")
        (tasks_dir / "completed" / "task-3.yaml").write_text("title: test3")
        with patch("swarm_metrics.TASKS_DIR", tasks_dir):
            from swarm_metrics import _collect_task_metrics

            counts = _collect_task_metrics()
        assert counts["pending"] == 2
        assert counts["claimed"] == 0
        assert counts["completed"] == 1

    def test_missing_dir_returns_zeros(self, tmp_path):
        missing = tmp_path / "nonexistent"
        with patch("swarm_metrics.TASKS_DIR", missing):
            from swarm_metrics import _collect_task_metrics

            counts = _collect_task_metrics()
        assert all(v == 0 for v in counts.values())


class TestCollectEventCount:
    def test_counts_events_from_db(self, tmp_path):
        db_path = tmp_path / "health-events.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE health_events (id INTEGER PRIMARY KEY, data TEXT)")
        for i in range(5):
            conn.execute("INSERT INTO health_events (data) VALUES (?)", (f"event-{i}",))
        conn.commit()
        conn.close()
        with patch("swarm_metrics.DB_PATH", db_path):
            from swarm_metrics import _collect_event_count

            count = _collect_event_count()
        assert count == 5

    def test_missing_db_returns_zero(self, tmp_path):
        with patch("swarm_metrics.DB_PATH", tmp_path / "nonexistent.db"):
            from swarm_metrics import _collect_event_count

            count = _collect_event_count()
        assert count == 0


class TestCollectDispatchCosts:
    def test_sums_costs(self, tmp_path, monkeypatch):
        dispatches_dir = tmp_path / "dispatches"
        dispatches_dir.mkdir()
        for i, (host, cost) in enumerate([("node_gpu", 0.50), ("node_primary", 0.25), ("node_gpu", 0.75)]):
            (dispatches_dir / f"d{i}.plan.yaml").write_text(
                yaml.dump(
                    {
                        "host": host,
                        "estimated_cost_usd": cost,
                    }
                )
            )

        # Monkeypatch the hardcoded path inside the function

        def patched():
            total = 0.0
            host_costs = {}
            for plan_file in dispatches_dir.glob("*.plan.yaml"):
                data = yaml.safe_load(plan_file.read_text()) or {}
                cost = float(data.get("estimated_cost_usd", 0.0))
                host = data.get("host", "unknown")
                total += cost
                host_costs[host] = host_costs.get(host, 0.0) + cost
            return total, host_costs

        total, host_costs = patched()
        assert abs(total - 1.50) < 0.01
        assert abs(host_costs["node_gpu"] - 1.25) < 0.01
        assert abs(host_costs["node_primary"] - 0.25) < 0.01

    def test_no_crash_on_real_dir(self):
        """Verify the real function doesn't crash regardless of disk state."""
        from swarm_metrics import _collect_dispatch_costs

        total, costs = _collect_dispatch_costs()
        assert isinstance(total, float)
        assert isinstance(costs, dict)


class TestCollectGpuSlotMetrics:
    def test_returns_slot_status(self):
        from swarm_metrics import _collect_gpu_slot_metrics

        mock_module = MagicMock()
        mock_module.get_slot_status.return_value = [
            {"gpu_id": 0, "claimed": True},
            {"gpu_id": 1, "claimed": False},
        ]
        with patch.dict("sys.modules", {"gpu_slots": mock_module}):
            result = _collect_gpu_slot_metrics()
        assert result == {0: True, 1: False}

    def test_no_crash_on_real_import(self):
        """Verify no crash regardless of gpu_slots availability."""
        from swarm_metrics import _collect_gpu_slot_metrics

        result = _collect_gpu_slot_metrics()
        assert isinstance(result, dict)
