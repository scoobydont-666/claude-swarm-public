"""Tests for worktree path traversal guard."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from swarm_lib import _is_path_within_base


class TestIsPathWithinBase:
    def test_normal_task_id(self):
        assert _is_path_within_base("/tmp/worktrees", "/tmp/worktrees/task-123") is True

    def test_nested_task_id(self):
        assert _is_path_within_base("/tmp/worktrees", "/tmp/worktrees/host/task-123") is True

    def test_traversal_attack(self):
        assert _is_path_within_base("/tmp/worktrees", "/tmp/worktrees/../../etc/passwd") is False

    def test_double_dot_in_middle(self):
        assert _is_path_within_base("/tmp/worktrees", "/tmp/worktrees/foo/../../../root") is False

    def test_base_itself(self):
        assert _is_path_within_base("/tmp/worktrees", "/tmp/worktrees") is True

    def test_sibling_directory(self):
        assert _is_path_within_base("/tmp/worktrees", "/tmp/other") is False

    def test_partial_name_match(self):
        # "/tmp/worktrees-evil" should NOT match "/tmp/worktrees"
        assert _is_path_within_base("/tmp/worktrees", "/tmp/worktrees-evil/task") is False


class TestCreateWorktreeGuard:
    def test_traversal_task_id_rejected(self, tmp_path):
        """create_worktree should reject task_id with path traversal."""
        from unittest.mock import patch

        with (
            patch(
                "swarm_lib.load_config",
                return_value={
                    "worktrees": {
                        "base_path": str(tmp_path / "worktrees"),
                        "branch_prefix": "swarm",
                    },
                },
            ),
            patch("swarm_lib._validate_git_repo", return_value=True),
        ):
            from swarm_lib import create_worktree

            with pytest.raises(ValueError, match="path traversal"):
                create_worktree("/opt/test-repo", "../../etc/shadow")

    def test_normal_task_id_accepted(self, tmp_path):
        """Normal task_id should pass the guard (may fail on git, but not the guard)."""
        from unittest.mock import patch

        wt_base = tmp_path / "worktrees"
        wt_base.mkdir()

        with (
            patch(
                "swarm_lib.load_config",
                return_value={
                    "worktrees": {"base_path": str(wt_base), "branch_prefix": "swarm"},
                },
            ),
            patch("swarm_lib._validate_git_repo", return_value=True),
            patch("swarm_lib._safe_subprocess") as mock_sp,
            patch("swarm_lib._hostname", return_value="test"),
        ):
            mock_sp.return_value.returncode = 0
            from swarm_lib import create_worktree

            # Should not raise ValueError
            result = create_worktree("/opt/test-repo", "task-safe-123")
            assert "task-safe-123" in result
