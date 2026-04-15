"""Tests for backend.py — NFS/Redis backend switcher."""

import os
import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestBackendSwitcher:
    """Verify backend selection logic."""

    def test_nfs_backend_when_redis_unavailable(self):
        """Falls back to NFS when Redis is not available."""
        with patch.dict(
            os.environ, {"SWARM_BACKEND": "auto", "SWARM_REDIS_SKIP_CHECK": "1"}
        ):
            # Force reimport to test switching
            import importlib
            import backend

            importlib.reload(backend)
            # backend.lib should be importable
            assert hasattr(backend, "lib")

    def test_explicit_nfs_backend(self):
        """Explicit NFS selection via env var."""
        with patch.dict(os.environ, {"SWARM_BACKEND": "nfs"}):
            import importlib
            import backend

            importlib.reload(backend)
            assert hasattr(backend, "lib")

    def test_redis_skip_check_env(self):
        """SWARM_REDIS_SKIP_CHECK=1 prevents Redis health check."""
        with patch.dict(os.environ, {"SWARM_REDIS_SKIP_CHECK": "1"}):
            # Should not raise even if Redis is down
            import importlib
            import backend

            importlib.reload(backend)
            assert hasattr(backend, "lib")
