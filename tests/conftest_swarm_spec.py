"""Swarm Spec conformance fixture registration for claude-swarm.

Pytest conftest addon that instantiates concrete implementations
for the Swarm Spec protocol suite.
"""

import sys
from pathlib import Path

import pytest

# Add swarm_spec to path
SPEC_PATH = Path(__file__).parent.parent.parent / "hydra-project" / "specs" / "swarm"
if str(SPEC_PATH) not in sys.path:
    sys.path.insert(0, str(SPEC_PATH))

# Add swarm_spec_impl (in this repo) to path
IMPL_PATH = Path(__file__).parent.parent
if str(IMPL_PATH) not in sys.path:
    sys.path.insert(0, str(IMPL_PATH))


@pytest.fixture
def task_queue_impl():
    """Fixture: instantiate claude-swarm's TaskQueueBackend adapter."""
    try:
        from swarm_spec_impl.task_queue import ClaudeSwarmTaskQueueAdapter

        return ClaudeSwarmTaskQueueAdapter()
    except ImportError:
        pytest.skip("ClaudeSwarmTaskQueueAdapter not available")
