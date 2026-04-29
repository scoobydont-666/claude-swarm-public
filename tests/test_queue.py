"""Tests for the unified TaskQueue with capability matching and lifecycle."""

import os
import shutil
import tempfile

import pytest

from task_queue import Task, TaskQueue, _normalize_priority

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tasks_dir():
    d = tempfile.mkdtemp(prefix="swarm-queue-test-")
    for sub in ("pending", "claimed", "completed", "failed", "running"):
        os.makedirs(os.path.join(d, sub))
    yield d
    shutil.rmtree(d)


@pytest.fixture
def q(tasks_dir):
    """TaskQueue using filesystem backend (no Redis)."""
    return TaskQueue(use_redis=False, tasks_dir=tasks_dir)


# ---------------------------------------------------------------------------
# Task dataclass
# ---------------------------------------------------------------------------


class TestTask:
    def test_from_dict(self):
        t = Task.from_dict({"id": "t1", "title": "Test", "priority": "high", "requires": ["gpu"]})
        assert t.id == "t1"
        assert t.priority == 1  # "high" maps to tier 1 (cicd)
        assert t.requires == ["gpu"]

    def test_from_dict_string_requires(self):
        t = Task.from_dict({"id": "t2", "title": "Test", "requires": "gpu,docker"})
        assert t.requires == ["gpu", "docker"]

    def test_to_dict_roundtrip(self):
        t = Task(id="t3", title="Round", priority=3, requires=["cpu"])
        d = t.to_dict()
        t2 = Task.from_dict(d)
        assert t2.id == t.id
        assert t2.requires == t.requires

    def test_default_state(self):
        t = Task(id="t4", title="Default")
        assert t.state == "pending"


class TestPriorityNormalization:
    def test_string_high(self):
        assert _normalize_priority("high") == 1  # tier 1

    def test_string_low(self):
        assert _normalize_priority("low") == 4  # tier 4

    def test_string_medium(self):
        assert _normalize_priority("medium") == 3  # tier 3

    def test_string_critical(self):
        assert _normalize_priority("critical") == 0  # tier 0

    def test_int_clamped(self):
        assert _normalize_priority(0) == 0  # tier 0 is valid
        assert _normalize_priority(15) == 5  # clamp to max tier

    def test_unknown_string(self):
        assert _normalize_priority("unknown") == 3  # defaults to standard


# ---------------------------------------------------------------------------
# TaskQueue — Create + Query
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_returns_task(self, q):
        t = q.create("Build feature X", project="/opt/test")
        assert t.id.startswith("task-")
        assert t.title == "Build feature X"
        assert t.state == "pending"
        assert t.created_at > 0

    def test_create_with_priority(self, q):
        t = q.create("Urgent", priority="critical")
        assert t.priority == 0  # critical = tier 0

    def test_create_with_requires(self, q):
        t = q.create("GPU work", requires=["gpu", "ollama"])
        assert t.requires == ["gpu", "ollama"]

    def test_created_task_appears_in_pending(self, q):
        q.create("Test task")
        pending = q.list_pending()
        assert len(pending) == 1

    def test_multiple_tasks(self, q):
        q.create("A")
        q.create("B")
        q.create("C")
        assert len(q.list_pending()) == 3


# ---------------------------------------------------------------------------
# TaskQueue — Claim
# ---------------------------------------------------------------------------


class TestClaim:
    def test_claim_returns_task(self, q):
        q.create("Claimable")
        t = q.claim("agent-1")
        assert t is not None
        assert t.state == "claimed"
        assert t.claimed_by == "agent-1"

    def test_claim_removes_from_pending(self, q):
        q.create("Only one")
        q.claim("agent-1")
        assert len(q.list_pending()) == 0

    def test_claim_empty_queue(self, q):
        t = q.claim("agent-1")
        assert t is None

    def test_claim_specific(self, q):
        t1 = q.create("First")
        q.create("Second")
        claimed = q.claim("agent-1", task_id=t1.id)
        assert claimed.id == t1.id

    def test_claim_highest_priority(self, q):
        q.create("Low", priority="low")
        q.create("High", priority="high")
        q.create("Medium", priority="medium")
        t = q.claim("agent-1")
        assert t.title == "High"

    def test_double_claim_fails(self, q):
        t = q.create("Once")
        q.claim("agent-1", task_id=t.id)
        second = q.claim("agent-2", task_id=t.id)
        assert second is None


# ---------------------------------------------------------------------------
# TaskQueue — Capability Matching
# ---------------------------------------------------------------------------


class TestCapabilityMatching:
    def test_match_by_dict(self, q):
        q.create("GPU task", requires=["gpu"])
        q.create("CPU task", requires=["cpu"])
        t = q.claim_matching({"gpu": True, "docker": True}, "agent-1")
        assert t is not None
        assert t.title == "GPU task"

    def test_match_by_list(self, q):
        q.create("GPU task", requires=["gpu", "ollama"])
        t = q.claim_matching(["gpu", "ollama", "docker"], "agent-1")
        assert t is not None

    def test_no_match(self, q):
        q.create("GPU task", requires=["gpu"])
        t = q.claim_matching(["cpu"], "agent-1")
        assert t is None

    def test_no_requires_matches_all(self, q):
        q.create("Any task")
        t = q.claim_matching(["cpu"], "agent-1")
        assert t is not None

    def test_priority_ordering_in_match(self, q):
        q.create("Low GPU", priority="low", requires=["gpu"])
        q.create("High GPU", priority="high", requires=["gpu"])
        t = q.claim_matching(["gpu"], "agent-1")
        assert t.title == "High GPU"

    def test_list_matching_without_claim(self, q):
        q.create("GPU task", requires=["gpu"])
        q.create("CPU task", requires=["cpu"])
        q.create("Any task")
        matching = q.list_matching(["gpu"])
        assert len(matching) == 2  # GPU task + Any task


# ---------------------------------------------------------------------------
# TaskQueue — Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_claim_to_running(self, q):
        t = q.create("Runnable")
        q.claim("agent-1", task_id=t.id)
        running = q.start(t.id)
        assert running.state == "running"

    def test_complete(self, q):
        t = q.create("Completable")
        q.claim("agent-1", task_id=t.id)
        done = q.complete(t.id, result="success")
        assert done.state == "completed"
        assert done.result == "success"

    def test_fail(self, q):
        t = q.create("Failable")
        q.claim("agent-1", task_id=t.id)
        failed = q.fail(t.id, error="crashed")
        assert failed.state == "failed"
        assert failed.error == "crashed"

    def test_requeue(self, q):
        t = q.create("Requeueable")
        q.claim("agent-1", task_id=t.id)
        requeued = q.requeue(t.id)
        assert requeued.state == "pending"
        assert requeued.claimed_by == ""

    def test_requeue_failed(self, q):
        t = q.create("Retry")
        q.claim("agent-1", task_id=t.id)
        q.fail(t.id, error="oops")
        requeued = q.requeue(t.id)
        assert requeued.state == "pending"

    def test_cannot_complete_pending(self, q):
        t = q.create("Still pending")
        result = q.complete(t.id)
        assert result is None

    def test_cannot_requeue_completed(self, q):
        t = q.create("Done")
        q.claim("agent-1", task_id=t.id)
        q.complete(t.id)
        result = q.requeue(t.id)
        assert result is None


# ---------------------------------------------------------------------------
# TaskQueue — Auto-Requeue Stale
# ---------------------------------------------------------------------------


class TestStaleRequeue:
    def test_requeue_stale_claims(self, q):
        import time

        t = q.create("Will go stale")
        q.claim("agent-1", task_id=t.id)
        # Manually backdate claim time
        task = q.get(t.id)
        task.claimed_at = time.time() - 1000
        q._save(task)

        requeued = q.requeue_stale(ttl=60)
        assert t.id in requeued
        assert q.get(t.id).state == "pending"

    def test_fresh_claims_not_requeued(self, q):
        t = q.create("Fresh claim")
        q.claim("agent-1", task_id=t.id)
        requeued = q.requeue_stale(ttl=600)
        assert len(requeued) == 0


# ---------------------------------------------------------------------------
# TaskQueue — Backend property
# ---------------------------------------------------------------------------


class TestBackend:
    def test_filesystem_backend(self, q):
        assert q.backend == "filesystem"
