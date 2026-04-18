"""Chaos tests — exercise error paths using the MockExternal harness.

Backport pattern from /opt/nai-swarm/tests/test_chaos_network.py (P3 item 1).
Tests are pure — they verify the mock_external harness itself, which gives
us the foundation for chaos-testing real downstream services in P8+.
"""

from __future__ import annotations

import pytest

from tests.fixtures.mock_external import (
    MockExternal,
    MockScenario,
    circuit_breaker_storm,
    connection_drop,
    slow_response,
)


class TestMockExternal:
    def test_default_ok_response(self):
        m = MockExternal()
        r = m.call({"x": 1})
        assert r.status_code == 200
        assert r.ok
        assert m.call_count == 1

    def test_circuit_breaker_storm_fails(self):
        m = MockExternal()
        m.set_scenario(circuit_breaker_storm(failure_rate=1.0))
        r = m.call()
        assert r.status_code == 503
        assert not r.ok
        assert r.body.get("scenario") == "circuit-breaker-storm"

    def test_slow_response_injects_latency(self):
        import time

        m = MockExternal()
        m.set_scenario(slow_response(latency_s=0.1))
        t0 = time.monotonic()
        m.call()
        dt = time.monotonic() - t0
        assert dt >= 0.1

    def test_connection_drop_raises(self):
        m = MockExternal()
        m.set_scenario(connection_drop())
        with pytest.raises(ConnectionError):
            m.call()

    def test_session_resets_scenarios(self):
        m = MockExternal()
        with m.session() as s:
            s.set_scenario(MockScenario("bad", probability=1.0, status_code=500))
            assert m.call().status_code == 500
        # After session exit, scenarios reset
        assert m.call().status_code == 200

    def test_call_log_tracks_payloads(self):
        m = MockExternal()
        m.call({"a": 1})
        m.call({"b": 2})
        assert m.call_count == 2

    def test_reset_clears_log(self):
        m = MockExternal()
        m.call()
        m.call()
        m.reset()
        assert m.call_count == 0
