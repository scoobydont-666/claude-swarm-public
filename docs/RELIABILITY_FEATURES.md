# Swarm Reliability Features

Five reliability features implemented to improve swarm resilience and task management:

## 1. GPU Slot Manager (`src/gpu_slots.py`)

**Purpose:** Atomic GPU slot allocation via lockfiles to prevent resource conflicts.

**Implementation:**
- `claim_slot(gpu_id: int) -> bool` — Claims a GPU slot atomically
- `release_slot(gpu_id: int) -> bool` — Releases a claimed slot
- `is_slot_available(gpu_id: int) -> bool` — Checks if slot is available
- `get_slot_status() -> list[dict]` — Returns status of all GPU slots
- `setup_ollama_slot() -> bool` — Permanently claims GPU 0 for Ollama on startup

**Storage:** `/opt/swarm/gpu/slot-{N}.lock` lockfiles

**GIGA Configuration:**
- GPU 0: Reserved for Ollama (permanently claimed)
- GPU 1: Workload allocation (dynamically allocated)

**Metrics:** Added `swarm_gpu_slots_used` gauge to `swarm_metrics.py`

## 2. Task Deadline Watchdog

**Purpose:** Detect tasks that exceed their deadline and auto-requeue them.

**Implementation:**
- Health rule: `task_deadline_exceeded` in `health_rules.py`
- Check: `_check_task_deadline()` in `health_monitor.py` — Scans claimed tasks, compares `claimed_at + (estimated_minutes * 2)` vs now
- Remediation: `requeue_task()` in `remediations.py` — Moves task back to pending, increments `_retries`

**Behavior:**
- Triggered if task deadline exceeded AND retries < 3
- If retries >= 3: escalates to email instead of auto-requeue
- New tasks start with `_retries: 0`

**Configuration:** No config changes needed (health rule has auto_remediate=True)

## 3. Agent Crash Deregistration (`src/crash_handler.py`)

**Purpose:** Graceful shutdown ensures node status is cleaned up and claimed tasks are requeued.

**Implementation:**
- `install_crash_handlers()` — Registers SIGTERM/SIGINT/SIGHUP handlers + atexit hook
- Signal handler: `_handle_crash(signum, frame)` — Marks node idle, releases claimed tasks, writes summary
- Session summary: `/opt/claude-swarm/data/crash-summaries/<timestamp>-<pid>.yaml`
- Callback system: `register_crash_callback()` for plugins to hook shutdown

**Behavior:**
1. On signal: execute registered callbacks (LIFO order)
2. Mark node idle
3. Requeue any claimed tasks (increment retry counter)
4. Write session summary to disk
5. Exit cleanly

## 4. Auto-Requeue Failed Dispatches (`src/remote_session.py`)

**Purpose:** Automatically requeue tasks when remote dispatches fail or timeout.

**Implementation:**
- `_auto_requeue_task(dispatch_id, task_id)` — Moves task from claimed → pending, increments retries
- `check_dispatch_status(dispatch_id)` — Monitors background dispatch processes, marks dead ones as failed
- Integrated into `execute_plan()` — After synchronous execution, checks status and auto-requeues if needed

**Behavior:**
- For synchronous dispatches: checks exit status, requeues if failed/timeout
- For background dispatches: can check process status via `check_dispatch_status()`
- Extracts task_id from dispatch plan via regex matching
- Max retries: 3 (requeue fails if retries >= 3)

## 5. Backpressure (`src/work_generator.py`)

**Purpose:** Prevent work queue overflow when tasks are slow to process.

**Implementation:**
- `WorkGenerator.generate_work()` checks pending task count before scanning projects
- Configuration: `work_generator.max_pending_tasks` in `swarm.yaml` (default: 10)
- Logging: debug-level log message when backpressure triggered

**Behavior:**
```python
if pending_count >= max_pending_tasks:
    log.debug("backpressure: %d tasks pending (max %d), skipping generation", ...)
    return []  # No new tasks created
```

**Configuration in swarm.yaml:**
```yaml
work_generator:
  max_pending_tasks: 10  # Adjust per deployment
```

---

## Testing

All features have comprehensive test coverage (47 new tests):

- **test_gpu_slots.py** (14 tests)
  - Claiming, releasing, availability checks
  - Multi-slot operations
  - Ollama permanent slot setup

- **test_deadline_watchdog.py** (12 tests)
  - Health rule configuration
  - Check and remediation method existence
  - Execute dispatcher integration
  - Monitor cycle verification

- **test_crash_handler.py** (13 tests)
  - Module loading and basic functions
  - Callback registration and execution (LIFO order)
  - Signal handling (SIGTERM, SIGINT, SIGHUP)
  - Session summary creation
  - Handler installation

- **test_backpressure.py** (8 tests)
  - Config validation
  - Generation behavior at/below/above limits
  - Default limit enforcement
  - Backpressure logging

**Test Results:**
```
47 passed in 0.28s
Total: 512 passing tests (up from 337)
```

---

## Integration Points

1. **health_monitor.py:** Task deadline check wired into main check dispatch
2. **remediations.py:** `requeue_task` action registered in execute dispatcher
3. **swarm_metrics.py:** GPU slot status collected in metrics update cycle
4. **work_generator.py:** Backpressure check in `generate_work()` entry point
5. **remote_session.py:** Auto-requeue logic in synchronous execution path

---

## Deployment Checklist

- [ ] Run full test suite: `pytest tests/ -x -q`
- [ ] Verify GPU directory exists: `/opt/swarm/gpu/`
- [ ] Check swarm.yaml has `max_pending_tasks` config
- [ ] Initialize Ollama slot once at startup: call `setup_ollama_slot()`
- [ ] Install crash handlers in agent init: call `install_crash_handlers()`
- [ ] Monitor `/opt/claude-swarm/data/crash-summaries/` for session records
- [ ] Test deadline escalation: create old task, wait for health cycle
- [ ] Monitor backpressure logs: `grep "backpressure:" /opt/claude-swarm/data/*.log`
