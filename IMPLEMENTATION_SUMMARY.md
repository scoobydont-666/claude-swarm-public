# Observability Features Implementation Summary

## Overview
Implemented 5 observability features for `/opt/claude-swarm/` with 40+ new tests. All 337 existing tests continue to pass.

## Features Implemented

### Item 3: Dispatch Output Streaming
**Files Modified:** `src/remote_session.py`

- **stdbuf Line-Buffering:** SSH commands now wrapped with `stdbuf -oL` to force line-buffered output
- **Live Tailing:** Output piped through `tee` to `/tmp/dispatch-{id}.log` for real-time `tail -f` monitoring
- **New Function:** `get_dispatch_output(dispatch_id, tail_lines=50) -> str`
  - Retrieves dispatch output from file system
  - Supports tailing last N lines or full output
  - Returns error messages for missing files

**Usage:**
```bash
swarm dispatches tail session-123456-node_gpu  # Live monitor
python3 -c "from remote_session import get_dispatch_output; print(get_dispatch_output('session-123456-node_gpu', 50))"
```

---

### Item 7: Dispatch History CLI
**Files Modified:** `src/swarm_cli.py`

Added new `swarm dispatches` command group with three subcommands:

#### `swarm dispatches`
Lists recent dispatches with status indicators:
- Shows: ID, host, strategy, model, status (color-coded), duration
- Status colors: green=completed, cyan=running, red=failed
- Reads dispatch metadata from `.plan.yaml` files

#### `swarm dispatches show <dispatch-id>`
Displays full dispatch details:
- Host, strategy, model, complexity, estimated duration
- Complete reasoning and truncated prompt
- Last 50 lines of output

#### `swarm dispatches tail <dispatch-id>`
Live monitoring with `tail -f` on output file (for active dispatches)

**Implementation Details:**
- Parses `.plan.yaml` files for dispatch metadata
- Checks `.pid` files to determine if dispatch is running
- Graceful handling of missing/malformed files

---

### Item 8: Session Cost Tracking Per Dispatch
**Files Modified:** `src/remote_session.py`, `src/swarm_metrics.py`

#### Cost Estimation
- **Function:** `_estimate_cost(model, output_length)` in `remote_session.py`
- **Pricing:** Based on Claude API rates:
  - Haiku: $0.80 per 1M tokens
  - Sonnet: $3.00 per 1M tokens
  - Opus: $15.00 per 1M tokens
- **Calculation:** Output length × 0.3 tokens/char × model rate

#### Cost Storage
- `estimated_cost_usd` field added to `SessionResult` dataclass
- Cost stored in dispatch plan YAML after completion
- Synchronous dispatches estimate cost from output length

#### Prometheus Metrics
Added two new counters:
- `swarm_dispatch_cost_usd_total` — cumulative cost across all dispatches
- `swarm_dispatch_cost_by_host_usd_total` — cost breakdown per hostname

**Cost Collection:**
- `_collect_dispatch_costs()` reads all `.plan.yaml` files
- Updates metrics on each scrape cycle
- Gracefully handles missing cost fields

---

### Item 9: Prometheus Alert Rules
**Files Created:** `deploy/swarm-alerts.yml`

Six new alert rules configured:

1. **SwarmNodeOffline** (warning)
   - Triggers: `swarm_nodes_total{state="offline"} > 0` for 5m
   - One or more nodes have gone offline

2. **SwarmTaskQueueBacklog** (warning)
   - Triggers: `swarm_tasks_total{state="pending"} > 5` for 30m
   - Tasks accumulating in pending queue

3. **SwarmHeartbeatStale** (critical)
   - Triggers: `swarm_last_heartbeat_age_seconds > 600` for 2m
   - Node heartbeat missing for 10+ minutes (hung/offline)

4. **SwarmGPUSlotsFull** (info)
   - Triggers: `swarm_gpu_slots_used >= 2` for 5m
   - All GPU slots claimed (informational)

5. **SwarmHighDispatchCost** (warning)
   - Triggers: dispatch cost rate > $5/hour for 10m
   - Runaway dispatch costs detected

6. **SwarmDispatchCostByHost** (warning)
   - Triggers: per-host dispatch cost > $2/hour for 10m
   - Specific host exceeding cost threshold

**Validation:**
- YAML syntax validated in tests
- All rules include severity, summary, description, and duration
- PromQL expressions reference new metrics

---

### Item 10: NFS Health Check
**Files Modified:** `src/health_monitor.py`, `src/health_rules.py`, `src/swarm_lib.py`

#### Health Check Implementation
- **Check Type:** `nfs_health` (new)
- **Location:** `HealthMonitor._check_nfs_health()`
- **Operation:**
  1. Write test file to `/opt/swarm/.health-check`
  2. Measure write and read times
  3. Verify content matches
  4. Delete test file
  5. Trigger if: timeout > 5s OR content mismatch OR I/O error

#### Rule Configuration
- **Name:** `nfs_unhealthy`
- **Check Type:** `nfs_health`
- **Severity:** critical
- **Threshold:** 5 seconds
- **Auto-Remediate:** False (alert only)
- **Cooldown:** 15 minutes

#### Graceful Degradation (swarm_lib.py)
- **Function:** `_is_nfs_healthy()` — fast NFS health check
- **Fallback Path:** `~/.swarm-status/{hostname}.json` when NFS unavailable
- **Integration:** `update_status()` gracefully falls back to local storage
- **Logging:** Warns when NFS is unavailable

**Impact:**
- NFS failures don't crash swarm operations
- Status file written to local fallback when NFS unresponsive
- Health check rule triggers alerts on NFS degradation

---

## Testing

### New Test Files
1. **test_dispatch_history.py** (14 tests)
   - Output retrieval with tail functionality
   - stdbuf command wrapping verification
   - Cost estimation accuracy
   - CLI command structure

2. **test_nfs_health.py** (16 tests)
   - NFS health check success/timeout/error scenarios
   - Rule configuration validation
   - Graceful degradation in swarm_lib
   - Integration with health monitor

3. **test_alert_rules.py** (10 tests)
   - YAML syntax validation
   - Rule structure requirements
   - Individual alert rule verification
   - Annotation completeness

### Test Coverage
- **Total New Tests:** 40
- **All Existing Tests:** 337 passing
- **Total Tests:** 377
- **Coverage:** All 5 features have corresponding unit and integration tests

**Run Tests:**
```bash
cd /opt/claude-swarm
python3 -m pytest tests/test_dispatch_history.py tests/test_nfs_health.py tests/test_alert_rules.py -v
python3 -m pytest tests/ -q  # Full suite
```

---

## Metrics & Observability

### Prometheus Metrics Added
- `swarm_dispatch_cost_usd_total` (Counter)
- `swarm_dispatch_cost_by_host_usd_total` (Counter with `hostname` label)

### Health Checks Added
- `nfs_health` — NFS responsiveness and data integrity

### Dispatch Artifacts
New/Enhanced dispatch artifacts:
- `.output` files now support `tail -f` via stdbuf/tee
- `.plan.yaml` includes `estimated_cost_usd` field
- Dispatch history queryable via CLI

---

## Configuration & Deployment

### Config Updates
- Alert rules loaded by Prometheus from `deploy/swarm-alerts.yml`
- NFS health check configured in `health_rules.py`
- Cost tracking enabled by default in swarm_metrics.py

### No Breaking Changes
- All features backward compatible
- Graceful handling of missing/old dispatch files
- Metrics exposed alongside existing ones

---

## Files Modified Summary

| File | Changes |
|------|---------|
| `src/remote_session.py` | stdbuf wrapping, cost tracking, `get_dispatch_output()` |
| `src/swarm_cli.py` | `dispatches` CLI commands (show, tail, list) |
| `src/swarm_metrics.py` | Cost metrics collection and exposure |
| `src/health_monitor.py` | NFS health check implementation |
| `src/health_rules.py` | NFS health rule configuration |
| `src/swarm_lib.py` | NFS graceful degradation with local fallback |
| `deploy/swarm-alerts.yml` | 6 Prometheus alert rules (new file) |
| `tests/test_dispatch_history.py` | 14 new tests |
| `tests/test_nfs_health.py` | 16 new tests |
| `tests/test_alert_rules.py` | 10 new tests |

---

## Verification

All features have been tested and verified:

```bash
# Item 3: Dispatch output streaming
cd /opt/claude-swarm && python3 -m pytest tests/test_dispatch_history.py::TestDispatchOutputStreaming -v

# Item 7: Dispatch history CLI
cd /opt/claude-swarm && python3 -m pytest tests/test_dispatch_history.py::TestDispatchHistoryCLI -v

# Item 8: Cost tracking
cd /opt/claude-swarm && python3 -m pytest tests/test_dispatch_history.py::TestDispatchCostEstimation -v

# Item 9: Alert rules
cd /opt/claude-swarm && python3 -m pytest tests/test_alert_rules.py -v

# Item 10: NFS health
cd /opt/claude-swarm && python3 -m pytest tests/test_nfs_health.py -v
```

All 40 new tests pass. All 337 existing tests remain passing.
