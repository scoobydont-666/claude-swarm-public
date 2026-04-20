"""E5: DLQ age-based prune + backend degradation flag tests.

Covers /opt/hydra-project/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase E5.

Semantics:
- prune_old_messages(hours=72) removes DLQ entries with stream_id older
  than now - 72h. Default is 72h per Josh directive 2026-04-18 (work
  spans multiple days; 24h was too aggressive for weekend triage cycles).
- /api/status returns {backend, degraded, degradation_reason} so operators
  see at a glance whether swarm is on Redis (healthy) or NFS (degraded).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestPruneOldMessages:
    """DLQ age-based pruning uses XTRIM MINID for O(N_removed) efficiency."""

    def test_default_hours_is_72(self):
        """Josh directive 2026-04-18: 72h covers Fri→Mon + 48h D3 staging."""
        from ipc import dlq

        # Read the default via inspect
        import inspect

        sig = inspect.signature(dlq.prune_old_messages)
        assert sig.parameters["hours"].default == 72

    def test_uses_xtrim_minid_with_cutoff_in_past(self):
        """prune_old_messages should call xtrim with a minid that's
        now - hours * 3600 * 1000 millis ago."""
        from ipc import dlq

        mock_client = MagicMock()
        mock_client.xtrim.return_value = 5
        with patch.object(dlq.transport, "get_client", return_value=mock_client):
            result = dlq.prune_old_messages(hours=48)

        assert result == 5
        assert mock_client.xtrim.called
        # Verify the minid is approximately 48h ago
        call_kwargs = mock_client.xtrim.call_args.kwargs
        call_args = mock_client.xtrim.call_args.args
        # xtrim signature may vary; handle both kwarg and positional minid
        minid = call_kwargs.get("minid")
        if minid is None:
            # some redis-py versions take positional
            minid = call_args[1] if len(call_args) > 1 else None
        assert minid is not None
        # minid format is "<millis>-<seq>"
        minid_ms = int(str(minid).split("-")[0])
        now_ms = int(time.time() * 1000)
        expected_ms = now_ms - 48 * 3600 * 1000
        # Within 10s of expected
        assert abs(minid_ms - expected_ms) < 10_000, (
            f"minid {minid_ms} not near expected {expected_ms}"
        )

    def test_zero_removed_returns_zero(self):
        from ipc import dlq

        mock_client = MagicMock()
        mock_client.xtrim.return_value = 0
        with patch.object(dlq.transport, "get_client", return_value=mock_client):
            assert dlq.prune_old_messages() == 0

    def test_xtrim_none_return_handled(self):
        from ipc import dlq

        mock_client = MagicMock()
        mock_client.xtrim.return_value = None
        with patch.object(dlq.transport, "get_client", return_value=mock_client):
            assert dlq.prune_old_messages() == 0

    def test_xtrim_failure_falls_back_to_xrange_xdel(self):
        """On old Redis without MINID support, fall back to explicit range+delete."""
        from ipc import dlq

        mock_client = MagicMock()
        mock_client.xtrim.side_effect = Exception("minid not supported")
        # Return 3 entries in the "before cutoff" range
        mock_client.xrange.return_value = [
            ("1000-0", {"envelope": "{}"}),
            ("2000-0", {"envelope": "{}"}),
            ("3000-0", {"envelope": "{}"}),
        ]
        with patch.object(dlq.transport, "get_client", return_value=mock_client):
            result = dlq.prune_old_messages(hours=1)

        assert result == 3
        mock_client.xdel.assert_called_once()


class TestBackendDegradation:
    """Dashboard /api/status reports backend state + degraded flag."""

    @pytest.fixture
    def dashboard_app(self, monkeypatch):
        """Fresh dashboard app per test."""
        monkeypatch.delenv("SWARM_API_KEY", raising=False)
        if "dashboard" in sys.modules:
            del sys.modules["dashboard"]
        import dashboard
        return dashboard

    def test_backend_pinned_to_redis_not_degraded(self, dashboard_app, monkeypatch):
        monkeypatch.setenv("SWARM_BACKEND", "redis")
        result = dashboard_app._get_backend_degradation()
        assert result["backend"] == "redis"
        assert result["degraded"] is False
        assert result["reason"] is None

    def test_backend_pinned_to_nfs_is_degraded(self, dashboard_app, monkeypatch):
        monkeypatch.setenv("SWARM_BACKEND", "nfs")
        result = dashboard_app._get_backend_degradation()
        assert result["backend"] == "nfs"
        assert result["degraded"] is True
        assert "pinned to nfs" in result["reason"]

    def test_auto_with_redis_healthy_returns_redis(self, dashboard_app, monkeypatch):
        monkeypatch.setenv("SWARM_BACKEND", "auto")
        mock_rc = MagicMock()
        mock_rc.health_check.return_value = True
        with patch.dict(sys.modules, {"redis_client": mock_rc}):
            result = dashboard_app._get_backend_degradation()
        assert result["backend"] == "redis"
        assert result["degraded"] is False

    def test_auto_with_redis_down_returns_nfs_degraded(
        self, dashboard_app, monkeypatch
    ):
        monkeypatch.setenv("SWARM_BACKEND", "auto")
        mock_rc = MagicMock()
        mock_rc.health_check.return_value = False
        with patch.dict(sys.modules, {"redis_client": mock_rc}):
            result = dashboard_app._get_backend_degradation()
        assert result["backend"] == "nfs"
        assert result["degraded"] is True
        assert "redis_health_check_failed" in result["reason"]

    def test_probe_error_returns_unknown_degraded(self, dashboard_app, monkeypatch):
        monkeypatch.setenv("SWARM_BACKEND", "auto")
        mock_rc = MagicMock()
        mock_rc.health_check.side_effect = RuntimeError("boom")
        with patch.dict(sys.modules, {"redis_client": mock_rc}):
            result = dashboard_app._get_backend_degradation()
        assert result["backend"] == "unknown"
        assert result["degraded"] is True
        assert "backend_probe_error" in result["reason"]
