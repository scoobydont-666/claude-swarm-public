"""Health rules for the claude-swarm health monitor.

Each rule is a dict describing what to check, what threshold triggers it,
what action to take, and optional cooldown/escalation config.

Rule fields:
    name            Unique rule identifier.
    check           Check type: prometheus_query | nfs_sync | swarm_heartbeat |
                    git_dirty | disk_usage
    query           PromQL string (prometheus_query checks only).
    severity        "high" | "medium" | "low" — informational.
    auto_remediate  Whether RemediationEngine may act without approval.
    action          Method name on RemediationEngine to call.
    cooldown_minutes  Minimum minutes between repeated actions for same rule+host.
    threshold_seconds / threshold_minutes / threshold_percent
                    Numeric thresholds specific to the check type.
    escalate        "email" — escalate via email if action fails.
"""

from typing import Any

RULES: list[dict[str, Any]] = [
    # ── Service availability ────────────────────────────────────────────────
    {
        "name": "service_down",
        "check": "prometheus_query",
        "query": "up == 0",
        "severity": "high",
        "auto_remediate": True,
        "action": "restart_service",
        "cooldown_minutes": 10,
    },
    # ── NFS replica drift ───────────────────────────────────────────────────
    {
        "name": "nfs_replica_drift",
        "check": "nfs_sync",
        "severity": "medium",
        "auto_remediate": True,
        "action": "force_sync_replica",
        "cooldown_minutes": 5,
    },
    # ── Stale swarm node ────────────────────────────────────────────────────
    {
        "name": "stale_node",
        "check": "swarm_heartbeat",
        "severity": "high",
        "threshold_seconds": 600,
        "miss_count": 3,  # require 3 consecutive stale observations (hysteresis)
        "auto_remediate": False,
        "action": "ssh_health_check",
        "escalate": "email",
    },
    # ── Dirty git repos ─────────────────────────────────────────────────────
    {
        "name": "uncommitted_changes",
        "check": "git_dirty",
        "severity": "low",
        "threshold_minutes": 480,
        "auto_remediate": False,  # NEVER auto-commit
        "action": "warn_swarm_message",
        "cooldown_minutes": 1440,  # Once per day max
    },
    # ── Disk space ──────────────────────────────────────────────────────────
    {
        "name": "disk_space_low",
        "check": "disk_usage",
        "severity": "high",
        "threshold_percent": 85,
        "auto_remediate": False,
        "action": "alert_email",
        "cooldown_minutes": 60,
    },
    # ── Prometheus target down ──────────────────────────────────────────────
    {
        "name": "prometheus_target_down",
        "check": "prometheus_query",
        "query": "up{job=~'.*'} == 0",
        "severity": "medium",
        "auto_remediate": False,
        "action": "alert_swarm_broadcast",
        "cooldown_minutes": 15,
    },
    # ── gpu-server-1 GPU VRAM full ──────────────────────────────────────────────────
    {
        "name": "gpu_vram_full",
        "check": "prometheus_query",
        "query": "DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_TOTAL > 0.95",
        "severity": "high",
        "auto_remediate": False,
        "action": "alert_email",
        "cooldown_minutes": 60,
    },
    # ── P2Pool no shares ────────────────────────────────────────────────────
    {
        "name": "p2pool_no_shares",
        "check": "prometheus_query",
        "query": "increase(p2pool_local_stratum_shares_found[1h]) == 0",
        "severity": "high",
        "auto_remediate": False,
        "action": "alert_email",
        "cooldown_minutes": 60,
    },
    # ── Prometheus itself unavailable ─────────────────────────────────────────
    {
        "name": "prometheus_unavailable",
        "check": "prometheus_health",
        "severity": "high",
        "miss_count": 3,  # 3 consecutive failures before alerting
        "auto_remediate": False,
        "action": "alert_email",
        "cooldown_minutes": 60,
    },
    # ── Task deadline exceeded ──────────────────────────────────────────────
    {
        "name": "task_deadline_exceeded",
        "check": "task_deadline",
        "severity": "high",
        "auto_remediate": True,
        "action": "requeue_task",
        "cooldown_minutes": 5,
    },
    # ── NFS health ──────────────────────────────────────────────────────────
    {
        "name": "nfs_unhealthy",
        "check": "nfs_health",
        "severity": "critical",
        "threshold_seconds": 5,  # Operations exceeding 5s trigger alert
        "auto_remediate": False,
        "action": "alert_email",
        "cooldown_minutes": 15,
    },
]

# ---------------------------------------------------------------------------
# Service allowlist — validate ALL service names before SSH commands
# ---------------------------------------------------------------------------

ALLOWED_SERVICES: dict[str, list[str]] = {
    "orchestration-node": [
        "monerod",
        "p2pool-main",
        "p2pool-mini",
        "p2pool-nano",
        "p2pool-exporter",
        "p2pool-observer-exporter",
        "prometheus",
        "grafana-server",
        "crowdsec",
        "fail2ban",
        "semaphore",
    ],
    "gpu-server-1": [
        "docker",
        "prometheus-node-exporter",
        "fail2ban",
        "crowdsec",
        "auditd",
    ],
}


def get_rule(name: str) -> dict[str, Any] | None:
    """Return a rule dict by name, or None if not found."""
    for rule in RULES:
        if rule["name"] == name:
            return rule
    return None


def rules_by_check_type(check_type: str) -> list[dict[str, Any]]:
    """Return all rules for a given check type."""
    return [r for r in RULES if r.get("check") == check_type]
