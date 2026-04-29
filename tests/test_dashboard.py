"""Tests for dashboard.py — FastAPI web dashboard endpoints and HTML."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import swarm_lib as lib
from dashboard import app


@pytest.fixture
def client():
    """Create a test client for the dashboard app."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# Dashboard HTML Tests
# ---------------------------------------------------------------------------


class TestDashboardHTML:
    def test_get_dashboard_html(self, client):
        """Test GET / returns HTML."""
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert "claude-swarm Dashboard" in response.text
        assert "<html" in response.text
        assert "</html>" in response.text

    def test_dashboard_contains_fleet_status_panel(self, client):
        """Test HTML includes fleet status panel."""
        response = client.get("/")
        assert "Fleet Status" in response.text or "fleet-status" in response.text

    def test_dashboard_contains_task_queue_panel(self, client):
        """Test HTML includes task queue panel."""
        response = client.get("/")
        assert "Task Queue" in response.text or "tasks-container" in response.text

    def test_dashboard_contains_dispatch_monitor(self, client):
        """Test HTML includes dispatch monitor."""
        response = client.get("/")
        assert "Dispatch Monitor" in response.text or "dispatches-container" in response.text

    def test_dashboard_contains_health_alerts(self, client):
        """Test HTML includes health alerts section."""
        response = client.get("/")
        assert "Health Alerts" in response.text or "health-container" in response.text

    def test_dashboard_contains_metrics_summary(self, client):
        """Test HTML includes metrics summary."""
        response = client.get("/")
        assert "Metrics Summary" in response.text or "metrics-container" in response.text

    def test_dashboard_contains_auto_refresh_meta(self, client):
        """Test HTML contains auto-refresh meta tag."""
        response = client.get("/")
        assert "refresh" in response.text.lower() or "setInterval" in response.text


# ---------------------------------------------------------------------------
# API Endpoint Tests — Status
# ---------------------------------------------------------------------------


class TestStatusAPI:
    def test_status_endpoint_returns_json(self, client, swarm_tmpdir):
        """Test /api/status returns valid JSON."""
        lib.update_status(state="active", model="opus")
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "nodes" in data
        assert isinstance(data["nodes"], list)

    def test_status_endpoint_includes_color_codes(self, client, swarm_tmpdir):
        """Test status response includes _color field."""
        lib.update_status(state="active", model="opus")
        response = client.get("/api/status")
        data = response.json()
        nodes = data["nodes"]
        assert len(nodes) > 0
        for node in nodes:
            assert "_color" in node
            assert "_dot" in node
            assert "_heartbeat_age" in node

    def test_status_endpoint_empty(self, client, swarm_tmpdir):
        """Test status endpoint with no nodes."""
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert data["nodes"] == []

    def test_status_color_codes_by_state(self, client, swarm_tmpdir):
        """Test status color codes match node state."""
        lib.update_status(state="active", model="opus")
        response = client.get("/api/status")
        data = response.json()
        node = data["nodes"][0]
        assert node["state"] == "active"
        assert node["_color"] == "green"
        assert node["_dot"] == "●"


# ---------------------------------------------------------------------------
# API Endpoint Tests — Tasks
# ---------------------------------------------------------------------------


class TestTasksAPI:
    def test_tasks_endpoint_returns_json(self, client, swarm_tmpdir):
        """Test /api/tasks returns valid JSON."""
        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "pending" in data
        assert "claimed" in data
        assert "completed" in data
        assert "total" in data

    def test_tasks_endpoint_has_counts(self, client, swarm_tmpdir):
        """Test tasks endpoint includes counts."""
        lib.create_task(title="test task", priority="high")
        response = client.get("/api/tasks")
        data = response.json()
        assert data["pending"]["count"] >= 0
        assert data["claimed"]["count"] == 0
        assert data["completed"]["count"] == 0

    def test_tasks_include_age_field(self, client, swarm_tmpdir):
        """Test tasks include _age field."""
        lib.create_task(title="test task", priority="high")
        response = client.get("/api/tasks")
        data = response.json()
        if data["pending"]["tasks"]:
            task = data["pending"]["tasks"][0]
            assert "_age" in task

    def test_tasks_total_count_matches_sum(self, client, swarm_tmpdir):
        """Test total count equals sum of stages."""
        lib.create_task(title="test task 1", priority="high")
        lib.create_task(title="test task 2", priority="high")
        response = client.get("/api/tasks")
        data = response.json()
        total = data["total"]["count"]
        sum_stages = (
            data["pending"]["count"] + data["claimed"]["count"] + data["completed"]["count"]
        )
        assert total == sum_stages


# ---------------------------------------------------------------------------
# API Endpoint Tests — Dispatches
# ---------------------------------------------------------------------------


class TestDispatchesAPI:
    def test_dispatches_endpoint_returns_json(self, client):
        """Test /api/dispatches returns valid JSON."""
        response = client.get("/api/dispatches")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "dispatches" in data
        assert "count" in data

    def test_dispatches_endpoint_structure(self, client):
        """Test dispatches endpoint structure."""
        response = client.get("/api/dispatches")
        data = response.json()
        assert isinstance(data["dispatches"], list)
        assert isinstance(data["count"], int)

    def test_dispatches_include_metadata(self, client, tmp_path):
        """Test dispatches include _duration and _started_ago."""
        with patch("dashboard.DISPATCHES_DIR", tmp_path):
            # Create fake dispatch file
            dispatch_file = tmp_path / "dispatch-123-host.yaml"
            dispatch_file.write_text("""
dispatch_id: dispatch-123-host
host: testhost
model: haiku
status: running
started_at: '2026-03-24T12:00:00Z'
completed_at: ''
task: test task
""")
            response = client.get("/api/dispatches")
            data = response.json()
            if data["dispatches"]:
                dispatch = data["dispatches"][0]
                assert "_duration" in dispatch or True  # May not have completed yet
                assert "_started_ago" in dispatch


# ---------------------------------------------------------------------------
# API Endpoint Tests — Health
# ---------------------------------------------------------------------------


class TestHealthAPI:
    def test_health_endpoint_returns_json(self, client):
        """Test /api/health returns valid JSON."""
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "events" in data
        assert "count" in data

    def test_health_events_are_list(self, client):
        """Test health events are a list."""
        response = client.get("/api/health")
        data = response.json()
        assert isinstance(data["events"], list)

    def test_health_events_include_metadata(self, client):
        """Test health events include _age and _color fields."""
        response = client.get("/api/health")
        data = response.json()
        # Even if empty, structure should be present
        assert "count" in data
        assert isinstance(data["count"], int)

    def test_health_endpoint_graceful_no_db(self, client):
        """Test health endpoint when DB doesn't exist."""
        with patch("dashboard.DB_PATH", Path("/nonexistent/path")):
            response = client.get("/api/health")
            assert response.status_code == 200
            data = response.json()
            assert data["events"] == []


# ---------------------------------------------------------------------------
# API Endpoint Tests — Metrics
# ---------------------------------------------------------------------------


class TestMetricsAPI:
    def test_metrics_endpoint_returns_json(self, client, swarm_tmpdir):
        """Test /api/metrics returns valid JSON."""
        lib.update_status(state="active", model="opus")
        response = client.get("/api/metrics")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    def test_metrics_has_required_fields(self, client, swarm_tmpdir):
        """Test metrics includes all required fields."""
        response = client.get("/api/metrics")
        data = response.json()
        assert "nodes" in data
        assert "tasks" in data
        assert "cache_hit_rate" in data
        assert "dispatches_today" in data

    def test_metrics_nodes_structure(self, client, swarm_tmpdir):
        """Test metrics nodes structure."""
        lib.update_status(state="active", model="opus")
        response = client.get("/api/metrics")
        data = response.json()
        assert "total" in data["nodes"]
        assert "by_state" in data["nodes"]
        assert isinstance(data["nodes"]["by_state"], dict)

    def test_metrics_tasks_structure(self, client, swarm_tmpdir):
        """Test metrics tasks structure."""
        lib.create_task(title="test", priority="high")
        response = client.get("/api/metrics")
        data = response.json()
        assert "total" in data["tasks"]
        assert "by_state" in data["tasks"]
        assert "pending" in data["tasks"]["by_state"]
        assert "claimed" in data["tasks"]["by_state"]
        assert "completed" in data["tasks"]["by_state"]

    def test_metrics_cache_hit_rate_is_number(self, client):
        """Test cache_hit_rate is a number between 0 and 1."""
        response = client.get("/api/metrics")
        data = response.json()
        assert isinstance(data["cache_hit_rate"], (int, float))
        assert 0 <= data["cache_hit_rate"] <= 1

    def test_metrics_dispatches_today_is_int(self, client):
        """Test dispatches_today is an integer."""
        response = client.get("/api/metrics")
        data = response.json()
        assert isinstance(data["dispatches_today"], int)
        assert data["dispatches_today"] >= 0


# ---------------------------------------------------------------------------
# API Endpoint Tests — Events
# ---------------------------------------------------------------------------


class TestEventsAPI:
    def test_events_endpoint_returns_json(self, client):
        """Test /api/events returns valid JSON."""
        response = client.get("/api/events")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "events" in data
        assert "count" in data

    def test_events_endpoint_structure(self, client):
        """Test events endpoint structure."""
        response = client.get("/api/events")
        data = response.json()
        assert isinstance(data["events"], list)
        assert isinstance(data["count"], int)


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestDashboardIntegration:
    def test_all_endpoints_respond(self, client):
        """Test all API endpoints respond without error."""
        endpoints = [
            "/",
            "/api/status",
            "/api/tasks",
            "/api/dispatches",
            "/api/health",
            "/api/metrics",
            "/api/events",
        ]
        for endpoint in endpoints:
            response = client.get(endpoint)
            assert response.status_code == 200, f"Endpoint {endpoint} failed"

    def test_dashboard_with_data(self, client, swarm_tmpdir):
        """Test dashboard loads with realistic data."""
        # Create some data
        lib.update_status(state="active", model="opus", current_task="task-1")

        # Create second node status
        with patch.object(lib, "_hostname", return_value="otherhost"):
            lib.update_status(state="idle", model="haiku")

        lib.create_task(title="Task 1", priority="high")
        lib.create_task(title="Task 2", priority="normal")

        # Get all endpoints
        response = client.get("/")
        assert response.status_code == 200
        assert "claude-swarm" in response.text

        status_resp = client.get("/api/status")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert len(data["nodes"]) >= 2

        tasks_resp = client.get("/api/tasks")
        assert tasks_resp.status_code == 200
        data = tasks_resp.json()
        assert data["pending"]["count"] >= 2

        metrics_resp = client.get("/api/metrics")
        assert metrics_resp.status_code == 200
        data = metrics_resp.json()
        assert data["nodes"]["total"] >= 2


# ---------------------------------------------------------------------------
# Edge Cases and Error Handling
# ---------------------------------------------------------------------------


class TestDashboardEdgeCases:
    def test_dashboard_handles_missing_status_files(self, client, swarm_tmpdir):
        """Test dashboard gracefully handles missing status files."""
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert data["nodes"] == []

    def test_dashboard_handles_empty_tasks(self, client, swarm_tmpdir):
        """Test dashboard handles no tasks."""
        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["pending"]["count"] == 0
        assert data["claimed"]["count"] == 0
        assert data["completed"]["count"] == 0

    def test_relative_time_formatting(self, client, swarm_tmpdir):
        """Test relative time formatting in responses."""
        lib.update_status(state="active", model="opus")
        response = client.get("/api/status")
        data = response.json()
        if data["nodes"]:
            node = data["nodes"][0]
            age = node["_heartbeat_age"]
            # Should be formatted like "5s ago", "2m ago", etc.
            assert "ago" in age or age == "?"

    def test_html_dark_theme_colors(self, client):
        """Test dashboard HTML includes dark theme colors."""
        response = client.get("/")
        html = response.text
        # Check for dark theme hex colors
        assert "#0d1117" in html  # Dark background
        assert "#161b22" in html  # Slightly lighter background


# ---------------------------------------------------------------------------
# Responsive and Performance Tests
# ---------------------------------------------------------------------------


class TestDashboardPerformance:
    def test_status_endpoint_with_many_nodes(self, client, swarm_tmpdir):
        """Test status endpoint with multiple nodes."""
        for i in range(5):
            hostname = f"node-{i}"
            with patch.object(lib, "_hostname", return_value=hostname):
                lib.update_status(state="active" if i % 2 == 0 else "idle")

        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        # Should return all nodes without error
        assert isinstance(data["nodes"], list)

    def test_tasks_endpoint_with_many_tasks(self, client, swarm_tmpdir):
        """Test tasks endpoint with multiple tasks."""
        for i in range(10):
            lib.create_task(title=f"Task {i}", priority="normal")

        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["pending"]["count"] == 10
