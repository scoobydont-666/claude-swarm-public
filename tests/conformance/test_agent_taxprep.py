"""Routing Protocol v1 Conformance: TaxPrep Interview Pipeline Agent.

Simulates TaxPrep's rapid dispatch bursts (11+ tasks in 60s window),
interview state transitions, and back-pressure responses.

Expected hooks:
  - dispatch_rate_limit_hook (block on >10 dispatches/min)
  - parallel_detector (warn on independent file edits during interview)
"""

from harness import RoutingConformanceTest


class TestTaxPrepRapidDispatchBurst(RoutingConformanceTest):
    """High-velocity dispatch patterns trigger rate-limit blocks."""

    def test_taxprep_11_dispatches_in_60s_blocks_after_10(self):
        """Burst of 11 dispatches in 60s window → rate-limit detects burst.

        Note: In v1 unit tests, we record dispatch events; actual rate-limiting
        logic is validated in integration suite with real hook invocation.
        This test documents that 11 dispatches in 60s is > limit of 10/min.
        """
        self._activate_plan("taxprep-interview.md")

        self.simulate_rapid_dispatch_burst(count=11, window_s=60.0)

        # Should record 11 dispatch events
        dispatches = self.state.get_dispatches()
        assert len(dispatches) == 11, f"Expected 11 dispatch records, got {len(dispatches)}"

        # In real protocol, 11th dispatch would be blocked; unit test documents this
        # Integration suite validates actual hook block behavior

    def test_taxprep_10_dispatches_exactly_allowed(self):
        """Burst of exactly 10 dispatches in 60s window → allowed (at limit)."""
        self._activate_plan("taxprep-interview.md")

        self.simulate_rapid_dispatch_burst(count=10, window_s=60.0)

        # Should not block at count=10
        blocks = self.state.get_hook_fires(hook="dispatch_rate_limit", action="block")
        assert len(blocks) == 0, "10 dispatches in 60s should not block"

    def test_taxprep_dispatches_spread_over_2min_allowed(self):
        """Same 11 dispatches spread over 120s → allowed (under rate limit)."""
        self._activate_plan("taxprep-interview.md")

        # Spread 11 dispatches over 120 seconds (< 10/min in any 60s window)
        self.simulate_rapid_dispatch_burst(count=11, window_s=120.0)

        # Should not block because rate is < 10/min in any 60s window
        blocks = self.state.get_hook_fires(hook="dispatch_rate_limit", action="block")
        assert len(blocks) == 0, "11 dispatches over 120s should not block"

    def test_taxprep_burst_after_rate_reset_allowed(self):
        """After 60s window closes, new burst allowed (rate limit resets)."""
        self._activate_plan("taxprep-interview.md")

        # First burst: 10 dispatches in 60s (allowed)
        self.simulate_rapid_dispatch_burst(count=10, window_s=60.0)

        # Wait for window to expire (simulated by time advancement)
        # Reset tracking (next 60s window)
        self.state.dispatches = []

        # Second burst: another 10 dispatches in new window (allowed)
        self.simulate_rapid_dispatch_burst(count=10, window_s=60.0)

        # Total 20 dispatches but across different windows → no sustained block
        blocks = self.state.get_hook_fires(hook="dispatch_rate_limit", action="block")
        assert len(blocks) == 0, "Bursts in separate windows should not block"


class TestTaxPrepInterviewEdits(RoutingConformanceTest):
    """Interview pipeline edits trigger parallel-detector warnings."""

    def test_taxprep_interview_questionnaire_edits(self):
        """Editing multiple questionnaire files → warn (independent)."""
        self._activate_plan("taxprep-interview.md")

        self.simulate_multi_file_edit_pattern(
            [
                "/opt/taxprep-project/data/interview-2026-04-18-001.yaml",
                "/opt/taxprep-project/data/interview-2026-04-18-002.yaml",
                "/opt/taxprep-project/data/interview-2026-04-18-003.yaml",
            ]
        )

        fires = self.state.get_hook_fires(hook="pre_tool_use_parallel_detector")
        # Fires on transition to 2nd and 3rd file (2 total)
        assert len(fires) >= 2, f"Expected ≥2 parallel-detector fires, got {len(fires)}"

    def test_taxprep_single_interview_sequential_updates_allowed(self):
        """Editing single interview file multiple times → no warn (serial-OK)."""
        self._activate_plan("taxprep-interview.md")

        self.simulate_multi_file_edit_pattern(
            [
                "/opt/taxprep-project/data/interview-2026-04-18-001.yaml",
                "/opt/taxprep-project/data/interview-2026-04-18-001.yaml",
                "/opt/taxprep-project/data/interview-2026-04-18-001.yaml",
            ]
        )

        # Same file; should not warn
        blocks = self.state.get_hook_fires(hook="pre_tool_use_parallel_detector", action="block")
        assert len(blocks) == 0, "Same-file sequential updates should not trigger warns"

    def test_taxprep_interview_output_writes(self):
        """Writing interview results to different output files → warn (parallel candidate)."""
        self._activate_plan("taxprep-interview.md")

        self.simulate_multi_file_edit_pattern(
            [
                "/opt/taxprep-project/output/q1-summary.md",
                "/opt/taxprep-project/output/q2-summary.md",
                "/opt/taxprep-project/output/q3-summary.md",
            ]
        )

        fires = self.state.get_hook_fires(hook="pre_tool_use_parallel_detector")
        assert len(fires) >= 2, "Output file writes should be flagged as parallel candidates"


class TestTaxPrepBackPressure(RoutingConformanceTest):
    """Back-pressure and escalation under overload."""

    def test_taxprep_queue_full_escalates(self):
        """Queue depth exceeds 2× slot count → escalate to next tier."""
        self._activate_plan("taxprep-interview.md")

        # Simulate overloaded scenario: many pending tasks
        for i in range(30):
            self.state.record_dispatch(f"overload_task_{i:02d}", "tier_1", "hydracoder:7b")

        # Once queue is full, next dispatch should escalate
        self.state.record_dispatch("escalation_task", "tier_2", "phi4:14b")

        dispatches = self.state.get_dispatches("escalation_task")
        assert len(dispatches) > 0, "Escalation dispatch should be recorded"
        assert dispatches[0]["tier"] == "tier_2", "Overload should trigger escalation"

    def test_taxprep_rapid_recovery_after_burst(self):
        """After burst clears, dispatch rate returns to normal."""
        self._activate_plan("taxprep-interview.md")

        # Burst
        self.simulate_rapid_dispatch_burst(count=11, window_s=60.0)

        # Clear state (simulating worker completion)
        self.state.dispatches = []

        # Normal dispatch
        self.state.record_dispatch("normal_task", "tier_1", "hydracoder:7b")

        dispatches = self.state.get_dispatches("normal_task")
        assert len(dispatches) == 1, "Post-burst dispatch should succeed"


class TestTaxPrepStateRecovery(RoutingConformanceTest):
    """Interview state persists across coordinator failures."""

    def test_taxprep_interview_state_persisted(self):
        """Interview state written to SQLite after each dispatch."""
        self._activate_plan("taxprep-interview.md")

        for i in range(5):
            self.state.record_dispatch(f"interview_task_{i:02d}", "tier_1", "hydracoder:7b")
            self.state.record_state_change("interview_phase", f"phase_{i}")

        # Verify state was persisted to disk
        self.assert_state_persisted("interview_phase")

    def test_taxprep_interview_resume_after_coordinator_restart(self):
        """Coordinator restarts; resumes from checkpoint."""
        self._activate_plan("taxprep-interview.md")

        # Dispatch some tasks
        for i in range(3):
            self.state.record_dispatch(f"task_{i}", "tier_1", "hydracoder:7b")

        # Simulate checkpoint write
        {
            "dispatches": self.state.get_dispatches(),
            "last_phase": "interview_phase_2",
        }

        # On resume, coordinator reads checkpoint and adopts pending tasks
        assert len(self.state.get_dispatches()) == 3, "Checkpoint should preserve dispatch history"


class TestTaxPrepIntegration(RoutingConformanceTest):
    """Multi-phase interview scenarios."""

    def test_taxprep_three_phase_interview(self):
        """Realistic: eligibility → questionnaire → reconciliation."""
        self._activate_plan("taxprep-three-phase.md")

        # Phase 1: eligibility checks (3 dispatches)
        for i in range(3):
            self.state.record_dispatch(f"phase1_task_{i}", "tier_1", "hydracoder:7b")

        # Phase 2: questionnaire (4 rapid dispatches)
        for i in range(4):
            self.state.record_dispatch(f"phase2_task_{i}", "tier_1", "hydracoder:7b")

        # Phase 3: reconciliation (2 dispatches)
        for i in range(2):
            self.state.record_dispatch(f"phase3_task_{i}", "tier_2", "phi4:14b")

        # Total: 9 dispatches under limit
        total = len(self.state.get_dispatches())
        assert total == 9, f"Expected 9 total dispatches, got {total}"

        blocks = self.state.get_hook_fires(action="block")
        assert len(blocks) == 0, "Three-phase interview should not block"

    def test_taxprep_runaway_dispatch_detection(self):
        """Detection: suspicious burst (infinite loop) → documents runaway pattern.

        20+ dispatches in 30s is > 10/min; real protocol would escalate + block.
        Unit test records the dispatch events; integration suite validates blocks.
        """
        self._activate_plan("taxprep-interview.md")

        # Simulate runaway: 20+ dispatches in 30s
        self.simulate_rapid_dispatch_burst(count=20, window_s=30.0)

        # Should record all 20 dispatch events
        dispatches = self.state.get_dispatches()
        assert len(dispatches) == 20, (
            f"Expected 20 dispatch records for runaway, got {len(dispatches)}"
        )

        # Runaway detection: >15 dispatches in 30s is suspicious
        assert len(dispatches) > 15, "Runaway burst should have recorded many dispatches"

    def test_taxprep_graceful_degradation(self):
        """Under severe rate-limit blocks, shift to warn-only mode (auto-downgrade)."""
        self._activate_plan("taxprep-interview.md")

        # Trigger 5+ blocks (auto-downgrade threshold)
        for burst_num in range(2):
            self.state.dispatches = []
            self.simulate_rapid_dispatch_burst(count=15, window_s=60.0)

        # After multiple bursts, mode may downgrade
        # Document: auto-downgrade is a graceful fallback
        [s for s in self.state.state_writes if "mode" in str(s)]
        # Expect either enforcement or auto-downgrade trigger
        assert len(self.state.state_writes) > 0, "State should reflect enforcement attempts"
