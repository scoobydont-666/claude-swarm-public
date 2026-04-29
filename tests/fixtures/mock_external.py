"""Mock External Service Harness.

Backport from /opt/nai-swarm/src/mock_prism.py (P3 item 1), stripped of
NAI/Prism-specific inventory. Simulates circuit-breaker, timeout, and
503-storm scenarios against any HTTP service dependency.

Use in chaos/integration tests to drive error-path coverage without a
real external service.
"""

from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class MockScenario:
    """A single failure-injection scenario."""

    name: str
    # Probability [0,1] that a given call fails with this scenario's behavior.
    probability: float = 0.0
    # Simulated latency (seconds). Applied before returning.
    latency_s: float = 0.0
    # HTTP-style status code if the scenario returns a response object.
    status_code: int = 200
    # If True, raise TimeoutError instead of returning.
    raise_timeout: bool = False
    # If True, raise ConnectionError.
    raise_conn_error: bool = False


@dataclass
class MockResponse:
    """Minimal HTTP-like response object returned by MockExternal.call()."""

    status_code: int = 200
    body: dict = field(default_factory=dict)
    latency_s: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400


class MockExternal:
    """Mock external service. Thread-safe.

    Typical use in tests:

        m = MockExternal(name="downstream-api")
        m.set_scenario(MockScenario("503-storm", probability=1.0, status_code=503))
        with m.session():
            resp = m.call({"payload": "x"})
            assert resp.status_code == 503

    Drop-in for circuit-breaker / retry / timeout tests.
    """

    def __init__(self, name: str = "mock-external", default_status: int = 200):
        self.name = name
        self.default_status = default_status
        self._scenarios: list[MockScenario] = []
        self._lock = threading.Lock()
        self._call_log: list[dict] = []

    def set_scenario(self, scenario: MockScenario) -> None:
        with self._lock:
            self._scenarios.append(scenario)

    def reset(self) -> None:
        with self._lock:
            self._scenarios.clear()
            self._call_log.clear()

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self._call_log)

    @contextmanager
    def session(self):
        """Context manager for scoped cleanup."""
        try:
            yield self
        finally:
            self.reset()

    def _select_scenario(self) -> MockScenario | None:
        for s in self._scenarios:
            if random.random() < s.probability:
                return s
        return None

    def call(self, payload: dict | None = None) -> MockResponse:
        """Simulate a single external call."""
        with self._lock:
            self._call_log.append({"payload": payload or {}, "at": time.time()})
        scenario = self._select_scenario()
        if scenario is None:
            return MockResponse(status_code=self.default_status, body={"ok": True})

        if scenario.latency_s:
            time.sleep(scenario.latency_s)
        if scenario.raise_timeout:
            raise TimeoutError(f"{self.name}: simulated timeout ({scenario.name})")
        if scenario.raise_conn_error:
            raise ConnectionError(f"{self.name}: simulated conn error ({scenario.name})")
        return MockResponse(
            status_code=scenario.status_code,
            body={"scenario": scenario.name, "injected": True},
            latency_s=scenario.latency_s,
        )


# Preset scenario factories for common chaos tests
def circuit_breaker_storm(failure_rate: float = 0.8) -> MockScenario:
    """N% of calls fail with 503, others succeed."""
    return MockScenario("circuit-breaker-storm", probability=failure_rate, status_code=503)


def slow_response(latency_s: float = 5.0) -> MockScenario:
    """Every call takes latency_s seconds (drives timeout tests)."""
    return MockScenario("slow-response", probability=1.0, latency_s=latency_s, status_code=200)


def connection_drop() -> MockScenario:
    """Every call raises ConnectionError."""
    return MockScenario("connection-drop", probability=1.0, raise_conn_error=True)
