"""Unit tests for routing protocol enforcement hooks.

Strategy: import the hook module's inner functions directly (not re-executing
main()) to avoid sys.exit() at import time. All file I/O is patched to tmp.
"""
import json
import sys
import time
import pytest
from pathlib import Path
from unittest.mock import patch
from io import StringIO

# Bootstrap path so hooks can import their lib
HOOKS_DIR = Path.home() / ".claude" / "hooks"
LIB_DIR = HOOKS_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(HOOKS_DIR))
# sentinel src for slot monitor
sys.path.insert(0, "/opt/hydra-sentinel/src")


@pytest.fixture(autouse=True)
def isolated_tmp(tmp_path, monkeypatch):
    """Redirect all routing state to tmp so tests never touch real files."""
    import routing_common as rc
    monkeypatch.setattr(rc, "ROUTING_TMP", tmp_path / "routing")
    monkeypatch.setattr(rc, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(rc, "PLAN_ACTIVE_FILE", tmp_path / "state" / "plan-active")
    monkeypatch.setattr(rc, "RECENT_EDITS_FILE", tmp_path / "routing" / "recent-edits.json")
    monkeypatch.setattr(rc, "DISPATCH_TIMES_FILE", tmp_path / "routing" / "dispatch-times.json")
    monkeypatch.setattr(rc, "HOOK_FIRES_FILE", tmp_path / "routing" / "hook-fires.jsonl")
    monkeypatch.setattr(rc, "FP_BLOCKS_FILE", tmp_path / "routing" / "fp-blocks.json")
    monkeypatch.setattr(rc, "PHASE_LOG_FILE", tmp_path / "phase-log.jsonl")
    monkeypatch.setattr(rc, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({"routing_protocol_mode": "enforce"}))
    (tmp_path / "routing").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    yield tmp_path


# ══════════════════════════════════════════════════════════════════════════════
# routing_common
# ══════════════════════════════════════════════════════════════════════════════

class TestRoutingCommon:
    def test_get_routing_mode_enforce(self, isolated_tmp):
        import routing_common as rc
        assert rc.get_routing_mode() == "enforce"

    def test_get_routing_mode_off(self, isolated_tmp):
        import routing_common as rc
        rc.SETTINGS_PATH.write_text(json.dumps({"routing_protocol_mode": "off"}))
        assert rc.get_routing_mode() == "off"

    def test_get_routing_mode_default_when_key_absent(self, isolated_tmp):
        import routing_common as rc
        rc.SETTINGS_PATH.write_text(json.dumps({}))
        assert rc.get_routing_mode() == "enforce"

    def test_plan_active_lifecycle(self, isolated_tmp):
        import routing_common as rc
        assert not rc.is_plan_active()
        rc.write_plan_active("sess-1", "/tmp/plan.md")
        assert rc.is_plan_active()
        rc.clear_plan_active()
        assert not rc.is_plan_active()

    def test_files_independent_different_dirs(self):
        import routing_common as rc
        assert rc.files_are_independent("/opt/proj-a/foo.py", "/opt/proj-b/bar.py")

    def test_files_independent_same_file(self):
        import routing_common as rc
        assert not rc.files_are_independent("/opt/proj/foo.py", "/opt/proj/foo.py")

    def test_files_independent_same_stem_different_dir(self):
        import routing_common as rc
        # Same stem "foo" — treated as serial-OK (not independent)
        assert not rc.files_are_independent("/opt/a/foo.py", "/opt/b/foo.py")

    def test_recent_edits_rolling_5(self, isolated_tmp):
        import routing_common as rc
        for i in range(7):
            rc.record_edit(f"/opt/proj/file{i}.py")
        edits = rc.get_recent_edits()
        assert len(edits) <= 5

    def test_dispatch_times_stale_pruned(self, isolated_tmp):
        import routing_common as rc
        stale = time.time() - 120
        rc.write_json_file(rc.DISPATCH_TIMES_FILE, {"times": [stale, stale]})
        times = rc.get_recent_dispatch_count(window_s=60)
        assert len(times) == 0

    def test_fp_block_counter_increments(self, isolated_tmp):
        import routing_common as rc
        with patch.object(rc, "get_routing_config", return_value={
            "auto_downgrade_fp_threshold": 99,
            "auto_downgrade_window_minutes": 60,
            "dispatch_rate_per_minute": 10,
        }):
            for _ in range(3):
                count = rc.record_fp_block()
        assert count == 3

    def test_fp_auto_downgrade_writes_settings(self, isolated_tmp):
        import routing_common as rc
        with patch.object(rc, "get_routing_config", return_value={
            "auto_downgrade_fp_threshold": 2,
            "auto_downgrade_window_minutes": 60,
            "dispatch_rate_per_minute": 10,
        }):
            for _ in range(3):
                rc.record_fp_block()
        data = json.loads(rc.SETTINGS_PATH.read_text())
        assert data["routing_protocol_mode"] == "warn-only"

    def test_reset_fp_blocks(self, isolated_tmp):
        import routing_common as rc
        with patch.object(rc, "get_routing_config", return_value={
            "auto_downgrade_fp_threshold": 99, "auto_downgrade_window_minutes": 60,
            "dispatch_rate_per_minute": 10,
        }):
            rc.record_fp_block()
        rc.reset_fp_blocks()
        data = rc.read_json_file(rc.FP_BLOCKS_FILE, {"blocks": []})
        assert data["blocks"] == []


# ══════════════════════════════════════════════════════════════════════════════
# Hook 1: routing_parallel_detector — test via files_are_independent + record_edit
# ══════════════════════════════════════════════════════════════════════════════

class TestParallelDetector:
    """Test detection logic directly — hooks call sys.exit so we test the lib."""

    def test_no_prior_edits_no_warn(self, isolated_tmp):
        import routing_common as rc
        prior = rc.get_recent_edits()
        assert prior == []
        # First edit — nothing to compare
        rc.record_edit("/opt/proj-a/foo.py")
        # No independent prior → no warning expected
        prior_after = rc.get_recent_edits(window_s=60)
        independents = [e for e in prior_after[:-1]
                        if rc.files_are_independent(e["path"], "/opt/proj-a/foo.py")]
        assert independents == []

    def test_independent_prior_triggers_warn(self, isolated_tmp):
        import routing_common as rc
        rc.record_edit("/opt/proj-a/alpha.py")
        prior = rc.get_recent_edits()
        current = "/opt/proj-b/beta.py"
        independents = [e for e in prior if rc.files_are_independent(e["path"], current)]
        assert len(independents) == 1

    def test_same_dir_not_independent(self, isolated_tmp):
        import routing_common as rc
        rc.record_edit("/opt/proj/alpha.py")
        prior = rc.get_recent_edits()
        current = "/opt/proj/beta.py"
        independents = [e for e in prior if rc.files_are_independent(e["path"], current)]
        assert independents == []

    def test_mode_off_returns_no_warning(self, isolated_tmp):
        import routing_common as rc
        rc.SETTINGS_PATH.write_text(json.dumps({"routing_protocol_mode": "off"}))
        assert rc.get_routing_mode() == "off"


# ══════════════════════════════════════════════════════════════════════════════
# Hook 3: routing_pause_ask_scanner — test scan_for_pause_ask directly
# ══════════════════════════════════════════════════════════════════════════════

class TestPauseAskScanner:
    @pytest.fixture(autouse=True)
    def _load_scanner(self):
        # Import the module but suppress the main() call by mocking sys.stdin
        # and catching the SystemExit — then keep the module reference
        with patch("sys.stdin", StringIO(json.dumps({}))):
            try:
                import routing_pause_ask_scanner as s
            except SystemExit:
                import routing_pause_ask_scanner as s
        self.s = s

    def test_pattern_want_me_to_continue(self):
        assert self.s.scan_for_pause_ask("Want me to continue with phase 2?") is not None

    def test_pattern_should_i_proceed(self):
        assert self.s.scan_for_pause_ask("Should I proceed to the next step?") is not None

    def test_pattern_pause_here(self):
        assert self.s.scan_for_pause_ask("Pause here until you're ready.") is not None

    def test_pattern_shall_i(self):
        assert self.s.scan_for_pause_ask("Shall I start the deployment?") is not None

    def test_pattern_ready_for_next_phase(self):
        assert self.s.scan_for_pause_ask("Ready for next phase when you are.") is not None

    def test_pattern_let_me_know_if(self):
        assert self.s.scan_for_pause_ask("Let me know if you want changes.") is not None

    def test_no_match_on_completion_text(self):
        assert self.s.scan_for_pause_ask("Deployment complete. All tests passed.") is None
        assert self.s.scan_for_pause_ask("Phase 2 dispatched to workers.") is None

    def test_block_when_enforce_plan_active(self, isolated_tmp):
        import routing_common as rc
        rc.write_plan_active("sess-test")
        with patch.object(self.s, "find_last_assistant_message",
                          return_value="Want me to continue with the next phase?"):
            with patch("sys.stdin", StringIO(json.dumps({}))):
                with patch("builtins.print") as mock_print:
                    with pytest.raises(SystemExit) as exc:
                        self.s.main()
        assert exc.value.code == 2
        out = json.loads(mock_print.call_args[0][0])
        assert out["decision"] == "block"
        assert "pause-ask" in out["message"]

    def test_continue_when_no_pattern_match(self, isolated_tmp):
        import routing_common as rc
        rc.write_plan_active("sess-test")
        with patch.object(self.s, "find_last_assistant_message",
                          return_value="All phases complete. Workers done."):
            with patch("sys.stdin", StringIO(json.dumps({}))):
                with patch("builtins.print") as mock_print:
                    with pytest.raises(SystemExit) as exc:
                        self.s.main()
        assert exc.value.code == 0
        out = json.loads(mock_print.call_args[0][0])
        assert out["decision"] == "continue"

    def test_continue_when_plan_not_active(self, isolated_tmp):
        with patch.object(self.s, "find_last_assistant_message",
                          return_value="Want me to continue?"):
            with patch("sys.stdin", StringIO(json.dumps({}))):
                with patch("builtins.print") as mock_print:
                    with pytest.raises(SystemExit) as exc:
                        self.s.main()
        assert exc.value.code == 0
        out = json.loads(mock_print.call_args[0][0])
        assert out["decision"] == "continue"

    def test_warn_only_does_not_block(self, isolated_tmp):
        import routing_common as rc
        rc.SETTINGS_PATH.write_text(json.dumps({"routing_protocol_mode": "warn-only"}))
        rc.write_plan_active("sess-test")
        with patch.object(self.s, "find_last_assistant_message",
                          return_value="Shall I proceed?"):
            with patch("sys.stdin", StringIO(json.dumps({}))):
                with patch("builtins.print") as mock_print:
                    with pytest.raises(SystemExit) as exc:
                        self.s.main()
        assert exc.value.code == 0
        out = json.loads(mock_print.call_args[0][0])
        assert out["decision"] == "continue"


# ══════════════════════════════════════════════════════════════════════════════
# Hook 4: routing_plan_approval
# ══════════════════════════════════════════════════════════════════════════════

class TestPlanApproval:
    @pytest.fixture(autouse=True)
    def _load(self):
        with patch("sys.stdin", StringIO(json.dumps({}))):
            try:
                import routing_plan_approval as pa
            except SystemExit:
                import routing_plan_approval as pa
        self.pa = pa

    def test_plan_approved_phrase_sets_flag(self, isolated_tmp):
        import routing_common as rc
        stdin = {"tool_input": {"prompt": "plan approved — go ahead"}, "session_id": "s1"}
        with patch("sys.stdin", StringIO(json.dumps(stdin))):
            with patch("builtins.print"):
                with pytest.raises(SystemExit):
                    self.pa.main()
        assert rc.is_plan_active()

    def test_exit_plan_mode_tool_sets_flag(self, isolated_tmp):
        import routing_common as rc
        stdin = {"tool_name": "ExitPlanMode", "tool_input": {}, "session_id": "s2"}
        with patch("sys.stdin", StringIO(json.dumps(stdin))):
            with patch("builtins.print"):
                with pytest.raises(SystemExit):
                    self.pa.main()
        assert rc.is_plan_active()

    def test_execute_this_plan_sets_flag(self, isolated_tmp):
        import routing_common as rc
        stdin = {"tool_input": {"prompt": "execute this plan now"}, "session_id": "s3"}
        with patch("sys.stdin", StringIO(json.dumps(stdin))):
            with patch("builtins.print"):
                with pytest.raises(SystemExit):
                    self.pa.main()
        assert rc.is_plan_active()

    def test_unrelated_prompt_does_not_set_flag(self, isolated_tmp):
        import routing_common as rc
        stdin = {"tool_input": {"prompt": "what time is it?"}, "session_id": "s4"}
        with patch("sys.stdin", StringIO(json.dumps(stdin))):
            with patch("builtins.print"):
                with pytest.raises(SystemExit):
                    self.pa.main()
        assert not rc.is_plan_active()

    def test_resets_fp_blocks_on_approval(self, isolated_tmp):
        import routing_common as rc
        with patch.object(rc, "get_routing_config", return_value={
            "auto_downgrade_fp_threshold": 99, "auto_downgrade_window_minutes": 60,
            "dispatch_rate_per_minute": 10,
        }):
            rc.record_fp_block()
            rc.record_fp_block()
        stdin = {"tool_input": {"prompt": "execute this plan"}, "session_id": "s5"}
        with patch("sys.stdin", StringIO(json.dumps(stdin))):
            with patch("builtins.print"):
                with pytest.raises(SystemExit):
                    self.pa.main()
        data = rc.read_json_file(rc.FP_BLOCKS_FILE, {"blocks": []})
        assert data["blocks"] == []


# ══════════════════════════════════════════════════════════════════════════════
# Hook 5: routing_phase_commit
# ══════════════════════════════════════════════════════════════════════════════

class TestPhaseCommit:
    @pytest.fixture(autouse=True)
    def _load(self):
        with patch("sys.stdin", StringIO(json.dumps({}))):
            try:
                import routing_phase_commit as pc
            except SystemExit:
                import routing_phase_commit as pc
        self.pc = pc

    def test_phase_commit_logged(self, isolated_tmp):
        import routing_common as rc
        log_path = isolated_tmp / "phase-log.jsonl"
        cmd = "git commit -m 'feat(p2): implement slot accounting'"
        stdin = {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s6"}
        with patch.object(self.pc, "PHASE_LOG_FILE", log_path):
            with patch("sys.stdin", StringIO(json.dumps(stdin))):
                with patch("builtins.print"):
                    with pytest.raises(SystemExit):
                        self.pc.main()
        assert log_path.exists()
        record = json.loads(log_path.read_text().strip())
        assert record["event"] == "phase_commit"
        assert "p2" in record["commit_msg"]

    def test_non_phase_commit_not_logged(self, isolated_tmp):
        log_path = isolated_tmp / "phase-log-2.jsonl"
        cmd = "git commit -m 'fix typo'"
        stdin = {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s7"}
        with patch.object(self.pc, "PHASE_LOG_FILE", log_path):
            with patch("sys.stdin", StringIO(json.dumps(stdin))):
                with patch("builtins.print"):
                    with pytest.raises(SystemExit):
                        self.pc.main()
        assert not log_path.exists()

    def test_non_bash_tool_ignored(self, isolated_tmp):
        log_path = isolated_tmp / "phase-log-3.jsonl"
        stdin = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}, "session_id": "s8"}
        with patch.object(self.pc, "PHASE_LOG_FILE", log_path):
            with patch("sys.stdin", StringIO(json.dumps(stdin))):
                with patch("builtins.print"):
                    with pytest.raises(SystemExit):
                        self.pc.main()
        assert not log_path.exists()

    def test_chore_phase_commit_logged(self, isolated_tmp):
        log_path = isolated_tmp / "phase-log-4.jsonl"
        cmd = "git commit -m 'chore(p3-cleanup): remove stale state'"
        stdin = {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s9"}
        with patch.object(self.pc, "PHASE_LOG_FILE", log_path):
            with patch("sys.stdin", StringIO(json.dumps(stdin))):
                with patch("builtins.print"):
                    with pytest.raises(SystemExit):
                        self.pc.main()
        assert log_path.exists()


# ══════════════════════════════════════════════════════════════════════════════
# Hook 6: routing_dispatch_rate_limit
# ══════════════════════════════════════════════════════════════════════════════

class TestDispatchRateLimit:
    @pytest.fixture(autouse=True)
    def _load(self):
        with patch("sys.stdin", StringIO(json.dumps({}))):
            try:
                import routing_dispatch_rate_limit as drl
            except SystemExit:
                import routing_dispatch_rate_limit as drl
        self.drl = drl

    def _call_main(self, stdin_data: dict) -> tuple[int, dict]:
        captured = []
        with patch("sys.stdin", StringIO(json.dumps(stdin_data))):
            with patch("builtins.print", side_effect=lambda x: captured.append(x)):
                with pytest.raises(SystemExit) as exc:
                    self.drl.main()
        out = json.loads(captured[-1]) if captured else {}
        return exc.value.code, out

    def test_agent_tool_is_dispatch(self):
        assert self.drl.is_dispatch("Agent", {}) is True

    def test_hydra_dispatch_bash_is_dispatch(self):
        assert self.drl.is_dispatch("Bash", {"command": "python hydra_dispatch.py --task foo"}) is True

    def test_ssh_bash_is_dispatch(self):
        assert self.drl.is_dispatch("Bash", {"command": "ssh giga 'ls /tmp'"}) is True

    def test_plain_bash_not_dispatch(self):
        assert self.drl.is_dispatch("Bash", {"command": "ls /tmp"}) is False

    def test_agent_under_limit_approves(self, isolated_tmp):
        code, out = self._call_main({"tool_name": "Agent", "tool_input": {}})
        assert code == 0
        assert out["decision"] == "approve"

    def test_rate_limit_exceeded_blocks_enforce(self, isolated_tmp):
        import routing_common as rc
        rc.write_json_file(rc.DISPATCH_TIMES_FILE, {"times": [time.time()] * 10})
        code, out = self._call_main({"tool_name": "Agent", "tool_input": {}})
        assert code == 2
        assert out["decision"] == "block"
        assert "rate limit" in out["message"]

    def test_rate_limit_exceeded_warn_only_no_block(self, isolated_tmp):
        import routing_common as rc
        rc.SETTINGS_PATH.write_text(json.dumps({"routing_protocol_mode": "warn-only"}))
        rc.write_json_file(rc.DISPATCH_TIMES_FILE, {"times": [time.time()] * 10})
        code, out = self._call_main({"tool_name": "Agent", "tool_input": {}})
        assert code == 0  # warn never exits 2

    def test_non_dispatch_tool_approves_without_counting(self, isolated_tmp):
        import routing_common as rc
        code, out = self._call_main(
            {"tool_name": "Bash", "tool_input": {"command": "echo hello"}}
        )
        assert code == 0
        times = rc.get_recent_dispatch_count()
        assert len(times) == 0


# ══════════════════════════════════════════════════════════════════════════════
# Sentinel slot monitor
# ══════════════════════════════════════════════════════════════════════════════

class TestSlotMonitor:
    def test_write_alert_creates_jsonl(self, isolated_tmp, monkeypatch):
        from hydra_sentinel import routing_slot_monitor as sm
        alert_path = isolated_tmp / "routing-alerts.jsonl"
        monkeypatch.setattr(sm, "ROUTING_ALERTS", alert_path)
        sm.write_alert(busy=1, duration=65.0)
        lines = alert_path.read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["alert"] == "slot_underutilization"
        assert rec["busy_workers"] == 1
        assert rec["underutil_duration_s"] == 65.0

    def test_multiple_alerts_appended(self, isolated_tmp, monkeypatch):
        from hydra_sentinel import routing_slot_monitor as sm
        alert_path = isolated_tmp / "routing-alerts.jsonl"
        monkeypatch.setattr(sm, "ROUTING_ALERTS", alert_path)
        sm.write_alert(busy=0, duration=70.0)
        sm.write_alert(busy=1, duration=90.0)
        lines = alert_path.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_plan_inactive_is_false(self, isolated_tmp, monkeypatch):
        from hydra_sentinel import routing_slot_monitor as sm
        monkeypatch.setattr(sm, "PLAN_ACTIVE_FILE", isolated_tmp / "plan-active")
        assert sm.is_plan_active() is False

    def test_plan_active_reads_flag(self, isolated_tmp, monkeypatch):
        from hydra_sentinel import routing_slot_monitor as sm
        flag = isolated_tmp / "plan-active"
        flag.write_text(json.dumps({"active": True}))
        monkeypatch.setattr(sm, "PLAN_ACTIVE_FILE", flag)
        assert sm.is_plan_active() is True
