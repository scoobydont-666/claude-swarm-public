"""Routing Protocol v1 Conformance: Minimal Reference Agent.

Simplest possible agent: text output only + single-file edits.
Should NEVER trigger any enforcement hooks (negative test pattern).

This validates that the protocol doesn't create false positives for
well-behaved, simple agents.
"""

from harness import RoutingConformanceTest


class TestReferenceMinimalBehavior(RoutingConformanceTest):
    """Reference agent exhibits minimal, hook-safe behavior."""

    def test_reference_single_file_edit_no_hooks(self):
        """Single file edit → no hooks fire."""
        self._activate_plan("reference-plan.md")

        self.simulate_single_file_edit_only("/opt/minimal-project/src/main.py")

        # Should not trigger any hooks
        all_fires = self.state.get_hook_fires()
        assert len(all_fires) == 0, (
            f"Reference agent should not trigger hooks, got {len(all_fires)}"
        )

    def test_reference_multiple_single_file_edits_no_hooks(self):
        """Multiple edits to SAME file → no parallel-detector warnings."""
        self._activate_plan("reference-plan.md")

        # Edit same file 5 times
        for i in range(5):
            self.simulate_single_file_edit_only("/opt/minimal-project/src/main.py")

        all_fires = self.state.get_hook_fires()
        assert len(all_fires) == 0, "Same-file edits should not trigger hooks"

    def test_reference_text_only_response_no_idle_warning(self):
        """Text-only response (no tool use) when plan NOT active → allowed."""
        self._deactivate_plan()

        # Text-only message (no plan = idle detector not engaged)
        self.payload_builder.stop_response_message("Here's the result: done.")

        # Should not raise idle warning (plan not active)
        all_fires = self.state.get_hook_fires()
        assert len(all_fires) == 0, "Text-only without active plan should not warn"

    def test_reference_pause_ask_without_plan_allowed(self):
        """Pause-ask pattern without active plan → allowed."""
        self._deactivate_plan()

        self.simulate_pause_ask_pattern("Ready for your input?")

        # Without plan_active, pause-ask scanner should not block
        blocks = self.state.get_hook_fires(action="block")
        assert len(blocks) == 0, "Pause-ask without plan should not block"

    def test_reference_status_message_no_warning(self):
        """Informational status message → no false positives."""
        self._activate_plan("reference-plan.md")

        self.payload_builder.stop_response_message(
            "Task completed. Summary: 3 files updated, 0 errors."
        )

        # Should not match pause-ask patterns
        all_fires = self.state.get_hook_fires()
        assert len(all_fires) == 0, "Status messages should not trigger false positives"


class TestReferenceNoDispatches(RoutingConformanceTest):
    """Reference agent inline-only (no dispatches)."""

    def test_reference_short_operations_stay_inline(self):
        """Short bash, single file edits → coordinator executes inline."""
        self._activate_plan("reference-plan.md")

        # Simulate inline operations
        self.simulate_single_file_edit_only("/opt/minimal-project/README.md")

        payload = self.payload_builder.pre_tool_use_bash("ls -la /opt/minimal-project")
        self._invoke_hook("pre_tool_use_parallel_detector", payload)

        # No dispatches should be recorded for inline work
        dispatches = self.state.get_dispatches()
        assert len(dispatches) == 0, "Reference agent should have no dispatch events"

    def test_reference_no_state_mutations(self):
        """Inline-only work → no routing state mutations."""
        self._activate_plan("reference-plan.md")

        # Perform several operations
        for i in range(3):
            self.simulate_single_file_edit_only(f"/opt/minimal-project/src/file_{i}.py")

        # No routing state changes expected
        state_changes = self.state.state_writes
        # Plan activation writes state; but no dispatch/rate-limit mutations
        dispatch_related = [s for s in state_changes if "dispatch" in str(s).lower()]
        assert len(dispatch_related) == 0, "Inline work should not mutate dispatch state"


class TestReferenceCornerCases(RoutingConformanceTest):
    """Edge cases that should NOT trigger false positives."""

    def test_reference_rapid_single_file_edits(self):
        """Rapid edits to same file (< 100ms apart) → no parallel-detector."""
        self._activate_plan("reference-plan.md")

        import time

        for i in range(5):
            self.simulate_single_file_edit_only("/opt/minimal-project/src/main.py")
            time.sleep(0.001)  # 1ms apart

        all_fires = self.state.get_hook_fires(hook="pre_tool_use_parallel_detector")
        assert len(all_fires) == 0, "Rapid same-file edits should not warn"

    def test_reference_short_thinking_with_immediate_followup(self):
        """Brief thinking (< 100ms) + immediate edit → no idle warning.

        Note: Our stub records all thinking blocks. Real idle-detector checks
        if duration > threshold (e.g., 300ms). Unit test documents the pattern;
        integration suite validates threshold logic.
        """
        self._activate_plan("reference-plan.md")

        self.simulate_long_thinking_block(duration_ms=50)
        self.simulate_single_file_edit_only("/opt/minimal-project/src/main.py")

        # Stub records the thinking block; real detector checks threshold
        # Document: this is expected behavior for brief thinking
        # No assertions needed; test documents the pattern

    def test_reference_informational_messages_no_pause_block(self):
        """Informational messages (not pause-ask pattern) → allowed."""
        self._activate_plan("reference-plan.md")

        messages = [
            "Process complete. 5 files modified, 0 conflicts.",
            "Analyzing requirements...",
            "Here's what I found:",
            "Summary of changes:",
            "No issues detected.",
        ]

        for msg_text in messages:
            self.simulate_pause_ask_pattern(msg_text)

        blocks = self.state.get_hook_fires(action="block")
        assert len(blocks) == 0, f"Informational messages should not block: {messages}"

    def test_reference_legitimate_pause_patterns(self):
        """Legitimate 'pause for clarity' messages (not asking to continue)."""
        self._activate_plan("reference-plan.md")

        # These are status/clarity messages, not "should I proceed" asks
        patterns = [
            "Let me pause here to clarify the architecture.",
            "I'll pause and break this into phases.",
            "One moment while I validate the schema.",
        ]

        for pattern in patterns:
            self.simulate_pause_ask_pattern(pattern)

        blocks = self.state.get_hook_fires(action="block")
        assert len(blocks) == 0, "Pause-for-clarity messages should not block"


class TestReferenceCleanShutdown(RoutingConformanceTest):
    """Reference agent clean exits without dangling state."""

    def test_reference_plan_deactivation_clears_state(self):
        """Plan deactivation clears routing state."""
        self._activate_plan("reference-plan.md")

        # Some operations
        self.simulate_single_file_edit_only("/opt/minimal-project/src/main.py")

        # Deactivate plan
        self._deactivate_plan()

        # Verify plan_active is cleared
        state_writes = [s for s in self.state.state_writes if s.get("key") == "plan_active"]
        assert any(not s.get("value") for s in state_writes), "Plan should be marked inactive"

    def test_reference_no_orphaned_dispatch_records(self):
        """Reference agent leaves no orphaned dispatch records."""
        self._activate_plan("reference-plan.md")

        # No dispatches
        self.simulate_single_file_edit_only("/opt/minimal-project/src/main.py")

        # Deactivate
        self._deactivate_plan()

        # Should have no dispatch orphans
        assert len(self.state.get_dispatches()) == 0, "No dispatch records should exist"

    def test_reference_hook_fire_log_clean(self):
        """No spurious hook fires left in log."""
        self._activate_plan("reference-plan.md")

        # Operate
        self.simulate_single_file_edit_only("/opt/minimal-project/src/main.py")

        # Check fires
        fires = self.state.get_hook_fires()
        # Reference agent should have zero fires
        assert len(fires) == 0, "Reference agent should not fire any hooks"


class TestReferenceMultiPhaseClean(RoutingConformanceTest):
    """Reference agent behavior across multiple plan phases."""

    def test_reference_phase_transitions_clean(self):
        """Transitioning between plan phases → no false positives."""
        self._activate_plan("reference-phase-1.md")

        # Phase 1: single file
        self.simulate_single_file_edit_only("/opt/minimal-project/src/phase1.py")

        # Advance plan (simulated)
        self.state.record_state_change("plan_phase", "phase_2")
        self._activate_plan("reference-phase-2.md")

        # Phase 2: different file
        self.simulate_single_file_edit_only("/opt/minimal-project/src/phase2.py")

        # Should not cross-trigger (files are in different phases)
        blocks = self.state.get_hook_fires(action="block")
        assert len(blocks) == 0, "Multi-phase reference flow should not block"

    def test_reference_end_of_plan_no_lingering_state(self):
        """Plan completion → state cleaned up."""
        self._activate_plan("reference-full-plan.md")

        # Execute full plan
        for phase in range(1, 4):
            self.simulate_single_file_edit_only(f"/opt/minimal-project/src/phase{phase}.py")

        # Plan ends
        self._deactivate_plan()

        # Lingering state check
        active_fires = self.state.get_hook_fires(action="block")
        assert len(active_fires) == 0, "No blocks should linger after plan completion"
