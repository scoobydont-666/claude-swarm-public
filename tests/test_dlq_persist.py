"""Tests for the SQLite write-through DLQ persistence mirror."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture
def persist_db(monkeypatch, tmp_path):
    """Point DLQ_PERSIST_DB at a fresh temp SQLite file per test."""
    db_file = tmp_path / "dlq-test.db"
    monkeypatch.setenv("DLQ_PERSIST_DB", str(db_file))

    # Force the module-level DB_PATH to re-read the env var.
    from ipc import dlq_persist

    dlq_persist.DB_PATH = db_file
    dlq_persist.reset_for_test()
    yield dlq_persist


class TestPersistEntry:
    def test_insert_one(self, persist_db):
        persist_db.persist_entry(
            stream_id="1700000000000-0",
            reason="pending_timeout_60001ms",
            envelope_fields={"envelope": '{"id":"abc"}', "reason": "pending_timeout_60001ms"},
        )
        assert persist_db.depth_persisted() == 1

    def test_insert_duplicate_is_idempotent(self, persist_db):
        persist_db.persist_entry("1700000000001-0", "r", {"envelope": "{}"})
        persist_db.persist_entry("1700000000001-0", "r", {"envelope": "{}"})
        assert persist_db.depth_persisted() == 1


class TestMarkResolved:
    def test_requeue_resolves(self, persist_db):
        persist_db.persist_entry("1700000000010-0", "r", {"envelope": "{}"})
        persist_db.mark_resolved("1700000000010-0", persist_db.RESOLUTION_REQUEUED)
        assert persist_db.depth_persisted() == 0

    def test_invalid_resolution_raises(self, persist_db):
        persist_db.persist_entry("1700000000011-0", "r", {"envelope": "{}"})
        with pytest.raises(ValueError):
            persist_db.mark_resolved("1700000000011-0", "nonsense")

    def test_bulk_resolve_counts_rows(self, persist_db):
        for i in range(5):
            persist_db.persist_entry(f"1700000000020-{i}", "r", {"envelope": "{}"})
        resolved = persist_db.mark_resolved_bulk(
            [f"1700000000020-{i}" for i in range(5)],
            persist_db.RESOLUTION_EXPIRED,
        )
        assert resolved == 5
        assert persist_db.depth_persisted() == 0

    def test_double_resolve_is_noop(self, persist_db):
        persist_db.persist_entry("1700000000030-0", "r", {"envelope": "{}"})
        persist_db.mark_resolved("1700000000030-0", persist_db.RESOLUTION_REQUEUED)
        # Second resolve should match zero rows (resolution IS NULL filter).
        persist_db.mark_resolved("1700000000030-0", persist_db.RESOLUTION_EXPIRED)

        # fp_rate should still count it as requeued.
        fp, requeued, total = persist_db.false_positive_rate(window_seconds=3600)
        assert requeued == 1
        assert total == 1


class TestFalsePositiveRate:
    def test_no_data_returns_zero(self, persist_db):
        fp, requeued, total = persist_db.false_positive_rate()
        assert fp == 0.0
        assert total == 0

    def test_all_requeued_means_full_fp(self, persist_db):
        for i in range(3):
            persist_db.persist_entry(f"1700000000040-{i}", "r", {"envelope": "{}"})
            persist_db.mark_resolved(f"1700000000040-{i}", persist_db.RESOLUTION_REQUEUED)

        fp, requeued, total = persist_db.false_positive_rate(window_seconds=3600)
        assert fp == 1.0
        assert requeued == 3
        assert total == 3

    def test_mixed_resolution_ratio(self, persist_db):
        # 1 requeued, 3 expired — fp_rate = 0.25
        persist_db.persist_entry("1700000000050-0", "r", {"envelope": "{}"})
        persist_db.mark_resolved("1700000000050-0", persist_db.RESOLUTION_REQUEUED)
        for i in range(1, 4):
            persist_db.persist_entry(f"1700000000050-{i}", "r", {"envelope": "{}"})
            persist_db.mark_resolved(f"1700000000050-{i}", persist_db.RESOLUTION_EXPIRED)

        fp, requeued, total = persist_db.false_positive_rate(window_seconds=3600)
        assert total == 4
        assert requeued == 1
        assert abs(fp - 0.25) < 1e-9

    def test_purged_excluded_from_denominator(self, persist_db):
        # Operator-initiated purges are not quality signal; they shouldn't
        # distort FP rate.
        persist_db.persist_entry("1700000000060-0", "r", {"envelope": "{}"})
        persist_db.mark_resolved("1700000000060-0", persist_db.RESOLUTION_REQUEUED)
        persist_db.persist_entry("1700000000060-1", "r", {"envelope": "{}"})
        persist_db.mark_resolved("1700000000060-1", persist_db.RESOLUTION_PURGED)

        fp, requeued, total = persist_db.false_positive_rate(window_seconds=3600)
        assert total == 1  # requeued only
        assert fp == 1.0


class TestDepth:
    def test_only_unresolved_counted(self, persist_db):
        persist_db.persist_entry("1700000000070-0", "r", {"envelope": "{}"})
        persist_db.persist_entry("1700000000070-1", "r", {"envelope": "{}"})
        persist_db.persist_entry("1700000000070-2", "r", {"envelope": "{}"})

        assert persist_db.depth_persisted() == 3
        persist_db.mark_resolved("1700000000070-0", persist_db.RESOLUTION_REQUEUED)
        assert persist_db.depth_persisted() == 2
