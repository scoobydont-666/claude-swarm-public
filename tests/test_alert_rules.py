"""Tests for Prometheus alert rules validation."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestAlertRulesYAML:
    """Tests for swarm-alerts.yml validation."""

    def test_alert_rules_file_exists(self):
        """Test that alert rules file exists."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        assert alerts_path.exists(), f"Alert rules file not found: {alerts_path}"

    def test_alert_rules_valid_yaml(self):
        """Test that alert rules file is valid YAML."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)
        assert data is not None
        assert isinstance(data, dict)

    def test_alert_rules_has_groups(self):
        """Test that alert rules have groups section."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)
        assert "groups" in data
        assert isinstance(data["groups"], list)
        assert len(data["groups"]) > 0

    def test_alert_rules_group_structure(self):
        """Test that alert groups have required structure."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)

        for group in data["groups"]:
            assert "name" in group
            assert "rules" in group
            assert isinstance(group["rules"], list)
            assert len(group["rules"]) > 0

    def test_alert_rules_have_required_fields(self):
        """Test that each alert rule has required fields."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)

        required_fields = ["alert", "expr", "labels", "annotations"]

        for group in data["groups"]:
            for rule in group["rules"]:
                for field in required_fields:
                    assert field in rule, (
                        f"Rule missing {field}: {rule.get('alert', 'unknown')}"
                    )

    def test_alert_node_offline_rule(self):
        """Test SwarmNodeOffline alert rule."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)

        alerts = []
        for group in data["groups"]:
            for rule in group["rules"]:
                if rule.get("alert") == "SwarmNodeOffline":
                    alerts.append(rule)

        assert len(alerts) == 1
        rule = alerts[0]
        assert "swarm_nodes_total" in rule["expr"]
        assert rule["labels"]["severity"] == "warning"

    def test_alert_task_queue_backlog_rule(self):
        """Test SwarmTaskQueueBacklog alert rule."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)

        alerts = []
        for group in data["groups"]:
            for rule in group["rules"]:
                if rule.get("alert") == "SwarmTaskQueueBacklog":
                    alerts.append(rule)

        assert len(alerts) == 1
        rule = alerts[0]
        assert "pending" in rule["expr"]
        assert rule["labels"]["severity"] == "warning"

    def test_alert_heartbeat_stale_rule(self):
        """Test SwarmHeartbeatStale alert rule."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)

        alerts = []
        for group in data["groups"]:
            for rule in group["rules"]:
                if rule.get("alert") == "SwarmHeartbeatStale":
                    alerts.append(rule)

        assert len(alerts) == 1
        rule = alerts[0]
        assert "swarm_last_heartbeat_age_seconds" in rule["expr"]
        assert rule["labels"]["severity"] == "critical"

    def test_alert_dispatch_cost_rule(self):
        """Test SwarmHighDispatchCost alert rule."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)

        alerts = []
        for group in data["groups"]:
            for rule in group["rules"]:
                if rule.get("alert") == "SwarmHighDispatchCost":
                    alerts.append(rule)

        assert len(alerts) == 1
        rule = alerts[0]
        assert "swarm_dispatch_cost_usd_total" in rule["expr"]
        assert rule["labels"]["severity"] == "warning"

    def test_alert_dispatch_cost_by_host_rule(self):
        """Test SwarmDispatchCostByHost alert rule."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)

        alerts = []
        for group in data["groups"]:
            for rule in group["rules"]:
                if rule.get("alert") == "SwarmDispatchCostByHost":
                    alerts.append(rule)

        assert len(alerts) == 1
        rule = alerts[0]
        assert "swarm_dispatch_cost_by_host_usd_total" in rule["expr"]
        assert rule["labels"]["severity"] == "warning"

    def test_all_alerts_have_annotations(self):
        """Test that all rules have summary and description."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)

        for group in data["groups"]:
            for rule in group["rules"]:
                annotations = rule.get("annotations", {})
                assert "summary" in annotations, (
                    f"Missing summary for {rule.get('alert')}"
                )
                assert "description" in annotations, (
                    f"Missing description for {rule.get('alert')}"
                )

    def test_alert_rules_for_durations(self):
        """Test that rules specify 'for' duration."""
        alerts_path = Path("/opt/claude-swarm/deploy/swarm-alerts.yml")
        with open(alerts_path) as f:
            data = yaml.safe_load(f)

        for group in data["groups"]:
            for rule in group["rules"]:
                assert "for" in rule, f"Rule {rule.get('alert')} missing 'for' duration"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
