"""Remediation actions for the claude-swarm health monitor.

All public methods return a (success: bool, detail: str) tuple so callers
can decide whether to escalate.

Security:
- ALL service names are validated against ALLOWED_SERVICES before any SSH.
- No shell=True; commands are lists.
- No user-supplied strings are ever interpolated into shell commands.
"""

import os
import subprocess
from typing import Optional

from health_rules import ALLOWED_SERVICES
from util import (
    now_iso as _now_iso,
    hostname as _hostname,
    atomic_write_yaml as _atomic_write_yaml,
)


class RemediationEngine:
    """Executes remediation actions. Some auto, some need approval."""

    def __init__(
        self,
        ssh_user: str = "user",
        email_to: str = "admin@example.com",
        replica_sync_script: str = "/usr/local/bin/swarm-replica-sync.sh",
    ) -> None:
        self.ssh_user = ssh_user
        self.email_to = email_to
        self.replica_sync_script = replica_sync_script

    # ── Internal helpers ────────────────────────────────────────────────────

    def _validate_service(self, host: str, service: str) -> None:
        """Raise ValueError if service is not on the allowlist for host."""
        allowed = ALLOWED_SERVICES.get(host, [])
        if service not in allowed:
            raise ValueError(
                f"Service '{service}' not in allowlist for host '{host}'. "
                f"Allowed: {allowed}"
            )

    def _ssh_run(
        self,
        host_ip: str,
        remote_cmd: list[str],
        timeout: int = 30,
    ) -> tuple[bool, str]:
        """Run a command on a remote host via SSH. Returns (success, output)."""
        cmd = [
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            f"{self.ssh_user}@{host_ip}",
            " ".join(remote_cmd),  # ssh takes the remote side as a single string
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout + result.stderr).strip()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, f"SSH command timed out after {timeout}s"
        except OSError as exc:
            return False, f"SSH failed to start: {exc}"

    def _resolve_host_ip(self, host: str) -> Optional[str]:
        """Return IP for a known fleet host, or None."""
        # Import lazily to avoid circular deps
        try:
            from hydra_dispatch import FLEET

            info = FLEET.get(host, {})
            return info.get("ip")
        except ImportError:
            # Fallback table — env vars override hardcoded defaults
            _FALLBACK = {
                "orchestration-node": os.environ.get("MINIBOSS_HOST", "10.0.0.5"),
                "gpu-server-1": os.environ.get("gpu-server-1-host", "10.0.0.1"),
            }
            return _FALLBACK.get(host)

    # ── Remediation actions ─────────────────────────────────────────────────

    def restart_service(self, host: str, service: str) -> tuple[bool, str]:
        """SSH to host, restart a systemd service.

        Validates service name against allowlist. Returns (success, detail).
        """
        self._validate_service(host, service)

        ip = self._resolve_host_ip(host)
        if not ip:
            return False, f"No IP known for host '{host}'"

        success, output = self._ssh_run(
            ip,
            ["sudo", "systemctl", "restart", service],
        )
        status = "restarted" if success else "restart failed"
        return success, f"{host}/{service} {status}: {output}"

    def force_sync_replica(self) -> tuple[bool, str]:
        """Run the NFS replica sync script locally."""
        try:
            result = subprocess.run(
                [self.replica_sync_script],
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode == 0:
                return True, f"Replica sync OK: {output[:200]}"
            return (
                False,
                f"Replica sync failed (rc={result.returncode}): {output[:200]}",
            )
        except FileNotFoundError:
            return False, f"Sync script not found: {self.replica_sync_script}"
        except subprocess.TimeoutExpired:
            return False, "Replica sync timed out after 120s"

    def ssh_health_check(self, host: str) -> dict:
        """SSH to host, collect basic health data. Returns a status dict."""
        ip = self._resolve_host_ip(host)
        if not ip:
            return {"host": host, "reachable": False, "error": "No IP known"}

        checks: dict = {"host": host, "ip": ip, "checked_at": _now_iso()}

        # uptime
        ok, out = self._ssh_run(ip, ["uptime", "-p"])
        checks["uptime"] = out if ok else "unreachable"
        checks["reachable"] = ok

        if not ok:
            return checks

        # disk — root partition
        ok, out = self._ssh_run(ip, ["df", "-h", "/"])
        checks["disk_root"] = out

        # memory
        ok, out = self._ssh_run(ip, ["free", "-h"])
        checks["memory"] = out

        # failed services
        ok, out = self._ssh_run(
            ip,
            ["systemctl", "--failed", "--no-legend", "--no-pager"],
        )
        checks["failed_services"] = out if out else "none"

        return checks

    def dispatch_fix(
        self,
        host: str,
        task: str,
        model: str = "haiku",
    ) -> str:
        """Dispatch a fix task to a remote host via hydra_dispatch.

        Returns the dispatch_id or an error string.
        """
        try:
            from hydra_dispatch import dispatch

            result = dispatch(
                host=host,
                task=task,
                model=model,
                background=True,
            )
            return result.dispatch_id
        except Exception as exc:
            return f"dispatch_fix failed: {exc}"

    def send_alert_email(self, subject: str, body: str) -> tuple[bool, str]:
        """Send an alert email via msmtp."""
        try:
            proc = subprocess.run(
                ["msmtp", self.email_to],
                input=f"Subject: {subject}\n\n{body}\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return True, f"Email sent to {self.email_to}"
            return False, f"msmtp failed (rc={proc.returncode}): {proc.stderr.strip()}"
        except FileNotFoundError:
            return False, "msmtp not found — email not sent"
        except subprocess.TimeoutExpired:
            return False, "msmtp timed out"

    def send_swarm_message(self, host: str, message: str) -> tuple[bool, str]:
        """Post a message to a specific swarm node's inbox."""
        try:
            from swarm_lib import send_message

            send_message(target=host, text=message, sender=_hostname())
            return True, f"Message sent to {host}"
        except Exception as exc:
            return False, f"send_message failed: {exc}"

    def send_swarm_broadcast(self, message: str) -> tuple[bool, str]:
        """Broadcast a message to all swarm nodes."""
        try:
            from swarm_lib import broadcast_message

            broadcast_message(text=message, sender=_hostname())
            return True, "Broadcast sent"
        except Exception as exc:
            return False, f"broadcast failed: {exc}"

    def kill_hung_task(self, host: str, pid: int) -> tuple[bool, str]:
        """Kill a hung task process via SSH. SIGTERM first, then SIGKILL after 5s."""
        import time as _time

        if not host or not pid:
            return False, "missing host or pid"

        host_ip = self._resolve_host_ip(host)
        if not host_ip:
            return False, f"No IP known for host '{host}'"

        # SIGTERM first
        ok, detail = self._ssh_run(host_ip, ["kill", str(pid)])
        if not ok:
            return False, f"SIGTERM failed: {detail}"

        # Wait 5s, then check if still alive
        _time.sleep(5)
        alive_ok, _ = self._ssh_run(host_ip, ["kill", "-0", str(pid)])
        if alive_ok:
            # Still alive — SIGKILL
            self._ssh_run(host_ip, ["kill", "-9", str(pid)])

        return True, f"killed pid {pid} on {host}"

    def requeue_task(
        self, host: str = "", task_id: str = "", **kwargs
    ) -> tuple[bool, str]:
        """Requeue a deadline-exceeded task back to pending if retries < 3.

        Extracts task_id from kwargs if needed (from health monitor item).
        Returns (success, detail).
        """
        # Extract task_id from various possible sources
        actual_task_id = task_id or kwargs.get("task_id", "")
        if not actual_task_id:
            return False, "No task_id provided"

        try:
            import os
            import yaml
            from pathlib import Path

            claimed_dir = Path("/var/lib/swarm/tasks/claimed")
            task_file = claimed_dir / f"{actual_task_id}.yaml"

            if not task_file.exists():
                return False, f"Task {actual_task_id} not found in claimed/"

            with open(task_file) as f:
                task = yaml.safe_load(f) or {}

            retries = task.get("_retries", 0)
            if retries >= 3:
                # Don't auto-requeue — escalate instead
                return (
                    False,
                    f"Task {actual_task_id} exceeded max retries (3). Manual intervention needed.",
                )

            # Kill the hung process before requeuing
            claimed_by = task.get("claimed_by", "")
            claimed_pid = task.get("pid")
            if claimed_by and claimed_pid:
                import logging as _logging

                kill_ok, kill_detail = self.kill_hung_task(claimed_by, int(claimed_pid))
                _logging.getLogger(__name__).info(
                    "requeue_task: kill_hung_task(%s, %s): %s — %s",
                    claimed_by,
                    claimed_pid,
                    "ok" if kill_ok else "fail",
                    kill_detail,
                )

            # Move back to pending and increment retries
            task["_retries"] = retries + 1
            task.pop("claimed_by", None)
            task.pop("claimed_at", None)

            pending_dir = Path("/var/lib/swarm/tasks/pending")
            pending_dir.mkdir(parents=True, exist_ok=True)
            pending_file = pending_dir / f"{actual_task_id}.yaml"

            # Write atomically to pending/ FIRST (safe ordering)
            _atomic_write_yaml(pending_file, task)

            # Remove from claimed — log warning if it fails (task is already safe in pending)
            try:
                os.remove(task_file)
            except OSError as remove_err:
                import logging

                logging.getLogger(__name__).warning(
                    "requeue_task: could not remove claimed file %s: %s",
                    task_file,
                    remove_err,
                )

            return (
                True,
                f"Task {actual_task_id} requeued to pending (retry {retries + 1}/3)",
            )

        except Exception as exc:
            return False, f"requeue_task failed: {exc}"

    # ── Composite action dispatcher ─────────────────────────────────────────

    def execute(
        self,
        action: str,
        host: str = "",
        service: str = "",
        message: str = "",
        subject: str = "",
        body: str = "",
        task: str = "",
        model: str = "haiku",
        **kwargs,
    ) -> tuple[bool, str]:
        """Dispatch to the appropriate remediation method by action name.

        Returns (success, detail).
        """
        if action == "restart_service":
            return self.restart_service(host, service)

        if action == "force_sync_replica":
            return self.force_sync_replica()

        if action == "ssh_health_check":
            result = self.ssh_health_check(host)
            ok = result.get("reachable", False)
            return ok, str(result)

        if action == "dispatch_fix":
            dispatch_id = self.dispatch_fix(host, task, model)
            return True, dispatch_id

        if action == "alert_email":
            return self.send_alert_email(
                subject or "swarm health alert", body or message
            )

        if action == "warn_swarm_message":
            return self.send_swarm_message(host, message)

        if action == "alert_swarm_broadcast":
            return self.send_swarm_broadcast(message)

        if action == "requeue_task":
            return self.requeue_task(host=host, task_id=task, **kwargs)

        if action == "kill_hung_task":
            pid = kwargs.get("pid", 0)
            return self.kill_hung_task(host, int(pid) if pid else 0)

        return False, f"Unknown action: {action}"
