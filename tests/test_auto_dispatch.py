"""Tests for auto_dispatch — approval gating, host matching, concurrent limits, model routing."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import swarm_lib as lib
from auto_dispatch import AutoDispatcher, _tier_of
from model_router import get_model_for_task
from work_generator import infer_model  # noqa: F401  (legacy; used by other tests)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(
    swarm_root: Path,
    enabled: bool = False,
    require_approval_for: list | None = None,
    max_concurrent: int = 3,
) -> dict:
    return {
        "swarm_root": str(swarm_root),
        "work_generator": {
            "enabled": True,
            "prometheus_url": "http://127.0.0.1:9090",
            "projects": {},
        },
        "auto_dispatch": {
            "enabled": enabled,
            "require_approval_for": require_approval_for
            if require_approval_for is not None
            else ["opus"],
            "max_concurrent_dispatches": max_concurrent,
        },
        "scheduled_maintenance": {"daily_hour": 0, "weekly_day": 0},
    }


# ---------------------------------------------------------------------------
# Approval gating
# ---------------------------------------------------------------------------


class TestApprovalGating:
    def test_opus_task_not_dispatched(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True, require_approval_for=["opus"])
        dispatcher = AutoDispatcher(config)

        # Create a pending opus-level task
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(
                title="Design the entire system architecture",
                description="Architectural design task",
                priority="high",
                requires=[],
            )

        # model_router "architecture" rule matches → opus tier (claude-opus-4-7).
        assert _tier_of(get_model_for_task("Design the entire system architecture")) == "opus"

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            dispatched = dispatcher.process_pending_tasks()

        assert dispatched == []

    def test_sonnet_task_dispatched(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True, require_approval_for=["opus"])
        dispatcher = AutoDispatcher(config)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(
                title="Implement logging middleware",
                description="Build it",
                priority="medium",
                requires=[],
            )

        # model_router "code_gen" rule matches implement/build → sonnet tier.
        assert _tier_of(get_model_for_task("Implement logging middleware")) == "sonnet"

        fake_dispatch_result = MagicMock()
        fake_dispatch_result.dispatch_id = "dispatch-999-testhost"

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch("auto_dispatch._find_best_host", return_value="node_primary"),
            patch("auto_dispatch.dispatch", return_value=fake_dispatch_result),
        ):
            dispatched = dispatcher.process_pending_tasks()

        assert len(dispatched) == 1
        # Dispatcher stores full model ID; sonnet tier → claude-sonnet-4-6.
        assert _tier_of(dispatched[0]["model"]) == "sonnet"

    def test_haiku_task_dispatched(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True, require_approval_for=["opus"])
        dispatcher = AutoDispatcher(config)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(
                title="Find all README files",
                description="Search repo",
                priority="medium",
                requires=[],
            )

        # model_router "search" rule matches find/grep/locate → haiku tier.
        assert _tier_of(get_model_for_task("Find all README files")) == "haiku"

        fake_result = MagicMock()
        fake_result.dispatch_id = "dispatch-123-testhost"

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch("auto_dispatch._find_best_host", return_value="node_primary"),
            patch("auto_dispatch.dispatch", return_value=fake_result),
        ):
            dispatched = dispatcher.process_pending_tasks()

        assert len(dispatched) == 1
        # Dispatcher stores the full model ID from the router.
        assert _tier_of(dispatched[0]["model"]) == "haiku"

    def test_haiku_only_mode_blocks_sonnet(self, swarm_tmpdir):
        """In haiku_only mode, sonnet tasks are not dispatched."""
        config = _make_config(swarm_tmpdir, enabled=True)
        # Override to haiku_only mode
        config["auto_dispatch"]["mode"] = "haiku_only"
        dispatcher = AutoDispatcher(config)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(title="Implement OAuth2", requires=[])

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch("auto_dispatch._find_best_host", return_value="node_primary"),
            patch("auto_dispatch.dispatch") as mock_dispatch,
        ):
            dispatched = dispatcher.process_pending_tasks()

        assert dispatched == []
        mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Auto-dispatch disabled guard
# ---------------------------------------------------------------------------


class TestDispatchDisabledGuard:
    def test_disabled_returns_empty(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=False)
        dispatcher = AutoDispatcher(config)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(title="Run tests on stuff", requires=[])

        with patch("auto_dispatch.dispatch") as mock_dispatch:
            result = dispatcher.process_pending_tasks()

        assert result == []
        mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Concurrent dispatch limits
# ---------------------------------------------------------------------------


class TestConcurrentDispatchLimits:
    def test_respects_max_concurrent(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True, max_concurrent=2)
        dispatcher = AutoDispatcher(config)

        # Pre-populate claimed tasks to hit the limit
        claimed_dir = swarm_tmpdir / "tasks" / "claimed"
        for i in range(2):
            task_data = {"id": f"task-{i:03d}", "title": f"Existing claimed {i}"}
            with open(claimed_dir / f"task-{i:03d}.yaml", "w") as f:
                yaml.dump(task_data, f)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(title="Run tests on new project", requires=[])

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch("auto_dispatch.dispatch") as mock_dispatch,
        ):
            dispatched = dispatcher.process_pending_tasks()

        # Already at max_concurrent=2 (2 claimed), no new dispatches
        assert dispatched == []
        mock_dispatch.assert_not_called()

    def test_dispatches_up_to_limit(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True, max_concurrent=2)
        dispatcher = AutoDispatcher(config)

        # 1 already claimed, limit=2, so 1 more can go
        claimed_dir = swarm_tmpdir / "tasks" / "claimed"
        with open(claimed_dir / "task-000.yaml", "w") as f:
            yaml.dump({"id": "task-000", "title": "Pre-existing"}, f)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(title="Run tests on project-a", requires=[])
            lib.create_task(title="Run tests on project-b", requires=[])

        fake_result = MagicMock()
        fake_result.dispatch_id = "dispatch-test"

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch("auto_dispatch._find_best_host", return_value="node_primary"),
            patch("auto_dispatch.dispatch", return_value=fake_result),
        ):
            dispatched = dispatcher.process_pending_tasks()

        # Should dispatch exactly 1 (fills up to limit of 2)
        assert len(dispatched) == 1


# ---------------------------------------------------------------------------
# Host matching
# ---------------------------------------------------------------------------


class TestHostMatching:
    def test_no_matching_host_skips_task(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True)
        dispatcher = AutoDispatcher(config)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(title="Run tests on thing", requires=["exotic-hardware"])

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch("auto_dispatch._find_best_host", return_value=None),
            patch("auto_dispatch.dispatch") as mock_dispatch,
        ):
            dispatched = dispatcher.process_pending_tasks()

        assert dispatched == []
        mock_dispatch.assert_not_called()

    def test_best_host_used(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True)
        dispatcher = AutoDispatcher(config)

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.create_task(title="Run checks on gpu node", requires=["gpu"])

        fake_result = MagicMock()
        fake_result.dispatch_id = "dispatch-gpu"

        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_hostname", return_value="testhost"),
            patch("auto_dispatch._find_best_host", return_value="node_gpu") as mock_host,
            patch("auto_dispatch.dispatch", return_value=fake_result),
        ):
            dispatched = dispatcher.process_pending_tasks()

        mock_host.assert_called_once_with(["gpu"])
        assert dispatched[0]["host"] == "node_gpu"


# ---------------------------------------------------------------------------
# Model routing via suggested_model field
# ---------------------------------------------------------------------------


class TestModelRouting:
    def test_suggested_model_overrides_inference(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True)
        dispatcher = AutoDispatcher(config)

        # Manually write a pending task with suggested_model=haiku
        task_data = {
            "id": "task-001",
            "title": "Design the world",  # would normally be opus
            "description": "But we override it",
            "project": "",
            "priority": "medium",
            "requires": [],
            "suggested_model": "haiku",
        }
        with open(swarm_tmpdir / "tasks" / "pending" / "task-001.yaml", "w") as f:
            yaml.dump(task_data, f)

        # "Design" → opus, but suggested_model overrides to haiku
        model = dispatcher._infer_model(task_data)
        assert model == "haiku"

    def test_missing_suggested_model_falls_back(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True)
        dispatcher = AutoDispatcher(config)

        task_data = {"title": "Build the authentication module", "description": ""}
        model = dispatcher._infer_model(task_data)
        # Full model ID from router; tier is sonnet ("build" → code_gen rule).
        assert _tier_of(model) == "sonnet"


# ---------------------------------------------------------------------------
# Enable / disable config persistence
# ---------------------------------------------------------------------------


class TestEnableDisable:
    def test_set_enabled_writes_config(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=False)
        dispatcher = AutoDispatcher(config)

        cfg_path = swarm_tmpdir / "config" / "swarm.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(config, f)

        dispatcher.set_enabled(True, cfg_path)

        with open(cfg_path) as f:
            updated = yaml.safe_load(f)

        assert updated["auto_dispatch"]["enabled"] is True
        assert dispatcher.auto_dispatch_enabled is True

    def test_set_disabled_writes_config(self, swarm_tmpdir):
        config = _make_config(swarm_tmpdir, enabled=True)
        dispatcher = AutoDispatcher(config)

        cfg_path = swarm_tmpdir / "config" / "swarm.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(config, f)

        dispatcher.set_enabled(False, cfg_path)

        with open(cfg_path) as f:
            updated = yaml.safe_load(f)

        assert updated["auto_dispatch"]["enabled"] is False
        assert dispatcher.auto_dispatch_enabled is False
