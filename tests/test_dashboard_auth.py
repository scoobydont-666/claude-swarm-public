"""E4: Dashboard bearer-token auth middleware tests.

Covers /opt/hydra-project/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase E4.

Semantics:
- SWARM_API_KEY unset → middleware is no-op, all requests pass (preserves
  loopback-only operational model; prevents lockout during rollout).
- SWARM_API_KEY set → /api/* requires X-Swarm-API-Key: <token>; /, /health,
  /live, /ready, /metrics remain unauthenticated (probes + HTML + Prometheus).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def dashboard_app(monkeypatch):
    """Fresh dashboard app per test with isolated env."""
    # Reset any leaked key from previous test
    monkeypatch.delenv("SWARM_API_KEY", raising=False)
    # Reimport to pick up the env state
    if "dashboard" in sys.modules:
        del sys.modules["dashboard"]
    import dashboard  # noqa: F401
    return dashboard.app


class TestAuthDisabledByDefault:
    def test_root_unauthenticated(self, dashboard_app):
        client = TestClient(dashboard_app)
        resp = client.get("/")
        assert resp.status_code == 200

    def test_health_unauthenticated(self, dashboard_app):
        client = TestClient(dashboard_app)
        resp = client.get("/health")
        # /health may return 200 or 503 depending on backend; auth-agnostic
        assert resp.status_code in (200, 503)

    def test_api_endpoint_unauthenticated_when_key_unset(
        self, dashboard_app, monkeypatch
    ):
        """With SWARM_API_KEY unset, /api/* is reachable without a key
        (loopback-only operational model preserved)."""
        monkeypatch.delenv("SWARM_API_KEY", raising=False)
        client = TestClient(dashboard_app)
        resp = client.get("/api/status")
        # /api/status may return 200, 503, etc based on swarm state, but NOT 401
        assert resp.status_code != 401, f"got 401 when SWARM_API_KEY is unset: {resp.text}"


class TestAuthEnabledWithKey:
    @pytest.fixture
    def authed_app(self, monkeypatch):
        """App with SWARM_API_KEY set for the duration of this test class."""
        monkeypatch.setenv("SWARM_API_KEY", "test-token-abc123")
        if "dashboard" in sys.modules:
            del sys.modules["dashboard"]
        import dashboard  # noqa: F401
        return dashboard.app

    def test_missing_key_returns_401(self, authed_app):
        client = TestClient(authed_app)
        resp = client.get("/api/status")
        assert resp.status_code == 401
        detail = resp.json()
        assert detail["error"] == "unauthorized"

    def test_wrong_key_returns_401(self, authed_app):
        client = TestClient(authed_app)
        resp = client.get(
            "/api/status",
            headers={"X-Swarm-API-Key": "wrong-token"},
        )
        assert resp.status_code == 401

    def test_correct_key_passes_through(self, authed_app):
        client = TestClient(authed_app)
        resp = client.get(
            "/api/status",
            headers={"X-Swarm-API-Key": "test-token-abc123"},
        )
        # Auth should succeed; downstream may return 200/503 based on swarm state
        assert resp.status_code != 401, f"auth failed with valid token: {resp.text}"

    def test_health_remains_unauthenticated(self, authed_app):
        """/health is in the exempt set — k8s liveness probes don't carry headers."""
        client = TestClient(authed_app)
        resp = client.get("/health")
        assert resp.status_code != 401

    def test_root_remains_unauthenticated(self, authed_app):
        client = TestClient(authed_app)
        resp = client.get("/")
        assert resp.status_code != 401

    def test_timing_safe_compare_used(self, authed_app):
        """Both missing header and wrong value path through hmac.compare_digest.
        This test just proves both return the same 401 shape — actual timing
        resistance is from hmac itself (well-tested upstream)."""
        client = TestClient(authed_app)
        r1 = client.get("/api/status")  # no header
        r2 = client.get("/api/status", headers={"X-Swarm-API-Key": "x"})  # wrong
        r3 = client.get("/api/status", headers={"X-Swarm-API-Key": "a" * 64})  # wrong long
        for r in (r1, r2, r3):
            assert r.status_code == 401
            assert r.json()["error"] == "unauthorized"
