# Churn Analysis: claude-swarm

**Audit Date:** 2026-04-17  
**Fix Ratio:** 30% (9 of 30 commits)  
**Analysis Period:** Last 3 months

## Commit Categorization

| Type | Count | Commits |
|------|-------|---------|
| feat | 13 | e37db4a (Phase 4, Redis Streams, router, GPU scheduler, training, etc.) |
| fix | 9 | 3645d14, cdd5218, e168fdc, e8e7dbf, 9a518b3, 4959aaf, ab1c539, 3d0a6b3, 70c6440 (partial) |
| chore | 2 | dd46aee, 70c6440 (partial) |
| docs | 1 | 30377d9 |

## Top 3 Recurring Fix Themes

1. **Redis/NFS Config & Password Issues** (3 fixes: 3645d14, e168fdc, a19c622)
   - Hardcoded Redis password committed to git (security + sync churn)
   - Missing Redis connection fallback logic in dashboard
   - NFS/Redis heartbeat coordination incomplete

2. **API Response Shape Mismatches** (3 fixes: cdd5218, 9a518b3, ab1c539)
   - Dashboard JS expects `GPU/warm_models` response shape but gets different structure
   - Health check API schema drift from `health_check()` → `swarm_lib` mismatch
   - Living spec / IPC consumers wired but response types wrong

3. **Host/Fleet Discovery Gaps** (3 fixes: e37db4a, e8e7dbf, 4959aaf)
   - Localhost detection broken in probe_host (IPv4 vs IPv6 confusion)
   - Miniboss not auto-discovered in GPU fleet inventory
   - Missing wiring: 9 gaps between dispatch path and v3 modules (IPC, dashboard APIs)

## Root Cause Analysis

**Two Root Causes:**

### A. Config Secrets & Defaults Management (50% of fixes)
- Redis password hardcoded in git → requires commits to fix, blocks syncs
- No `.env.example` template or secret validation at startup
- Fallback logic missing (NFS should be secondary, not alternative)

### B. Rapid Phase Development Without Integration Tests (50% of fixes)
- Phase 3 → Phase 4 big refactor split API shape, broke consumers
- Host discovery (localhost/miniboss) added without cross-platform testing
- IPC/living-spec wiring created 9 cascading gaps because integration wasn't verified end-to-end

**Design Flaw:** No contract validation between dispatch engine → dashboard/IPC/health_check. Schema-driven API development would have caught all 3 themes in CI.

## Concrete Remediation Actions

### 1. Extract Secrets + Add Validation (Effort: 20min)
**Code Fix:**
- Create `.env.example` with `REDIS_PASSWORD=changeme`
- Add startup validation: throw error if `REDIS_PASSWORD` not set or weak
- Remove hardcoded password from all config files

**Test:**
- Unit test: config loader validates password on startup
- Test: falls back to NFS if Redis unavailable

**CI:**
- Pre-commit: detect hardcoded `password:` patterns in config (block commit)
- GitLeaks hook already in place (extend with custom pattern)

**Status:** QUICK WIN — implement immediately

### 2. Add Integration Test for API Response Shapes (Effort: 25min)
**Code Fix:**
- Create `tests/integration/test_api_contracts.py`
- Mock dispatch → dashboard/health_check/IPC endpoints
- Assert response shapes match schema (Pydantic or json-schema)

**Test:**
- Test GPU response: `{gpu_id, utilization, model_name, ...}` 
- Test health_check: match `swarm_lib.HealthCheckResponse` schema
- Test IPC: heartbeat payload matches expected fields

**Status:** QUICK WIN — catches future API drift

### 3. Add Localhost Detection Cross-Platform Fix (Effort: 10min)
**Code Fix:**
- Use socket.gethostbyname(hostname) instead of string matching
- Validate against `127.0.0.1` AND `::1` (IPv4 + IPv6)
- Add unit tests for both loopback formats

**Status:** QUICK WIN — 10 lines of code

## Implementation Order (Est. Total: 55min for all 3)

1. Extract secrets + add startup validation (20min)
2. Create integration test suite for API contracts (25min)
3. Fix localhost detection (10min)

---

## Remediation Status

**Action Taken:** Implementing remediation #1 (secrets extraction + validation)  
See commits below for execution.
