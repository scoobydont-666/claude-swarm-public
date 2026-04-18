"""Time-bounded cache for hot-path external queries.

Provides a thread-safe TTL cache keyed on args — intended for remote-host
checks (Prometheus, SSH, NFS) whose results can be stale for ~5s without
masking incidents, but which get hammered by duplicate rules inside the
1s health_monitor loop.

Usage:
    from ttl_cache import ttl_cache

    @ttl_cache(ttl_seconds=5.0, max_size=256)
    def expensive_read(arg1, arg2):
        ...

Guarantees:
- Thread-safe: single RLock around store access
- Bounded: LRU eviction at `max_size`
- Opaque to breaker/retry: cache hit returns stored value exactly; misses
  invoke the wrapped function normally so existing retry/circuit-breaker
  wrappers still fire on real calls
- Only caches non-exception returns. Exceptions propagate and are not cached.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from functools import wraps
from typing import Callable, TypeVar

T = TypeVar("T")


class _CacheStore:
    """Per-decorator in-memory LRU store with TTL eviction."""

    __slots__ = ("_store", "_lock", "_ttl", "_max_size", "hits", "misses")

    def __init__(self, ttl_seconds: float, max_size: int) -> None:
        self._store: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self._lock = threading.RLock()
        self._ttl = ttl_seconds
        self._max_size = max_size
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> tuple[bool, object]:
        """Returns (hit, value). Evicts expired entries lazily."""
        now = time.monotonic()
        with self._lock:
            if key not in self._store:
                self.misses += 1
                return (False, None)
            expires_at, value = self._store[key]
            if expires_at < now:
                del self._store[key]
                self.misses += 1
                return (False, None)
            # Move to end (LRU)
            self._store.move_to_end(key)
            self.hits += 1
            return (True, value)

    def put(self, key: tuple, value: object) -> None:
        expires_at = time.monotonic() + self._ttl
        with self._lock:
            self._store[key] = (expires_at, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "size": len(self._store),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
            }


def ttl_cache(ttl_seconds: float = 5.0, max_size: int = 256) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator: cache a function's return for `ttl_seconds` keyed on args.

    - `self` is INCLUDED in the key for methods (one cache per instance-args tuple).
    - kwargs must be hashable (rare in hot-path callers); they become part of the key.
    - Exposes `.cache_stats()` and `.cache_clear()` on the wrapped function.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        store = _CacheStore(ttl_seconds=ttl_seconds, max_size=max_size)

        @wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> T:
            key = args + tuple(sorted(kwargs.items()))
            try:
                hash(key)  # Validate hashability BEFORE touching the store
            except TypeError:
                # Unhashable arg: bypass cache entirely
                return fn(*args, **kwargs)
            hit, value = store.get(key)
            if hit:
                return value  # type: ignore[return-value]
            result = fn(*args, **kwargs)
            store.put(key, result)
            return result

        wrapper.cache_stats = store.stats  # type: ignore[attr-defined]
        wrapper.cache_clear = store.clear  # type: ignore[attr-defined]
        return wrapper

    return decorator
