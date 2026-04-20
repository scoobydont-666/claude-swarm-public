"""B2: Swarm→Sentinel event schema tests.

Covers /opt/hydra-project/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase B2.
"""

from __future__ import annotations

import logging

import pytest

from src.events_schema import (
    EVENT_SCHEMA_VERSION,
    BlockerFoundEvent,
    CommitEvent,
    RateLimitEvent,
    TaskCompletedEvent,
    TaskFailedEvent,
    TestResultEvent,
    registered_event_types,
    validate,
)


class TestRegistry:
    def test_all_core_event_types_registered(self):
        types = set(registered_event_types())
        expected = {
            "commit",
            "test_result",
            "task_completed",
            "task_claimed",
            "task_failed",
            "rate_limit",
            "blocker_found",
            "session_start",
            "session_end",
            "context_handoff",
            "config_sync",
        }
        missing = expected - types
        assert not missing, f"missing event types from registry: {missing}"

    def test_schema_version_is_stable(self):
        # Regression guard: bumping this must be intentional (documented in commit)
        assert EVENT_SCHEMA_VERSION == "1.0.0"


class TestValidateLaxMode:
    def test_known_event_clean_details_roundtrips(self):
        out = validate("commit", {"commit": "abc123", "message": "x", "files_changed": 1})
        assert out == {"commit": "abc123", "message": "x", "files_changed": 1}

    def test_unknown_event_type_logs_warning_and_passes_through(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = validate("made_up_event", {"foo": 1})
        assert out == {"foo": 1}
        assert any("unregistered event_type" in r.message for r in caplog.records)

    def test_unknown_field_dropped_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = validate(
                "commit",
                {"commit": "abc123", "message": "x", "files_changed": 1, "bogus_field": 42},
            )
        assert "bogus_field" not in out
        assert out == {"commit": "abc123", "message": "x", "files_changed": 1}
        assert any("unknown fields" in r.message for r in caplog.records)

    def test_missing_required_field_logs_warning_but_allows(self, caplog):
        # 'commit' has required field 'commit' and 'message'; drop 'message'
        with caplog.at_level(logging.WARNING):
            out = validate("commit", {"commit": "abc123"})
        # lax mode doesn't add default values, it just warns
        assert any("missing required fields" in r.message for r in caplog.records)
        assert out == {"commit": "abc123"}

    def test_none_details_treated_as_empty(self):
        # Event with no required fields
        out = validate("session_start", None)
        assert out == {}


class TestValidateStrictMode:
    def test_unknown_event_type_raises(self):
        with pytest.raises(ValueError, match="unregistered event_type"):
            validate("no_such_event", {}, strict=True)

    def test_unknown_field_raises(self):
        with pytest.raises(ValueError, match="unknown fields"):
            validate(
                "commit",
                {"commit": "x", "message": "m", "bogus": 1},
                strict=True,
            )

    def test_missing_required_field_raises(self):
        with pytest.raises(ValueError, match="missing required fields"):
            validate("commit", {"commit": "x"}, strict=True)

    def test_clean_event_passes_strict(self):
        out = validate("commit", {"commit": "x", "message": "m"}, strict=True)
        assert out == {"commit": "x", "message": "m"}


class TestDataclasses:
    def test_commit_event_to_details(self):
        e = CommitEvent(commit="abc", message="msg", files_changed=3)
        assert e.to_details() == {"commit": "abc", "message": "msg", "files_changed": 3}

    def test_test_result_event_defaults(self):
        e = TestResultEvent(passed=10, failed=0, total=10)
        assert e.duration_s == 0.0

    def test_task_completed_event(self):
        e = TaskCompletedEvent(task_id="task-1", result="success", duration_s=1.5)
        d = e.to_details()
        assert d["task_id"] == "task-1"
        assert d["duration_s"] == 1.5

    def test_task_failed_event(self):
        e = TaskFailedEvent(task_id="task-1", error="timeout", retry_count=2)
        assert e.to_details()["retry_count"] == 2

    def test_rate_limit_event(self):
        e = RateLimitEvent(profile="opus-4-7", limit_type="daily", reset_hint="2026-04-19T00:00Z")
        assert e.to_details()["profile"] == "opus-4-7"

    def test_blocker_found_event_default_severity(self):
        e = BlockerFoundEvent(blocker_type="missing_credential", description="no JWT")
        assert e.to_details()["severity"] == "medium"

    def test_events_are_immutable(self):
        e = CommitEvent(commit="x", message="m")
        with pytest.raises(AttributeError):
            e.commit = "y"  # type: ignore[misc]
