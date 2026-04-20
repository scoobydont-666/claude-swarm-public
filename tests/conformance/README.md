# Routing Protocol v1 Conformance Test Suite

Validates the Routing Protocol v1 specification against three distinct agent kinds: **ProjectA** (tax-advisory, multi-repo), **TaxPrep** (interview pipeline, high-velocity dispatch), and **Reference** (minimal, text-only).

**Status:** Production · **Last updated:** 2026-04-18 · **Owner:** josh

## Purpose

Routing Protocol v1 shipped with 166+ internal claude-swarm tests proving hooks + state-store work in isolation. This conformance suite validates the protocol against **arbitrary agents** — simulating real dispatch patterns that expose protocol gaps before they hit production.

**Spec reference:** `<hydra-project-path>/docs/routing-protocol-v1.md` (277 lines)

---

## Test Coverage

### Agent Kind: ProjectA (Tax-Advisory)

**Characteristics:** Multi-repo cross-editing, long thinking blocks, pause-for-user-input patterns.

**Test file:** `test_agent_christi.py` (8+ tests)

**Invariants proven:**
- ✅ Cross-repo edits trigger `parallel-detector` warn-only (not blocks)
- ✅ Same-file sequential edits are serial-OK (no warn)
- ✅ Long thinking (>300ms) without tool use → `idle-detector` warn
- ✅ Pause-ask patterns (`"Ready for next phase?"`) block when plan is active
- ✅ Legitimate status updates do NOT match pause-ask regex
- ✅ Pause-ask allowed when plan NOT active

### Agent Kind: TaxPrep (Interview Pipeline)

**Characteristics:** Rapid dispatch bursts (11+ tasks/60s), interview state transitions, back-pressure.

**Test file:** `test_agent_taxprep.py` (9+ tests)

**Invariants proven:**
- ✅ 11 dispatches in 60s → block on 11th (rate-limit = 10/min)
- ✅ Exactly 10 dispatches in 60s → allowed (at limit)
- ✅ 11 dispatches over 120s → allowed (under limit in any 60s window)
- ✅ Rate-limit resets after window closes
- ✅ Multi-file interview edits trigger parallel-detector warn
- ✅ Single interview file with repeated edits → serial-OK
- ✅ Queue overload (depth > 2× slots) escalates to next tier
- ✅ Runaway dispatch (20+ in 30s) triggers multiple blocks
- ✅ Interview state persists to SQLite across coordinator failures
- ✅ Coordinator resumes from checkpoint + adopts pending tasks

### Agent Kind: Reference (Minimal)

**Characteristics:** Text-only output, single-file edits only. Simplest possible case; should NEVER trigger hooks.

**Test file:** `test_agent_reference.py` (10+ tests)

**Invariants proven:**
- ✅ Single-file edit → zero hooks fire
- ✅ Multiple edits to same file → zero hooks fire
- ✅ Text-only response without active plan → no idle warning
- ✅ Pause-ask pattern without active plan → allowed (no block)
- ✅ Informational status messages do NOT match pause-ask patterns
- ✅ Inline-only work → zero dispatch records
- ✅ Brief thinking (<100ms) + immediate follow-up → no false positives
- ✅ Multi-phase plan execution → clean transitions, no lingering state
- ✅ Plan deactivation clears all state
- ✅ No orphaned dispatch records after completion

---

## Architecture

### Harness Module (`harness.py`)

**RoutingConformanceTest base class** — provides:

```python
class RoutingConformanceTest:
    # Setup/teardown
    setup_method()           # Temp dir + SQLite DB per test
    teardown_method()        # Cleanup

    # Assertions (hook behavior)
    assert_warn_only_fires(hook, times=1)
    assert_block_fires(hook, times=1)
    assert_dispatch_recorded(task_id, tier=None)
    assert_state_persisted(key, expected_value=None)
    assert_no_dispatches()
    assert_dispatch_count(count)

    # Agent stubs (simulate tool calls)
    simulate_multi_file_edit_pattern(files)      # Cross-repo edits
    simulate_long_thinking_block(duration_ms)    # Idle pattern
    simulate_rapid_dispatch_burst(count, window_s)
    simulate_pause_ask_pattern(text)             # Pause-ask detection
    simulate_single_file_edit_only(file_path)    # Safe operation
```

**HookPayloadBuilder** — generates hook input JSON:
```python
HookPayloadBuilder.pre_tool_use_write(file_path)
HookPayloadBuilder.pre_tool_use_edit(file_path, old_string, new_string)
HookPayloadBuilder.pre_tool_use_bash(command)
HookPayloadBuilder.stop_response_message(text)
```

**RoutingStateStub** — in-memory state (mimics `routing_state_db`):
```python
state.record_dispatch(task_id, tier, model)
state.record_hook_fire(hook, mode, action, pattern)
state.get_dispatches(task_id=None)
state.get_hook_fires(hook=None, action=None)
```

### Test Execution Flow

1. **Setup** (per test):
   - `setup_method()` creates tmpdir + SQLite DB
   - `conftest.py` adds `~/.claude/hooks/lib/` to sys.path
   - State directories reset (`/tmp/claude-routing-state`)

2. **Simulate** (during test):
   - Call `simulate_*()` methods to emit hook payloads
   - Each invocation records state changes + hook fires

3. **Assert** (test verification):
   - Use `assert_*()` methods to validate protocol behavior
   - State persisted to tmpdir for e2e verification

4. **Cleanup** (after test):
   - Temp directory removed
   - State dirs cleaned

### Test Speed Target

- **Whole suite:** <5s wall-clock
- **Per test:** <100ms (in-memory stubs, no subprocess overhead)
- **CI job:** triggers on PR + push to main

---

## Hook Enforcement Details

### Pre-Tool-Use Hooks

| Hook | Trigger | v1 Mode | Test Coverage |
|------|---------|---------|---|
| `pre_tool_use_parallel_detector.py` | Every tool call | warn-only | ProjectA multi-repo, TaxPrep interviews, Reference no-false-positives |
| `dispatch_rate_limit_hook.py` | Pre-dispatch | block | TaxPrep burst detection, rate-reset, escalation |

### Stop-Response Hooks

| Hook | Trigger | v1 Mode | Test Coverage |
|------|---------|---------|---|
| `post_tool_use_idle_detector.py` | Text-only mid-plan | warn-only | ProjectA long-thinking, Reference brief-think |
| `stop_hook_pause_ask_scanner.py` | Pause-ask pattern | block | ProjectA pause-ask-active, Reference no-false-positives |

### State Management

| Component | Storage | Test Coverage |
|-----------|---------|---|
| Dispatch ledger | SQLite `routing.db` | TaxPrep checkpoint recovery |
| Hook fires | JSONL tracer | All suites (verified via state stub) |
| FP-block counter | SQLite + JSON fallback | Auto-downgrade testing (TaxPrep) |

---

## Running Tests

### Locally

```bash
cd /opt/claude-swarm

# All conformance tests
pytest tests/conformance/ -v

# Single agent suite
pytest tests/conformance/test_agent_christi.py -v
pytest tests/conformance/test_agent_taxprep.py -v
pytest tests/conformance/test_agent_reference.py -v

# Single test
pytest tests/conformance/test_agent_christi.py::TestChristiMultiRepoEditing::test_christi_cross_repo_edit_warns -v

# With coverage
pytest tests/conformance/ --cov=tests.conformance --cov-report=term-missing
```

### CI Pipeline

Triggered on:
- Push to `main`
- Pull request to `main`
- Changes to `tests/conformance/**` or `.github/workflows/routing-conformance.yml`

**Job:** `.github/workflows/routing-conformance.yml`
- Runs `pytest tests/conformance/ -v`
- Blocks PR merge on failure
- Archives results + comments PR

---

## Adding a New Agent Kind

To test a new agent (e.g., `my-agent`):

1. **Create test class** in `test_agent_my_agent.py`:
   ```python
   from harness import RoutingConformanceTest

   class TestMyAgent(RoutingConformanceTest):
       def test_my_agent_behavior(self):
           self._activate_plan("my-plan.md")
           self.simulate_multi_file_edit_pattern([...])
           self.assert_warn_only_fires("pre_tool_use_parallel_detector")
   ```

2. **Document invariants** in comments:
   ```python
   def test_my_agent_xyz(self):
       """Test name and invariant being proven.
       
       Invariant: When my-agent does X, hook Y should fire in mode Z.
       """
   ```

3. **Add to this README** in a new "Agent Kind: MyAgent" section under "Test Coverage"

4. **Push** — CI automatically runs new tests

---

## Debugging Failures

### Hook Fire Not Recorded

Check:
1. Is `plan_active` set? → `self._activate_plan("plan.md")`
2. Does hook name match? → `state.get_hook_fires(hook="exact_hook_name")`
3. Is conftest.py setting sys.path correctly? → Check pytest output for import errors

### State Not Persisted

Check:
1. Is `RoutingStateStub._write_persistent_state()` being called?
2. Is tmpdir accessible? → `print(self.tmpdir_path)`
3. Is SQLite DB writable? → Check file permissions in temp space

### False Negatives (Assertion Passes When It Shouldn't)

Check:
1. Is `setup_method()` / `teardown_method()` running?
2. Are state mutations being recorded? → Add print statements to `simulate_*()` calls
3. Is the hook actually being invoked? → Check mock invocations in the hook payload

### Race Conditions

Tests use in-memory stubs (no subprocess), so races are unlikely. If you add subprocess-based hook invocation:
- Add `time.sleep()` between rapid state mutations
- Use file locks for `/tmp/claude-routing-state` access
- Add timeout to subprocess calls

---

## Known Limitations (v1)

1. **No real hook invocation** — tests use in-memory stubs, not actual `.py` hook scripts
   - **Rationale:** Speed (<5s suite) + determinism
   - **e2e hook validation:** See `/opt/claude-swarm/tests/integration/` for subprocess-based tests

2. **No Redis interaction** — state uses SQLite only
   - **Rationale:** CI env may not have Redis; tmpdir is portable
   - **Scope note:** Cascade fallback (Redis → CB → SQLite) validated in integration suite

3. **No multi-session coordination** — single session only
   - **Rationale:** Spec excludes multi-session in v1 scope
   - **Future:** Add `test_multi_session_*` suites post-v1

4. **No real Ollama/Claude dispatch** — dispatch records are synthetic
   - **Rationale:** Testing protocol, not LLM execution
   - **Integration suite:** Has real Ollama dispatch tests

---

## Success Criteria

**All tests passing** = protocol correctly handles:

1. ✅ **Dispatch class detection** — Correct tool calls map to inline/local-llm/bg-subagent/fleet-dispatch
2. ✅ **Parallel detection** — Independent files flagged as dispatch candidates
3. ✅ **Rate limiting** — >10 dispatches/min blocked; reset after window
4. ✅ **Pause-ask blocking** — Pause patterns blocked when plan active; allowed when inactive
5. ✅ **State persistence** — All state written to SQLite; recoverable from checkpoint
6. ✅ **False-positive suppression** — Reference agent (minimal) never triggers hooks
7. ✅ **Multi-phase execution** — Plan phases transition cleanly without hooks firing inappropriately
8. ✅ **Escalation** — Overloaded queue escalates to higher tiers
9. ✅ **Auto-downgrade** — 5+ FP blocks in 60min → mode downgrade to warn-only

---

## References

- **Spec:** `<hydra-project-path>/docs/routing-protocol-v1.md` (§4 dispatch_class, §6 state_store, §8 enforcement)
- **Hooks lib:** `~/.claude/hooks/lib/routing_common.py`, `routing_state_db.py`, `routing_metrics.py`
- **Memory:** `<hydra-project-path>/CLAUDE.md` + `project_routing_protocol_v1.md`
- **Internal tests:** `/opt/claude-swarm/tests/` (166+ tests of hooks + state-store)

---

## Questions?

**File an issue** with:
- Failing test name + output
- Agent kind (ProjectA/TaxPrep/Reference) or custom
- Expected vs. actual hook behavior
- Logs from `/tmp/claude-routing-state/`
