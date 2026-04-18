"""Tests for cost tracker module."""

from unittest.mock import MagicMock, patch

from src.cost_tracker import TaskCost, check_budget, format_cost, get_task_cost


class TestTaskCost:
    def test_defaults(self):
        tc = TaskCost(task_id="test-1")
        assert tc.total_cost_usd == 0.0
        assert tc.session_count == 0

    def test_budget_check_unlimited(self):
        within, cost = check_budget("test-1", 0.0)
        assert within is True
        assert cost == 0.0

    def test_budget_check_no_data(self):
        with patch("src.cost_tracker.get_task_cost", return_value=None):
            within, cost = check_budget("test-no-data", 10.0)
            assert within is True

    def test_budget_check_within(self):
        mock_cost = TaskCost(task_id="test-2", total_cost_usd=5.0)
        with patch("src.cost_tracker.get_task_cost", return_value=mock_cost):
            within, cost = check_budget("test-2", 10.0)
            assert within is True
            assert cost == 5.0

    def test_budget_check_exceeded(self):
        mock_cost = TaskCost(task_id="test-3", total_cost_usd=15.0)
        with patch("src.cost_tracker.get_task_cost", return_value=mock_cost):
            within, cost = check_budget("test-3", 10.0)
            assert within is False
            assert cost == 15.0


class TestFormatCost:
    def test_small_cost(self):
        assert format_cost(0.001) == "$0.0010"

    def test_medium_cost(self):
        assert format_cost(0.50) == "$0.50"

    def test_large_cost(self):
        assert format_cost(5.25) == "$5.25"


class TestGetTaskCost:
    def test_cli_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.05, "sessions": 1, "model": "sonnet", "duration_seconds": 30}'

        with patch("subprocess.run", return_value=mock_result):
            cost = get_task_cost("task-cli-test")
            assert cost is not None
            assert cost.total_input_tokens == 1000
            assert cost.total_cost_usd == 0.05
            assert cost.model == "sonnet"

    def test_cli_failure_returns_none(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            # Also mock the DB paths to not exist
            with patch("src.cost_tracker.PULSE_DB_PATHS", []):
                cost = get_task_cost("task-fail-test")
                assert cost is None
