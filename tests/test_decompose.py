"""Tests for task decomposition — subtask creation, parent state tracking, auto-complete."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import swarm_lib as lib


class TestTaskDecomposer:
    """Test rule-based decomposition suggestions."""

    def test_all_sections_pattern(self, swarm_tmpdir):
        task = {
            "title": "Generate 50 CPA questions across all sections",
            "description": "Need questions for FAR, AUD, REG, BAR",
            "project": "/opt/examforge",
        }
        suggestions = lib.TaskDecomposer.suggest(task)
        # 4 sections + 1 validation = 5
        assert len(suggestions) == 5
        titles = [s["title"] for s in suggestions]
        assert any("FAR" in t for t in titles)
        assert any("AUD" in t for t in titles)
        assert any("REG" in t for t in titles)
        assert any("BAR" in t for t in titles)
        assert any("Validate" in t for t in titles)

    def test_generate_and_validate_pattern(self, swarm_tmpdir):
        task = {
            "title": "Generate and validate tax scenarios",
            "description": "Create scenarios then validate outputs",
            "project": "<project-a-path>",
        }
        suggestions = lib.TaskDecomposer.suggest(task)
        assert len(suggestions) == 2
        titles = [s["title"] for s in suggestions]
        assert any("Generate" in t for t in titles)
        assert any("Validate" in t for t in titles)

    def test_multiple_projects_pattern(self, swarm_tmpdir):
        task = {
            "title": "Update configs for project-a and monero",
            "description": "Both projects need config updates",
            "project": "",
        }
        suggestions = lib.TaskDecomposer.suggest(task)
        assert len(suggestions) == 2
        projects = [s["project"] for s in suggestions]
        assert "<project-a-path>/" in projects
        assert "/opt/monero-farm/" in projects

    def test_no_pattern_match(self, swarm_tmpdir):
        task = {
            "title": "Fix a simple bug",
            "description": "Just one thing to do",
            "project": "/opt/test",
        }
        suggestions = lib.TaskDecomposer.suggest(task)
        assert len(suggestions) == 0

    def test_capability_inference_ollama(self, swarm_tmpdir):
        task = {
            "title": "Generate and validate embeddings with ollama",
            "description": "",
            "project": "/opt/test",
        }
        suggestions = lib.TaskDecomposer.suggest(task)
        assert len(suggestions) == 2
        for s in suggestions:
            assert "ollama" in s["requires"]

    def test_capability_inference_gpu(self, swarm_tmpdir):
        task = {
            "title": "Generate all sections with GPU training",
            "description": "Train across all sections",
            "project": "/opt/test",
        }
        suggestions = lib.TaskDecomposer.suggest(task)
        assert len(suggestions) > 0
        for s in suggestions:
            assert "gpu" in s["requires"]


class TestDecomposeTask:
    """Test actual decomposition execution."""

    def test_decompose_creates_subtasks(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            task = lib.create_task(
                title="Generate 50 CPA questions across all sections",
                project="/opt/examforge",
            )
            task_id = task["id"]

            subtasks = [
                {"title": "Generate FAR questions", "requires": ["ollama"]},
                {"title": "Generate AUD questions", "requires": ["ollama"]},
                {"title": "Validate all questions", "requires": ["ollama"]},
            ]
            parent = lib.decompose_task(task_id, subtasks)

            assert parent["state"] == "decomposed"
            assert len(parent["subtasks"]) == 3
            assert parent["subtasks"][0] == f"{task_id}-a"
            assert parent["subtasks"][1] == f"{task_id}-b"
            assert parent["subtasks"][2] == f"{task_id}-c"

            # Parent moved to decomposed/
            assert not (swarm_tmpdir / "tasks" / "pending" / f"{task_id}.yaml").exists()
            assert (swarm_tmpdir / "tasks" / "decomposed" / f"{task_id}.yaml").exists()

            # Subtasks in pending/
            for sub_id in parent["subtasks"]:
                sub_path = swarm_tmpdir / "tasks" / "pending" / f"{sub_id}.yaml"
                assert sub_path.exists()
                sub_data = yaml.safe_load(open(sub_path))
                assert sub_data["parent_id"] == task_id

    def test_decompose_subtask_inherits_project(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            task = lib.create_task(title="Parent task", project="/opt/examforge")
            subtasks = [{"title": "Sub 1"}, {"title": "Sub 2"}]
            parent = lib.decompose_task(task["id"], subtasks)

            for sub_id in parent["subtasks"]:
                sub_path = swarm_tmpdir / "tasks" / "pending" / f"{sub_id}.yaml"
                sub_data = yaml.safe_load(open(sub_path))
                assert sub_data["project"] == "/opt/examforge"

    def test_decompose_nonexistent_task(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            with pytest.raises(FileNotFoundError):
                lib.decompose_task("task-999", [{"title": "sub"}])


class TestParentAutoComplete:
    """Test that parent auto-completes when all subtasks complete."""

    def test_parent_auto_completes(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            task = lib.create_task(title="Parent", project="/opt/test")
            task_id = task["id"]

            subtasks = [
                {"title": "Sub A"},
                {"title": "Sub B"},
            ]
            parent = lib.decompose_task(task_id, subtasks)
            sub_a, sub_b = parent["subtasks"]

            # Claim and complete sub A
            lib.claim_task(sub_a)
            lib.complete_task(sub_a)

            # Parent should NOT be complete yet
            assert (swarm_tmpdir / "tasks" / "decomposed" / f"{task_id}.yaml").exists()
            assert not (swarm_tmpdir / "tasks" / "completed" / f"{task_id}.yaml").exists()

            # Claim and complete sub B
            lib.claim_task(sub_b)
            lib.complete_task(sub_b)

            # Now parent should auto-complete
            assert not (swarm_tmpdir / "tasks" / "decomposed" / f"{task_id}.yaml").exists()
            assert (swarm_tmpdir / "tasks" / "completed" / f"{task_id}.yaml").exists()

            # Verify parent data
            completed_data = yaml.safe_load(
                open(swarm_tmpdir / "tasks" / "completed" / f"{task_id}.yaml")
            )
            assert completed_data["state"] == "completed"
            assert completed_data["completed_by"] == "auto"

    def test_parent_not_completed_with_pending_subtasks(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            task = lib.create_task(title="Parent", project="/opt/test")
            subtasks = [{"title": "Sub A"}, {"title": "Sub B"}]
            parent = lib.decompose_task(task["id"], subtasks)

            # Complete only one
            sub_a = parent["subtasks"][0]
            lib.claim_task(sub_a)
            lib.complete_task(sub_a)

            result = lib.check_parent_completion(task["id"])
            assert result is False


class TestDecomposedTaskListing:
    """Test that decomposed tasks appear in listings."""

    def test_decomposed_tasks_in_list(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            task = lib.create_task(title="Decomposable", project="/opt/test")
            lib.decompose_task(task["id"], [{"title": "Sub"}])

            all_tasks = lib.list_tasks()
            stages = {t["_stage"] for t in all_tasks}
            assert "decomposed" in stages

    def test_decomposed_filter(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            task = lib.create_task(title="Decomposable", project="/opt/test")
            lib.decompose_task(task["id"], [{"title": "Sub"}])

            decomposed = lib.list_tasks("decomposed")
            assert len(decomposed) == 1
            assert decomposed[0]["state"] == "decomposed"
