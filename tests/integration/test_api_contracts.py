"""API contract tests for the claude-swarm dashboard FastAPI app.

Backport pattern from
/opt/nai-control-center/src/app/api/health/__tests__/route.test.ts (P3 item 10
= audit remediation #2 from docs/churn-analysis-2026-04-17.md).

Captures the response-shape-mismatch class of fixes
(cdd5218/9a518b3/ab1c539 in git history).

Uses fastapi.TestClient so tests don't need live Redis/NFS.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


@pytest.fixture(scope="module")
def client():
    """Module-scoped TestClient against the dashboard FastAPI app.

    Imports inside the fixture so conftest's HYDRA_ENV=dev is set first.
    """
    from dashboard import app

    return TestClient(app)


class TestRootAndStatus:
    def test_root_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        # HTMLResponse content-type
        assert "text/html" in r.headers.get("content-type", "")

    def test_status_returns_json_with_nodes(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        assert "nodes" in data
        assert isinstance(data["nodes"], list)

    def test_status_node_shape(self, client):
        """Each node entry must have the well-known fields."""
        r = client.get("/api/status")
        data = r.json()
        if not data["nodes"]:
            pytest.skip("no nodes registered in test fixture")
        n = data["nodes"][0]
        for field in ("hostname", "ip", "state", "session_id", "model", "updated_at"):
            assert field in n, f"node missing field: {field}"


class TestTasksEndpoint:
    def test_tasks_returns_json_shape(self, client):
        r = client.get("/api/tasks")
        assert r.status_code == 200
        data = r.json()
        # Either a list or an object with a top-level list
        assert isinstance(data, (dict, list))

    def test_tasks_filter_by_state(self, client):
        """Filter param shouldn't 500 even if no tasks match."""
        r = client.get("/api/tasks?state=pending")
        assert r.status_code in (200, 204)


class TestDispatchesEndpoint:
    def test_dispatches_returns_json(self, client):
        r = client.get("/api/dispatches")
        assert r.status_code == 200
        assert isinstance(r.json(), (dict, list))


class TestHealthEndpoints:
    def test_api_health_returns_events_shape(self, client):
        """/api/health is the health-EVENTS feed (history), not liveness.
        Must return {count, events: [...]}.
        """
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        assert "events" in data
        assert "count" in data

    def test_liveness_health_endpoint(self, client):
        """/health (NO /api prefix) is the K8s-style liveness probe.
        Added in P4 of DoD plan — will carry {status, degradation_reason}.
        Until then, 404 is acceptable; once P4 lands, update this test.
        """
        r = client.get("/health")
        assert r.status_code in (200, 404), (
            f"/health returned {r.status_code}; expected 200 (post-P4) or 404 (pre-P4)"
        )
        if r.status_code == 200:
            data = r.json()
            assert "status" in data
            # Backport 4 pattern: degradation_reason present (may be null).
            assert "degradation_reason" in data


class TestMetricsEndpoint:
    def test_metrics_returns_json(self, client):
        """Dashboard /api/metrics is a JSON aggregator (not Prometheus text)."""
        r = client.get("/api/metrics")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)


class TestEventsEndpoint:
    def test_events_returns_json(self, client):
        r = client.get("/api/events")
        assert r.status_code == 200
        assert isinstance(r.json(), (dict, list))


class TestGpuEndpoint:
    def test_gpu_returns_json(self, client):
        r = client.get("/api/gpu")
        assert r.status_code == 200
        assert isinstance(r.json(), (dict, list))


class TestRoutingEndpoint:
    def test_routing_returns_json(self, client):
        r = client.get("/api/routing")
        assert r.status_code == 200
        assert isinstance(r.json(), (dict, list))


class TestErrorHandling:
    def test_unknown_route_returns_404(self, client):
        r = client.get("/api/does-not-exist-xyz")
        assert r.status_code == 404


class TestOpenAPISchema:
    def test_openapi_json_available(self, client):
        """FastAPI exposes /openapi.json — contract consumers rely on it."""
        r = client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert schema["openapi"].startswith("3.")
        paths = schema["paths"]
        # Every endpoint verified above should be in the schema
        for p in ("/api/status", "/api/tasks", "/api/dispatches", "/api/health"):
            assert p in paths, f"schema missing path: {p}"
