"""Backend switcher for claude-swarm.

Selects Redis or NFS backend based on SWARM_BACKEND env var and Redis availability.
Usage:
    from backend import lib, registry, events, gpu_slots

This provides the correct module regardless of backend selection.
"""

import os

_BACKEND = os.environ.get("SWARM_BACKEND", "auto")


def _redis_available() -> bool:
    """Check if Redis is available."""
    try:
        import redis_client

        return redis_client.health_check()
    except (ImportError, Exception):
        return False


def _use_redis() -> bool:
    """Determine if Redis backend should be used."""
    if _BACKEND == "nfs":
        return False
    if _BACKEND == "redis":
        return _redis_available()
    # auto: use Redis if available, fallback to NFS
    return _redis_available()


if _use_redis():
    try:
        import swarm_redis as lib
        import registry_redis as registry
        import events_redis as events
        import gpu_slots_redis as gpu_slots
    except ImportError:
        from src import swarm_redis as lib
        from src import registry_redis as registry
        from src import events_redis as events
        from src import gpu_slots_redis as gpu_slots
    BACKEND = "redis"
else:
    try:
        import swarm_lib as lib
        import registry
        import events
        import gpu_slots
    except ImportError:
        pass
    BACKEND = "nfs"
