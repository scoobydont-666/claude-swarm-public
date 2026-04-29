"""Tests for ttl_cache (F5 — hot-path caching).

Covers:
- Cache hit within TTL returns stored value
- Cache miss after TTL expiry re-invokes the function
- LRU eviction at max_size
- Exceptions propagate and are NOT cached
- Thread-safety under concurrent access
- Method usage (self as part of key)
- Unhashable args bypass cache rather than crash
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ttl_cache import ttl_cache  # noqa: E402


def test_hit_within_ttl():
    calls = []

    @ttl_cache(ttl_seconds=1.0, max_size=10)
    def read(key):
        calls.append(key)
        return f"val-{key}"

    assert read("a") == "val-a"
    assert read("a") == "val-a"  # hit
    assert read("a") == "val-a"  # hit
    assert len(calls) == 1, "identical calls within TTL should hit cache once"
    stats = read.cache_stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1


def test_miss_after_ttl_expiry():
    calls = []

    @ttl_cache(ttl_seconds=0.05, max_size=10)
    def read(key):
        calls.append(key)
        return len(calls)

    assert read("x") == 1
    time.sleep(0.1)
    assert read("x") == 2, "after TTL expiry the function should re-run"


def test_lru_eviction():
    calls = []

    @ttl_cache(ttl_seconds=60.0, max_size=2)
    def read(key):
        calls.append(key)
        return key

    read("a")
    read("b")
    read("a")  # bumps 'a' to most-recent
    read("c")  # should evict 'b' (least-recent), NOT 'a'
    assert read("a") == "a"  # still cached
    read("b")  # should re-fetch (was evicted)
    assert calls == ["a", "b", "c", "b"]


def test_exception_not_cached():
    attempt = [0]

    @ttl_cache(ttl_seconds=10.0)
    def flaky():
        attempt[0] += 1
        if attempt[0] < 3:
            raise RuntimeError("boom")
        return "ok"

    try:
        flaky()
    except RuntimeError:
        pass
    try:
        flaky()
    except RuntimeError:
        pass
    assert flaky() == "ok"
    assert attempt[0] == 3, "exceptions must not poison the cache"


def test_distinct_args_get_distinct_entries():
    calls = []

    @ttl_cache(ttl_seconds=10.0)
    def query(q):
        calls.append(q)
        return f"result-{q}"

    query("up")
    query("rate")
    query("up")  # hit
    assert calls == ["up", "rate"]


def test_method_cache_per_instance():
    """Methods: `self` is part of key, so two instances don't share cache."""

    class Reader:
        def __init__(self, label):
            self.label = label

        @ttl_cache(ttl_seconds=10.0)
        def read(self, key):
            return f"{self.label}:{key}"

    a = Reader("a")
    b = Reader("b")
    assert a.read("x") == "a:x"
    assert b.read("x") == "b:x"  # must NOT get a's cached result


def test_unhashable_args_bypass_cache():
    calls = []

    @ttl_cache(ttl_seconds=10.0)
    def read(d):
        calls.append(d)
        return len(d)

    assert read({"a": 1, "b": 2}) == 2
    assert read({"a": 1, "b": 2}) == 2
    # dict is unhashable → bypass → both calls hit the function
    assert len(calls) == 2


def test_thread_safety_under_concurrency():
    """Many threads hitting the same key should see one computation."""
    compute_count = [0]
    lock = threading.Lock()

    @ttl_cache(ttl_seconds=60.0)
    def compute(k):
        with lock:
            compute_count[0] += 1
        time.sleep(0.02)  # simulate work
        return k * 2

    # Warm the cache with one call first
    compute(7)
    assert compute_count[0] == 1

    # Now 20 threads all calling with same key — all should hit cache
    results = []
    rlock = threading.Lock()

    def worker():
        r = compute(7)
        with rlock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r == 14 for r in results)
    assert compute_count[0] == 1, "all 20 concurrent reads should hit the warm cache"


def test_cache_clear():
    calls = []

    @ttl_cache(ttl_seconds=60.0)
    def read(k):
        calls.append(k)
        return k

    read("a")
    read("a")  # hit
    read.cache_clear()
    read("a")  # miss again after clear
    assert calls == ["a", "a"]


def test_cache_stats_hit_rate():
    @ttl_cache(ttl_seconds=60.0)
    def read(k):
        return k

    read("a")
    read("a")
    read("a")
    read("b")
    stats = read.cache_stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 2
    assert stats["hit_rate"] == 0.5


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
