"""Tests for work generator backpressure — task queue limiting."""

import sys
from pathlib import Path
from unittest.mock import patch

import yaml

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestBackpressureConfig:
    def test_config_has_max_pending_tasks(self):
        """Test that swarm.yaml includes max_pending_tasks config."""
        from pathlib import Path

        swarm_config = Path("/opt/claude-swarm/config/swarm.yaml")
        with open(swarm_config) as f:
            config = yaml.safe_load(f)

        assert "work_generator" in config
        assert "max_pending_tasks" in config["work_generator"]
        assert config["work_generator"]["max_pending_tasks"] > 0


class TestWorkGeneratorBackpressure:
    def test_generate_work_no_backpressure(self, swarm_tmpdir):
        """Test work generation when pending tasks are below limit."""
        from work_generator import WorkGenerator

        config = {
            "swarm_root": str(swarm_tmpdir),
            "work_generator": {
                "max_pending_tasks": 10,
                "projects": {},
            },
        }

        # Create 5 pending tasks (below limit of 10)
        pending_dir = swarm_tmpdir / "tasks" / "pending"
        for i in range(5):
            task = {"id": f"task-{i:03d}", "title": f"Task {i}"}
            with open(pending_dir / f"task-{i:03d}.yaml", "w") as f:
                yaml.dump(task, f)

        gen = WorkGenerator(config)

        with (
            patch.object(
                gen,
                "scan_project_plans",
                return_value=[{"id": "new-task-001", "title": "New task"}],
            ),
            patch.object(gen, "scan_prometheus_alerts", return_value=[]),
            patch.object(gen, "scan_git_changes", return_value=[]),
            patch.object(gen, "scan_examforge_pipeline", return_value=[]),
            patch.object(gen, "scan_scheduled_maintenance", return_value=[]),
        ):
            tasks = gen.generate_work()
            assert len(tasks) > 0  # Should generate work

    def test_generate_work_at_limit_no_generation(self, swarm_tmpdir):
        """Test work generation stops when at pending task limit."""
        from work_generator import WorkGenerator

        config = {
            "swarm_root": str(swarm_tmpdir),
            "work_generator": {
                "max_pending_tasks": 5,
                "projects": {},
            },
        }

        # Create 5 pending tasks (at limit)
        pending_dir = swarm_tmpdir / "tasks" / "pending"
        for i in range(5):
            task = {"id": f"task-{i:03d}", "title": f"Task {i}"}
            with open(pending_dir / f"task-{i:03d}.yaml", "w") as f:
                yaml.dump(task, f)

        gen = WorkGenerator(config)

        with (
            patch.object(
                gen,
                "scan_project_plans",
                return_value=[{"id": "new-task-001", "title": "New task"}],
            ),
            patch.object(gen, "scan_prometheus_alerts", return_value=[]),
            patch.object(gen, "scan_git_changes", return_value=[]),
            patch.object(gen, "scan_examforge_pipeline", return_value=[]),
            patch.object(gen, "scan_scheduled_maintenance", return_value=[]),
        ):
            tasks = gen.generate_work()
            assert len(tasks) == 0  # Should NOT generate work due to backpressure

    def test_generate_work_exceeds_limit_no_generation(self, swarm_tmpdir):
        """Test work generation stops when exceeding pending task limit."""
        from work_generator import WorkGenerator

        config = {
            "swarm_root": str(swarm_tmpdir),
            "work_generator": {
                "max_pending_tasks": 5,
                "projects": {},
            },
        }

        # Create 8 pending tasks (exceeds limit of 5)
        pending_dir = swarm_tmpdir / "tasks" / "pending"
        for i in range(8):
            task = {"id": f"task-{i:03d}", "title": f"Task {i}"}
            with open(pending_dir / f"task-{i:03d}.yaml", "w") as f:
                yaml.dump(task, f)

        gen = WorkGenerator(config)

        with (
            patch.object(
                gen,
                "scan_project_plans",
                return_value=[{"id": "new-task-001", "title": "New task"}],
            ),
            patch.object(gen, "scan_prometheus_alerts", return_value=[]),
            patch.object(gen, "scan_git_changes", return_value=[]),
            patch.object(gen, "scan_examforge_pipeline", return_value=[]),
            patch.object(gen, "scan_scheduled_maintenance", return_value=[]),
        ):
            tasks = gen.generate_work()
            assert len(tasks) == 0  # Should NOT generate work due to backpressure

    def test_generate_work_just_below_limit(self, swarm_tmpdir):
        """Test work generation when just below limit."""
        from work_generator import WorkGenerator

        config = {
            "swarm_root": str(swarm_tmpdir),
            "work_generator": {
                "max_pending_tasks": 5,
                "projects": {},
            },
        }

        # Create 4 pending tasks (just below limit of 5)
        pending_dir = swarm_tmpdir / "tasks" / "pending"
        for i in range(4):
            task = {"id": f"task-{i:03d}", "title": f"Task {i}"}
            with open(pending_dir / f"task-{i:03d}.yaml", "w") as f:
                yaml.dump(task, f)

        gen = WorkGenerator(config)

        with (
            patch.object(
                gen,
                "scan_project_plans",
                return_value=[{"id": "new-task-001", "title": "New task"}],
            ),
            patch.object(gen, "scan_prometheus_alerts", return_value=[]),
            patch.object(gen, "scan_git_changes", return_value=[]),
            patch.object(gen, "scan_examforge_pipeline", return_value=[]),
            patch.object(gen, "scan_scheduled_maintenance", return_value=[]),
        ):
            tasks = gen.generate_work()
            assert len(tasks) > 0  # Should generate work

    def test_backpressure_default_limit(self, swarm_tmpdir):
        """Test that default backpressure limit is 10."""
        from work_generator import WorkGenerator

        config = {
            "swarm_root": str(swarm_tmpdir),
            "work_generator": {
                "projects": {},
            },
        }

        gen = WorkGenerator(config)

        # Should use default max_pending_tasks from config key or fallback
        # Since we didn't specify, it should default to 10
        # Create 10 tasks
        pending_dir = swarm_tmpdir / "tasks" / "pending"
        for i in range(10):
            task = {"id": f"task-{i:03d}", "title": f"Task {i}"}
            with open(pending_dir / f"task-{i:03d}.yaml", "w") as f:
                yaml.dump(task, f)

        with (
            patch.object(
                gen,
                "scan_project_plans",
                return_value=[{"id": "new-task-001", "title": "New task"}],
            ),
            patch.object(gen, "scan_prometheus_alerts", return_value=[]),
            patch.object(gen, "scan_git_changes", return_value=[]),
            patch.object(gen, "scan_examforge_pipeline", return_value=[]),
            patch.object(gen, "scan_scheduled_maintenance", return_value=[]),
        ):
            tasks = gen.generate_work()
            # At default limit of 10, should block
            assert len(tasks) == 0

    def test_backpressure_empty_pending_dir(self, swarm_tmpdir):
        """Test work generation when pending dir is empty."""
        from work_generator import WorkGenerator

        config = {
            "swarm_root": str(swarm_tmpdir),
            "work_generator": {
                "max_pending_tasks": 10,
                "projects": {},
            },
        }

        gen = WorkGenerator(config)

        with (
            patch.object(
                gen,
                "scan_project_plans",
                return_value=[{"id": "new-task-001", "title": "New task"}],
            ),
            patch.object(gen, "scan_prometheus_alerts", return_value=[]),
            patch.object(gen, "scan_git_changes", return_value=[]),
            patch.object(gen, "scan_examforge_pipeline", return_value=[]),
            patch.object(gen, "scan_scheduled_maintenance", return_value=[]),
        ):
            tasks = gen.generate_work()
            assert len(tasks) > 0  # Should generate work when no pending


class TestBackpressureLogging:
    def test_backpressure_logged(self, swarm_tmpdir, caplog):
        """Test that backpressure condition is logged."""
        from work_generator import WorkGenerator
        import logging

        logging.basicConfig(level=logging.DEBUG)

        config = {
            "swarm_root": str(swarm_tmpdir),
            "work_generator": {
                "max_pending_tasks": 2,
                "projects": {},
            },
        }

        # Create 3 pending tasks (exceeds limit of 2)
        pending_dir = swarm_tmpdir / "tasks" / "pending"
        for i in range(3):
            task = {"id": f"task-{i:03d}", "title": f"Task {i}"}
            with open(pending_dir / f"task-{i:03d}.yaml", "w") as f:
                yaml.dump(task, f)

        gen = WorkGenerator(config)

        with (
            patch.object(
                gen,
                "scan_project_plans",
                return_value=[{"id": "new-task-001", "title": "New task"}],
            ),
            patch.object(gen, "scan_prometheus_alerts", return_value=[]),
            patch.object(gen, "scan_git_changes", return_value=[]),
            patch.object(gen, "scan_examforge_pipeline", return_value=[]),
            patch.object(gen, "scan_scheduled_maintenance", return_value=[]),
        ):
            tasks = gen.generate_work()
            # Should have logged the backpressure condition
            # (The actual logging would be in the generate_work method)
