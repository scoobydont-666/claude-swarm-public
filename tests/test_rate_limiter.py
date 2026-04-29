"""Tests for rate-limit detection and profile tracking."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rate_limiter import (
    RateLimitEvent,
    RateLimitTracker,
    detect_rate_limit,
    _classify_limit,
)


class TestDetectRateLimit:
    def test_limit_reached_session(self):
        line = "Limit reached · resets in 3 hours"
        event = detect_rate_limit(line, profile="pro-1")
        assert event is not None
        assert event.limit_type == "session"
        assert event.profile == "pro-1"
        assert "3 hours" in event.reset_hint
        assert event.cooldown_until > time.time()

    def test_limit_reached_weekly(self):
        line = "Limit reached · resets Monday at 9:00 AM"
        event = detect_rate_limit(line, profile="pro-1")
        assert event is not None
        assert event.limit_type == "weekly"
        assert "Monday" in event.reset_hint

    def test_limit_reached_minutes(self):
        line = "Limit reached · resets in 45 minutes"
        event = detect_rate_limit(line, profile="default")
        assert event is not None
        assert event.limit_type == "session"
        assert event.cooldown_until - time.time() == pytest.approx(45 * 60, abs=5)

    def test_limit_reached_bullet_variant(self):
        line = "Limit reached • resets in 2 hours"
        event = detect_rate_limit(line, profile="default")
        assert event is not None
        assert event.limit_type == "session"

    def test_auth_failure(self):
        line = "Error: 401 Unauthorized - invalid API key"
        event = detect_rate_limit(line, profile="key-2")
        assert event is not None
        assert event.limit_type == "auth"
        assert event.cooldown_until == 0.0  # permanent

    def test_billing_failure(self):
        line = "Error: 402 Payment Required - insufficient credit"
        event = detect_rate_limit(line)
        assert event is not None
        assert event.limit_type == "billing"

    def test_overloaded(self):
        line = "Server overloaded, try again later"
        event = detect_rate_limit(line, profile="default")
        assert event is not None
        assert event.limit_type == "overloaded"
        assert event.cooldown_until - time.time() == pytest.approx(300, abs=5)

    def test_normal_output_no_detection(self):
        assert detect_rate_limit("Building component...") is None
        assert detect_rate_limit("Tests passed: 42") is None
        assert detect_rate_limit("") is None

    def test_event_to_dict(self):
        event = RateLimitEvent(
            profile="test",
            limit_type="session",
            reset_hint="in 1 hour",
            cooldown_until=time.time() + 3600,
        )
        d = event.to_dict()
        assert d["profile"] == "test"
        assert d["limit_type"] == "session"
        assert "T" in d["detected_at"]  # ISO format


class TestClassifyLimit:
    def test_hours(self):
        lt, cd = _classify_limit("in 3 hours")
        assert lt == "session"
        assert cd == pytest.approx(10800, abs=1)

    def test_minutes(self):
        lt, cd = _classify_limit("in 45 minutes")
        assert lt == "session"
        assert cd == pytest.approx(2700, abs=1)

    def test_hours_and_minutes(self):
        lt, cd = _classify_limit("in 1 hour 30 minutes")
        assert lt == "session"
        assert cd == pytest.approx(5400, abs=1)

    def test_day_name_weekly(self):
        lt, _ = _classify_limit("Monday at 9:00 AM")
        assert lt == "weekly"

    def test_month_name_weekly(self):
        lt, _ = _classify_limit("March 28 at 12:00 PM")
        assert lt == "weekly"

    def test_unknown_defaults_session(self):
        lt, cd = _classify_limit("soon")
        assert lt == "session"
        assert cd == 3600.0


class TestRateLimitTracker:
    def test_fresh_profile_available(self):
        tracker = RateLimitTracker()
        assert tracker.is_available("pro-1") is True

    def test_rate_limited_profile_unavailable(self):
        tracker = RateLimitTracker()
        tracker.record(
            RateLimitEvent(
                profile="pro-1",
                limit_type="session",
                reset_hint="in 1 hour",
                cooldown_until=time.time() + 3600,
            )
        )
        assert tracker.is_available("pro-1") is False

    def test_expired_cooldown_available(self):
        tracker = RateLimitTracker()
        tracker.record(
            RateLimitEvent(
                profile="pro-1",
                limit_type="session",
                reset_hint="in 1 second",
                cooldown_until=time.time() - 1,
            )
        )
        assert tracker.is_available("pro-1") is True

    def test_auth_failure_permanent(self):
        tracker = RateLimitTracker()
        tracker.record(
            RateLimitEvent(
                profile="bad-key",
                limit_type="auth",
                reset_hint="auth failed",
                cooldown_until=0.0,
            )
        )
        assert tracker.is_available("bad-key") is False

    def test_get_available_profiles(self):
        tracker = RateLimitTracker()
        tracker.record(
            RateLimitEvent(
                profile="pro-1",
                limit_type="session",
                reset_hint="in 1 hour",
                cooldown_until=time.time() + 3600,
            )
        )
        available = tracker.get_available_profiles(["pro-1", "pro-2", "pro-3"])
        assert "pro-1" not in available
        assert "pro-2" in available
        assert "pro-3" in available

    def test_get_best_profile_prefers_never_limited(self):
        tracker = RateLimitTracker()
        tracker.record(
            RateLimitEvent(
                profile="pro-1",
                limit_type="session",
                reset_hint="expired",
                cooldown_until=time.time() - 1,
            )
        )
        # pro-1 is available (expired) but was rate-limited before
        # pro-2 has never been limited — should be preferred
        best = tracker.get_best_profile(["pro-1", "pro-2"])
        assert best == "pro-2"

    def test_get_best_profile_none_when_all_limited(self):
        tracker = RateLimitTracker()
        tracker.record(
            RateLimitEvent(
                profile="pro-1",
                limit_type="session",
                reset_hint="in 1 hour",
                cooldown_until=time.time() + 3600,
            )
        )
        assert tracker.get_best_profile(["pro-1"]) is None

    def test_status(self):
        tracker = RateLimitTracker()
        tracker.record(
            RateLimitEvent(
                profile="pro-1",
                limit_type="session",
                reset_hint="in 1 hour",
                cooldown_until=time.time() + 3600,
            )
        )
        status = tracker.status()
        assert "pro-1" in status
        assert status["pro-1"]["limit_type"] == "session"
        assert status["pro-1"]["available"] is False
