"""Pipeline registry — maps pipeline names to Pipeline objects."""

# plan-approved: claude-swarm-scripts
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import Pipeline
from pipelines.bug_fix import BUG_FIX
from pipelines.feature_build import FEATURE_BUILD
from pipelines.question_generation import QUESTION_GEN
from pipelines.security_audit import SECURITY_AUDIT

# Lazily import optional pipelines to avoid import errors during development
try:
    from pipelines.fleet_capability_index import FLEET_CAPABILITY_INDEX

    _FLEET_CAPABILITY_INDEX_AVAILABLE = True
except ImportError:
    _FLEET_CAPABILITY_INDEX_AVAILABLE = False

PIPELINES: dict[str, Pipeline] = {
    "feature-build": FEATURE_BUILD,
    "bug-fix": BUG_FIX,
    "security-audit": SECURITY_AUDIT,
    "question-gen": QUESTION_GEN,
}

if _FLEET_CAPABILITY_INDEX_AVAILABLE:
    PIPELINES["fleet-capability-index"] = FLEET_CAPABILITY_INDEX


def get_pipeline(name: str) -> Pipeline:
    """Return a named pipeline, raising ValueError if not found."""
    if name not in PIPELINES:
        raise ValueError(f"Unknown pipeline: {name!r}. Available: {sorted(PIPELINES.keys())}")
    return PIPELINES[name]


def list_pipelines() -> list[dict]:
    """Return summary info for all registered pipelines."""
    return [
        {
            "name": p.name,
            "description": p.description,
            "stages": [s.name for s in p.stages],
            "timeout_minutes": p.timeout_minutes,
        }
        for p in PIPELINES.values()
    ]
