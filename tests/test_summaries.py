"""Tests for session summaries — creation, filtering, recency, context loading."""

import sys
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import swarm_lib as lib


class TestSessionSummaryDataclass:
    def test_create_summary(self):
        summary = lib.SessionSummary(
            hostname="node_gpu",
            session_id="sess-001",
            timestamp="2026-03-22T10:00:00Z",
            project="<project-a-path>",
            task_id="task-001",
            duration_minutes=45,
            key_decisions=["Switched to ChromaDB v2", "Added retry logic"],
            files_changed=["src/rag.py", "tests/test_rag.py"],
            issues_found=["ChromaDB connection timeout at scale"],
            artifacts_produced=["results.json"],
            context_for_next="RAG pipeline refactored. Need to test with full corpus next.",
        )
        assert summary.hostname == "node_gpu"
        assert summary.duration_minutes == 45
        assert len(summary.key_decisions) == 2
        assert len(summary.files_changed) == 2

    def test_summary_defaults(self):
        summary = lib.SessionSummary(
            hostname="testhost",
            session_id="",
            timestamp="2026-03-22T10:00:00Z",
            project="/opt/test",
        )
        assert summary.task_id is None
        assert summary.duration_minutes == 0
        assert summary.key_decisions == []
        assert summary.files_changed == []
        assert summary.context_for_next == ""


class TestShareSessionSummary:
    def test_share_summary(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            summary = lib.SessionSummary(
                hostname="testhost",
                session_id="sess-100",
                timestamp="2026-03-22T14:30:00Z",
                project="/opt/test-project",
                duration_minutes=30,
                key_decisions=["Refactored module X"],
                files_changed=["src/module_x.py"],
                context_for_next="Module X refactored, needs integration test.",
            )
            path = lib.share_session_summary(summary)
            assert path.exists()
            assert path.name.startswith("testhost-")
            assert path.suffix == ".yaml"

            # Verify contents
            data = yaml.safe_load(open(path))
            assert data["hostname"] == "testhost"
            assert data["project"] == "/opt/test-project"
            assert data["context_for_next"] == "Module X refactored, needs integration test."

    def test_share_multiple_summaries(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            for i in range(3):
                summary = lib.SessionSummary(
                    hostname="testhost",
                    session_id=f"sess-{i}",
                    timestamp=f"2026-03-22T1{i}:00:00Z",
                    project="/opt/test-project",
                    context_for_next=f"Summary {i}",
                )
                lib.share_session_summary(summary)

            summaries_dir = swarm_tmpdir / "artifacts" / "summaries"
            files = list(summaries_dir.glob("*.yaml"))
            assert len(files) == 3


class TestGetRelevantSummaries:
    def test_filter_by_project(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            # Create summaries for different projects with distinct timestamps
            projects_and_ts = [
                ("/opt/project-a", "2026-03-22T10:00:00Z"),
                ("/opt/project-b", "2026-03-22T10:01:00Z"),
                ("/opt/project-a", "2026-03-22T10:02:00Z"),
            ]
            for proj, ts in projects_and_ts:
                summary = lib.SessionSummary(
                    hostname="testhost",
                    session_id="sess",
                    timestamp=ts,
                    project=proj,
                )
                lib.share_session_summary(summary)

            results = lib.get_relevant_summaries("/opt/project-a")
            assert len(results) == 2
            assert all(r["project"] == "/opt/project-a" for r in results)

    def test_recency_sorting(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            timestamps = [
                "2026-03-22T10:00:00Z",
                "2026-03-22T12:00:00Z",
                "2026-03-22T11:00:00Z",
            ]
            for ts in timestamps:
                summary = lib.SessionSummary(
                    hostname="testhost",
                    session_id="sess",
                    timestamp=ts,
                    project="/opt/test",
                    context_for_next=f"ctx-{ts}",
                )
                lib.share_session_summary(summary)

            results = lib.get_relevant_summaries("/opt/test")
            # Should be sorted by timestamp descending
            assert results[0]["timestamp"] == "2026-03-22T12:00:00Z"
            assert results[1]["timestamp"] == "2026-03-22T11:00:00Z"
            assert results[2]["timestamp"] == "2026-03-22T10:00:00Z"

    def test_limit_results(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            for i in range(10):
                summary = lib.SessionSummary(
                    hostname="testhost",
                    session_id=f"sess-{i}",
                    timestamp=f"2026-03-22T{10 + i}:00:00Z",
                    project="/opt/test",
                )
                lib.share_session_summary(summary)

            results = lib.get_relevant_summaries("/opt/test", limit=3)
            assert len(results) == 3

    def test_empty_summaries(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            results = lib.get_relevant_summaries("/opt/nonexistent")
            assert results == []


class TestGetLatestSummaryContext:
    def test_returns_context_string(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            summary = lib.SessionSummary(
                hostname="node_gpu",
                session_id="sess-1",
                timestamp="2026-03-22T14:00:00Z",
                project="<project-a-path>",
                duration_minutes=60,
                context_for_next="RAG pipeline complete. Run integration tests next.",
            )
            lib.share_session_summary(summary)

            ctx = lib.get_latest_summary_context("<project-a-path>")
            assert "node_gpu" in ctx
            assert "RAG pipeline complete" in ctx

    def test_empty_when_no_summaries(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            ctx = lib.get_latest_summary_context("/opt/nonexistent")
            assert ctx == ""

    def test_empty_when_no_context(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            summary = lib.SessionSummary(
                hostname="testhost",
                session_id="sess-1",
                timestamp="2026-03-22T14:00:00Z",
                project="/opt/test",
                context_for_next="",
            )
            lib.share_session_summary(summary)

            ctx = lib.get_latest_summary_context("/opt/test")
            assert ctx == ""


class TestGenerateSessionSummary:
    def test_generate_basic_summary(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            summary = lib.generate_session_summary(
                project="/opt/nonexistent",  # no git, that's fine
                session_id="sess-gen-1",
                duration_minutes=20,
                key_decisions=["Decision 1", "Decision 2"],
                context_for_next="Needs review.",
            )
            assert summary.hostname == "testhost"
            assert summary.session_id == "sess-gen-1"
            assert summary.duration_minutes == 20
            assert len(summary.key_decisions) == 2
            assert summary.context_for_next == "Needs review."

    def test_max_decisions_respected(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            many_decisions = [f"Decision {i}" for i in range(50)]
            summary = lib.generate_session_summary(
                project="/opt/test",
                key_decisions=many_decisions,
            )
            # Config sets max_decisions to 10
            assert len(summary.key_decisions) == 10

    def test_generate_with_git_repo(self, swarm_tmpdir, tmp_path):
        """Test that files_changed is populated from a real git repo."""
        import subprocess

        repo = tmp_path / "git-test"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=str(repo),
            capture_output=True,
        )
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
        (repo / "file1.py").write_text("pass")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

        # Make uncommitted changes
        (repo / "file2.py").write_text("new")
        subprocess.run(["git", "add", "file2.py"], cwd=str(repo), capture_output=True)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            summary = lib.generate_session_summary(project=str(repo))
            assert "file2.py" in summary.files_changed


class TestSessionStartContextLoading:
    """Verify session-start hook can load context from summaries."""

    def test_context_available_for_session_start(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            # Simulate a previous session leaving a summary
            summary = lib.SessionSummary(
                hostname="node_gpu",
                session_id="prev-sess",
                timestamp="2026-03-22T13:00:00Z",
                project="<hydra-project-path>",
                duration_minutes=90,
                context_for_next="Finished Kin indexing. 78 entities. Run kin verify next.",
            )
            lib.share_session_summary(summary)

            # Now a new session starts and loads context
            ctx = lib.get_latest_summary_context("<hydra-project-path>")
            assert "node_gpu" in ctx
            assert "Finished Kin indexing" in ctx
            assert "78 entities" in ctx
