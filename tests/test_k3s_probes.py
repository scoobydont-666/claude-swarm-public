"""E6: K3s liveness + readiness probe tests.

Covers /opt/hydra-project/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase E6.

Semantics:
- /live is always 200 when the ASGI app is responding (lenient — restart
  only on total wedge, not on Redis/NFS flap).
- /ready is 200 only when both Redis and /opt/swarm NFS are reachable;
  503 otherwise (strict — stop routing work to this pod during degradation
  but don't restart it, since restart doesn't fix an external outage).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def dashboard_app(monkeypatch):
    monkeypatch.delenv("SWARM_API_KEY", raising=False)
    if "dashboard" in sys.modules:
        del sys.modules["dashboard"]
    import dashboard
    return dashboard.app


class TestLiveness:
    """/live = 'is the process alive?' — lenient probe."""

    def test_live_returns_200(self, dashboard_app):
        client = TestClient(dashboard_app)
        resp = client.get("/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "alive"
        assert body["probe"] == "liveness"

    def test_live_does_not_check_redis(self, dashboard_app):
        """Liveness should NOT fail when Redis is down — restart doesn't
        fix an external outage, and k8s would thrash the pod."""
        client = TestClient(dashboard_app)
        # Force Redis into a broken state via mock — /live should still 200
        import redis_client

        with patch.object(redis_client, "get_client", side_effect=RuntimeError("boom")):
            resp = client.get("/live")
        assert resp.status_code == 200

    def test_live_is_exempt_from_api_key_auth(self, dashboard_app, monkeypatch):
        """K8s probes never carry auth headers — /live must be reachable
        even when SWARM_API_KEY is set."""
        monkeypatch.setenv("SWARM_API_KEY", "secret")
        if "dashboard" in sys.modules:
            del sys.modules["dashboard"]
        import dashboard
        client = TestClient(dashboard.app)
        resp = client.get("/live")
        assert resp.status_code == 200


class TestReadiness:
    """/ready = 'can this pod serve real traffic?' — strict probe."""

    def test_ready_200_when_both_redis_and_nfs_ok(self, dashboard_app):
        client = TestClient(dashboard_app)
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        with patch("redis_client.get_client", return_value=mock_client), patch(
            "os.path.isdir", return_value=True
        ):
            resp = client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["redis"] is True
        assert body["checks"]["nfs_swarm"] is True

    def test_ready_503_when_redis_down(self, dashboard_app):
        client = TestClient(dashboard_app)
        with patch("redis_client.get_client", side_effect=RuntimeError("boom")):
            resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert any("redis_unreachable" in r for r in body["not_ready_reasons"])

    def test_ready_503_when_nfs_missing(self, dashboard_app):
        client = TestClient(dashboard_app)
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        with patch("redis_client.get_client", return_value=mock_client), patch(
            "os.path.ismount", return_value=False
        ), patch("os.path.isdir", return_value=False):
            resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert "nfs_swarm_mount_missing" in body["not_ready_reasons"]

    def test_ready_is_exempt_from_api_key_auth(self, dashboard_app, monkeypatch):
        monkeypatch.setenv("SWARM_API_KEY", "secret")
        if "dashboard" in sys.modules:
            del sys.modules["dashboard"]
        import dashboard
        client = TestClient(dashboard.app)
        # Should not return 401 regardless of readiness state
        resp = client.get("/ready")
        assert resp.status_code != 401


class TestExemptPathRegistration:
    """/live and /ready must be in _AUTH_EXEMPT_PATHS so k8s probes
    never get a 401 when auth is enabled."""

    def test_live_in_auth_exempt_paths(self):
        if "dashboard" in sys.modules:
            del sys.modules["dashboard"]
        import dashboard
        assert "/live" in dashboard._AUTH_EXEMPT_PATHS

    def test_ready_in_auth_exempt_paths(self):
        if "dashboard" in sys.modules:
            del sys.modules["dashboard"]
        import dashboard
        assert "/ready" in dashboard._AUTH_EXEMPT_PATHS
