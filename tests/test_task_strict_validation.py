"""E3: Task.from_dict(strict=True) validation tests.

Covers the input-validation hardening from
<hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase E3.

Lax mode (default) preserves pre-E3 behavior: malformed input gets
sensible defaults (uuid4 id, empty title, pending state). Strict mode
rejects malformed input with ValueError — opt-in for callers that
need hard validation (e.g., CI pipeline importing task YAMLs).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from task_queue import Task


class TestLaxMode:
    """Lax mode = default = pre-E3 behavior preserved."""

    def test_empty_dict_gets_defaults(self):
        t = Task.from_dict({})
        assert t.id  # uuid4 generated
        assert t.title == ""
        assert t.state == "pending"

    def test_missing_id_defaults_to_uuid(self):
        t = Task.from_dict({"title": "x"})
        # uuid4 default
        assert len(t.id) == 36  # uuid4 format: 8-4-4-4-12

    def test_unknown_state_passes_through(self):
        """Lax mode doesn't validate state — preserves backward compat."""
        t = Task.from_dict({"id": "t1", "title": "x", "state": "wibbly"})
        assert t.state == "wibbly"

    def test_non_dict_input_returns_default_task(self):
        """Lax mode treats non-dict as empty — no crash."""
        t = Task.from_dict("not a dict")  # type: ignore[arg-type]
        assert t.title == ""
        assert t.state == "pending"


class TestStrictMode:
    """Strict mode = opt-in validation for callers that need hard rejection."""

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="expected dict"):
            Task.from_dict("not a dict", strict=True)  # type: ignore[arg-type]

    def test_missing_id_raises(self):
        with pytest.raises(ValueError, match="requires 'id'"):
            Task.from_dict({"title": "x"}, strict=True)

    def test_missing_title_raises(self):
        with pytest.raises(ValueError, match="requires 'title'"):
            Task.from_dict({"id": "t1"}, strict=True)

    def test_invalid_state_raises(self):
        with pytest.raises(ValueError, match="state 'wibbly' not in allowed set"):
            Task.from_dict(
                {"id": "t1", "title": "x", "state": "wibbly"},
                strict=True,
            )

    def test_valid_payload_passes_strict(self):
        t = Task.from_dict(
            {"id": "t1", "title": "Build the thing", "state": "pending"},
            strict=True,
        )
        assert t.id == "t1"
        assert t.title == "Build the thing"
        assert t.state == "pending"

    def test_all_valid_states_accepted(self):
        for state in ("pending", "claimed", "running", "completed", "failed"):
            t = Task.from_dict(
                {"id": "t1", "title": "x", "state": state}, strict=True
            )
            assert t.state == state

    def test_empty_id_counts_as_missing(self):
        """Empty string id should be treated as missing in strict mode."""
        with pytest.raises(ValueError, match="requires 'id'"):
            Task.from_dict({"id": "", "title": "x"}, strict=True)

    def test_none_id_counts_as_missing(self):
        with pytest.raises(ValueError, match="requires 'id'"):
            Task.from_dict({"id": None, "title": "x"}, strict=True)
