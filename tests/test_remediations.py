"""Tests for remediations.py — service restart (mocked SSH), sync, email, dispatch."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remediations import RemediationEngine


@pytest.fixture
def engine():
    return RemediationEngine(
        ssh_user="josh",
        email_to="r.josh.jones@gmail.com",
        replica_sync_script="/usr/local/bin/swarm-replica-sync.sh",
    )


class TestRestartService:
    def test_rejects_unlisted_service(self, engine):
        with pytest.raises(ValueError, match="not in allowlist"):
            engine.restart_service("miniboss", "evil-service")

    def test_rejects_unknown_host(self, engine):
        with pytest.raises(ValueError, match="not in allowlist"):
            engine.restart_service("unknown-host", "monerod")

    def test_rejects_shell_metacharacters(self, engine):
        # Even if it were on the allowlist, injection must fail
        # (allowlist validation catches this before SSH)
        with pytest.raises(ValueError):
            engine.restart_service("miniboss", "monerod; rm -rf /")

    def test_successful_restart(self, engine):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "restarted"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            success, detail = engine.restart_service("miniboss", "monerod")

        assert success is True
        assert "monerod" in detail
        # Validate SSH command structure — no shell=True
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert isinstance(cmd, list)
        assert "ssh" in cmd[0]
        assert "sudo" in " ".join(cmd)
        assert "monerod" in " ".join(cmd)

    def test_failed_restart(self, engine):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Unit monerod.service not found"

        with patch("subprocess.run", return_value=mock_result):
            success, detail = engine.restart_service("miniboss", "monerod")

        assert success is False
        assert "failed" in detail.lower()

    def test_ssh_timeout_handled(self, engine):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 30)):
            success, detail = engine.restart_service("miniboss", "monerod")

        assert success is False
        assert "timed out" in detail.lower()

    def test_giga_services_allowed(self, engine):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            success, _ = engine.restart_service("GIGA", "docker")
        assert success is True


class TestForceSyncReplica:
    def test_successful_sync(self, engine):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Sync complete"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            success, detail = engine.force_sync_replica()

        assert success is True
        assert "OK" in detail

    def test_sync_script_not_found(self, engine):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            success, detail = engine.force_sync_replica()

        assert success is False
        assert "not found" in detail.lower()

    def test_sync_failure(self, engine):
        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stdout = ""
        mock_result.stderr = "Permission denied"

        with patch("subprocess.run", return_value=mock_result):
            success, detail = engine.force_sync_replica()

        assert success is False
        assert "failed" in detail.lower()

    def test_sync_timeout(self, engine):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("sync", 120)):
            success, detail = engine.force_sync_replica()

        assert success is False
        assert "timed out" in detail.lower()


class TestSendAlertEmail:
    def test_successful_email(self, engine):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            success, detail = engine.send_alert_email(
                subject="miniboss-health-alert-service_down-2026-03-22",
                body="monerod is down",
            )

        assert success is True
        assert "r.josh.jones@gmail.com" in detail
        # Validate msmtp invocation
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert "msmtp" in cmd[0]
        assert "r.josh.jones@gmail.com" in cmd

    def test_msmtp_not_found(self, engine):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            success, detail = engine.send_alert_email("test", "body")

        assert success is False
        assert "not found" in detail.lower()

    def test_msmtp_failure(self, engine):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "authentication failed"

        with patch("subprocess.run", return_value=mock_result):
            success, detail = engine.send_alert_email("test", "body")

        assert success is False

    def test_email_subject_contains_hostname_and_rule(self, engine):
        """Email subjects must follow: {hostname}-health-alert-{rule}-{date}"""
        captured_input = {}

        def capture_run(*args, **kwargs):
            captured_input["input"] = kwargs.get("input", "")
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=capture_run):
            engine.send_alert_email(
                subject="GIGA-health-alert-gpu_vram_full-2026-03-22",
                body="GPU VRAM at 98%",
            )

        assert "GIGA-health-alert-gpu_vram_full-2026-03-22" in captured_input["input"]


class TestSendSwarmMessage:
    def test_send_to_host(self, engine):
        with patch("swarm_lib.send_message") as mock_send:
            success, detail = engine.send_swarm_message("miniboss", "disk at 90%")

        mock_send.assert_called_once()
        assert success is True

    def test_broadcast(self, engine):
        with patch("swarm_lib.broadcast_message") as mock_bcast:
            success, detail = engine.send_swarm_broadcast("Prometheus target down")

        mock_bcast.assert_called_once()
        assert success is True

    def test_swarm_lib_import_failure_handled(self, engine):
        # swarm_lib is already imported at module load time in the test environment.
        # Test that send_swarm_message returns gracefully when swarm_lib.send_message raises.
        with patch("swarm_lib.send_message", side_effect=RuntimeError("NFS unavailable")):
            success, detail = engine.send_swarm_message("miniboss", "test message")
        assert success is False
        assert "send_message failed" in detail


class TestDispatchFix:
    def test_dispatch_returns_id(self, engine):
        mock_result = MagicMock()
        mock_result.dispatch_id = "dispatch-1234-GIGA"

        with patch("hydra_dispatch.dispatch", return_value=mock_result):
            dispatch_id = engine.dispatch_fix("GIGA", "restart failing service")

        assert dispatch_id == "dispatch-1234-GIGA"

    def test_dispatch_failure_returns_error_string(self, engine):
        with patch("hydra_dispatch.dispatch", side_effect=RuntimeError("SSH failed")):
            result = engine.dispatch_fix("GIGA", "fix something")

        assert "dispatch_fix failed" in result


class TestExecuteDispatcher:
    def test_unknown_action(self, engine):
        success, detail = engine.execute(action="totally_unknown_action")
        assert success is False
        assert "Unknown action" in detail

    def test_restart_service_dispatched(self, engine):
        with patch.object(engine, "restart_service", return_value=(True, "OK")) as mock:
            success, _ = engine.execute(
                action="restart_service", host="miniboss", service="monerod"
            )
        mock.assert_called_once_with("miniboss", "monerod")
        assert success is True

    def test_force_sync_dispatched(self, engine):
        with patch.object(engine, "force_sync_replica", return_value=(True, "synced")) as mock:
            success, _ = engine.execute(action="force_sync_replica")
        mock.assert_called_once()
        assert success is True

    def test_alert_email_dispatched(self, engine):
        with patch.object(engine, "send_alert_email", return_value=(True, "sent")) as mock:
            success, _ = engine.execute(
                action="alert_email",
                subject="test subject",
                body="test body",
            )
        mock.assert_called_once_with("test subject", "test body")
        assert success is True


class TestRequeueTaskAtomicWrite:
    """S6.1 — requeue_task() must write atomically and tolerate a failed remove."""

    def _setup_task(self, tmp_path, task_id="task-001", retries=0):
        """Create a claimed task file and return paths."""
        import yaml as _yaml

        claimed_dir = tmp_path / "tasks" / "claimed"
        claimed_dir.mkdir(parents=True)
        pending_dir = tmp_path / "tasks" / "pending"
        pending_dir.mkdir(parents=True)
        task_data = {
            "id": task_id,
            "type": "test",
            "_retries": retries,
            "claimed_by": "miniboss",
            "claimed_at": "2026-03-31T00:00:00Z",
        }
        task_file = claimed_dir / f"{task_id}.yaml"
        with open(task_file, "w") as f:
            _yaml.dump(task_data, f)
        return claimed_dir, pending_dir, task_file

    def _patch_paths(self, claimed_dir, pending_dir):
        """Return a Path side-effect that redirects swarm paths to tmp_path."""
        from pathlib import Path as _Path

        def patched_path(p):
            s = str(p)
            if s == "/opt/swarm/tasks/claimed":
                return claimed_dir
            if s == "/opt/swarm/tasks/pending":
                return pending_dir
            return _Path(p)

        return patched_path

    def test_requeue_failed_remove_does_not_raise(self, engine, tmp_path):
        """If os.remove raises, requeue_task must succeed and pending file must exist."""
        import yaml

        task_id = "task-remove-fail-001"
        claimed_dir, pending_dir, _ = self._setup_task(tmp_path, task_id)

        # requeue_task uses local `from pathlib import Path` so we patch pathlib.Path
        with patch("pathlib.Path", side_effect=self._patch_paths(claimed_dir, pending_dir)):
            with patch("os.remove", side_effect=OSError("simulated disk error")):
                success, detail = engine.requeue_task(task_id=task_id)

        assert success is True, f"Expected success, got: {detail}"
        pending_file = pending_dir / f"{task_id}.yaml"
        assert pending_file.exists(), "Pending file must exist even when remove fails"
        loaded = yaml.safe_load(pending_file.read_text())
        assert loaded["id"] == task_id
        assert loaded.get("_retries") == 1
        assert "claimed_by" not in loaded

    def test_requeue_pending_written_before_claimed_removed(self, engine, tmp_path):
        """Pending file must already exist at the moment os.remove is called."""

        task_id = "task-order-001"
        claimed_dir, pending_dir, claimed_file = self._setup_task(tmp_path, task_id)
        pending_file = pending_dir / f"{task_id}.yaml"

        pending_existed_at_remove = []
        original_remove = __import__("os").remove

        def tracking_remove(path):
            pending_existed_at_remove.append(pending_file.exists())
            original_remove(path)

        with patch("pathlib.Path", side_effect=self._patch_paths(claimed_dir, pending_dir)):
            with patch("os.remove", side_effect=tracking_remove):
                success, detail = engine.requeue_task(task_id=task_id)

        assert success is True, detail
        assert pending_existed_at_remove, "os.remove was never called"
        assert pending_existed_at_remove[0] is True, (
            "Pending file did not exist before claimed was removed"
        )

    def test_requeue_pending_file_is_valid_yaml(self, engine, tmp_path):
        """Atomic write must produce a valid YAML file (not a partial/corrupt write)."""
        import yaml

        task_id = "task-valid-yaml-001"
        claimed_dir, pending_dir, _ = self._setup_task(tmp_path, task_id)

        with patch("pathlib.Path", side_effect=self._patch_paths(claimed_dir, pending_dir)):
            success, _ = engine.requeue_task(task_id=task_id)

        assert success is True
        pending_file = pending_dir / f"{task_id}.yaml"
        assert pending_file.exists()
        loaded = yaml.safe_load(pending_file.read_text())
        assert isinstance(loaded, dict)
        assert loaded["id"] == task_id


class TestKillHungTask:
    """S6.3 — kill_hung_task() via SSH: SIGTERM then SIGKILL."""

    def test_missing_host_returns_false(self, engine):
        success, detail = engine.kill_hung_task("", 1234)
        assert success is False
        assert "missing" in detail

    def test_missing_pid_returns_false(self, engine):
        success, detail = engine.kill_hung_task("miniboss", 0)
        assert success is False
        assert "missing" in detail

    def test_unknown_host_returns_false(self, engine):
        success, detail = engine.kill_hung_task("no-such-host", 1234)
        assert success is False
        assert "No IP" in detail

    def test_sigterm_sent_first(self, engine):
        """SIGTERM is the first SSH call (kill <pid>)."""
        calls = []

        def mock_ssh_run(host_ip, remote_cmd, timeout=30):
            calls.append(list(remote_cmd))
            # SIGTERM succeeds; kill -0 check returns non-zero (process gone)
            if remote_cmd == ["kill", "9876"]:
                return True, ""
            if remote_cmd == ["kill", "-0", "9876"]:
                return False, ""  # process gone
            return False, ""

        with (
            patch.object(engine, "_resolve_host_ip", return_value="192.168.200.213"),
            patch.object(engine, "_ssh_run", side_effect=mock_ssh_run),
            patch("time.sleep"),
        ):
            success, detail = engine.kill_hung_task("miniboss", 9876)

        assert success is True
        assert calls[0] == ["kill", "9876"]  # SIGTERM first

    def test_sigkill_sent_if_process_still_alive(self, engine):
        """If kill -0 succeeds (process alive), SIGKILL must follow."""
        calls = []

        def mock_ssh_run(host_ip, remote_cmd, timeout=30):
            calls.append(list(remote_cmd))
            if remote_cmd == ["kill", "1111"]:
                return True, ""  # SIGTERM sent
            if remote_cmd == ["kill", "-0", "1111"]:
                return True, ""  # process still alive
            if remote_cmd == ["kill", "-9", "1111"]:
                return True, ""  # SIGKILL
            return False, ""

        with (
            patch.object(engine, "_resolve_host_ip", return_value="192.168.200.213"),
            patch.object(engine, "_ssh_run", side_effect=mock_ssh_run),
            patch("time.sleep"),
        ):
            success, detail = engine.kill_hung_task("miniboss", 1111)

        assert success is True
        assert ["kill", "-9", "1111"] in calls

    def test_sigterm_failure_returns_false(self, engine):
        """If SIGTERM SSH call fails, return failure immediately."""

        def mock_ssh_run(host_ip, remote_cmd, timeout=30):
            return False, "SSH connection refused"

        with (
            patch.object(engine, "_resolve_host_ip", return_value="192.168.200.213"),
            patch.object(engine, "_ssh_run", side_effect=mock_ssh_run),
            patch("time.sleep"),
        ):
            success, detail = engine.kill_hung_task("miniboss", 5555)

        assert success is False
        assert "SIGTERM failed" in detail

    def test_requeue_calls_kill_before_move(self, engine, tmp_path):
        """requeue_task must attempt kill_hung_task when task has claimed_by + pid."""
        import yaml as _yaml

        task_id = "task-kill-test-001"
        claimed_dir = tmp_path / "tasks" / "claimed"
        claimed_dir.mkdir(parents=True)
        pending_dir = tmp_path / "tasks" / "pending"
        pending_dir.mkdir(parents=True)

        task_data = {
            "id": task_id,
            "type": "test",
            "_retries": 0,
            "claimed_by": "miniboss",
            "claimed_at": "2026-03-31T00:00:00Z",
            "pid": 12345,
        }
        task_file = claimed_dir / f"{task_id}.yaml"
        with open(task_file, "w") as f:
            _yaml.dump(task_data, f)

        from pathlib import Path as _Path

        def patched_path(p):
            s = str(p)
            if s == "/opt/swarm/tasks/claimed":
                return claimed_dir
            if s == "/opt/swarm/tasks/pending":
                return pending_dir
            return _Path(p)

        kill_calls = []

        def mock_kill(host, pid):
            kill_calls.append((host, pid))
            return True, f"killed pid {pid} on {host}"

        with (
            patch("pathlib.Path", side_effect=patched_path),
            patch.object(engine, "kill_hung_task", side_effect=mock_kill),
        ):
            success, detail = engine.requeue_task(task_id=task_id)

        assert success is True
        assert len(kill_calls) == 1
        assert kill_calls[0] == ("miniboss", 12345)

    def test_requeue_proceeds_if_kill_fails(self, engine, tmp_path):
        """requeue_task must succeed even when kill_hung_task fails."""
        import yaml as _yaml

        task_id = "task-kill-fail-001"
        claimed_dir = tmp_path / "tasks" / "claimed"
        claimed_dir.mkdir(parents=True)
        pending_dir = tmp_path / "tasks" / "pending"
        pending_dir.mkdir(parents=True)

        task_data = {
            "id": task_id,
            "type": "test",
            "_retries": 0,
            "claimed_by": "miniboss",
            "claimed_at": "2026-03-31T00:00:00Z",
            "pid": 99999,
        }
        task_file = claimed_dir / f"{task_id}.yaml"
        with open(task_file, "w") as f:
            _yaml.dump(task_data, f)

        from pathlib import Path as _Path

        def patched_path(p):
            s = str(p)
            if s == "/opt/swarm/tasks/claimed":
                return claimed_dir
            if s == "/opt/swarm/tasks/pending":
                return pending_dir
            return _Path(p)

        with (
            patch("pathlib.Path", side_effect=patched_path),
            patch.object(engine, "kill_hung_task", return_value=(False, "SSH timeout")),
        ):
            success, detail = engine.requeue_task(task_id=task_id)

        assert success is True
        assert (pending_dir / f"{task_id}.yaml").exists()
