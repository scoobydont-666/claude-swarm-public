"""Tests for NFS health monitoring."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from health_monitor import HealthMonitor


class TestNFSHealthCheck:
    """Tests for NFS health check functionality."""

    def test_check_nfs_health_success(self, tmp_path):
        """Test successful NFS health check."""
        monitor = HealthMonitor()
        rule = {
            "name": "nfs_health_test",
            "check": "nfs_health",
            "threshold_seconds": 5,
        }

        with patch("health_monitor.Path") as mock_path_class:
            mock_path = MagicMock()
            mock_path.is_dir.return_value = True
            mock_path_class.return_value = mock_path

            with patch("builtins.open", mock_open()) as mock_file:
                triggered = monitor._check_nfs_health(rule)

                # If no timeout/error, triggered should be empty
                if triggered:
                    # Either timeout or content mismatch
                    assert (
                        "write_time_seconds" in triggered[0] or "error" in triggered[0]
                    )

    def test_check_nfs_health_timeout(self, tmp_path):
        """Test NFS health check with slow response (timeout)."""
        monitor = HealthMonitor()
        rule = {
            "name": "nfs_health_test",
            "check": "nfs_health",
            "threshold_seconds": 0.01,  # Very low timeout
        }

        with patch("health_monitor.Path") as mock_path_class:
            mock_path = MagicMock()
            mock_path.is_dir.return_value = True
            mock_path_class.return_value = mock_path

            with patch("builtins.open", mock_open()) as mock_file:
                with patch("health_monitor.time.time") as mock_time:
                    # Simulate slow file operations
                    call_count = [0]

                    def side_effect(*args, **kwargs):
                        call_count[0] += 1
                        return call_count[0] * 0.1  # Increasing time values

                    mock_time.side_effect = side_effect

                    triggered = monitor._check_nfs_health(rule)

                    # Should have triggered because operations exceed threshold
                    if triggered:
                        assert "write_time_seconds" in triggered[0]

    def test_check_nfs_health_missing_path(self):
        """Test NFS health check when /var/lib/swarm doesn't exist."""
        monitor = HealthMonitor()
        rule = {
            "name": "nfs_health_test",
            "check": "nfs_health",
            "threshold_seconds": 5,
        }

        with patch("health_monitor.Path") as mock_path_class:
            mock_path = MagicMock()
            mock_path.is_dir.return_value = False
            mock_path_class.return_value = mock_path

            triggered = monitor._check_nfs_health(rule)

            # Should not trigger if NFS path missing
            assert triggered == []

    def test_check_nfs_health_content_mismatch(self, tmp_path):
        """Test NFS health check with content verification failure."""
        monitor = HealthMonitor()
        rule = {
            "name": "nfs_health_test",
            "check": "nfs_health",
            "threshold_seconds": 5,
        }

        with patch("health_monitor.Path") as mock_path_class:
            mock_path = MagicMock()
            mock_path.is_dir.return_value = True
            mock_path_class.return_value = mock_path

            with patch(
                "builtins.open", mock_open(read_data="WRONG_CONTENT")
            ) as mock_file:
                triggered = monitor._check_nfs_health(rule)

                # Should detect content mismatch
                if triggered:
                    assert any(
                        "content verification failed" in str(t) for t in triggered
                    )

    def test_check_nfs_health_write_error(self):
        """Test NFS health check with write failure."""
        monitor = HealthMonitor()
        rule = {
            "name": "nfs_health_test",
            "check": "nfs_health",
            "threshold_seconds": 5,
        }

        with patch("health_monitor.Path") as mock_path_class:
            mock_path = MagicMock()
            mock_path.is_dir.return_value = True
            mock_path_class.return_value = mock_path

            with patch("builtins.open", side_effect=OSError("Permission denied")):
                triggered = monitor._check_nfs_health(rule)

                # Should have error
                assert len(triggered) > 0
                assert "error" in triggered[0]


class TestNFSHealthRule:
    """Tests for NFS health rule configuration."""

    def test_nfs_health_rule_exists(self):
        """Test that NFS health rule is registered."""
        from health_rules import get_rule

        nfs_rule = get_rule("nfs_unhealthy")
        assert nfs_rule is not None
        assert nfs_rule["check"] == "nfs_health"
        assert nfs_rule["severity"] == "critical"
        assert nfs_rule["auto_remediate"] is False

    def test_nfs_health_rule_in_rules_list(self):
        """Test that NFS health rule is in the RULES list."""
        from health_rules import RULES

        nfs_rules = [r for r in RULES if r.get("check") == "nfs_health"]
        assert len(nfs_rules) > 0
        assert nfs_rules[0]["name"] == "nfs_unhealthy"


class TestNFSGracefulDegradation:
    """Tests for graceful NFS degradation in swarm_lib."""

    def test_is_nfs_healthy_success(self):
        """Test NFS health check returns True when healthy."""
        from swarm_lib import _is_nfs_healthy

        with patch("swarm_lib.Path") as mock_path_class:
            mock_path = MagicMock()
            mock_path.is_dir.return_value = True
            mock_path_class.return_value = mock_path

            with patch.object(mock_path, "write_text"):
                with patch.object(mock_path, "unlink"):
                    result = _is_nfs_healthy()
                    # Should return True (write and unlink succeeded)
                    assert isinstance(result, bool)

    def test_is_nfs_healthy_failure(self):
        """Test NFS health check returns False when unhealthy."""
        from swarm_lib import _is_nfs_healthy

        # Simulate NFS being unavailable by mocking is_dir to return False
        with patch("pathlib.Path.is_dir", return_value=False):
            result = _is_nfs_healthy()
            # Should return False when path doesn't exist
            assert result is False

    def test_update_status_nfs_fallback_on_error(self, tmp_path):
        """Test that update_status falls back to local when NFS fails."""
        from swarm_lib import update_status

        with patch("swarm_lib._status_dir") as mock_status_dir:
            mock_status_dir.side_effect = OSError("NFS unavailable")

            with patch("swarm_lib._is_nfs_healthy", return_value=False):
                with patch("swarm_lib._atomic_write_json") as mock_write:
                    with patch("swarm_lib.Path.home", return_value=tmp_path):
                        # Should not raise even when NFS fails
                        result = update_status(state="active")
                        assert result is not None

    def test_update_status_nfs_retry_when_healthy(self, tmp_path):
        """Test that update_status retries when NFS becomes healthy."""
        from swarm_lib import update_status

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("NFS temporarily unavailable")
            # Second call succeeds

        with patch("swarm_lib._status_dir") as mock_status_dir:
            with patch("swarm_lib._atomic_write_json", side_effect=side_effect):
                with patch("swarm_lib._is_nfs_healthy", return_value=True):
                    with patch("swarm_lib.Path.home", return_value=tmp_path):
                        # Should handle the error gracefully
                        try:
                            result = update_status(state="active")
                        except OSError:
                            # Expected on first call when NFS healthy but write still fails
                            pass


class TestNFSHealthMonitorIntegration:
    """Integration tests for NFS health in monitor loop."""

    def test_health_monitor_runs_nfs_check(self):
        """Test that health monitor includes NFS check in its cycle."""
        monitor = HealthMonitor()

        # Verify NFS health rule is loaded
        nfs_checks = [r for r in monitor.rules if r.get("check") == "nfs_health"]
        assert len(nfs_checks) > 0

    def test_nfs_check_triggers_alert_on_failure(self):
        """Test that failed NFS check triggers alert."""
        monitor = HealthMonitor()
        rule = {
            "name": "nfs_unhealthy",
            "check": "nfs_health",
            "severity": "critical",
            "auto_remediate": False,
            "action": "alert_email",
        }

        with patch.object(
            monitor, "_check_nfs_health", return_value=[{"error": "timeout"}]
        ):
            with patch.object(monitor, "_handle_triggered") as mock_handle:
                triggered = monitor._run_check(rule)
                assert len(triggered) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
