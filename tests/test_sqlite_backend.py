"""Tests for SQLite task backend — NAI Swarm backport."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from sqlite_backend import SQLiteTaskBackend


@pytest.fixture
def db(tmp_path):
    """Create a fresh SQLite backend for each test."""
    db_path = str(tmp_path / "test_tasks.db")
    return SQLiteTaskBackend(db_path)


class TestCreate:
    def test_create_task(self, db):
        task = db.create("Fix bug", description="Auth issue", priority=1)
        assert task["title"] == "Fix bug"
        assert task["priority"] == 1
        assert task["state"] == "pending"
        assert task["id"].startswith("task-")

    def test_create_with_requires(self, db):
        task = db.create("GPU work", requires=["gpu", "ollama"])
        assert task["requires"] == ["gpu", "ollama"]

    def test_create_default_priority(self, db):
        task = db.create("Standard work")
        assert task["priority"] == 3

    def test_create_sets_created_at(self, db):
        before = time.time()
        task = db.create("Timed task")
        assert task["created_at"] >= before


class TestClaim:
    def test_claim_next_available(self, db):
        db.create("Task 1", priority=3)
        db.create("Task 2", priority=1)
        claimed = db.claim("agent-1")
        assert claimed is not None
        assert claimed["priority"] == 1  # Higher priority claimed first
        assert claimed["state"] == "claimed"
        assert claimed["claimed_by"] == "agent-1"

    def test_claim_specific(self, db):
        db.create("Task 1")
        t2 = db.create("Task 2")
        claimed = db.claim("agent-1", task_id=t2["id"])
        assert claimed["id"] == t2["id"]

    def test_claim_empty_queue(self, db):
        assert db.claim("agent-1") is None

    def test_double_claim_prevented(self, db):
        t = db.create("Unique task")
        c1 = db.claim("agent-1", t["id"])
        c2 = db.claim("agent-2", t["id"])
        assert c1 is not None
        assert c2 is None  # Already claimed

    def test_claim_respects_priority_order(self, db):
        db.create("Low", priority=5)
        db.create("High", priority=0)
        db.create("Medium", priority=3)
        c1 = db.claim("a1")
        c2 = db.claim("a2")
        c3 = db.claim("a3")
        assert c1["priority"] == 0
        assert c2["priority"] == 3
        assert c3["priority"] == 5

    def test_claim_fifo_within_priority(self, db):
        t1 = db.create("First", priority=3)
        time.sleep(0.01)
        t2 = db.create("Second", priority=3)
        c1 = db.claim("a1")
        c2 = db.claim("a2")
        assert c1["id"] == t1["id"]
        assert c2["id"] == t2["id"]


class TestClaimMatching:
    def test_claim_matching_capabilities(self, db):
        db.create("CPU work", requires=["cpu"])
        db.create("GPU work", requires=["gpu"])
        claimed = db.claim_matching(["cpu"], "agent-1")
        assert claimed is not None
        assert claimed["title"] == "CPU work"

    def test_claim_matching_no_match(self, db):
        db.create("GPU work", requires=["gpu"])
        claimed = db.claim_matching(["cpu"], "agent-1")
        assert claimed is None

    def test_claim_matching_empty_requires(self, db):
        db.create("Any work", requires=[])
        claimed = db.claim_matching(["cpu"], "agent-1")
        assert claimed is not None

    def test_claim_matching_priority_order(self, db):
        db.create("Low GPU", requires=["gpu"], priority=5)
        db.create("High GPU", requires=["gpu"], priority=0)
        claimed = db.claim_matching(["gpu", "cpu"], "agent-1")
        assert claimed["priority"] == 0


class TestStateTransitions:
    def test_complete(self, db):
        t = db.create("Work")
        db.claim("a1", t["id"])
        result = db.complete(t["id"], result="Done!")
        assert result["state"] == "completed"
        assert result["result"] == "Done!"
        assert result["completed_at"] > 0

    def test_fail(self, db):
        t = db.create("Work")
        db.claim("a1", t["id"])
        result = db.fail(t["id"], error="Crashed")
        assert result["state"] == "failed"
        assert result["error"] == "Crashed"

    def test_start(self, db):
        t = db.create("Work")
        db.claim("a1", t["id"])
        result = db.start(t["id"])
        assert result["state"] == "running"

    def test_complete_from_running(self, db):
        t = db.create("Work")
        db.claim("a1", t["id"])
        db.start(t["id"])
        result = db.complete(t["id"])
        assert result["state"] == "completed"

    def test_cannot_complete_pending(self, db):
        t = db.create("Work")
        result = db.complete(t["id"])
        assert result is None

    def test_requeue(self, db):
        t = db.create("Work")
        db.claim("a1", t["id"])
        result = db.requeue(t["id"])
        assert result["state"] == "pending"
        assert result["claimed_by"] == ""


class TestQuery:
    def test_get_existing(self, db):
        t = db.create("Work")
        fetched = db.get(t["id"])
        assert fetched["id"] == t["id"]

    def test_get_missing(self, db):
        assert db.get("nonexistent") is None

    def test_list_pending(self, db):
        db.create("P1", priority=1)
        db.create("P2", priority=2)
        t3 = db.create("P3", priority=3)
        db.claim("a1", t3["id"])
        pending = db.list_pending()
        assert len(pending) == 2

    def test_list_claimed(self, db):
        t = db.create("Work")
        db.claim("a1", t["id"])
        claimed = db.list_claimed()
        assert len(claimed) == 1

    def test_list_all(self, db):
        db.create("T1")
        db.create("T2")
        t3 = db.create("T3")
        db.claim("a1", t3["id"])
        all_tasks = db.list_all()
        assert len(all_tasks) == 3

    def test_count_by_state(self, db):
        db.create("T1")
        db.create("T2")
        t3 = db.create("T3")
        db.claim("a1", t3["id"])
        db.complete(t3["id"])
        counts = db.count_by_state()
        assert counts.get("pending", 0) == 2
        assert counts.get("completed", 0) == 1


class TestRequeueStale:
    def test_requeue_stale_tasks(self, db):
        t = db.create("Work")
        db.claim("a1", t["id"])
        # Manually backdate claimed_at
        conn = db._get_conn()
        conn.execute(
            "UPDATE tasks SET claimed_at = ? WHERE id = ?",
            (time.time() - 700, t["id"]),
        )
        conn.commit()
        conn.close()

        requeued = db.requeue_stale(ttl=600)
        assert t["id"] in requeued
        task = db.get(t["id"])
        assert task["state"] == "pending"

    def test_fresh_claim_not_requeued(self, db):
        t = db.create("Work")
        db.claim("a1", t["id"])
        requeued = db.requeue_stale(ttl=600)
        assert len(requeued) == 0


class TestConcurrency:
    def test_concurrent_claims_no_double_booking(self, db):
        """Only one claim should succeed for the same task."""
        t = db.create("Contested task")

        results = []
        for i in range(5):
            result = db.claim(f"agent-{i}", t["id"])
            results.append(result)

        # Exactly one should succeed
        claimed = [r for r in results if r is not None]
        assert len(claimed) == 1
