"""Tests for sync engine — git sync and config propagation."""

import sys
from pathlib import Path
from unittest.mock import patch
import subprocess


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class TestRun:
    def test_returns_completed_process(self):
        from sync_engine import _run

        result = _run(["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_handles_timeout(self):
        from sync_engine import _run

        result = _run(["sleep", "10"], timeout=1)
        assert result.returncode == 1

    def test_handles_missing_command(self):
        from sync_engine import _run

        result = _run(["nonexistent_command_xyz"])
        assert result.returncode == 1


class TestIsGitRepo:
    def test_detects_git_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        from sync_engine import _is_git_repo

        assert _is_git_repo(str(tmp_path)) is True

    def test_rejects_non_repo(self, tmp_path):
        from sync_engine import _is_git_repo

        assert _is_git_repo(str(tmp_path)) is False


class TestGitPull:
    def test_skips_non_repo(self, tmp_path):
        from sync_engine import git_pull

        result = git_pull(str(tmp_path))
        assert result["status"] == "skip"

    def test_successful_pull(self, tmp_path):
        (tmp_path / ".git").mkdir()
        from sync_engine import git_pull

        with patch(
            "sync_engine._run", return_value=_completed(stdout="Already up to date.")
        ):
            result = git_pull(str(tmp_path))
        assert result["status"] == "ok"

    def test_failed_pull(self, tmp_path):
        (tmp_path / ".git").mkdir()
        from sync_engine import git_pull

        with patch(
            "sync_engine._run", return_value=_completed(returncode=1, stderr="error")
        ):
            result = git_pull(str(tmp_path))
        assert result["status"] == "error"


class TestGitPush:
    def test_skips_non_repo(self, tmp_path):
        from sync_engine import git_push

        result = git_push(str(tmp_path))
        assert result["status"] == "skip"

    def test_skips_dirty_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        from sync_engine import git_push

        with patch("sync_engine._run") as mock_run:
            mock_run.return_value = _completed(stdout="M file.py\n")
            result = git_push(str(tmp_path))
        assert result["status"] == "skip"
        assert "uncommitted" in result["reason"]

    def test_successful_push(self, tmp_path):
        (tmp_path / ".git").mkdir()
        from sync_engine import git_push

        with patch("sync_engine._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # status --porcelain (clean)
                _completed(stdout="Everything up-to-date"),  # push
                _completed(stdout="abc123 feat: test"),  # git log
            ]
            with patch("sync_engine.emit"):
                result = git_push(str(tmp_path))
        assert result["status"] == "ok"


class TestPullAllProjects:
    def test_pulls_existing_projects(self, tmp_path):
        from sync_engine import pull_all_projects

        proj = tmp_path / "test-project"
        proj.mkdir()
        (proj / ".git").mkdir()
        with patch("sync_engine.projects_for_host", return_value=[str(proj)]):
            with patch(
                "sync_engine._run",
                return_value=_completed(stdout="Already up to date."),
            ):
                results = pull_all_projects()
        assert str(proj) in results

    def test_skips_nonexistent_projects(self):
        from sync_engine import pull_all_projects

        with patch(
            "sync_engine.projects_for_host", return_value=["/nonexistent/project"]
        ):
            results = pull_all_projects()
        assert results == {}


class TestGetDirtyRepos:
    def test_finds_dirty_repos(self, tmp_path):
        proj = tmp_path / "dirty-project"
        proj.mkdir()
        (proj / ".git").mkdir()
        from sync_engine import get_dirty_repos

        with patch("sync_engine.projects_for_host", return_value=[str(proj)]):
            with patch(
                "sync_engine._run", return_value=_completed(stdout="M file.py\n")
            ):
                result = get_dirty_repos()
        assert len(result) == 1
        assert result[0]["project"] == str(proj)
        assert result[0]["files"] == 1

    def test_clean_repos_not_listed(self, tmp_path):
        proj = tmp_path / "clean-project"
        proj.mkdir()
        (proj / ".git").mkdir()
        from sync_engine import get_dirty_repos

        with patch("sync_engine.projects_for_host", return_value=[str(proj)]):
            with patch("sync_engine._run", return_value=_completed(stdout="")):
                result = get_dirty_repos()
        assert len(result) == 0


class TestPullAllProjectsParallel:
    def test_parallel_pull_uses_thread_pool(self, tmp_path):
        """Verify pull_all_projects runs concurrently via ThreadPoolExecutor."""
        import threading
        import time as _time

        proj1 = tmp_path / "proj1"
        proj2 = tmp_path / "proj2"
        for p in (proj1, proj2):
            p.mkdir()
            (p / ".git").mkdir()

        call_times = []
        lock = threading.Lock()

        def slow_pull(path):
            with lock:
                call_times.append((_time.monotonic(), path))
            _time.sleep(0.05)
            return {"status": "ok", "stdout": "", "stderr": ""}

        from sync_engine import pull_all_projects

        with patch(
            "sync_engine.projects_for_host", return_value=[str(proj1), str(proj2)]
        ):
            with patch("sync_engine.git_pull", side_effect=slow_pull):
                start = _time.monotonic()
                results = pull_all_projects()
                elapsed = _time.monotonic() - start

        assert str(proj1) in results
        assert str(proj2) in results
        # Both pulls started before either could finish (concurrent, not serial)
        # With 2 projects sleeping 0.05s each, serial = ~0.1s; concurrent < 0.1s + overhead
        assert elapsed < 0.15, f"Expected concurrent execution, took {elapsed:.3f}s"

    def test_parallel_pull_error_per_project(self, tmp_path):
        """A single project failure doesn't abort the batch."""
        proj_ok = tmp_path / "ok"
        proj_bad = tmp_path / "bad"
        for p in (proj_ok, proj_bad):
            p.mkdir()
            (p / ".git").mkdir()

        def selective_pull(path):
            if "bad" in path:
                raise RuntimeError("network error")
            return {"status": "ok", "stdout": "", "stderr": ""}

        from sync_engine import pull_all_projects

        with patch(
            "sync_engine.projects_for_host", return_value=[str(proj_ok), str(proj_bad)]
        ):
            with patch("sync_engine.git_pull", side_effect=selective_pull):
                results = pull_all_projects()

        assert results[str(proj_ok)]["status"] == "ok"
        assert results[str(proj_bad)]["status"] == "error"


class TestProcessCommitEvents:
    def test_pulls_repos_from_other_hosts(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()
        (proj / ".git").mkdir()
        from sync_engine import process_commit_events

        events = [
            {"hostname": "gpu-server-1", "project": str(proj), "type": "commit"},
        ]
        with patch("events.query", return_value=events):
            with patch("socket.gethostname", return_value="orchestration-node"):
                with patch(
                    "sync_engine.git_pull",
                    return_value={"status": "ok", "stdout": "Updated"},
                ):
                    result = process_commit_events("2026-01-01T00:00:00Z")
        assert str(proj) in result

    def test_ignores_own_commits(self, tmp_path):
        from sync_engine import process_commit_events

        events = [
            {"hostname": "orchestration-node", "project": "/opt/test", "type": "commit"},
        ]
        with patch("events.query", return_value=events):
            with patch("socket.gethostname", return_value="orchestration-node"):
                result = process_commit_events("2026-01-01T00:00:00Z")
        assert result == {}
