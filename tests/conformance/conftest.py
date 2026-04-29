"""Pytest configuration for routing protocol conformance tests.

Sets up:
  - sys.path to include tests/conformance/ (harness.py)
  - sys.path to include ~/.claude/hooks/lib/ (routing_common, routing_state_db, routing_metrics)
  - CLAUDE_ROUTING_DB tmpdir fixture
  - Hooks lib import compatibility
"""

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_import_paths():
    """Add conformance dir and hooks lib to sys.path."""
    conformance_dir = Path(__file__).resolve().parent
    if str(conformance_dir) not in sys.path:
        sys.path.insert(0, str(conformance_dir))

    hooks_lib = Path.home() / ".claude" / "hooks" / "lib"
    if str(hooks_lib) not in sys.path:
        sys.path.insert(0, str(hooks_lib))


@pytest.fixture
def claude_routing_db(tmp_path):
    """Provide a tempdir SQLite DB path for each test.

    Tests inherit RoutingConformanceTest which sets up RoutingStateStub(db_path).
    This fixture ensures DB isolation between test runs.
    """
    db_path = tmp_path / "routing.db"
    os.environ["CLAUDE_ROUTING_DB"] = str(db_path)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture(autouse=True)
def reset_routing_state(tmp_path):
    """Reset routing state dirs before each test (plan-active, recent-edits, etc.)."""
    routing_tmp = Path("/tmp/claude-routing-state")
    if routing_tmp.exists():
        import shutil

        try:
            shutil.rmtree(routing_tmp)
        except Exception:
            pass
    routing_tmp.mkdir(parents=True, exist_ok=True)
    yield
    # Cleanup after test
    try:
        import shutil

        shutil.rmtree(routing_tmp)
    except Exception:
        pass
