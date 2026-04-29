"""Tests for work_generator — plan scanning, git detection, deduplication, model inference."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from work_generator import (
    WorkGenerator,
    infer_model,
    infer_requires,
    is_human_task,
    load_scan_state,
    save_scan_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path):
    """A fake project directory with a plans/ subdir."""
    proj = tmp_path / "my-project"
    (proj / "plans").mkdir(parents=True)
    return proj


def _make_generator(swarm_root: Path, projects: dict | None = None) -> WorkGenerator:
    config = {
        "swarm_root": str(swarm_root),
        "work_generator": {
            "enabled": True,
            "prometheus_url": "http://127.0.0.1:9090",
            "projects": projects or {},
        },
        "scheduled_maintenance": {"daily_hour": 0, "weekly_day": 0},
    }
    return WorkGenerator(config)


# ---------------------------------------------------------------------------
# Model inference
# ---------------------------------------------------------------------------


class TestInferModel:
    def test_opus_keywords(self):
        assert infer_model("Design the architecture for microservices") == "opus"
        assert infer_model("Research OAuth2 implementation") == "opus"
        assert infer_model("Analyze performance bottleneck") == "opus"

    def test_haiku_keywords(self):
        assert infer_model("Run tests on monero-farm") == "haiku"
        assert infer_model("Check service status on node_gpu") == "haiku"
        assert infer_model("Verify backup integrity") == "haiku"
        assert infer_model("Scan for vulnerabilities") == "haiku"

    def test_sonnet_default(self):
        assert infer_model("Implement user authentication") == "sonnet"
        assert infer_model("Build the REST API endpoint") == "sonnet"
        assert infer_model("Write migration script") == "sonnet"

    def test_opus_beats_haiku(self):
        # Opus keywords take precedence
        assert infer_model("Design and verify the schema") == "opus"


class TestInferRequires:
    def test_gpu_keywords(self):
        caps = infer_requires("Run GPU training job with CUDA")
        assert "gpu" in caps

    def test_docker_keywords(self):
        caps = infer_requires("Deploy with Ansible to Docker Swarm")
        assert "docker" in caps

    def test_no_capabilities(self):
        caps = infer_requires("Write a markdown summary")
        assert caps == []

    def test_both_capabilities(self):
        caps = infer_requires("Deploy GPU-accelerated Docker container")
        assert "gpu" in caps
        assert "docker" in caps


class TestIsHumanTask:
    def test_josh_keyword(self):
        assert is_human_task("Josh reviews the output") is True

    def test_review_keyword(self):
        assert is_human_task("Manual review required") is True

    def test_physical_keyword(self):
        assert is_human_task("Physical access needed for hardware swap") is True

    def test_machine_task(self):
        assert is_human_task("Run tests on all repos") is False
        assert is_human_task("Deploy service to node_gpu") is False


# ---------------------------------------------------------------------------
# Project plan scanner
# ---------------------------------------------------------------------------


class TestScanProjectPlans:
    def test_finds_first_incomplete_item(self, swarm_tmpdir, project_dir):
        plan = project_dir / "plans" / "my-project-plan.md"
        plan.write_text(
            "# Phase 1 Setup\n"
            "- [x] Done item\n"
            "- [ ] Implement authentication\n"
            "- [ ] Write tests\n"
        )
        wg = _make_generator(
            swarm_tmpdir,
            {"my-project": {"path": str(project_dir), "host": "node_primary"}},
        )
        tasks = wg.scan_project_plans()
        assert len(tasks) == 1
        assert "Implement authentication" in tasks[0]["title"]

    def test_skips_human_tasks(self, swarm_tmpdir, project_dir):
        plan = project_dir / "plans" / "my-project-plan.md"
        plan.write_text(
            "# Phase 1\n- [ ] Josh reviews the draft\n- [ ] Run automated tests\n"
        )
        wg = _make_generator(
            swarm_tmpdir,
            {"my-project": {"path": str(project_dir), "host": "node_primary"}},
        )
        tasks = wg.scan_project_plans()
        # Josh review should be skipped; "Run automated tests" should be found
        assert len(tasks) == 1
        assert "Run automated tests" in tasks[0]["title"]

    def test_no_plans_dir(self, swarm_tmpdir, tmp_path):
        empty_proj = tmp_path / "empty-proj"
        empty_proj.mkdir()
        wg = _make_generator(
            swarm_tmpdir,
            {"empty-proj": {"path": str(empty_proj), "host": "node_primary"}},
        )
        tasks = wg.scan_project_plans()
        assert tasks == []

    def test_all_complete(self, swarm_tmpdir, project_dir):
        plan = project_dir / "plans" / "my-project-plan.md"
        plan.write_text("# Phase 1\n- [x] Done\n- [x] Also done\n")
        wg = _make_generator(
            swarm_tmpdir,
            {"my-project": {"path": str(project_dir), "host": "node_primary"}},
        )
        tasks = wg.scan_project_plans()
        assert tasks == []

    def test_giga_host_adds_gpu_require(self, swarm_tmpdir, project_dir):
        plan = project_dir / "plans" / "my-project-plan.md"
        plan.write_text("# Phase 1\n- [ ] Implement inference pipeline\n")
        wg = _make_generator(
            swarm_tmpdir,
            {"my-project": {"path": str(project_dir), "host": "node_gpu"}},
        )
        tasks = wg.scan_project_plans()
        assert len(tasks) == 1
        assert "gpu" in tasks[0]["requires"]

    def test_phase_parsed_in_description(self, swarm_tmpdir, project_dir):
        plan = project_dir / "plans" / "my-project-plan.md"
        plan.write_text("# Phase 2 Data Pipeline\n- [ ] Build ETL job\n")
        wg = _make_generator(
            swarm_tmpdir,
            {"my-project": {"path": str(project_dir), "host": "node_primary"}},
        )
        tasks = wg.scan_project_plans()
        assert "Phase 2 Data Pipeline" in tasks[0]["description"]


# ---------------------------------------------------------------------------
# Prometheus alert scanner
# ---------------------------------------------------------------------------


class TestScanPrometheusAlerts:
    def _mock_response(self, alerts: list[dict]) -> MagicMock:
        mock = MagicMock()
        payload = json.dumps({"data": {"alerts": alerts}}).encode()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        mock.read = MagicMock(return_value=payload)
        return mock

    def test_no_firing_alerts(self, swarm_tmpdir):
        wg = _make_generator(swarm_tmpdir)
        alerts = [{"state": "pending", "labels": {"alertname": "TestAlert"}}]
        with patch("work_generator.urlopen", return_value=self._mock_response(alerts)):
            tasks = wg.scan_prometheus_alerts()
        assert tasks == []

    def test_firing_warning_alert(self, swarm_tmpdir):
        wg = _make_generator(swarm_tmpdir)
        alerts = [
            {
                "state": "firing",
                "labels": {
                    "alertname": "HighDiskUsage",
                    "instance": "node_primary:9100",
                    "severity": "warning",
                },
            }
        ]
        with patch("work_generator.urlopen", return_value=self._mock_response(alerts)):
            tasks = wg.scan_prometheus_alerts()
        assert len(tasks) == 1
        assert "HighDiskUsage" in tasks[0]["title"]
        assert tasks[0]["priority"] == "medium"

    def test_firing_critical_alert(self, swarm_tmpdir):
        wg = _make_generator(swarm_tmpdir)
        alerts = [
            {
                "state": "firing",
                "labels": {
                    "alertname": "NodeDown",
                    "instance": "node_gpu:9100",
                    "severity": "critical",
                },
            }
        ]
        with patch("work_generator.urlopen", return_value=self._mock_response(alerts)):
            tasks = wg.scan_prometheus_alerts()
        assert len(tasks) == 1
        assert tasks[0]["priority"] == "high"

    def test_prometheus_unreachable(self, swarm_tmpdir):
        wg = _make_generator(swarm_tmpdir)
        from urllib.error import URLError

        with patch(
            "work_generator.urlopen", side_effect=URLError("connection refused")
        ):
            tasks = wg.scan_prometheus_alerts()
        assert tasks == []


# ---------------------------------------------------------------------------
# Git change scanner
# ---------------------------------------------------------------------------


class TestScanGitChanges:
    def _fake_repo(self, base: Path, name: str) -> Path:
        proj = base / name
        (proj / ".git").mkdir(parents=True)
        (proj / "tests").mkdir(parents=True)
        return proj

    def test_no_new_commits(self, swarm_tmpdir, tmp_path):
        proj = self._fake_repo(tmp_path, "stable-proj")
        same_sha = "abc123def456abc123def456abc123def456abc1"
        save_scan_state(swarm_tmpdir, {"stable-proj": {"last_commit": same_sha}})

        wg = _make_generator(
            swarm_tmpdir,
            {"stable-proj": {"path": str(proj), "host": "node_primary"}},
        )

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "rev-parse" in cmd:
                result.returncode = 0
                result.stdout = same_sha + "\n"
            elif "status" in cmd:
                result.returncode = 0
                result.stdout = ""  # clean
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            tasks = wg.scan_git_changes()

        assert tasks == []

    def test_new_commits_with_tests(self, swarm_tmpdir, tmp_path):
        proj = self._fake_repo(tmp_path, "active-proj")
        old_sha = "aaa" + "0" * 37
        new_sha = "bbb" + "0" * 37
        save_scan_state(swarm_tmpdir, {"active-proj": {"last_commit": old_sha}})

        wg = _make_generator(
            swarm_tmpdir,
            {"active-proj": {"path": str(proj), "host": "node_primary"}},
        )

        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "rev-parse" in cmd:
                result.returncode = 0
                result.stdout = new_sha + "\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.returncode = 0
                result.stdout = "src/main.py\n"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            tasks = wg.scan_git_changes()

        titles = [t["title"] for t in tasks]
        assert any("run tests" in t.lower() for t in titles)

    def test_claude_md_change_creates_review_task(self, swarm_tmpdir, tmp_path):
        proj = self._fake_repo(tmp_path, "doc-proj")
        old_sha = "ccc" + "0" * 37
        new_sha = "ddd" + "0" * 37
        save_scan_state(swarm_tmpdir, {"doc-proj": {"last_commit": old_sha}})

        wg = _make_generator(
            swarm_tmpdir,
            {"doc-proj": {"path": str(proj), "host": "node_primary"}},
        )

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "rev-parse" in cmd:
                result.returncode = 0
                result.stdout = new_sha + "\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.returncode = 0
                result.stdout = "CLAUDE.md\nsrc/foo.py\n"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            tasks = wg.scan_git_changes()

        titles = [t["title"] for t in tasks]
        assert any("CLAUDE.md" in t for t in titles)

    def test_uncommitted_changes_creates_kin_commit_task(self, swarm_tmpdir, tmp_path):
        proj = self._fake_repo(tmp_path, "dirty-proj")
        same_sha = "eee" + "0" * 37
        save_scan_state(swarm_tmpdir, {"dirty-proj": {"last_commit": same_sha}})

        wg = _make_generator(
            swarm_tmpdir,
            {"dirty-proj": {"path": str(proj), "host": "node_primary"}},
        )

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "rev-parse" in cmd:
                result.returncode = 0
                result.stdout = same_sha + "\n"
            elif "status" in cmd:
                result.returncode = 0
                result.stdout = "M  src/main.py\n"
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            tasks = wg.scan_git_changes()

        titles = [t["title"] for t in tasks]
        assert any("Kin commit" in t for t in titles)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_removes_existing_pending(self, swarm_tmpdir):
        # Write an existing pending task
        pending_dir = swarm_tmpdir / "tasks" / "pending"
        existing = {
            "id": "task-001",
            "title": "Run tests on my-project",
            "description": "",
        }
        with open(pending_dir / "task-001.yaml", "w") as f:
            yaml.dump(existing, f)

        wg = _make_generator(swarm_tmpdir)
        proposed = [
            {"title": "Run tests on my-project", "source": "git_scan"},
            {"title": "New unique task", "source": "project_plan"},
        ]
        result = wg.deduplicate(proposed)
        titles = [t["title"] for t in result]
        assert "Run tests on my-project" not in titles
        assert "New unique task" in titles

    def test_removes_existing_claimed(self, swarm_tmpdir):
        claimed_dir = swarm_tmpdir / "tasks" / "claimed"
        claimed_dir.mkdir(parents=True, exist_ok=True)
        existing = {"id": "task-002", "title": "Investigate alert: NodeDown on node_gpu"}
        with open(claimed_dir / "task-002.yaml", "w") as f:
            yaml.dump(existing, f)

        wg = _make_generator(swarm_tmpdir)
        proposed = [
            {"title": "Investigate alert: NodeDown on node_gpu"},
            {"title": "Another new task"},
        ]
        result = wg.deduplicate(proposed)
        titles = [t["title"] for t in result]
        assert "Investigate alert: NodeDown on node_gpu" not in titles
        assert "Another new task" in titles

    def test_deduplicates_within_proposed(self, swarm_tmpdir):
        wg = _make_generator(swarm_tmpdir)
        proposed = [
            {"title": "Same task", "source": "a"},
            {"title": "Same task", "source": "b"},
        ]
        result = wg.deduplicate(proposed)
        assert len(result) == 1

    def test_empty_proposed(self, swarm_tmpdir):
        wg = _make_generator(swarm_tmpdir)
        assert wg.deduplicate([]) == []


# ---------------------------------------------------------------------------
# Scan state persistence
# ---------------------------------------------------------------------------


class TestScanState:
    def test_save_and_load(self, swarm_tmpdir):
        state = {"my-project": {"last_commit": "abc123"}}
        save_scan_state(swarm_tmpdir, state)
        loaded = load_scan_state(swarm_tmpdir)
        assert loaded["my-project"]["last_commit"] == "abc123"

    def test_load_missing_returns_empty(self, tmp_path):
        result = load_scan_state(tmp_path / "nonexistent")
        assert result == {}

    def test_save_updates_existing(self, swarm_tmpdir):
        save_scan_state(swarm_tmpdir, {"proj-a": {"last_commit": "111"}})
        save_scan_state(swarm_tmpdir, {"proj-a": {"last_commit": "222"}, "proj-b": {}})
        loaded = load_scan_state(swarm_tmpdir)
        assert loaded["proj-a"]["last_commit"] == "222"
        assert "proj-b" in loaded


# ---------------------------------------------------------------------------
# Scheduled maintenance
# ---------------------------------------------------------------------------


class TestScheduledMaintenance:
    def test_daily_tasks_at_or_after_hour(self, swarm_tmpdir):
        # daily_hour = 0 means any hour qualifies
        wg = _make_generator(swarm_tmpdir)
        tasks = wg.scan_scheduled_maintenance()
        titles = [t["title"] for t in tasks]
        assert any("security scan" in t.lower() for t in titles)
        assert any("package updates" in t.lower() for t in titles)
        assert any("claude-config" in t.lower() for t in titles)
        assert any("Kin commit" in t for t in titles)

    def test_daily_tasks_not_duplicated(self, swarm_tmpdir):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        save_scan_state(swarm_tmpdir, {"maintenance": {"last_daily": today}})

        # daily_hour=0 so it should run... but last_daily is today so it shouldn't
        config = {
            "swarm_root": str(swarm_tmpdir),
            "work_generator": {
                "projects": {},
                "prometheus_url": "http://127.0.0.1:9090",
            },
            "scheduled_maintenance": {"daily_hour": 0, "weekly_day": 0},
        }
        wg = WorkGenerator(config)
        tasks = wg.scan_scheduled_maintenance()
        # Should be empty or only weekly tasks (if today is the right day)
        daily_titles = [
            t["title"] for t in tasks if t.get("source") == "scheduled_daily"
        ]
        assert daily_titles == []
