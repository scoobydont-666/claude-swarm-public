#!/usr/bin/env python3
"""claude-swarm Health Monitor — daemon that checks fleet health and auto-remediates.

Run as a daemon:
    python3 /opt/claude-swarm/src/health_monitor.py

Or manage via systemd:
    systemctl start swarm-health
    systemctl stop swarm-health

Loop: check → decide → act → log.
"""

import concurrent.futures
import logging
import os
import signal
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml

# Ensure src/ is on the path when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent))

from event_log import EventLog
from health_rules import RULES
from remediations import RemediationEngine
from util import now_iso as _now_iso
from util import now_ts as _now_ts
from util import projects_for_host

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_DIR = Path("/opt/claude-swarm/data")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "health-monitor.log"),
    ],
)
log = logging.getLogger("swarm.health")

# Repo paths monitored for dirty-check — loaded from swarm.yaml per-host config
MONITORED_REPOS: list[str] = projects_for_host()

# ---------------------------------------------------------------------------
# Default config (overridden by swarm.yaml health_monitor section)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "check_interval_seconds": 60,
    "prometheus_url": "http://127.0.0.1:9090",
    "email_alerts": "admin@example.com",
    "hosts": {
        "node_primary": {
            "ip": os.environ.get("MINIBOSS_HOST", "<orchestration-node-ip>"),
            "services": [
                "monerod",
                "p2pool-main",
                "p2pool-mini",
                "p2pool-nano",
                "prometheus",
                "grafana-server",
                "crowdsec",
            ],
        },
        "node_gpu": {
            "ip": os.environ.get("GIGA_HOST", "<primary-node-ip>"),
            "services": ["docker", "fail2ban", "crowdsec"],
        },
    },
    "cooldowns": {
        "restart_service": 600,
        "force_sync_replica": 300,
        "alert_email": 3600,
    },
    "thresholds": {
        "disk_usage_percent": 85,
        "stale_node_seconds": 600,
        "dirty_repo_minutes": 480,
        "gpu_vram_percent": 95,
    },
}


def _load_swarm_config() -> dict[str, Any]:
    """Load swarm.yaml and extract the health_monitor section."""
    from util import load_swarm_config

    config = load_swarm_config()
    return config.get("health_monitor", {})


def _merge_config(base: dict, override: dict) -> dict:
    """Shallow-merge override into base. Nested dicts are also merged one level."""
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = {**result[k], **v}
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# HealthMonitor
# ---------------------------------------------------------------------------


class HealthMonitor:
    """Watches Prometheus, services, NFS, and git state. Auto-remediates or escalates."""

    def __init__(self, config: dict | None = None) -> None:
        yaml_cfg = _load_swarm_config()
        self.config: dict[str, Any] = _merge_config(DEFAULT_CONFIG, yaml_cfg)
        if config:
            self.config = _merge_config(self.config, config)

        self.prometheus_url: str = self.config.get("prometheus_url", "http://127.0.0.1:9090")
        self.check_interval: int = int(self.config.get("check_interval_seconds", 60))
        self.hosts: dict[str, Any] = self.config.get("hosts", {})
        self.cooldowns_cfg: dict[str, int] = self.config.get("cooldowns", {})
        self.thresholds: dict[str, Any] = self.config.get("thresholds", {})
        self.email_to: str = self.config.get("email_alerts", "admin@example.com")

        self.rules: list[dict] = self._load_rules()
        self.event_log = EventLog()
        self.remediation = RemediationEngine(email_to=self.email_to)

        # Cooldown tracker: persisted to disk, loaded on startup
        self._cooldown_file = Path(
            self.config.get("cooldown_file", "/opt/claude-swarm/data/cooldowns.json")
        )
        self._cooldown_state: dict[tuple[str, str], float] = self._load_cooldowns()

        # Graceful shutdown flag
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)

        # Thread pool for parallel rule checks (remediation stays single-threaded)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        log.info("Received signal %d — shutting down", signum)
        self._running = False
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _load_rules(self) -> list[dict]:
        """Return a copy of RULES with cooldowns applied from config."""
        rules = []
        for rule in RULES:
            r = dict(rule)
            action = r.get("action", "")
            if action in self.cooldowns_cfg:
                r.setdefault("cooldown_minutes", self.cooldowns_cfg[action] // 60)
            rules.append(r)
        return rules

    # ── Cooldown management ────────────────────────────────────────────────

    def _cooldown_key(self, rule_name: str, host: str) -> tuple[str, str]:
        return (rule_name, host)

    def _in_cooldown(self, rule: dict, host: str) -> bool:
        """Return True if the rule+host is within its cooldown window."""
        cooldown_minutes = rule.get("cooldown_minutes", 0)
        if cooldown_minutes <= 0:
            return False
        key = self._cooldown_key(rule["name"], host)
        last = self._cooldown_state.get(key)
        if last is None:
            # Also check persistent event log
            last_str = self.event_log.last_action_time(rule["name"], host)
            if last_str:
                try:
                    dt = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
                    last = dt.timestamp()
                except ValueError:
                    last = None
        if last is None:
            return False
        elapsed_minutes = (_now_ts() - last) / 60
        return elapsed_minutes < cooldown_minutes

    def _record_action(self, rule: dict, host: str) -> None:
        """Mark this rule+host action as having just fired."""
        self._cooldown_state[self._cooldown_key(rule["name"], host)] = _now_ts()
        self._save_cooldowns()

    def _load_cooldowns(self) -> dict[tuple[str, str], float]:
        """Load cooldown state from disk."""
        import json as _json

        if not self._cooldown_file.exists():
            return {}
        try:
            with open(self._cooldown_file) as f:
                raw = _json.load(f)
            # Keys stored as "rule_name|host" → epoch
            return {tuple(k.split("|", 1)): v for k, v in raw.items() if "|" in k}
        except (OSError, ValueError):
            return {}

    def _save_cooldowns(self) -> None:
        """Persist cooldown state to disk."""
        import json as _json

        serializable = {f"{k[0]}|{k[1]}": v for k, v in self._cooldown_state.items()}
        tmp = self._cooldown_file.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                _json.dump(serializable, f)
            os.rename(tmp, self._cooldown_file)
        except OSError:
            pass

    # ── Prometheus helpers ─────────────────────────────────────────────────

    def _prom_query(self, query: str) -> list[dict]:
        """Execute an instant PromQL query. Returns list of result dicts."""
        url = f"{self.prometheus_url}/api/v1/query"
        try:
            resp = requests.get(url, params={"query": query}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "success":
                return data.get("data", {}).get("result", [])
        except requests.RequestException as exc:
            log.warning("Prometheus query failed (%s): %s", query, exc)
        return []

    # ── Individual check implementations ──────────────────────────────────

    def _check_prometheus_query(self, rule: dict) -> list[dict]:
        """Run a PromQL check. Returns list of triggered {host, labels, value} dicts."""
        query = rule.get("query", "")
        if not query:
            return []
        results = self._prom_query(query)
        triggered = []
        for item in results:
            metric = item.get("metric", {})
            value = item.get("value", [None, "0"])
            host = metric.get("instance", "") or metric.get("job", "") or "unknown"
            # value[1] is the scalar result as string
            try:
                val = float(value[1]) if len(value) > 1 else 0.0
            except (ValueError, TypeError):
                val = 0.0
            if val != 0.0:  # non-zero means condition is met
                triggered.append({"host": host, "labels": metric, "value": val})
        return triggered

    def _check_nfs_sync(self) -> list[dict]:
        """Compare mtime of /opt/swarm vs /opt/swarm-replica to detect drift."""
        primary = Path("/opt/swarm")
        replica = Path("/opt/swarm-replica")
        if not primary.is_dir() or not replica.is_dir():
            return []

        try:
            primary_mtime = primary.stat().st_mtime
            replica_mtime = replica.stat().st_mtime
            drift_seconds = abs(primary_mtime - replica_mtime)
            # Flag as drifted if replica is more than 120 s behind primary
            if drift_seconds > 120:
                return [{"host": socket.gethostname(), "drift_seconds": drift_seconds}]
        except OSError as exc:
            log.warning("NFS drift check failed: %s", exc)
        return []

    def _check_swarm_heartbeat(self, rule: dict) -> list[dict]:
        """Check all known swarm nodes for stale heartbeats."""
        threshold = rule.get(
            "threshold_seconds",
            self.thresholds.get("stale_node_seconds", 600),
        )
        try:
            from swarm_lib import get_all_status

            stale = []
            now = datetime.now(UTC)
            for status in get_all_status():
                updated = status.get("updated_at", "")
                if not updated:
                    continue
                try:
                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    age = (now - dt).total_seconds()
                    if age > threshold:
                        stale.append(
                            {
                                "host": status.get("hostname", "unknown"),
                                "age_seconds": int(age),
                            }
                        )
                except ValueError:
                    continue
            return stale
        except ImportError:
            return []

    def _check_git_dirty(self, rule: dict) -> list[dict]:
        """Check monitored repos for uncommitted changes older than threshold.

        Supports exclude_patterns in rule config to filter out known-noisy paths
        (e.g., *.db, *.log, data/).
        """
        import fnmatch
        import subprocess

        threshold_minutes = rule.get(
            "threshold_minutes",
            self.thresholds.get("dirty_repo_minutes", 60),
        )
        exclude_patterns = rule.get(
            "exclude_patterns",
            [
                "*.db",
                "*.log",
                "data/*",
                "*.db-journal",
            ],
        )
        triggered = []
        for repo in MONITORED_REPOS:
            rp = Path(repo)
            if not rp.is_dir() or not (rp / ".git").exists():
                continue
            try:
                result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    cwd=repo,
                    timeout=15,
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Filter out excluded patterns
                    dirty_lines = []
                    for line in result.stdout.strip().splitlines():
                        # git status --porcelain format: XY filename
                        filepath = line[3:].strip().strip('"')
                        if not any(fnmatch.fnmatch(filepath, pat) for pat in exclude_patterns):
                            dirty_lines.append(line)

                    if not dirty_lines:
                        continue

                    # Check how long since last commit
                    age_result = subprocess.run(
                        ["git", "log", "-1", "--format=%ct"],
                        capture_output=True,
                        text=True,
                        cwd=repo,
                        timeout=15,
                    )
                    last_commit_ts = 0.0
                    if age_result.returncode == 0:
                        try:
                            last_commit_ts = float(age_result.stdout.strip())
                        except ValueError:
                            pass
                    age_minutes = (_now_ts() - last_commit_ts) / 60 if last_commit_ts else 9999
                    if age_minutes >= threshold_minutes:
                        triggered.append(
                            {
                                "host": socket.gethostname(),
                                "repo": repo,
                                "dirty_lines": len(dirty_lines),
                                "minutes_since_commit": int(age_minutes),
                            }
                        )
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.debug("git status failed for %s: %s", repo, exc)
        return triggered

    def _check_disk_usage(self, rule: dict) -> list[dict]:
        """Check disk usage on monitored paths."""
        import shutil

        threshold = rule.get(
            "threshold_percent",
            self.thresholds.get("disk_usage_percent", 85),
        )
        triggered = []
        for mount in ["/", "/opt"]:
            try:
                usage = shutil.disk_usage(mount)
                pct = (usage.used / usage.total) * 100
                if pct >= threshold:
                    triggered.append(
                        {
                            "host": socket.gethostname(),
                            "mount": mount,
                            "used_percent": round(pct, 1),
                            "free_gb": round(usage.free / 1024**3, 1),
                        }
                    )
            except OSError:
                pass
        return triggered

    def _check_nfs_health(self, rule: dict) -> list[dict]:
        """Check NFS mount health: write/read a test file, check response time."""
        nfs_path = Path("/opt/swarm")
        if not nfs_path.is_dir():
            return []

        timeout_seconds = rule.get("threshold_seconds", 5)
        triggered = []

        test_file = nfs_path / ".health-check"
        test_content = f"health-check-{int(time.time())}\n"

        try:
            # Write test file with timeout
            start = time.time()
            with open(test_file, "w") as f:
                f.write(test_content)
            write_time = time.time() - start

            # Read it back
            start = time.time()
            with open(test_file) as f:
                content = f.read()
            read_time = time.time() - start

            # Delete test file
            test_file.unlink()

            # Check if any operation exceeded timeout
            if write_time > timeout_seconds or read_time > timeout_seconds:
                triggered.append(
                    {
                        "host": socket.gethostname(),
                        "nfs_path": str(nfs_path),
                        "write_time_seconds": round(write_time, 2),
                        "read_time_seconds": round(read_time, 2),
                        "timeout_seconds": timeout_seconds,
                    }
                )

            # Verify content
            if content != test_content:
                triggered.append(
                    {
                        "host": socket.gethostname(),
                        "nfs_path": str(nfs_path),
                        "error": "NFS content verification failed",
                    }
                )

        except (OSError, TimeoutError) as exc:
            triggered.append(
                {
                    "host": socket.gethostname(),
                    "nfs_path": str(nfs_path),
                    "error": str(exc),
                }
            )

        return triggered

    def _check_task_deadline(self, rule: dict) -> list[dict]:
        """Check for tasks that have exceeded their deadline.

        A task is considered exceeded if:
        claimed_at + (estimated_minutes * 2) < now
        and retries < 3
        """
        from pathlib import Path

        triggered = []
        claimed_dir = Path("/opt/swarm/tasks/claimed")
        if not claimed_dir.is_dir():
            return triggered

        now = datetime.now(UTC)

        for task_file in claimed_dir.glob("*.yaml"):
            try:
                with open(task_file) as f:
                    task = yaml.safe_load(f) or {}

                claimed_at_str = task.get("claimed_at", "")
                estimated_minutes = task.get("estimated_minutes", 0)
                retries = task.get("_retries", 0)

                if not claimed_at_str or estimated_minutes <= 0:
                    continue

                try:
                    claimed_dt = datetime.fromisoformat(claimed_at_str.replace("Z", "+00:00"))
                    deadline = claimed_dt.replace(tzinfo=UTC) + timedelta(
                        minutes=estimated_minutes * 2
                    )

                    if now > deadline:
                        triggered.append(
                            {
                                "host": socket.gethostname(),
                                "task_id": task.get("id", task_file.stem),
                                "claimed_at": claimed_at_str,
                                "estimated_minutes": estimated_minutes,
                                "retries": retries,
                                "deadline_exceeded_by_minutes": int(
                                    (now - deadline).total_seconds() / 60
                                ),
                            }
                        )
                except (ValueError, AttributeError):
                    continue
            except (yaml.YAMLError, OSError):
                continue

        return triggered

    # ── Rule dispatch ──────────────────────────────────────────────────────

    def _run_check(self, rule: dict) -> list[dict]:
        """Run the appropriate check for a rule. Returns list of triggered items."""
        check = rule.get("check", "")
        if check == "prometheus_query":
            return self._check_prometheus_query(rule)
        if check == "nfs_sync":
            return self._check_nfs_sync()
        if check == "nfs_health":
            return self._check_nfs_health(rule)
        if check == "swarm_heartbeat":
            return self._check_swarm_heartbeat(rule)
        if check == "git_dirty":
            return self._check_git_dirty(rule)
        if check == "disk_usage":
            return self._check_disk_usage(rule)
        if check == "task_deadline":
            return self._check_task_deadline(rule)
        if check == "prometheus_health":
            return self._check_prometheus_health(rule)
        log.warning("Unknown check type '%s' in rule '%s'", check, rule.get("name"))
        return []

    # ── Action dispatch ────────────────────────────────────────────────────

    def _build_action_kwargs(self, rule: dict, item: dict) -> dict[str, Any]:
        """Build kwargs for RemediationEngine.execute() from rule + triggered item."""
        host = item.get("host", "")
        action = rule.get("action", "")

        # Attempt to extract service name for service_down from job/instance label
        service = ""
        labels = item.get("labels", {})
        if action == "restart_service":
            svc_candidate = labels.get("job", "") or labels.get("service", "")
            # Validate against allowlist silently — RemediationEngine will raise if invalid
            service = svc_candidate

        # Build email subject with hostname convention
        hostname = socket.gethostname()
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        email_subject = f"{hostname}-health-alert-{rule['name']}-{date_str}"

        # Build readable body
        body = (
            f"Health alert from {hostname}\n"
            f"Rule: {rule['name']}\n"
            f"Severity: {rule.get('severity', 'medium')}\n"
            f"Host: {host}\n"
            f"Details: {item}\n"
            f"Time: {_now_iso()}\n"
        )

        message = f"[health-alert] Rule '{rule['name']}' triggered on {host}: {item}"

        return {
            "action": action,
            "host": host,
            "service": service,
            "message": message,
            "subject": email_subject,
            "body": body,
        }

    def _handle_triggered(self, rule: dict, item: dict) -> None:
        """Decide whether to act, act, then log the event."""
        rule_name = rule["name"]
        host = item.get("host", "")
        severity = rule.get("severity", "medium")
        action = rule.get("action", "")

        description = f"Rule '{rule_name}' triggered: {item}"
        log.info("%s | %s | %s", severity.upper(), rule_name, description)

        action_taken = ""
        action_result = ""
        escalated_to = ""

        if action and not self._in_cooldown(rule, host):
            if rule.get("auto_remediate", False):
                kwargs = self._build_action_kwargs(rule, item)
                try:
                    success, detail = self.remediation.execute(**kwargs)
                    action_taken = action
                    action_result = f"{'OK' if success else 'FAIL'}: {detail}"
                    self._record_action(rule, host)
                    log.info("Remediation %s: %s", action, action_result)

                    # Escalate if action failed and rule requests it
                    if not success and rule.get("escalate") == "email":
                        _, email_detail = self.remediation.send_alert_email(
                            subject=kwargs["subject"],
                            body=kwargs["body"] + f"\nRemediation result: {action_result}",
                        )
                        escalated_to = f"email: {email_detail}"
                        log.warning("Escalated to email: %s", email_detail)

                except ValueError as exc:
                    # Service not in allowlist — log and alert
                    action_result = f"BLOCKED: {exc}"
                    action_taken = action
                    log.error("Remediation blocked: %s", exc)

            else:
                # Non-auto rules: just send notification
                kwargs = self._build_action_kwargs(rule, item)
                try:
                    success, detail = self.remediation.execute(**kwargs)
                    action_taken = action
                    action_result = f"{'OK' if success else 'FAIL'}: {detail}"
                    self._record_action(rule, host)
                    log.info("Notification %s: %s", action, action_result)
                except Exception as exc:
                    action_taken = action
                    action_result = f"FAIL: {exc}"
                    log.error("Notification failed: %s", exc)
        else:
            if self._in_cooldown(rule, host):
                log.debug("Rule '%s' host '%s' is in cooldown — skipping", rule_name, host)
            action_result = "skipped (cooldown or no action configured)"

        # Always record in event log
        self.event_log.record(
            rule_name=rule_name,
            host=host,
            severity=severity,
            description=description,
            action_taken=action_taken,
            action_result=action_result,
            escalated_to=escalated_to,
        )

    # ── Prometheus availability ────────────────────────────────────────────

    def _prometheus_available(self) -> bool:
        """Quick check if Prometheus is reachable. Cached per cycle."""
        try:
            resp = requests.get(f"{self.prometheus_url}/-/healthy", timeout=5)
            available = resp.status_code == 200
        except requests.RequestException:
            available = False

        # Track consecutive failures for prometheus_health rule
        if available:
            self._prom_consecutive_failures = 0
        else:
            self._prom_consecutive_failures = getattr(self, "_prom_consecutive_failures", 0) + 1

        return available

    def _check_prometheus_health(self, rule: dict) -> list[dict]:
        """Check if Prometheus itself is unavailable. Uses consecutive failure count
        to avoid alerting on transient blips."""
        miss_count = rule.get("miss_count", 3)
        failures = getattr(self, "_prom_consecutive_failures", 0)
        if failures >= miss_count:
            return [
                {
                    "host": socket.gethostname(),
                    "consecutive_failures": failures,
                    "minutes_down": failures,  # ~1 failure per minute (check interval)
                }
            ]
        return []

    # ── Main loop ──────────────────────────────────────────────────────────

    def _check_nfs_mount(self) -> None:
        """Check if /opt/swarm is a proper NFS mount or local dir. Log warning if not mounted."""
        import subprocess

        try:
            result = subprocess.run(
                ["mountpoint", "-q", "/opt/swarm"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                log.debug(
                    "NFS not mounted at /opt/swarm — using local directory (sync via swarm-sync.sh)"
                )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _run_cycle(self) -> None:
        """Run one complete check cycle across all rules."""
        log.debug("Starting check cycle")

        # Check NFS mount status
        self._check_nfs_mount()

        # Pre-check Prometheus once per cycle to avoid repeated failures
        prom_ok = self._prometheus_available()
        if not prom_ok:
            log.debug("Prometheus unreachable — skipping prometheus_query rules this cycle")

        futures: dict[concurrent.futures.Future, dict] = {}
        for rule in self.rules:
            # Skip prometheus_query rules when Prometheus is down
            # (but NOT prometheus_health — that rule specifically checks for Prometheus outages)
            if rule.get("check") == "prometheus_query" and not prom_ok:
                continue
            futures[self._executor.submit(self._run_check, rule)] = rule

        for future in concurrent.futures.as_completed(futures, timeout=45):
            rule = futures[future]
            try:
                triggered_items = future.result(timeout=30)
                for item in triggered_items:
                    self._handle_triggered(rule, item)
            except Exception as exc:
                log.error("Unhandled error in rule '%s': %s", rule.get("name"), exc)
        log.debug("Check cycle complete")

    def run(self) -> None:
        """Main loop: check → decide → act → log."""
        log.info(
            "Health monitor starting — interval=%ds prometheus=%s",
            self.check_interval,
            self.prometheus_url,
        )

        while self._running:
            try:
                self._run_cycle()
            except Exception as exc:
                log.error("Unhandled error in check cycle: %s", exc)
            # Sleep in small increments so SIGTERM is handled promptly
            for _ in range(self.check_interval):
                if not self._running:
                    break
                time.sleep(1)

        log.info("Health monitor stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    monitor = HealthMonitor()
    if not monitor.config.get("enabled", True):
        log.info("Health monitor disabled in config — exiting")
        sys.exit(0)
    monitor.run()
