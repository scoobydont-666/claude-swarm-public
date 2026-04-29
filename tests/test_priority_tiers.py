"""Tests for priority tier system — NAI Swarm backport."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from task_queue import (
    Task,
    TaskQueue,
    _normalize_priority,
    PRIORITY_TIERS,
    PRIORITY_MAP,
    PREEMPT_SOURCE_MAX,
    PREEMPT_TARGET_MIN,
    PREEMPT_GAP,
)


class TestNormalizePriority:
    """Test priority normalization across all input formats."""

    def test_int_passthrough(self):
        assert _normalize_priority(0) == 0
        assert _normalize_priority(3) == 3
        assert _normalize_priority(5) == 5

    def test_int_clamped(self):
        assert _normalize_priority(-1) == 0
        assert _normalize_priority(10) == 5
        assert _normalize_priority(99) == 5

    def test_tier_names(self):
        assert _normalize_priority("production") == 0
        assert _normalize_priority("cicd") == 1
        assert _normalize_priority("lead") == 2
        assert _normalize_priority("standard") == 3
        assert _normalize_priority("batch") == 4
        assert _normalize_priority("sandbox") == 5

    def test_legacy_names(self):
        assert _normalize_priority("critical") == 0
        assert _normalize_priority("high") == 1
        assert _normalize_priority("medium") == 3
        assert _normalize_priority("low") == 4

    def test_p_format(self):
        assert _normalize_priority("P0") == 0
        assert _normalize_priority("P1") == 1
        assert _normalize_priority("P3") == 3
        assert _normalize_priority("P5") == 5
        assert _normalize_priority("p2") == 2

    def test_unknown_defaults_to_standard(self):
        assert _normalize_priority("bogus") == 3
        assert _normalize_priority(None) == 3

    def test_case_insensitive(self):
        assert _normalize_priority("PRODUCTION") == 0
        assert _normalize_priority("CiCd") == 1
        assert _normalize_priority("BATCH") == 4


class TestTierName:
    def test_all_tiers(self):
        q = TaskQueue(use_redis=False, tasks_dir="/tmp/test-tiers")
        assert q.tier_name(0) == "production"
        assert q.tier_name(1) == "cicd"
        assert q.tier_name(2) == "lead"
        assert q.tier_name(3) == "standard"
        assert q.tier_name(4) == "batch"
        assert q.tier_name(5) == "sandbox"

    def test_unknown_tier(self):
        q = TaskQueue(use_redis=False, tasks_dir="/tmp/test-tiers")
        assert q.tier_name(99) == "tier-99"


class TestTaskSortingByTier:
    """Verify that tasks sort by tier then FIFO within tier."""

    def test_sort_by_priority(self):
        tasks = [
            Task(id="t1", title="batch work", priority=4, created_at=1.0),
            Task(id="t2", title="production fix", priority=0, created_at=2.0),
            Task(id="t3", title="standard work", priority=3, created_at=3.0),
        ]
        tasks.sort(key=lambda t: (t.priority, t.created_at))
        assert tasks[0].id == "t2"  # P0
        assert tasks[1].id == "t3"  # P3
        assert tasks[2].id == "t1"  # P4

    def test_fifo_within_same_tier(self):
        tasks = [
            Task(id="t1", title="later", priority=3, created_at=300.0),
            Task(id="t2", title="earlier", priority=3, created_at=100.0),
            Task(id="t3", title="middle", priority=3, created_at=200.0),
        ]
        tasks.sort(key=lambda t: (t.priority, t.created_at))
        assert tasks[0].id == "t2"  # earliest
        assert tasks[1].id == "t3"
        assert tasks[2].id == "t1"  # latest


class TestPreemption:
    """Test preemption logic: P0-P2 can preempt P4-P5."""

    def _make_queue(self, tasks_dir):
        q = TaskQueue(use_redis=False, tasks_dir=tasks_dir)
        return q

    def test_p0_preempts_p5(self, tmp_path):
        q = self._make_queue(str(tmp_path))
        # Create a P5 task and claim it
        t = q.create(title="sandbox work", priority=5, created_by="test")
        q.claim("agent-1", t.id)
        # P0 should find it preemptable
        preemptable = q.find_preemptable(0)
        assert len(preemptable) == 1
        assert preemptable[0].id == t.id

    def test_p0_preempts_p4(self, tmp_path):
        q = self._make_queue(str(tmp_path))
        t = q.create(title="batch work", priority=4, created_by="test")
        q.claim("agent-1", t.id)
        preemptable = q.find_preemptable(0)
        assert len(preemptable) == 1

    def test_p2_preempts_p5(self, tmp_path):
        q = self._make_queue(str(tmp_path))
        t = q.create(title="sandbox", priority=5, created_by="test")
        q.claim("agent-1", t.id)
        # P2 can preempt P5 (gap = 3 >= PREEMPT_GAP)
        preemptable = q.find_preemptable(2)
        assert len(preemptable) == 1

    def test_p2_cannot_preempt_p3(self, tmp_path):
        q = self._make_queue(str(tmp_path))
        t = q.create(title="standard", priority=3, created_by="test")
        q.claim("agent-1", t.id)
        # P2 cannot preempt P3 (gap = 1 < PREEMPT_GAP and P3 < PREEMPT_TARGET_MIN)
        preemptable = q.find_preemptable(2)
        assert len(preemptable) == 0

    def test_p3_cannot_preempt_anything(self, tmp_path):
        q = self._make_queue(str(tmp_path))
        t = q.create(title="sandbox", priority=5, created_by="test")
        q.claim("agent-1", t.id)
        # P3 > PREEMPT_SOURCE_MAX, cannot preempt
        preemptable = q.find_preemptable(3)
        assert len(preemptable) == 0

    def test_no_claimed_tasks_returns_empty(self, tmp_path):
        q = self._make_queue(str(tmp_path))
        q.create(title="pending", priority=5, created_by="test")
        # Task is pending, not claimed — no preemption targets
        preemptable = q.find_preemptable(0)
        assert len(preemptable) == 0

    def test_multiple_preemptable(self, tmp_path):
        q = self._make_queue(str(tmp_path))
        t1 = q.create(title="sandbox1", priority=5, created_by="test")
        t2 = q.create(title="batch1", priority=4, created_by="test")
        t3 = q.create(title="standard1", priority=3, created_by="test")
        q.claim("agent-1", t1.id)
        q.claim("agent-2", t2.id)
        q.claim("agent-3", t3.id)
        # P0 should preempt P4 and P5 but not P3
        preemptable = q.find_preemptable(0)
        ids = {t.id for t in preemptable}
        assert t1.id in ids
        assert t2.id in ids
        assert t3.id not in ids


class TestTaskFromDict:
    """Verify Task.from_dict handles new priority formats."""

    def test_int_priority(self):
        t = Task.from_dict({"id": "t1", "title": "test", "priority": 0})
        assert t.priority == 0

    def test_string_tier_priority(self):
        t = Task.from_dict({"id": "t1", "title": "test", "priority": "production"})
        assert t.priority == 0

    def test_p_format_priority(self):
        t = Task.from_dict({"id": "t1", "title": "test", "priority": "P2"})
        assert t.priority == 2

    def test_legacy_priority(self):
        t = Task.from_dict({"id": "t1", "title": "test", "priority": "critical"})
        assert t.priority == 0

    def test_default_priority(self):
        t = Task.from_dict({"id": "t1", "title": "test"})
        assert t.priority == 3  # standard
