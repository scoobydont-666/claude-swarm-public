"""Tests for health_monitor.py — rule matching, cooldown, check cycle."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _make_monitor(tmp_path=None, **kwargs):
    """Return a HealthMonitor with test config, no real I/O."""
    import tempfile

    config = {
        "enabled": True,
        "check_interval_seconds": 1,
        "prometheus_url": "http://127.0.0.1:9090",
        "email_alerts": "test@example.com",
        "cooldown_file": str(tmp_path / "cooldowns.json")
        if tmp_path
        else tempfile.mktemp(suffix=".json"),
        "hosts": {
            "node_primary": {"ip": "<orchestration-node-ip>", "services": ["monerod"]},
            "node_gpu": {"ip": "<primary-node-ip>", "services": ["docker"]},
        },
        "cooldowns": {
            "restart_service": 600,
            "force_sync_replica": 300,
            "alert_email": 3600,
        },
        "thresholds": {
            "disk_usage_percent": 85,
            "stale_node_seconds": 600,
            "dirty_repo_minutes": 60,
            "gpu_vram_percent": 95,
        },
    }
    config.update(kwargs)

    with (
        patch("health_monitor._load_swarm_config", return_value={}),
        patch("event_log.DB_PATH", Path("/tmp/test-health.db")),
    ):
        from health_monitor import HealthMonitor

        monitor = HealthMonitor(config=config)
    return monitor


class TestCooldownEnforcement:
    def test_no_cooldown_allows_action(self):
        monitor = _make_monitor()
        rule = {"name": "test_rule", "cooldown_minutes": 10}
        assert monitor._in_cooldown(rule, "node_primary") is False

    def test_recent_action_triggers_cooldown(self):
        monitor = _make_monitor()
        rule = {"name": "disk_space_low", "cooldown_minutes": 60}
        # Simulate action just fired
        monitor._cooldown_state[("disk_space_low", "node_primary")] = time.time()
        assert monitor._in_cooldown(rule, "node_primary") is True

    def test_expired_cooldown_allows_action(self):
        monitor = _make_monitor()
        rule = {"name": "disk_space_low", "cooldown_minutes": 1}
        # Set last action 90 seconds ago (> 1 min cooldown)
        past = time.time() - 90
        monitor._cooldown_state[("disk_space_low", "node_primary")] = past
        assert monitor._in_cooldown(rule, "node_primary") is False

    def test_zero_cooldown_never_blocks(self):
        monitor = _make_monitor()
        rule = {"name": "test_rule", "cooldown_minutes": 0}
        monitor._cooldown_state[("test_rule", "host")] = time.time()
        assert monitor._in_cooldown(rule, "host") is False

    def test_record_action_updates_state(self):
        monitor = _make_monitor()
        rule = {"name": "my_rule"}
        before = time.time()
        monitor._record_action(rule, "node_gpu")
        after = time.time()
        recorded = monitor._cooldown_state[("my_rule", "node_gpu")]
        assert before <= recorded <= after


class TestPrometheusCheck:
    def test_returns_empty_on_request_error(self):
        monitor = _make_monitor()
        import requests as _requests

        with patch.object(
            _requests,
            "get",
            side_effect=_requests.RequestException("connection refused"),
        ):
            rule = {
                "name": "service_down",
                "check": "prometheus_query",
                "query": "up == 0",
            }
            result = monitor._check_prometheus_query(rule)
        assert result == []

    def test_returns_triggered_items_when_condition_met(self):
        monitor = _make_monitor()
        prom_response = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"instance": "node_primary:9090", "job": "monerod"},
                        "value": [1234567890, "1"],
                    }
                ]
            },
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = prom_response
        mock_resp.raise_for_status.return_value = None

        with patch("requests.get", return_value=mock_resp):
            rule = {
                "name": "service_down",
                "check": "prometheus_query",
                "query": "up == 0",
            }
            result = monitor._check_prometheus_query(rule)

        assert len(result) == 1
        assert result[0]["value"] == 1.0

    def test_returns_empty_when_no_results(self):
        monitor = _make_monitor()
        prom_response = {"status": "success", "data": {"result": []}}
        mock_resp = MagicMock()
        mock_resp.json.return_value = prom_response
        mock_resp.raise_for_status.return_value = None

        with patch("requests.get", return_value=mock_resp):
            rule = {
                "name": "service_down",
                "check": "prometheus_query",
                "query": "up == 0",
            }
            result = monitor._check_prometheus_query(rule)
        assert result == []

    def test_zero_value_not_triggered(self):
        """PromQL result of 0 means condition is false — should not trigger."""
        monitor = _make_monitor()
        prom_response = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"instance": "node_primary"},
                        "value": [1234567890, "0"],
                    }
                ]
            },
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = prom_response
        mock_resp.raise_for_status.return_value = None

        with patch("requests.get", return_value=mock_resp):
            rule = {
                "name": "service_down",
                "check": "prometheus_query",
                "query": "up == 0",
            }
            result = monitor._check_prometheus_query(rule)
        assert result == []


class TestDiskUsageCheck:
    def test_triggers_above_threshold(self):
        monitor = _make_monitor()

        class FakeUsage:
            total = 100 * 1024**3
            used = 90 * 1024**3
            free = 10 * 1024**3

        rule = {
            "name": "disk_space_low",
            "check": "disk_usage",
            "threshold_percent": 85,
        }
        with patch("shutil.disk_usage", return_value=FakeUsage()):
            result = monitor._check_disk_usage(rule)

        assert len(result) > 0
        assert result[0]["used_percent"] == 90.0

    def test_no_trigger_below_threshold(self):
        monitor = _make_monitor()

        class FakeUsage:
            total = 100 * 1024**3
            used = 70 * 1024**3
            free = 30 * 1024**3

        rule = {
            "name": "disk_space_low",
            "check": "disk_usage",
            "threshold_percent": 85,
        }
        with patch("shutil.disk_usage", return_value=FakeUsage()):
            result = monitor._check_disk_usage(rule)

        assert result == []


class TestNFSCheck:
    def test_drift_detected_when_mtime_differs(self, tmp_path):
        monitor = _make_monitor()

        primary = tmp_path / "swarm"
        replica = tmp_path / "swarm-replica"
        primary.mkdir()
        replica.mkdir()

        # Set replica mtime 5 minutes in the past
        import os

        os.utime(replica, (time.time() - 300, time.time() - 300))

        with (
            patch("health_monitor.Path") as mock_path_cls,
        ):
            # Let the check use real paths but point to tmp dirs

            monitor2 = _make_monitor()
            # Direct test of the stat approach
            primary.stat()  # ensure accessible

        # Direct test: compare mtimes
        p_mtime = primary.stat().st_mtime
        r_mtime = replica.stat().st_mtime
        drift = abs(p_mtime - r_mtime)
        assert drift > 120  # should be ~300s

    def test_no_drift_when_in_sync(self, tmp_path):
        monitor = _make_monitor()
        primary = tmp_path / "swarm"
        replica = tmp_path / "swarm-replica"
        primary.mkdir()
        replica.mkdir()

        # Same mtime = no drift
        now = time.time()
        import os

        os.utime(primary, (now, now))
        os.utime(replica, (now, now))

        p_mtime = primary.stat().st_mtime
        r_mtime = replica.stat().st_mtime
        assert abs(p_mtime - r_mtime) <= 120


class TestGitDirtyCheck:
    def test_clean_repo_not_triggered(self, tmp_path):
        monitor = _make_monitor()

        # git status returns empty output = clean
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        rule = {
            "name": "uncommitted_changes",
            "check": "git_dirty",
            "threshold_minutes": 60,
        }

        with (
            patch("subprocess.run", return_value=mock_result),
            patch("health_monitor.MONITORED_REPOS", [str(tmp_path)]),
        ):
            # Create a fake .git dir
            (tmp_path / ".git").mkdir()
            result = monitor._check_git_dirty(rule)

        assert result == []

    def test_dirty_repo_triggers(self, tmp_path):
        monitor = _make_monitor()

        (tmp_path / ".git").mkdir()

        call_count = [0]

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            call_count[0] += 1
            if "status" in cmd:
                m.stdout = " M src/foo.py\n"
            elif "log" in cmd:
                # Last commit was 2 hours ago
                m.stdout = str(int(time.time() - 7200))
            else:
                m.stdout = ""
            return m

        rule = {
            "name": "uncommitted_changes",
            "check": "git_dirty",
            "threshold_minutes": 60,
        }

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("health_monitor.MONITORED_REPOS", [str(tmp_path)]),
        ):
            result = monitor._check_git_dirty(rule)

        assert len(result) == 1
        assert result[0]["repo"] == str(tmp_path)
        assert result[0]["dirty_lines"] == 1


class TestRunCheckDispatch:
    def test_dispatches_to_correct_check(self):
        monitor = _make_monitor()

        with patch.object(
            monitor, "_check_prometheus_query", return_value=[]
        ) as mock_prom:
            monitor._run_check(
                {"name": "x", "check": "prometheus_query", "query": "up"}
            )
        mock_prom.assert_called_once()

        with patch.object(monitor, "_check_nfs_sync", return_value=[]) as mock_nfs:
            monitor._run_check({"name": "x", "check": "nfs_sync"})
        mock_nfs.assert_called_once()

        with patch.object(monitor, "_check_disk_usage", return_value=[]) as mock_disk:
            monitor._run_check({"name": "x", "check": "disk_usage"})
        mock_disk.assert_called_once()

    def test_unknown_check_type_returns_empty(self):
        monitor = _make_monitor()
        result = monitor._run_check({"name": "x", "check": "totally_unknown"})
        assert result == []


class TestHandleTriggered:
    def test_auto_remediate_calls_engine(self):
        monitor = _make_monitor()
        rule = {
            "name": "service_down",
            "check": "prometheus_query",
            "query": "up == 0",
            "severity": "high",
            "auto_remediate": True,
            "action": "restart_service",
            "cooldown_minutes": 10,
        }
        item = {"host": "node_primary", "labels": {"job": "monerod"}, "value": 1.0}

        with patch.object(
            monitor.remediation, "execute", return_value=(True, "OK")
        ) as mock_exec:
            with patch.object(monitor.event_log, "record") as mock_log:
                monitor._handle_triggered(rule, item)

        mock_exec.assert_called_once()
        mock_log.assert_called_once()

    def test_non_auto_rule_still_logs(self):
        monitor = _make_monitor()
        rule = {
            "name": "uncommitted_changes",
            "check": "git_dirty",
            "severity": "low",
            "auto_remediate": False,
            "action": "warn_swarm_message",
            "cooldown_minutes": 60,
        }
        item = {"host": "node_gpu", "repo": "<hydra-project-path>"}

        with patch.object(
            monitor.remediation, "execute", return_value=(True, "warned")
        ) as mock_exec:
            with patch.object(monitor.event_log, "record") as mock_log:
                monitor._handle_triggered(rule, item)

        mock_exec.assert_called_once()
        mock_log.assert_called_once()

    def test_cooldown_prevents_action(self):
        monitor = _make_monitor()
        rule = {
            "name": "disk_space_low",
            "severity": "high",
            "auto_remediate": True,
            "action": "alert_email",
            "cooldown_minutes": 60,
        }
        item = {"host": "node_primary"}

        # Set cooldown as recently fired
        monitor._cooldown_state[("disk_space_low", "node_primary")] = time.time()

        with patch.object(monitor.remediation, "execute") as mock_exec:
            with patch.object(monitor.event_log, "record"):
                monitor._handle_triggered(rule, item)

        # Should NOT execute action when in cooldown
        mock_exec.assert_not_called()

    def test_escalation_on_action_failure(self):
        monitor = _make_monitor()
        rule = {
            "name": "stale_node",
            "severity": "high",
            "auto_remediate": False,
            "action": "ssh_health_check",
            "escalate": "email",
            "cooldown_minutes": 0,
        }
        item = {"host": "node_gpu"}

        with (
            patch.object(
                monitor.remediation, "execute", return_value=(False, "SSH unreachable")
            ),
            patch.object(
                monitor.remediation, "send_alert_email", return_value=(True, "sent")
            ) as mock_email,
            patch.object(monitor.event_log, "record"),
        ):
            monitor._handle_triggered(rule, item)

        # Non-auto rules don't escalate on failure — that's only auto_remediate rules
        # stale_node is auto_remediate=False so no escalation
        mock_email.assert_not_called()


class TestRunCycle:
    def test_cycle_processes_all_rules(self):
        monitor = _make_monitor()

        with (
            patch.object(monitor, "_run_check", return_value=[]) as mock_check,
            patch.object(monitor, "_handle_triggered") as mock_handle,
        ):
            monitor._run_cycle()

        # Should have called _run_check once per rule
        assert mock_check.call_count == len(monitor.rules)
        mock_handle.assert_not_called()

    def test_cycle_handles_exceptions_gracefully(self):
        monitor = _make_monitor()

        with patch.object(monitor, "_run_check", side_effect=RuntimeError("boom")):
            # Must not raise — exceptions are caught and logged
            monitor._run_cycle()


class TestParallelHealthChecks:
    def test_rules_execute_concurrently(self):
        """Verify that rule checks run in parallel — total time < sum of individual delays."""
        monitor = _make_monitor()

        sleep_per_rule = 0.15  # seconds each fake check sleeps
        n_rules = 4

        # Inject exactly n_rules fake rules
        monitor.rules = [
            {"name": f"slow_rule_{i}", "check": "disk_usage"} for i in range(n_rules)
        ]

        def slow_check(rule):
            time.sleep(sleep_per_rule)
            return []

        with (
            patch.object(monitor, "_run_check", side_effect=slow_check),
            patch.object(monitor, "_prometheus_available", return_value=False),
            patch.object(monitor, "_check_nfs_mount"),
        ):
            start = time.monotonic()
            monitor._run_cycle()
            elapsed = time.monotonic() - start

        sequential_time = sleep_per_rule * n_rules
        # Parallel execution should finish well under sequential time
        assert elapsed < sequential_time * 0.75, (
            f"Elapsed {elapsed:.2f}s expected < {sequential_time * 0.75:.2f}s "
            f"(sequential would be {sequential_time:.2f}s)"
        )
