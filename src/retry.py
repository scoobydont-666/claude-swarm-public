"""F2: centralized retry decorator with exponential backoff.

Covers <hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase F2.

Replaces ad-hoc retry logic scattered across remote_session.py and similar
callers. Provides a single decorator callers can apply to any function
that makes a flaky external call (HTTP, SSH, Redis, NFS).

Features:
- configurable max_attempts (default 3)
- exponential backoff with jitter (prevents thundering herd)
- configurable exception types (default: only network-like)
- hook for DLQ deposit on final exhaustion (callers supply the deposit fn)
- zero external deps (stdlib only)

Usage:
    @with_retry(max_attempts=3, backoff="exp", retry_on=(RequestException,))
    def call_prometheus(url: str) -> dict:
        return requests.get(url).json()
"""

from __future__ import annotations

import functools
import logging
import random
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default exception types that are retry-safe. ValueError / TypeError are
# programmer errors and should NOT be retried — they'll just fail 3 times.
DEFAULT_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    ConnectionError,
)


def with_retry(
    *,
    max_attempts: int = 3,
    backoff: str = "exp",
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 30.0,
    retry_on: tuple[type[BaseException], ...] | None = None,
    jitter: bool = True,
    on_exhausted: Callable[[BaseException, tuple, dict], None] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Return a decorator that retries `fn` on configured exceptions.

    Args:
        max_attempts: total attempts including the initial one (>=1).
        backoff: "exp" for 2^n * base; "linear" for n * base; "fixed" for base.
        base_delay_seconds: first delay before retry.
        max_delay_seconds: cap any individual sleep.
        retry_on: tuple of exception classes that trigger retry. Default
            is (OSError, TimeoutError, ConnectionError) — NOT ValueError /
            TypeError (those are bugs, not transient failures).
        jitter: if True, multiply each sleep by uniform(0.5, 1.5) to avoid
            thundering herd on fleet-wide retries.
        on_exhausted: optional callback invoked with (final_exc, args, kwargs)
            after the last attempt fails. Use this to deposit to DLQ, log
            to a dedicated channel, emit a metric, etc.

    Returns:
        Decorator.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    if backoff not in {"exp", "linear", "fixed"}:
        raise ValueError(f"backoff must be 'exp' | 'linear' | 'fixed', got {backoff!r}")

    retry_exc = retry_on if retry_on is not None else DEFAULT_RETRY_EXCEPTIONS

    def _compute_delay(attempt: int) -> float:
        if backoff == "fixed":
            d = base_delay_seconds
        elif backoff == "linear":
            d = base_delay_seconds * attempt
        else:  # exp
            d = base_delay_seconds * (2 ** (attempt - 1))
        if jitter:
            d *= random.uniform(0.5, 1.5)
        return min(d, max_delay_seconds)

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retry_exc as exc:
                    last_exc = exc
                    if attempt >= max_attempts:
                        break
                    delay = _compute_delay(attempt)
                    logger.info(
                        "retry: %s attempt %d/%d failed (%s); sleeping %.2fs",
                        fn.__qualname__,
                        attempt,
                        max_attempts,
                        type(exc).__name__,
                        delay,
                    )
                    time.sleep(delay)
            # All attempts exhausted
            assert last_exc is not None  # type guard for mypy
            logger.warning(
                "retry: %s exhausted %d attempts; last error: %s",
                fn.__qualname__,
                max_attempts,
                last_exc,
            )
            if on_exhausted is not None:
                try:
                    on_exhausted(last_exc, args, kwargs)
                except Exception as hook_exc:  # noqa: BLE001
                    logger.warning(
                        "retry: on_exhausted hook raised (ignoring): %s", hook_exc
                    )
            raise last_exc

        return wrapper

    return decorator
