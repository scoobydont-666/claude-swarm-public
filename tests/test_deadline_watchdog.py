"""Tests for task deadline watchdog — detecting and requeuing expired tasks."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestTaskDeadlineHealthRule:
    def test_task_deadline_rule_exists(self):
        """Test that task_deadline rule is in health_rules.RULES."""
        from health_rules import get_rule

        rule = get_rule("task_deadline_exceeded")
        assert rule is not None
        assert rule["check"] == "task_deadline"
        assert rule["severity"] == "high"
        assert rule["auto_remediate"] is True
        assert rule["action"] == "requeue_task"

    def test_task_deadline_rule_in_rules_list(self):
        """Verify task_deadline_exceeded rule is in the RULES list."""
        from health_rules import RULES

        assert any(r["name"] == "task_deadline_exceeded" for r in RULES)


class TestTaskDeadlineCheck:
    def test_check_method_exists(self):
        """Test that _check_task_deadline method exists on HealthMonitor."""
        from health_monitor import HealthMonitor

        monitor = HealthMonitor()
        assert hasattr(monitor, "_check_task_deadline")
        assert callable(monitor._check_task_deadline)

    def test_check_returns_list(self):
        """Test that _check_task_deadline returns a list."""
        from health_monitor import HealthMonitor

        monitor = HealthMonitor()
        result = monitor._check_task_deadline({})
        assert isinstance(result, list)


class TestTaskDeadlineRemediation:
    def test_requeue_task_method_exists(self):
        """Test that requeue_task method exists on RemediationEngine."""
        from remediations import RemediationEngine

        engine = RemediationEngine()
        assert hasattr(engine, "requeue_task")
        assert callable(engine.requeue_task)

    def test_requeue_task_no_task_id_fails(self):
        """Test that requeue_task fails with no task_id."""
        from remediations import RemediationEngine

        engine = RemediationEngine()
        success, detail = engine.requeue_task()
        assert not success
        assert "task_id" in detail.lower() or "no task" in detail.lower()

    def test_execute_requeue_task_action(self):
        """Test that execute() dispatcher supports requeue_task action."""
        from remediations import RemediationEngine

        engine = RemediationEngine()
        # Call with invalid task, should get graceful error
        success, detail = engine.execute(action="requeue_task", task="")
        assert isinstance(success, bool)
        assert isinstance(detail, str)


class TestHealthMonitorDispatch:
    def test_task_deadline_check_in_dispatch(self):
        """Test that task_deadline check is handled in _run_check."""
        from health_monitor import HealthMonitor

        monitor = HealthMonitor()
        rule = {"check": "task_deadline"}

        # Should not raise
        result = monitor._run_check(rule)
        assert isinstance(result, list)

    def test_unknown_check_handled_gracefully(self):
        """Test that unknown check types are handled gracefully."""
        from health_monitor import HealthMonitor

        monitor = HealthMonitor()
        rule = {"check": "unknown_check_type", "name": "test_rule"}

        result = monitor._run_check(rule)
        assert result == []  # Should return empty list for unknown checks


class TestRemediationExecute:
    def test_execute_unknown_action(self):
        """Test that execute() handles unknown actions gracefully."""
        from remediations import RemediationEngine

        engine = RemediationEngine()
        success, detail = engine.execute(action="unknown_action")
        assert not success
        assert "unknown" in detail.lower()

    def test_execute_requeue_task_dispatches(self):
        """Test that requeue_task action is callable via execute()."""
        from remediations import RemediationEngine

        engine = RemediationEngine()
        # Should dispatch to requeue_task method
        success, detail = engine.execute(action="requeue_task", task="nonexistent")
        # Either success or appropriate error
        assert isinstance(success, bool)
        assert isinstance(detail, str)


class TestHealthMonitorCycleWithDeadline:
    def test_monitor_can_run_with_deadline_rule(self):
        """Test that health monitor can run with task_deadline rule."""
        from health_monitor import HealthMonitor

        monitor = HealthMonitor()
        assert monitor.rules  # Has rules
        assert any(r["name"] == "task_deadline_exceeded" for r in monitor.rules)

        # Run one cycle — should not raise
        monitor._run_cycle()
