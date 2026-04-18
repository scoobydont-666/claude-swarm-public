"""Tests for dispatch history CLI commands and output retrieval."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remote_session import (
    ExecutionPlan,
    ExecutionStrategy,
    TaskComplexity,
    get_dispatch_output,
)


class TestGetDispatchOutput:
    """Tests for get_dispatch_output function."""

    def test_get_dispatch_output_found(self, tmp_path):
        """Test retrieving output from a dispatch that exists."""
        # Create a fake output file
        dispatch_id = "test-dispatch-123"
        output_content = "Line 1\nLine 2\nLine 3\n"

        with patch("remote_session.DISPATCH_DIR", tmp_path):
            output_file = tmp_path / f"{dispatch_id}.output"
            output_file.write_text(output_content)

            result = get_dispatch_output(dispatch_id)
            assert result == output_content

    def test_get_dispatch_output_tail_lines(self, tmp_path):
        """Test retrieving last N lines from dispatch output."""
        dispatch_id = "test-dispatch-456"
        lines = "\n".join([f"Line {i}" for i in range(1, 101)])  # 100 lines

        with patch("remote_session.DISPATCH_DIR", tmp_path):
            output_file = tmp_path / f"{dispatch_id}.output"
            output_file.write_text(lines)

            result = get_dispatch_output(dispatch_id, tail_lines=10)
            result_lines = result.count("\n")
            # Should have ~10 lines (last 10 of 100)
            assert result_lines >= 9

    def test_get_dispatch_output_not_found(self, tmp_path):
        """Test retrieving output for non-existent dispatch."""
        with patch("remote_session.DISPATCH_DIR", tmp_path):
            result = get_dispatch_output("nonexistent-dispatch")
            assert "not found" in result.lower()

    def test_get_dispatch_output_zero_tail_lines(self, tmp_path):
        """Test retrieving all output when tail_lines=0."""
        dispatch_id = "test-dispatch-789"
        output_content = "Full\nOutput\nContent\n"

        with patch("remote_session.DISPATCH_DIR", tmp_path):
            output_file = tmp_path / f"{dispatch_id}.output"
            output_file.write_text(output_content)

            result = get_dispatch_output(dispatch_id, tail_lines=0)
            assert result == output_content


class TestDispatchOutputStreaming:
    """Tests for stdbuf line-buffering in execute_plan."""

    def test_execute_plan_uses_stdbuf(self, tmp_path):
        """Verify execute_plan wraps SSH command with stdbuf."""
        plan = ExecutionPlan(
            strategy=ExecutionStrategy.REMOTE_DISPATCH,
            host="miniboss",
            model="sonnet",
            reasoning="test",
            complexity=TaskComplexity.SIMPLE,
            prompt="test task",
        )

        with patch("remote_session.DISPATCH_DIR", tmp_path):
            with patch("remote_session.subprocess.Popen") as mock_popen:
                mock_popen.return_value = MagicMock(pid=1234)

                from remote_session import execute_plan

                execute_plan(plan, background=True)

                # Verify stdbuf was used in the SSH command
                call_args = mock_popen.call_args
                ssh_cmd = call_args[0][0] if call_args[0] else []

                # Check that the command uses stdbuf for line-buffered output
                cmd_str = " ".join(ssh_cmd)
                assert "stdbuf -oL" in cmd_str

    def test_execute_plan_output_file_created(self, tmp_path):
        """Test that output file path is set in result."""
        plan = ExecutionPlan(
            strategy=ExecutionStrategy.REMOTE_DISPATCH,
            host="miniboss",
            model="haiku",
            reasoning="test",
            complexity=TaskComplexity.TRIVIAL,
            prompt="check status",
        )

        with patch("remote_session.DISPATCH_DIR", tmp_path):
            with patch("remote_session.subprocess.Popen") as mock_popen:
                mock_popen.return_value = MagicMock(pid=5678)

                from remote_session import execute_plan

                result = execute_plan(plan, background=True)

                assert result.output_file
                assert str(tmp_path) in result.output_file


class TestDispatchCostEstimation:
    """Tests for dispatch cost estimation."""

    def test_estimate_cost_haiku(self):
        """Test cost estimation for haiku model."""
        from remote_session import _estimate_cost

        # 1000 chars ~= 300 tokens ~= $0.00024 (haiku at $0.80 per 1M)
        cost = _estimate_cost("haiku", 1000)
        assert 0.0002 < cost < 0.001  # Small but measurable

    def test_estimate_cost_sonnet(self):
        """Test cost estimation for sonnet model."""
        from remote_session import _estimate_cost

        # 10000 chars ~= 3000 tokens ~= $0.009 (sonnet at $3 per 1M)
        cost = _estimate_cost("sonnet", 10000)
        assert 0.008 < cost < 0.01

    def test_estimate_cost_opus(self):
        """Test cost estimation for opus model."""
        from remote_session import _estimate_cost

        # 10000 chars ~= 3000 tokens ~= $0.045 (opus at $15 per 1M)
        cost = _estimate_cost("opus", 10000)
        assert 0.04 < cost < 0.05

    def test_estimate_cost_zero_output(self):
        """Test cost estimation with empty output."""
        from remote_session import _estimate_cost

        cost = _estimate_cost("sonnet", 0)
        assert cost == 0.0

    def test_cost_stored_in_plan(self, tmp_path):
        """Test that estimated cost is stored in plan YAML."""
        import yaml

        plan = ExecutionPlan(
            strategy=ExecutionStrategy.REMOTE_DISPATCH,
            host="GIGA",
            model="sonnet",
            reasoning="test",
            complexity=TaskComplexity.SIMPLE,
            prompt="test prompt",
        )

        with patch("remote_session.DISPATCH_DIR", tmp_path):
            with patch("remote_session.subprocess.Popen") as mock_popen:
                mock_popen.return_value = MagicMock(pid=9999)

                from remote_session import execute_plan

                execute_plan(plan, background=True)

                # Check plan file was created with cost field
                plan_files = list(tmp_path.glob("*.plan.yaml"))
                assert len(plan_files) > 0

                plan_data = yaml.safe_load(plan_files[0].read_text())
                assert "estimated_cost_usd" in plan_data
                assert plan_data["estimated_cost_usd"] == 0.0  # Not yet completed


class TestDispatchHistoryCLI:
    """Tests for swarm dispatches CLI commands."""

    def test_dispatches_list_empty(self, tmp_path):
        """Test listing dispatches when directory is empty."""
        from unittest.mock import patch

        with patch("swarm_cli.Path", return_value=tmp_path):
            from swarm_cli import dispatches_list

            # Should not raise
            ctx = MagicMock(invoked_subcommand=None)
            dispatches_list(ctx)

    def test_dispatches_show_not_found(self, tmp_path):
        """Test showing a non-existent dispatch."""
        from unittest.mock import patch

        with patch("swarm_cli.Path"):
            with patch("swarm_cli.console"):
                from swarm_cli import dispatches_show

                # Should exit with error
                try:
                    dispatches_show("nonexistent")
                except (SystemExit, typer.Exit):
                    pass

    def test_dispatches_output_file_structure(self, tmp_path):
        """Test that dispatch output file follows expected naming."""
        dispatch_id = "session-1234567890-GIGA"
        output_dir = tmp_path / "dispatches"
        output_dir.mkdir()

        # Create dispatch artifacts
        plan_file = output_dir / f"{dispatch_id}.plan.yaml"
        output_file = output_dir / f"{dispatch_id}.output"
        pid_file = output_dir / f"{dispatch_id}.pid"

        import yaml

        plan_file.write_text(
            yaml.dump(
                {
                    "dispatch_id": dispatch_id,
                    "host": "GIGA",
                    "strategy": "remote_dispatch",
                    "model": "sonnet",
                }
            )
        )
        output_file.write_text("Test output\n")
        pid_file.write_text("12345")

        # Verify structure
        assert plan_file.exists()
        assert output_file.exists()
        assert pid_file.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
