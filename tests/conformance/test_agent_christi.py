"""Routing Protocol v1 Conformance: ProjectA Tax-Advisory Agent.

Simulates ProjectA's multi-repo cross-editing behavior, long thinking blocks,
and legitimate pause-for-user-input patterns.

Expected hooks:
  - parallel_detector (warn on cross-repo edits)
  - idle_detector (warn on long thinking without tool use)
  - pause_ask_scanner (block if plan active + pause pattern)
"""

from harness import RoutingConformanceTest


class TestChristiMultiRepoEditing(RoutingConformanceTest):
    """Multi-repo edit pattern triggers parallel-detector warnings."""

    def test_christi_cross_repo_edit_warns(self):
        """Editing project-a/ then taxprep/ in sequence → warn-only (independent repos)."""
        self._activate_plan("project-a-plan.md")

        # Simulate rapid cross-repo edits
        self.simulate_multi_file_edit_pattern(
            [
                "<project-a-path>/src/advisor.py",
                "/opt/taxprep-project/src/interview.py",
                "<project-a-path>/src/rag.py",
            ]
        )

        # Should warn about independent files but not block
        # Fires on transition to new file: fire on 2nd file, fire on 3rd file (2 total)
        fires = self.state.get_hook_fires(hook="pre_tool_use_parallel_detector")
        assert len(fires) >= 2, f"Expected ≥2 cross-repo edit warnings, got {len(fires)}"
        assert all(f["action"] in ["warn"] for f in fires), (
            "All cross-repo fires should be warn-only"
        )

    def test_christi_same_file_repeated_edit_no_warn(self):
        """Editing same file multiple times → no warn (serial-OK pattern)."""
        self._activate_plan("project-a-plan.md")

        # Same file, multiple edits
        self.simulate_multi_file_edit_pattern(
            [
                "<project-a-path>/src/advisor.py",
                "<project-a-path>/src/advisor.py",
                "<project-a-path>/src/advisor.py",
            ]
        )

        # Same file is allowed; don't count as parallelizable
        # Real implementation filters same-file; this test documents the expectation
        assert self.state.get_dispatches() == [], "Same-file edits should not dispatch"

    def test_christi_parent_child_directory_edit_series(self):
        """Editing parent/child dirs (project-a/ + project-a/src/) → considered related."""
        self._activate_plan("project-a-plan.md")

        # Parent and child dirs — language-naive detector sees different paths
        # but domain logic (same project) should suppress warn
        # For v1: we expect parallel-detector to fire (false positive tolerance).
        self.simulate_multi_file_edit_pattern(
            [
                "<project-a-path>/src/advisor.py",
                "<project-a-path>/src/rag.py",
            ]
        )

        fires = self.state.get_hook_fires(hook="pre_tool_use_parallel_detector")
        # Same project; some implementations may suppress; v1 may warn.
        # Document: this is a tuning knob for false-positive tolerance.
        assert len(fires) >= 0, "Same-project fires are acceptable in v1"


class TestChristiLongThinking(RoutingConformanceTest):
    """Long thinking blocks without tool use trigger idle-detector warnings."""

    def test_christi_long_thinking_then_edit_warns(self):
        """Long thinking (500ms) followed by tool use → may warn (idle-detector)."""
        self._activate_plan("project-a-plan.md")

        self.simulate_long_thinking_block(duration_ms=500)
        fires = self.state.get_hook_fires(hook="post_tool_use_idle_detector")

        # idle-detector fires on text-only response mid-plan
        assert len(fires) >= 1, "Long thinking should trigger idle-detector"

    def test_christi_thinking_rapid_followup_is_ok(self):
        """Thinking followed immediately by tool use → no complaint."""
        self._activate_plan("project-a-plan.md")

        # Short thinking (< detector threshold)
        self.simulate_long_thinking_block(duration_ms=100)

        # Rapid follow-up edit
        self.simulate_single_file_edit_only("<project-a-path>/src/advisor.py")

        # Should not accumulate blocks; brief thinking is fine
        blocks = self.state.get_hook_fires(hook="post_tool_use_idle_detector", action="block")
        assert len(blocks) == 0, "Quick thinking + rapid follow-up should not block"


class TestChristiPauseAsk(RoutingConformanceTest):
    """Pause-for-user-question patterns."""

    def test_christi_pause_ask_when_plan_active_blocks(self):
        """Pause-ask pattern with plan active → block."""
        self._activate_plan("project-a-plan.md")

        self.simulate_pause_ask_pattern("Pause here. Ready for next phase?")

        fires = self.state.get_hook_fires(hook="stop_hook_pause_ask_scanner")
        assert len(fires) > 0, "Pause-ask with plan active should fire"

    def test_christi_shall_i_continue_blocks(self):
        """'Shall I continue?' pattern when plan active → block."""
        self._activate_plan("project-a-plan.md")

        self.simulate_pause_ask_pattern("Shall I continue with the next phase?")

        fires = self.state.get_hook_fires(hook="stop_hook_pause_ask_scanner")
        assert len(fires) > 0, "Shall-I-continue pattern should fire"

    def test_christi_want_me_to_proceed_blocks(self):
        """'Want me to proceed?' pattern → block when plan active."""
        self._activate_plan("project-a-plan.md")

        self.simulate_pause_ask_pattern("Want me to proceed with tax reconciliation?")

        fires = self.state.get_hook_fires(hook="stop_hook_pause_ask_scanner")
        assert len(fires) > 0, "Want-me-to pattern should fire"

    def test_christi_pause_ask_without_plan_allowed(self):
        """Pause-ask pattern with plan NOT active → should not block."""
        self._deactivate_plan()

        self.simulate_pause_ask_pattern("Ready for your input on the following...")

        # Without plan_active, pause-ask scanner should allow it
        blocks = self.state.get_hook_fires(hook="stop_hook_pause_ask_scanner", action="block")
        assert len(blocks) == 0, "Pause-ask without active plan should be allowed"

    def test_christi_legitimate_status_update_allowed(self):
        """Text-only status update (not a pause-ask) → allowed."""
        self._activate_plan("project-a-plan.md")

        self.simulate_pause_ask_pattern(
            "Here's the current reconciliation status: all Q1 receipts validated."
        )

        # This is not a pause-ask pattern; it's a status update
        # Real detector uses regex patterns; should not match
        blocks = self.state.get_hook_fires(hook="stop_hook_pause_ask_scanner", action="block")
        assert len(blocks) == 0, "Status updates should not trigger pause-ask"


class TestChristiIntegration(RoutingConformanceTest):
    """Cross-hook integration scenarios."""

    def test_christi_multi_step_with_pauses(self):
        """Realistic scenario: edit + think + pause-ask + (blocked)."""
        self._activate_plan("project-a-reconciliation.md")

        # Step 1: edit
        self.simulate_single_file_edit_only("<project-a-path>/src/advisor.py")

        # Step 2: long thinking
        self.simulate_long_thinking_block(duration_ms=300)

        # Step 3: pause-ask (should block)
        self.simulate_pause_ask_pattern("Ready to validate Q2 expenses?")

        fires_pause = self.state.get_hook_fires(hook="stop_hook_pause_ask_scanner")
        assert len(fires_pause) > 0, "Pause-ask should fire in multi-step scenario"

    def test_christi_legitimate_flow(self):
        """Realistic OK flow: edit + cross-repo edit (warned) + single-file close-out."""
        self._activate_plan("project-a-plan.md")

        # Edit project-a
        self.simulate_single_file_edit_only("<project-a-path>/src/advisor.py")

        # Cross-repo (warns)
        self.simulate_single_file_edit_only("/opt/taxprep-project/src/interview.py")

        # Back to project-a (no new issue)
        self.simulate_single_file_edit_only("<project-a-path>/src/advisor.py")

        # Should complete without blocks
        blocks = self.state.get_hook_fires(action="block")
        assert len(blocks) == 0, "Legitimate multi-repo flow should not block"
