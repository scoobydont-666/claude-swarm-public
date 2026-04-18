"""Tests for worktree dispatch module."""

import subprocess
from pathlib import Path

import pytest

from src.worktree_dispatch import (
    cleanup_worktree,
    create_worktree,
    list_worktrees,
    merge_worktree,
)


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo for testing."""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"], capture_output=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True)
    # Create initial commit
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)
    return str(repo)


@pytest.fixture
def worktree_base(tmp_path):
    return str(tmp_path / "worktrees")


class TestCreateWorktree:
    def test_create_local(self, git_repo, worktree_base):
        wt = create_worktree(
            repo_path=git_repo,
            dispatch_id="test-dispatch-1",
            host="localhost",
            worktree_base=worktree_base,
        )
        assert wt is not None
        assert Path(wt.path).exists()
        assert wt.branch == "swarm/test-dispatch-1"
        assert wt.dispatch_id == "test-dispatch-1"

    def test_worktree_has_files(self, git_repo, worktree_base):
        wt = create_worktree(git_repo, "test-files", host="localhost", worktree_base=worktree_base)
        assert wt is not None
        assert (Path(wt.path) / "README.md").exists()

    def test_sanitizes_dispatch_id(self, git_repo, worktree_base):
        wt = create_worktree(
            git_repo, "dispatch/with spaces/special", host="localhost", worktree_base=worktree_base
        )
        assert wt is not None
        assert "/" not in wt.branch.replace("swarm/", "")


class TestMergeWorktree:
    def test_merge_with_changes(self, git_repo, worktree_base):
        wt = create_worktree(git_repo, "test-merge", host="localhost", worktree_base=worktree_base)
        assert wt is not None
        # Make a change in the worktree
        (Path(wt.path) / "new_file.txt").write_text("hello from worktree")
        subprocess.run(["git", "-C", wt.path, "add", "."], capture_output=True)
        subprocess.run(
            ["git", "-C", wt.path, "commit", "-m", "worktree change"], capture_output=True
        )
        # Merge back
        result = merge_worktree(wt, host="localhost")
        assert result is True
        # Verify the file exists in the main repo
        assert (Path(git_repo) / "new_file.txt").exists()

    def test_merge_no_changes(self, git_repo, worktree_base):
        wt = create_worktree(
            git_repo, "test-no-change", host="localhost", worktree_base=worktree_base
        )
        assert wt is not None
        # Merge without changes (should still succeed — fast-forward/no-op)
        result = merge_worktree(wt, host="localhost")
        assert result is True


class TestCleanup:
    def test_cleanup_removes_worktree(self, git_repo, worktree_base):
        wt = create_worktree(
            git_repo, "test-cleanup", host="localhost", worktree_base=worktree_base
        )
        assert wt is not None
        assert Path(wt.path).exists()
        cleanup_worktree(wt, host="localhost")
        assert not Path(wt.path).exists()


class TestListWorktrees:
    def test_list_includes_worktree(self, git_repo, worktree_base):
        wt = create_worktree(git_repo, "test-list", host="localhost", worktree_base=worktree_base)
        assert wt is not None
        worktrees = list_worktrees(git_repo, host="localhost")
        assert any("test-list" in w for w in worktrees)
        cleanup_worktree(wt, host="localhost")
