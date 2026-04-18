"""E7: Prometheus query circuit breaker.

Covers <hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase E7.

Problem: health_monitor runs checks every 1s; each check may hit Prometheus.
When Prometheus is down or slow, the monitor can hammer it with requests that
all time out — turning the monitor into a mini-DDoS during incidents.

Fix: circuit breaker pattern. After N failures in a rolling window, the
breaker OPENS (all queries short-circuit to empty/error for a cooldown
period). After cooldown, it goes HALF-OPEN — a single probe query; if it
succeeds the breaker CLOSES, if it fails the cooldown doubles (exponential
backoff capped at max_cooldown_seconds).

Standalone implementation (no tenacity dep) — stdlib-only, thread-safe
with a single mutex around state transitions.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the breaker is OPEN."""


class CircuitBreaker:
    """Rolling-window failure counter + exponential cooldown.

    States:
        CLOSED    — normal operation; failures accumulate
        OPEN      — fail-fast; all calls rejected for `cooldown_seconds`
        HALF_OPEN — one probe call allowed; success → CLOSED, failure → OPEN
                    with doubled cooldown (capped at max_cooldown_seconds)
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        window_size: int = 10,
        cooldown_seconds: float = 30.0,
        max_cooldown_seconds: float = 300.0,
        name: str = "prometheus",
    ):
        self.failure_threshold = failure_threshold
        self.window_size = window_size
        self.initial_cooldown = cooldown_seconds
        self.max_cooldown = max_cooldown_seconds
        self.name = name

        self._lock = threading.Lock()
        self._window: deque[bool] = deque(maxlen=window_size)  # True=fail, False=ok
        self._state: str = "CLOSED"
        self._opened_at: float = 0.0
        self._current_cooldown: float = cooldown_seconds

    @property
    def state(self) -> str:
        """Current state (read without lock for observability; may be stale)."""
        return self._state

    def call(self, fn: Callable[..., T], /, *args, **kwargs) -> T:
        """Invoke `fn`; track result; raise CircuitBreakerOpen if tripped."""
        self._maybe_transition_to_half_open()

        if self._state == "OPEN":
            raise CircuitBreakerOpen(
                f"{self.name} circuit breaker is OPEN "
                f"(cooldown remaining: "
                f"{self._cooldown_remaining():.1f}s)"
            )

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result

    def _maybe_transition_to_half_open(self) -> None:
        """OPEN → HALF_OPEN after cooldown elapses."""
        with self._lock:
            if self._state != "OPEN":
                return
            if time.monotonic() - self._opened_at >= self._current_cooldown:
                self._state = "HALF_OPEN"
                logger.info("%s breaker: OPEN → HALF_OPEN (probe attempt)", self.name)

    def _record_success(self) -> None:
        with self._lock:
            self._window.append(False)
            if self._state == "HALF_OPEN":
                # Probe succeeded — close the breaker + reset cooldown
                self._state = "CLOSED"
                self._current_cooldown = self.initial_cooldown
                self._window.clear()
                logger.info("%s breaker: HALF_OPEN → CLOSED (recovery)", self.name)

    def _record_failure(self) -> None:
        with self._lock:
            self._window.append(True)
            if self._state == "HALF_OPEN":
                # Probe failed — reopen with doubled cooldown (exponential backoff)
                self._current_cooldown = min(
                    self._current_cooldown * 2, self.max_cooldown
                )
                self._state = "OPEN"
                self._opened_at = time.monotonic()
                self._window.clear()
                logger.warning(
                    "%s breaker: HALF_OPEN → OPEN (probe failed; cooldown=%.1fs)",
                    self.name,
                    self._current_cooldown,
                )
                return

            # CLOSED: count failures in rolling window; trip if threshold reached
            failures = sum(1 for x in self._window if x)
            if failures >= self.failure_threshold:
                self._state = "OPEN"
                self._opened_at = time.monotonic()
                logger.warning(
                    "%s breaker: CLOSED → OPEN (%d/%d failures; cooldown=%.1fs)",
                    self.name,
                    failures,
                    len(self._window),
                    self._current_cooldown,
                )

    def _cooldown_remaining(self) -> float:
        if self._state != "OPEN":
            return 0.0
        return max(0.0, self._current_cooldown - (time.monotonic() - self._opened_at))

    def reset(self) -> None:
        """Force-close the breaker (for testing or manual recovery)."""
        with self._lock:
            self._state = "CLOSED"
            self._window.clear()
            self._current_cooldown = self.initial_cooldown
            self._opened_at = 0.0
