"""Integration test conftest — fixtures for tests that touch real infra.

Backport from /opt/nai-swarm/tests/integration/conftest.py (P3 item 18),
adapted to claude-swarm's stack (Redis Streams + NFS + Celery).
"""

import os
import socket

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-mark everything in tests/integration/ with @pytest.mark.integration."""
    for item in items:
        if "tests/integration/" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def live_redis_available() -> bool:
    """Return True if a Redis instance is reachable at SWARM_REDIS_HOST:port.

    Integration tests that require live Redis should skip if this is False.
    """
    import redis

    host = os.environ.get("SWARM_REDIS_HOST", "127.0.0.1")
    port = int(os.environ.get("SWARM_REDIS_PORT", "6379"))
    password = os.environ.get("SWARM_REDIS_PASSWORD") or None
    try:
        c = redis.Redis(host=host, port=port, password=password, socket_timeout=1)
        c.ping()
        return True
    except (redis.ConnectionError, redis.AuthenticationError, OSError):
        return False


@pytest.fixture(scope="session")
def live_nfs_available() -> bool:
    """Return True if /opt/swarm/ is mounted (NFS primary or local)."""
    return os.path.ismount("/opt/swarm") or os.path.isdir("/opt/swarm/artifacts")


@pytest.fixture
def free_tcp_port() -> int:
    """Bind to port 0, read assigned port, release — lets caller bind without collision."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def skip_without_redis(live_redis_available):
    """Skip the test if live Redis is unreachable."""
    if not live_redis_available:
        pytest.skip("Live Redis not reachable — set SWARM_REDIS_* or start miniboss:6379")
