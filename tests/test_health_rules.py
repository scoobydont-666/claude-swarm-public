"""Tests for health_rules.py — rule structure, allowlist, lookup helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from health_rules import ALLOWED_SERVICES, RULES, get_rule, rules_by_check_type


class TestRuleStructure:
    def test_all_rules_have_name(self):
        for rule in RULES:
            assert "name" in rule, f"Missing 'name' in rule: {rule}"

    def test_all_rules_have_check(self):
        for rule in RULES:
            assert "check" in rule, f"Rule '{rule['name']}' missing 'check'"

    def test_all_rules_have_action(self):
        for rule in RULES:
            assert "action" in rule, f"Rule '{rule['name']}' missing 'action'"

    def test_rule_names_unique(self):
        names = [r["name"] for r in RULES]
        assert len(names) == len(set(names)), "Duplicate rule names found"

    def test_check_types_are_valid(self):
        valid_checks = {
            "prometheus_query",
            "nfs_sync",
            "nfs_health",
            "swarm_heartbeat",
            "git_dirty",
            "disk_usage",
            "task_deadline",
            "prometheus_health",
        }
        for rule in RULES:
            assert rule["check"] in valid_checks, (
                f"Rule '{rule['name']}' has unknown check type '{rule['check']}'"
            )

    def test_severity_values(self):
        valid_severities = {"high", "medium", "low", "critical"}
        for rule in RULES:
            sev = rule.get("severity", "medium")
            assert sev in valid_severities, f"Rule '{rule['name']}' has invalid severity '{sev}'"

    def test_auto_remediate_is_bool(self):
        for rule in RULES:
            if "auto_remediate" in rule:
                assert isinstance(rule["auto_remediate"], bool), (
                    f"Rule '{rule['name']}' auto_remediate must be bool"
                )

    def test_cooldown_minutes_positive(self):
        for rule in RULES:
            if "cooldown_minutes" in rule:
                assert rule["cooldown_minutes"] > 0, (
                    f"Rule '{rule['name']}' cooldown_minutes must be > 0"
                )


class TestPrometheusRules:
    def test_prometheus_rules_have_query(self):
        prom_rules = [r for r in RULES if r["check"] == "prometheus_query"]
        assert len(prom_rules) > 0
        for rule in prom_rules:
            assert "query" in rule and rule["query"], (
                f"prometheus_query rule '{rule['name']}' missing query"
            )

    def test_service_down_auto_remediate(self):
        rule = get_rule("service_down")
        assert rule is not None
        assert rule["auto_remediate"] is True
        assert rule["action"] == "restart_service"

    def test_gpu_vram_full_not_auto(self):
        rule = get_rule("gpu_vram_full")
        assert rule is not None
        assert rule.get("auto_remediate", False) is False

    def test_p2pool_no_shares_has_action(self):
        rule = get_rule("p2pool_no_shares")
        assert rule is not None
        assert rule["action"] == "alert_email"


class TestNonPrometheusRules:
    def test_nfs_replica_drift_auto(self):
        rule = get_rule("nfs_replica_drift")
        assert rule is not None
        assert rule["auto_remediate"] is True
        assert rule["action"] == "force_sync_replica"

    def test_uncommitted_changes_never_auto_commits(self):
        rule = get_rule("uncommitted_changes")
        assert rule is not None
        # CRITICAL: must never auto-commit
        assert rule.get("auto_remediate", False) is False

    def test_disk_space_low_has_threshold(self):
        rule = get_rule("disk_space_low")
        assert rule is not None
        assert "threshold_percent" in rule
        assert rule["threshold_percent"] <= 90

    def test_stale_node_has_escalation(self):
        rule = get_rule("stale_node")
        assert rule is not None
        assert rule.get("escalate") == "email"


class TestAllowlist:
    def test_allowlist_has_miniboss_and_giga(self):
        assert "miniboss" in ALLOWED_SERVICES
        assert "GIGA" in ALLOWED_SERVICES

    def test_miniboss_has_required_services(self):
        required = [
            "monerod",
            "p2pool-main",
            "p2pool-mini",
            "prometheus",
            "grafana-server",
        ]
        for svc in required:
            assert svc in ALLOWED_SERVICES["miniboss"], f"{svc} missing from miniboss allowlist"

    def test_giga_has_required_services(self):
        required = ["docker", "fail2ban", "crowdsec"]
        for svc in required:
            assert svc in ALLOWED_SERVICES["GIGA"], f"{svc} missing from GIGA allowlist"

    def test_no_empty_service_names(self):
        for host, services in ALLOWED_SERVICES.items():
            for svc in services:
                assert svc.strip(), f"Empty service name found for host {host}"

    def test_no_shell_metacharacters_in_services(self):
        bad_chars = set(";|&$`\n\r")
        for host, services in ALLOWED_SERVICES.items():
            for svc in services:
                found = bad_chars.intersection(svc)
                assert not found, (
                    f"Service '{svc}' on {host} contains shell metacharacters: {found}"
                )


class TestLookupHelpers:
    def test_get_rule_found(self):
        rule = get_rule("service_down")
        assert rule is not None
        assert rule["name"] == "service_down"

    def test_get_rule_not_found(self):
        assert get_rule("nonexistent_rule") is None

    def test_rules_by_check_type_prometheus(self):
        prom = rules_by_check_type("prometheus_query")
        assert len(prom) >= 4
        assert all(r["check"] == "prometheus_query" for r in prom)

    def test_rules_by_check_type_unknown(self):
        assert rules_by_check_type("totally_unknown") == []
