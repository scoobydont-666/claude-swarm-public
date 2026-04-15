"""Tests for task priority re-ranking and preemption."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def tmp_swarm_root(tmp_path):
    """Provide temporary swarm root with task directories."""
    swarm_root = tmp_path / "swarm"
    (swarm_root / "tasks" / "pending").mkdir(parents=True)
    (swarm_root / "tasks" / "claimed").mkdir(parents=True)
    (swarm_root / "tasks" / "completed").mkdir(parents=True)
    (swarm_root / "tasks" / "preempted").mkdir(parents=True)
    (swarm_root / "messages" / "inbox").mkdir(parents=True)
    with patch("swarm_lib._swarm_root", return_value=swarm_root):
        yield swarm_root


@pytest.fixture
def config_with_auto_dispatch():
    return {
        "auto_dispatch": {
            "enabled": True,
            "require_approval_for": ["opus"],
            "max_concurrent_dispatches": 5,
        },
        "swarm_root": "/var/lib/swarm",
    }


class TestPriorityValue:
    def test_priority_value_conversion(self, config_with_auto_dispatch):
        from auto_dispatch import AutoDispatcher

        dispatcher = AutoDispatcher(config_with_auto_dispatch)

        assert dispatcher._priority_value("P0") == 0
        assert dispatcher._priority_value("P1") == 1
        assert dispatcher._priority_value("P5") == 5
        assert dispatcher._priority_value("invalid") == 5  # defaults to P5

    def test_priority_comparison(self, config_with_auto_dispatch):
        from auto_dispatch import AutoDispatcher

        dispatcher = AutoDispatcher(config_with_auto_dispatch)

        # P0 should have lower numeric value than P5
        assert dispatcher._priority_value("P0") < dispatcher._priority_value("P5")
        assert dispatcher._priority_value("P2") < dispatcher._priority_value("P4")


class TestRerankTasks:
    def test_rerank_empty_pending(self, tmp_swarm_root, config_with_auto_dispatch):
        from auto_dispatch import AutoDispatcher

        dispatcher = AutoDispatcher(config_with_auto_dispatch)

        pending = dispatcher.rerank_tasks()
        assert pending == []

    def test_rerank_maintains_order(self, tmp_swarm_root, config_with_auto_dispatch):
        import yaml
        from auto_dispatch import AutoDispatcher

        # Create tasks with different priorities
        for priority in ["P3", "P0", "P5", "P1"]:
            task = {
                "id": f"task-{priority}",
                "title": f"Task {priority}",
                "priority": priority,
            }
            task_dir = tmp_swarm_root / "tasks" / "pending"
            with open(task_dir / f"task-{priority}.yaml", "w") as f:
                yaml.dump(task, f)

        dispatcher = AutoDispatcher(config_with_auto_dispatch)
        pending = dispatcher.rerank_tasks()

        # Should be ordered P0, P1, P3, P5
        priorities = [t.get("priority") for t in pending]
        assert priorities == ["P0", "P1", "P3", "P5"]

    def test_rerank_with_default_priority(
        self, tmp_swarm_root, config_with_auto_dispatch
    ):
        import yaml
        from auto_dispatch import AutoDispatcher

        # Create tasks, some without explicit priority
        for i in range(3):
            task = {
                "id": f"task-{i}",
                "title": f"Task {i}",
                "priority": "P2" if i == 0 else "",
            }
            task_dir = tmp_swarm_root / "tasks" / "pending"
            with open(task_dir / f"task-{i}.yaml", "w") as f:
                yaml.dump(task, f)

        dispatcher = AutoDispatcher(config_with_auto_dispatch)
        pending = dispatcher.rerank_tasks()
        assert len(pending) == 3


class TestInterruptForPriority:
    def test_p0_preempts_p5_task(self, tmp_swarm_root, config_with_auto_dispatch):
        import yaml
        from auto_dispatch import AutoDispatcher

        # Create a P5 claimed task
        claimed_task = {
            "id": "task-p5",
            "title": "Low priority",
            "priority": "P5",
            "claimed_by": "host1",
        }
        claimed_dir = tmp_swarm_root / "tasks" / "claimed"
        with open(claimed_dir / "task-p5.yaml", "w") as f:
            yaml.dump(claimed_task, f)

        # Create a P0 pending task
        p0_task = {
            "id": "task-p0",
            "title": "Critical",
            "priority": "P0",
        }
        pending_dir = tmp_swarm_root / "tasks" / "pending"
        with open(pending_dir / "task-p0.yaml", "w") as f:
            yaml.dump(p0_task, f)

        # Need to also mock swarm_root in the dispatcher
        config_with_auto_dispatch["swarm_root"] = str(tmp_swarm_root)
        dispatcher = AutoDispatcher(config_with_auto_dispatch)
        result = dispatcher.interrupt_for_priority("task-p0")

        assert result is True
        # Verify P5 task was moved to preempted
        preempted_dir = tmp_swarm_root / "tasks" / "preempted"
        assert (preempted_dir / "task-p5.yaml").exists()
        assert not (claimed_dir / "task-p5.yaml").exists()

    def test_p1_does_preempt_p5(self, tmp_swarm_root, config_with_auto_dispatch):
        import yaml
        from auto_dispatch import AutoDispatcher

        # Create a P5 claimed task
        claimed_task = {
            "id": "task-p5",
            "title": "Low priority",
            "priority": "P5",
            "claimed_by": "host1",
        }
        claimed_dir = tmp_swarm_root / "tasks" / "claimed"
        with open(claimed_dir / "task-p5.yaml", "w") as f:
            yaml.dump(claimed_task, f)

        # Create a P1 pending task
        p1_task = {
            "id": "task-p1",
            "title": "Medium-high priority",
            "priority": "P1",
        }
        pending_dir = tmp_swarm_root / "tasks" / "pending"
        with open(pending_dir / "task-p1.yaml", "w") as f:
            yaml.dump(p1_task, f)

        config_with_auto_dispatch["swarm_root"] = str(tmp_swarm_root)
        dispatcher = AutoDispatcher(config_with_auto_dispatch)
        result = dispatcher.interrupt_for_priority("task-p1")

        # P1 (value 1) and P5 (value 5) have difference of 4, which is >= 2, so preemption happens
        assert result is True
        # Verify P5 task was moved to preempted
        preempted_dir = tmp_swarm_root / "tasks" / "preempted"
        assert (preempted_dir / "task-p5.yaml").exists()
        assert not (claimed_dir / "task-p5.yaml").exists()

    def test_p0_preempts_p3_and_below(self, tmp_swarm_root, config_with_auto_dispatch):
        import yaml
        from auto_dispatch import AutoDispatcher

        # Create claimed tasks at different priorities
        for priority in ["P3", "P4", "P5"]:
            task = {
                "id": f"task-{priority}",
                "title": f"Task {priority}",
                "priority": priority,
                "claimed_by": "host1",
            }
            claimed_dir = tmp_swarm_root / "tasks" / "claimed"
            with open(claimed_dir / f"task-{priority}.yaml", "w") as f:
                yaml.dump(task, f)

        # Create P0 task
        p0_task = {
            "id": "task-p0",
            "title": "Critical",
            "priority": "P0",
        }
        pending_dir = tmp_swarm_root / "tasks" / "pending"
        with open(pending_dir / "task-p0.yaml", "w") as f:
            yaml.dump(p0_task, f)

        config_with_auto_dispatch["swarm_root"] = str(tmp_swarm_root)
        dispatcher = AutoDispatcher(config_with_auto_dispatch)
        result = dispatcher.interrupt_for_priority("task-p0")

        assert result is True
        preempted_dir = tmp_swarm_root / "tasks" / "preempted"
        # P0 should preempt P2 and below (P2, P3, P4, P5)
        # P3 has difference of 3 (preempted)
        # P4 has difference of 4 (preempted)
        # P5 has difference of 5 (preempted)
        for priority in ["P3", "P4", "P5"]:
            assert (preempted_dir / f"task-{priority}.yaml").exists()

    def test_p2_does_not_preempt_p3(self, tmp_swarm_root, config_with_auto_dispatch):
        import yaml
        from auto_dispatch import AutoDispatcher

        # Create P3 claimed task
        claimed_task = {
            "id": "task-p3",
            "title": "Task P3",
            "priority": "P3",
            "claimed_by": "host1",
        }
        claimed_dir = tmp_swarm_root / "tasks" / "claimed"
        with open(claimed_dir / "task-p3.yaml", "w") as f:
            yaml.dump(claimed_task, f)

        # Create P2 task
        p2_task = {
            "id": "task-p2",
            "title": "Task P2",
            "priority": "P2",
        }
        pending_dir = tmp_swarm_root / "tasks" / "pending"
        with open(pending_dir / "task-p2.yaml", "w") as f:
            yaml.dump(p2_task, f)

        config_with_auto_dispatch["swarm_root"] = str(tmp_swarm_root)
        dispatcher = AutoDispatcher(config_with_auto_dispatch)
        result = dispatcher.interrupt_for_priority("task-p2")

        # P2 and P3 are only 1 level apart, need 2+ to preempt
        assert result is False


class TestPreemptionMessaging:
    def test_preempt_task_sends_message(
        self, tmp_swarm_root, config_with_auto_dispatch
    ):
        import yaml
        from auto_dispatch import AutoDispatcher

        # Create a claimed task
        claimed_task = {
            "id": "task-001",
            "title": "Task",
            "claimed_by": "target-host",
        }
        claimed_dir = tmp_swarm_root / "tasks" / "claimed"
        with open(claimed_dir / "task-001.yaml", "w") as f:
            yaml.dump(claimed_task, f)

        config_with_auto_dispatch["swarm_root"] = str(tmp_swarm_root)
        dispatcher = AutoDispatcher(config_with_auto_dispatch)
        dispatcher._preempt_task("task-001", "target-host")

        # Verify message was sent
        inbox_dir = tmp_swarm_root / "messages" / "inbox" / "target-host"
        assert inbox_dir.exists()
        messages = list(inbox_dir.glob("*.yaml"))
        assert len(messages) > 0

    def test_preempt_task_moves_to_preempted(
        self, tmp_swarm_root, config_with_auto_dispatch
    ):
        import yaml
        from auto_dispatch import AutoDispatcher

        # Create a claimed task
        claimed_task = {
            "id": "task-001",
            "title": "Task",
            "claimed_by": "host1",
        }
        claimed_dir = tmp_swarm_root / "tasks" / "claimed"
        with open(claimed_dir / "task-001.yaml", "w") as f:
            yaml.dump(claimed_task, f)

        config_with_auto_dispatch["swarm_root"] = str(tmp_swarm_root)
        dispatcher = AutoDispatcher(config_with_auto_dispatch)
        dispatcher._preempt_task("task-001", "host1")

        preempted_dir = tmp_swarm_root / "tasks" / "preempted"
        assert (preempted_dir / "task-001.yaml").exists()
        assert not (claimed_dir / "task-001.yaml").exists()


class TestProcessPendingTasksWithPriority:
    def test_process_respects_priority(self, tmp_swarm_root, config_with_auto_dispatch):
        import yaml
        from auto_dispatch import AutoDispatcher

        # Create tasks with different priorities
        for i, priority in enumerate(["P5", "P1", "P3"]):
            task = {
                "id": f"task-{i}",
                "title": f"Task {priority}",
                "priority": priority,
                "requires": [],
            }
            pending_dir = tmp_swarm_root / "tasks" / "pending"
            with open(pending_dir / f"task-{i}.yaml", "w") as f:
                yaml.dump(task, f)

        dispatcher = AutoDispatcher(config_with_auto_dispatch)
        pending = dispatcher.rerank_tasks()

        # P1 should be before P3 and P5
        p1_idx = next(i for i, t in enumerate(pending) if t.get("priority") == "P1")
        p3_idx = next(i for i, t in enumerate(pending) if t.get("priority") == "P3")
        p5_idx = next(i for i, t in enumerate(pending) if t.get("priority") == "P5")

        assert p1_idx < p3_idx < p5_idx
