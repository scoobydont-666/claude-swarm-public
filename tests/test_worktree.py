"""Tests for worktree isolation — create/complete/cleanup, branch naming, merge vs branch-only."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import swarm_lib as lib


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repo for worktree tests."""
    repo_dir = tmp_path / "test-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo_dir),
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo_dir), capture_output=True
    )
    # Create initial commit so HEAD exists
    (repo_dir / "README.md").write_text("test")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo_dir), capture_output=True
    )
    return repo_dir


class TestValidateGitRepo:
    def test_valid_repo(self, git_repo):
        assert lib._validate_git_repo(str(git_repo)) is True

    def test_invalid_path(self, tmp_path):
        assert lib._validate_git_repo(str(tmp_path / "nonexistent")) is False

    def test_non_git_dir(self, tmp_path):
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        assert lib._validate_git_repo(str(plain_dir)) is False


class TestSafeSubprocess:
    def test_rejects_shell_metacharacters(self):
        with pytest.raises(ValueError, match="Invalid character"):
            lib._safe_subprocess(["echo", "hello; rm -rf /"])

    def test_rejects_pipe(self):
        with pytest.raises(ValueError, match="Invalid character"):
            lib._safe_subprocess(["echo", "hello | cat"])

    def test_rejects_backtick(self):
        with pytest.raises(ValueError, match="Invalid character"):
            lib._safe_subprocess(["echo", "`whoami`"])

    def test_rejects_dollar(self):
        with pytest.raises(ValueError, match="Invalid character"):
            lib._safe_subprocess(["echo", "$HOME"])

    def test_valid_command_runs(self):
        result = lib._safe_subprocess(["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout


class TestCreateWorktree:
    def test_create_worktree_success(self, swarm_tmpdir, git_repo):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            worktree_path = lib.create_worktree(str(git_repo), "task-001")
            expected_path = str(swarm_tmpdir / "worktrees" / "task-001")
            assert worktree_path == expected_path
            assert Path(worktree_path).is_dir()

    def test_create_worktree_branch_naming(self, swarm_tmpdir, git_repo):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_worktree(str(git_repo), "task-002")
            # Verify branch was created
            result = subprocess.run(
                ["git", "branch", "--list", "swarm/testhost/task-002"],
                cwd=str(git_repo),
                capture_output=True,
                text=True,
            )
            assert "swarm/testhost/task-002" in result.stdout

    def test_create_worktree_not_git_repo(self, swarm_tmpdir, tmp_path):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            plain_dir = tmp_path / "not-git"
            plain_dir.mkdir()
            with pytest.raises(ValueError, match="Not a git repository"):
                lib.create_worktree(str(plain_dir), "task-003")

    def test_create_worktree_records_in_task(self, swarm_tmpdir, git_repo):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            # Create and claim a task first
            task = lib.create_task(title="Worktree test", project=str(git_repo))
            lib.claim_task(task["id"])

            lib.create_worktree(str(git_repo), task["id"])

            # Check task YAML has worktree info
            claimed_path = swarm_tmpdir / "tasks" / "claimed" / f"{task['id']}.yaml"
            task_data = yaml.safe_load(open(claimed_path))
            assert "worktree" in task_data
            assert "branch" in task_data


class TestCompleteWorktree:
    def test_complete_worktree_merge(self, swarm_tmpdir, git_repo):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            wt_path = lib.create_worktree(str(git_repo), "task-010")

            # Make a change in the worktree
            (Path(wt_path) / "new-file.txt").write_text("from worktree")
            subprocess.run(
                ["git", "add", "new-file.txt"], cwd=wt_path, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "worktree change"],
                cwd=wt_path,
                capture_output=True,
            )

            result = lib.complete_worktree(str(git_repo), "task-010", merge=True)
            assert result["action"] == "merged"
            assert result["branch"] == "swarm/testhost/task-010"

            # Worktree should be cleaned up
            assert not Path(wt_path).exists()

            # Branch artifact recorded
            artifact_path = swarm_tmpdir / "artifacts" / "branches" / "task-010.yaml"
            assert artifact_path.exists()

    def test_complete_worktree_branch_only(self, swarm_tmpdir, git_repo):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            wt_path = lib.create_worktree(str(git_repo), "task-011")

            # No remote, so push will fail gracefully
            result = lib.complete_worktree(str(git_repo), "task-011", merge=False)
            assert result["action"] == "branch-only-local"

            # Artifact still recorded
            artifact_path = swarm_tmpdir / "artifacts" / "branches" / "task-011.yaml"
            assert artifact_path.exists()


class TestListWorktrees:
    def test_list_worktrees(self, swarm_tmpdir, git_repo):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(
                lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"
            ),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_worktree(str(git_repo), "task-020")

            worktrees = lib.list_worktrees(str(git_repo))
            # At least 2: main + our worktree
            assert len(worktrees) >= 2
            paths = [wt["path"] for wt in worktrees]
            expected = str(swarm_tmpdir / "worktrees" / "task-020")
            assert expected in paths

    def test_list_worktrees_invalid_repo(self, tmp_path):
        result = lib.list_worktrees(str(tmp_path / "nonexistent"))
        assert result == []

    def test_list_worktrees_no_extras(self, git_repo):
        worktrees = lib.list_worktrees(str(git_repo))
        # Just the main worktree
        assert len(worktrees) == 1
