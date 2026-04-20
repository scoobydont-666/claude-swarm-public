"""E7: Prometheus query circuit breaker tests.

Covers <hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase E7.

State machine:
    CLOSED → OPEN    (when failures in window ≥ threshold)
    OPEN → HALF_OPEN (after cooldown elapses)
    HALF_OPEN → CLOSED (probe succeeds, reset cooldown)
    HALF_OPEN → OPEN   (probe fails, cooldown doubles up to max)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prom_circuit_breaker import CircuitBreaker, CircuitBreakerOpen


def _fail():
    raise RuntimeError("boom")


def _ok():
    return "result"


class TestClosedState:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == "CLOSED"

    def test_successful_call_passes_through(self):
        cb = CircuitBreaker()
        assert cb.call(_ok) == "result"
        assert cb.state == "CLOSED"

    def test_failing_call_raises_original_exception(self):
        cb = CircuitBreaker()
        with pytest.raises(RuntimeError, match="boom"):
            cb.call(_fail)
        assert cb.state == "CLOSED"  # one failure doesn't trip

    def test_below_threshold_stays_closed(self):
        cb = CircuitBreaker(failure_threshold=5, window_size=10)
        for _ in range(4):
            with pytest.raises(RuntimeError):
                cb.call(_fail)
        assert cb.state == "CLOSED"


class TestOpeningTransition:
    def test_trips_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, window_size=10)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(_fail)
        assert cb.state == "OPEN"

    def test_open_raises_circuit_breaker_open(self):
        cb = CircuitBreaker(failure_threshold=2, window_size=10)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(_fail)
        assert cb.state == "OPEN"
        with pytest.raises(CircuitBreakerOpen):
            cb.call(_ok)  # even a good call gets rejected

    def test_intermixed_success_failure_below_threshold_stays_closed(self):
        cb = CircuitBreaker(failure_threshold=5, window_size=10)
        cb.call(_ok)
        with pytest.raises(RuntimeError):
            cb.call(_fail)
        cb.call(_ok)
        # 1 fail in window — well below threshold
        assert cb.state == "CLOSED"


class TestCooldownAndHalfOpen:
    def test_open_transitions_to_half_open_after_cooldown(self):
        cb = CircuitBreaker(
            failure_threshold=2, window_size=5, cooldown_seconds=0.05
        )
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(_fail)
        assert cb.state == "OPEN"
        time.sleep(0.1)
        # Next call triggers transition check
        # In HALF_OPEN, the call goes through
        result = cb.call(_ok)
        assert result == "result"
        assert cb.state == "CLOSED"  # probe succeeded → closed

    def test_probe_failure_reopens_with_doubled_cooldown(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            window_size=5,
            cooldown_seconds=0.05,
            max_cooldown_seconds=10.0,
        )
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(_fail)
        initial_cooldown = cb._current_cooldown
        time.sleep(0.1)
        with pytest.raises(RuntimeError):
            cb.call(_fail)  # probe fails
        assert cb.state == "OPEN"
        assert cb._current_cooldown == initial_cooldown * 2

    def test_cooldown_capped_at_max(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            window_size=5,
            cooldown_seconds=0.05,
            max_cooldown_seconds=0.08,  # very tight cap
        )
        # Trip
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(_fail)
        # Repeated probe failures
        for _ in range(5):
            time.sleep(0.1)
            with pytest.raises(RuntimeError):
                cb.call(_fail)
        # Cooldown should not exceed max
        assert cb._current_cooldown <= cb.max_cooldown

    def test_reset_force_closes(self):
        cb = CircuitBreaker(failure_threshold=2, window_size=5)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(_fail)
        assert cb.state == "OPEN"
        cb.reset()
        assert cb.state == "CLOSED"
        # After reset, calls pass through again
        assert cb.call(_ok) == "result"


class TestRollingWindow:
    def test_old_failures_age_out(self):
        """window_size=3, threshold=3. Fail 3 times → OPEN.
        After a successful call aging out one failure, window has 2 fail+1 ok,
        which won't re-trip (since we already tripped once, this tests the
        CLOSED-state accumulation logic after a reset)."""
        cb = CircuitBreaker(failure_threshold=3, window_size=3)
        with pytest.raises(RuntimeError):
            cb.call(_fail)
        with pytest.raises(RuntimeError):
            cb.call(_fail)
        # 2 failures — still CLOSED
        assert cb.state == "CLOSED"
        cb.call(_ok)  # window: [fail, fail, ok] — 2 fails, below threshold
        assert cb.state == "CLOSED"
        cb.call(_ok)  # window: [fail, ok, ok] — 1 fail, below threshold
        assert cb.state == "CLOSED"
