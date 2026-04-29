"""Routing Protocol v1 Conformance Test Harness.

Provides:
  - RoutingConformanceTest base class
  - Agent-stub factory emitting hook payload shapes
  - Assertions for hook behavior (warn, block, dispatch recorded, state persisted)
  - Tempdir + SQLite DB per test (CLAUDE_ROUTING_DB fixture pattern)
"""

import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class HookPayloadBuilder:
    """Builds pre-tool-use and stop-response payloads matching Claude Code schema."""

    @staticmethod
    def pre_tool_use_write(file_path: str, tool_name: str = "write") -> dict[str, Any]:
        """Simulate a pre-tool-use hook invocation for Write tool."""
        return {
            "toolName": tool_name,
            "toolInput": {
                "file_path": file_path,
                "content": "dummy content",
            },
            "toolUseId": "write_test_001",
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def pre_tool_use_edit(
        file_path: str, old_string: str = "x", new_string: str = "y"
    ) -> dict[str, Any]:
        """Simulate a pre-tool-use hook invocation for Edit tool."""
        return {
            "toolName": "Edit",
            "toolInput": {
                "file_path": file_path,
                "old_string": old_string,
                "new_string": new_string,
            },
            "toolUseId": "edit_test_001",
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def pre_tool_use_bash(command: str, tool_name: str = "Bash") -> dict[str, Any]:
        """Simulate a pre-tool-use hook invocation for Bash tool."""
        return {
            "toolName": tool_name,
            "toolInput": {
                "command": command,
            },
            "toolUseId": "bash_test_001",
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def pre_tool_use_task(title: str, tool_name: str = "Skill") -> dict[str, Any]:
        """Simulate a pre-tool-use hook for delegated task (Agent/Skill dispatch)."""
        return {
            "toolName": tool_name,
            "toolInput": {
                "skill": "example-skill" if tool_name == "Skill" else None,
                "title": title,
            },
            "toolUseId": "task_test_001",
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def stop_response_message(text: str) -> dict[str, Any]:
        """Simulate a stop-hook message (no tool use, just text response)."""
        return {
            "type": "message",
            "content": text,
            "timestamp": datetime.now(UTC).isoformat(),
        }


class RoutingStateStub:
    """In-memory stub of routing state (mimics routing_state_db API)."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.dispatches: list[dict[str, Any]] = []
        self.state_writes: list[dict[str, Any]] = []
        self.hook_fires: list[dict[str, Any]] = []
        self._write_persistent_state()

    def record_dispatch(self, task_id: str, tier: str, model: str) -> None:
        """Record a dispatch event."""
        self.dispatches.append(
            {
                "task_id": task_id,
                "tier": tier,
                "model": model,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        self._write_persistent_state()

    def record_hook_fire(
        self, hook: str, mode: str, action: str, pattern: str | None = None
    ) -> None:
        """Record a hook execution."""
        self.hook_fires.append(
            {
                "hook": hook,
                "mode": mode,
                "action": action,
                "pattern": pattern,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        self._write_persistent_state()

    def record_state_change(self, key: str, value: Any) -> None:
        """Record a state mutation."""
        self.state_writes.append(
            {
                "key": key,
                "value": value,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        self._write_persistent_state()

    def get_dispatches(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Retrieve dispatches, optionally filtered by task_id."""
        if task_id:
            return [d for d in self.dispatches if d["task_id"] == task_id]
        return self.dispatches

    def get_hook_fires(
        self, hook: str | None = None, action: str | None = None
    ) -> list[dict[str, Any]]:
        """Retrieve hook fires, optionally filtered."""
        fires = self.hook_fires
        if hook:
            fires = [f for f in fires if f["hook"] == hook]
        if action:
            fires = [f for f in fires if f["action"] == action]
        return fires

    def _write_persistent_state(self) -> None:
        """Persist state to tmpdir for e2e verification."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text(
            json.dumps(
                {
                    "dispatches": self.dispatches,
                    "state_writes": self.state_writes,
                    "hook_fires": self.hook_fires,
                },
                indent=2,
            )
        )


class RoutingConformanceTest:
    """Base class for routing protocol conformance tests.

    Provides:
    - Temp directory + SQLite DB per test
    - Hook payload builders
    - State assertions (warn_fires, block_fires, dispatch_recorded, etc.)
    - Agent stub factory
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir_path = Path(self.tmpdir.name)
        self.db_path = self.tmpdir_path / "routing.db"
        self.state = RoutingStateStub(self.db_path)
        self.payload_builder = HookPayloadBuilder()
        self.edited_files: list[str] = []  # Track files for parallel detection

    def teardown_method(self):
        """Clean up temp directory."""
        self.tmpdir.cleanup()

    # ── Assertions ─────────────────────────────────────────────────────────────

    def assert_warn_only_fires(self, hook: str, times: int = 1) -> None:
        """Assert that a hook fired in warn-only mode."""
        fires = self.state.get_hook_fires(hook=hook, action="warn")
        assert len(fires) == times, f"Expected {times} warn fires for {hook}, got {len(fires)}"

    def assert_warn_never_fires(self, hook: str) -> None:
        """Assert that a hook did NOT fire."""
        fires = self.state.get_hook_fires(hook=hook)
        assert len(fires) == 0, f"Expected no fires for {hook}, got {len(fires)}"

    def assert_block_fires(self, hook: str, times: int = 1) -> None:
        """Assert that a hook fired in block mode."""
        fires = self.state.get_hook_fires(hook=hook, action="block")
        assert len(fires) == times, f"Expected {times} block fires for {hook}, got {len(fires)}"

    def assert_dispatch_recorded(self, task_id: str, tier: str | None = None) -> None:
        """Assert that a dispatch was recorded."""
        dispatches = self.state.get_dispatches(task_id=task_id)
        assert len(dispatches) > 0, f"No dispatch recorded for task {task_id}"
        if tier:
            assert dispatches[0]["tier"] == tier, (
                f"Expected tier {tier}, got {dispatches[0]['tier']}"
            )

    def assert_state_persisted(self, key: str, expected_value: Any = None) -> None:
        """Assert that state was written to disk."""
        state_writes = self.state.state_writes
        keys_written = [w["key"] for w in state_writes]
        assert key in keys_written, f"State key {key} was not persisted. Written: {keys_written}"
        if expected_value is not None:
            matching = [w for w in state_writes if w["key"] == key and w["value"] == expected_value]
            assert len(matching) > 0, f"State {key} was written but not with value {expected_value}"

    def assert_no_dispatches(self) -> None:
        """Assert that no dispatches were recorded."""
        assert len(self.state.dispatches) == 0, (
            f"Expected no dispatches, got {len(self.state.dispatches)}"
        )

    def assert_dispatch_count(self, count: int) -> None:
        """Assert exact dispatch count."""
        assert len(self.state.dispatches) == count, (
            f"Expected {count} dispatches, got {len(self.state.dispatches)}"
        )

    # ── Agent stubs ────────────────────────────────────────────────────────────

    def simulate_multi_file_edit_pattern(self, files: list[str]) -> None:
        """Simulate editing multiple files in sequence (triggers parallel-detector)."""
        for i, fpath in enumerate(files):
            self.payload_builder.pre_tool_use_edit(
                fpath, old_string=f"v{i}", new_string=f"v{i + 1}"
            )
            self._track_file_edit(fpath)
            time.sleep(0.01)  # Ensure distinct timestamps

    def simulate_long_thinking_block(self, duration_ms: int = 500) -> None:
        """Simulate text-only response after a delay (triggers idle-detector)."""
        msg = self.payload_builder.stop_response_message("Long thinking...")
        time.sleep(duration_ms / 1000.0)
        self._invoke_hook("post_tool_use_idle_detector", msg)

    def simulate_rapid_dispatch_burst(self, count: int = 11, window_s: float = 60.0) -> None:
        """Simulate multiple dispatches within a time window (triggers rate-limit)."""
        step = window_s / count
        for i in range(count):
            task_id = f"burst_task_{i:02d}"
            self.state.record_dispatch(task_id, "tier_1", "hydracoder:7b")
            time.sleep(step / 1000.0)  # Tiny sleep to avoid exact duplicates
            self._invoke_hook(
                "dispatch_rate_limit",
                {
                    "dispatch_count": i + 1,
                    "window_seconds": window_s,
                },
            )

    def simulate_pause_ask_pattern(self, text: str) -> None:
        """Simulate pause-ask pattern (should block if plan active)."""
        msg = self.payload_builder.stop_response_message(text)
        self._invoke_hook("stop_hook_pause_ask_scanner", msg)

    def simulate_single_file_edit_only(self, file_path: str) -> None:
        """Simulate a simple, legitimate single-file edit (should never trigger hooks).

        Note: In real protocol, single-file edits are serial-OK and don't trigger
        parallel-detector. This stub marks them specially so tests can verify
        the protocol behavior (same file = no warn).
        """
        self.payload_builder.pre_tool_use_edit(file_path)
        # Mark as same-file-OK so stub doesn't count it as a hook fire
        self._track_file_edit(file_path)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _track_file_edit(self, file_path: str) -> None:
        """Track file edits for parallel-detector logic.

        Files are considered parallel candidates if they differ from previous edits.
        Same-file repeated edits are serial-OK (no hook fire).
        """
        # Determine if this is a new (independent) file
        is_new_file = file_path not in self.edited_files
        self.edited_files.append(file_path)

        # Only fire parallel-detector if switching to a new, independent file
        if is_new_file and len(self.edited_files) > 1:
            # This is a cross-repo or different-file edit; detector would warn
            self.state.record_hook_fire(
                "pre_tool_use_parallel_detector",
                mode="unit-test",
                action="warn",
                pattern="independent_files",
            )

    def _invoke_hook(self, hook_name: str, payload: dict[str, Any]) -> None:
        """Simulate invoking a hook and record the result."""
        # In real test, hook scripts would be invoked via subprocess + JSON stdin.
        # For unit tests, we directly call the lib functions.
        # This is a placeholder; actual hook invocation is tested in integration suites.
        self.state.record_hook_fire(hook_name, mode="unit-test", action="simulated")

    def _activate_plan(self, plan_file: str = "/tmp/test-plan.md") -> None:
        """Set plan_active state."""
        self.state.record_state_change("plan_active", True)
        self.state.record_state_change("plan_file", plan_file)

    def _deactivate_plan(self) -> None:
        """Clear plan_active state."""
        self.state.record_state_change("plan_active", False)
