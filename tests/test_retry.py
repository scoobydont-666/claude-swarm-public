"""F2: centralized retry decorator tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retry import DEFAULT_RETRY_EXCEPTIONS, with_retry


class TestBasicRetry:
    def test_success_on_first_attempt_no_retry(self):
        calls = []

        @with_retry(max_attempts=3)
        def f():
            calls.append(1)
            return "ok"

        assert f() == "ok"
        assert len(calls) == 1

    def test_retries_until_success(self):
        attempts = [0]

        @with_retry(max_attempts=5, base_delay_seconds=0.001, jitter=False)
        def f():
            attempts[0] += 1
            if attempts[0] < 3:
                raise ConnectionError("transient")
            return "ok"

        assert f() == "ok"
        assert attempts[0] == 3

    def test_raises_last_exception_after_exhaustion(self):
        @with_retry(max_attempts=2, base_delay_seconds=0.001, jitter=False)
        def f():
            raise ConnectionError("always")

        with pytest.raises(ConnectionError, match="always"):
            f()


class TestExceptionFiltering:
    def test_non_retryable_exception_raises_immediately(self):
        attempts = [0]

        @with_retry(max_attempts=3, base_delay_seconds=0.001, jitter=False)
        def f():
            attempts[0] += 1
            raise ValueError("bug")

        with pytest.raises(ValueError):
            f()
        # ValueError is not in DEFAULT_RETRY_EXCEPTIONS → 1 attempt only
        assert attempts[0] == 1

    def test_custom_retry_on(self):
        attempts = [0]

        class MyError(Exception):
            pass

        @with_retry(
            max_attempts=3,
            base_delay_seconds=0.001,
            jitter=False,
            retry_on=(MyError,),
        )
        def f():
            attempts[0] += 1
            raise MyError("flaky")

        with pytest.raises(MyError):
            f()
        assert attempts[0] == 3


class TestBackoffStrategies:
    def test_invalid_backoff_rejected(self):
        with pytest.raises(ValueError, match="backoff must be"):
            with_retry(max_attempts=3, backoff="bogus")

    def test_invalid_max_attempts_rejected(self):
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            with_retry(max_attempts=0)

    def test_backoff_fixed(self):
        delays = []

        original_sleep = __import__("time").sleep

        def fake_sleep(d):
            delays.append(d)

        import time as _t

        _t.sleep = fake_sleep
        try:

            @with_retry(
                max_attempts=3, backoff="fixed", base_delay_seconds=1.0, jitter=False
            )
            def f():
                raise ConnectionError("x")

            with pytest.raises(ConnectionError):
                f()
            assert delays == [1.0, 1.0]  # 2 sleeps before 3rd final attempt
        finally:
            _t.sleep = original_sleep

    def test_backoff_exp(self):
        delays = []
        import time as _t

        original = _t.sleep
        _t.sleep = lambda d: delays.append(d)
        try:

            @with_retry(
                max_attempts=4, backoff="exp", base_delay_seconds=1.0, jitter=False
            )
            def f():
                raise ConnectionError("x")

            with pytest.raises(ConnectionError):
                f()
            # delays: 1.0, 2.0, 4.0
            assert delays == [1.0, 2.0, 4.0]
        finally:
            _t.sleep = original

    def test_max_delay_caps_sleep(self):
        delays = []
        import time as _t

        original = _t.sleep
        _t.sleep = lambda d: delays.append(d)
        try:

            @with_retry(
                max_attempts=5,
                backoff="exp",
                base_delay_seconds=10.0,
                max_delay_seconds=15.0,
                jitter=False,
            )
            def f():
                raise ConnectionError("x")

            with pytest.raises(ConnectionError):
                f()
            # 10, 20 (capped to 15), 40 (capped to 15), 80 (capped to 15)
            assert delays == [10.0, 15.0, 15.0, 15.0]
        finally:
            _t.sleep = original


class TestOnExhaustedHook:
    def test_hook_called_on_exhaustion(self):
        hook = MagicMock()

        @with_retry(
            max_attempts=2,
            base_delay_seconds=0.001,
            jitter=False,
            on_exhausted=hook,
        )
        def f(x, y=10):
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            f(1, y=2)

        hook.assert_called_once()
        exc, args, kwargs = hook.call_args.args
        assert isinstance(exc, ConnectionError)
        assert args == (1,)
        assert kwargs == {"y": 2}

    def test_hook_not_called_on_success(self):
        hook = MagicMock()

        @with_retry(max_attempts=3, on_exhausted=hook)
        def f():
            return 42

        assert f() == 42
        hook.assert_not_called()

    def test_hook_exception_ignored(self):
        def bad_hook(*a, **k):
            raise RuntimeError("hook broken")

        @with_retry(
            max_attempts=2,
            base_delay_seconds=0.001,
            jitter=False,
            on_exhausted=bad_hook,
        )
        def f():
            raise ConnectionError("x")

        # Original ConnectionError still propagates; hook failure swallowed
        with pytest.raises(ConnectionError):
            f()


class TestDefaultRetryableExceptions:
    def test_oserror_retried(self):
        assert OSError in DEFAULT_RETRY_EXCEPTIONS

    def test_timeout_retried(self):
        assert TimeoutError in DEFAULT_RETRY_EXCEPTIONS

    def test_connection_retried(self):
        assert ConnectionError in DEFAULT_RETRY_EXCEPTIONS

    def test_valueerror_not_retried(self):
        assert ValueError not in DEFAULT_RETRY_EXCEPTIONS
