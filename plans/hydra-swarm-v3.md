# Hydra Swarm v3 — Next-Gen Multi-Agent Orchestration

## Context

claude-swarm (914 tests) is already strong: NFS task board, SSH dispatch, GPU slots, pipelines, auto-dispatch, conflict detection, health monitoring, dashboard. But it's limited by 2 hardcoded GPU slots, YAML-file task queues, no cost tracking, no worktree isolation, and no integration with the Hydra infrastructure tools we've built (Context Bridge v2, Hydra Pulse, Agent Bridge, Prompt Forge).

Meanwhile, NAI Swarm (1,057 tests, forked from claude-swarm) added PostgreSQL SKIP LOCKED queues, dynamic GPU discovery, VRAM-aware scheduling, multi-tenant quotas, and a model routing proxy — but these innovations never flowed back to the parent.

**Goal:** Backport the best NAI Swarm innovations, integrate Hydra infrastructure projects, and add state-of-the-art features (worktree isolation, generator-verifier loops, MCP server, cost tracking) to create a best-in-class local swarm.

---

## Architecture

```
Hydra Swarm v3
├── Task Queue (SQLite default, PostgreSQL optional, YAML/Redis legacy)
├── GPU Scheduler (dynamic discovery + VRAM-aware allocation)
├── Model Router (unified 4-tier routing: LOCAL/HAIKU/SONNET/OPUS)
├── Dispatch Engine (SSH + git worktree isolation + cost tracking)
├── Pipeline Engine (+ generator-verifier loops, self-correcting tests)
├── Context Layer (Context Bridge v2 living specs)
├── Cost Tracker (Hydra Pulse integration, per-task budgets)
├── Swarm MCP Server (expose operations as MCP tools)
├── KV-Cache Router (warm model affinity for Ollama)
└── Dashboard (+ cost view, GPU scheduler, warm models)
```

---

## Hydra Project Integrations

| Hydra Project | What We Take | Integration Point |
|---|---|---|
| **Context Bridge v2** | Temporal KG, living specs, MCP tools | `living_spec.py` wraps cb_ingest/cb_peek for pipeline context sharing |
| **Hydra Pulse** | Per-task cost tracking, anomaly detection, budget forecasting | `cost_tracker.py` queries Pulse via SWARM_TASK_ID correlation |
| **Agent Bridge** | 4-tier model routing with context-size escalation | `model_router.py` unifies 3 scattered routing implementations |
| **Prompt Forge** | Regression testing model outputs | Future: validate pipeline prompt templates on model upgrades |

## NAI Swarm Backports

| NAI Feature | Adaptation |
|---|---|
| PostgreSQL SKIP LOCKED task queue | `pg_backend.py`, optional via `SWARM_BACKEND=postgres` |
| Dynamic GPU discovery | SSH `nvidia-smi` probe (replaces Prism Central API) |
| VRAM-aware GPU scheduler | SQLite-backed allocation (replaces PostgreSQL) |
| Unified model router | Strip Nutanix deps, keep tier/escalation logic |
| `swarm_base/` package layout | Adopt for public/private separation |

## New Features (State-of-Art)

| Feature | Source | Description |
|---|---|---|
| Git worktree isolation | Cursor, Agent Teams | Create worktree per dispatch, merge on complete, cleanup |
| Generator-verifier loop | Anthropic patterns | Generate → test → fix → repeat (max N iterations) |
| Self-correcting test loop | Cursor | Run pytest → parse failures → fix → repeat |
| Per-task cost tracking | Original | Budget caps via Hydra Pulse integration |
| Swarm MCP server | Original | Expose task/dispatch/status/GPU as MCP tools |
| KV-cache routing | llm-d | Query Ollama /api/ps, prefer hosts with warm models |
| Hierarchical planning | Paperclip | Planner → Manager → Worker pipeline |
| Living specifications | Augment Intent | Shared spec doc via Context Bridge, updated bidirectionally |

---

## New/Modified Files

### New (20 files, ~2,750 LOC)
| File | Purpose |
|------|---------|
| `src/pg_backend.py` | PostgreSQL SKIP LOCKED task queue |
| `src/gpu_discovery.py` | SSH nvidia-smi fleet probe |
| `src/gpu_scheduler_v2.py` | VRAM-aware allocation (SQLite) |
| `src/model_router.py` | Unified 4-tier routing |
| `src/worktree_dispatch.py` | Git worktree create/merge/cleanup |
| `src/cost_tracker.py` | Hydra Pulse integration, budgets |
| `src/swarm_mcp.py` | MCP server for swarm ops |
| `src/gen_verify.py` | Generator-verifier loop engine |
| `src/living_spec.py` | Context Bridge spec wrapper |
| `src/pipelines/gen_verify_loop.py` | Gen-verify pipeline |
| `src/pipelines/test_fix_loop.py` | Self-correcting test pipeline |
| `src/pipelines/hierarchical.py` | Planner→Manager→Worker |
| `config/routing_v3.yaml` | Extended model routing config |
| 7 test files | ~750 LOC tests |

### Modified (15 files)
| File | Changes |
|------|---------|
| `src/hydra_dispatch.py` | Worktree, SWARM_TASK_ID, model router, KV-cache |
| `src/task_queue.py` | cost_budget_usd, actual_cost_usd fields |
| `src/backend.py` | Add `postgres` backend option |
| `src/gpu_slots.py` | Dynamic discovery, deprecate hardcoded slots |
| `src/pipeline.py` | Add `loop_to` for generator-verifier |
| `src/auto_dispatch.py` | Use model_router, budget checks |
| `src/performance_rating.py` | KV-cache weight, cost metric |
| `src/dashboard.py` | Cost column, GPU view, warm models |
| `src/swarm_cli.py` | gpu discover, costs, dispatch --worktree |
| `src/swarm_metrics.py` | task_cost, vram_allocated, warm_models |
| `src/work_generator.py` | Delegate to model_router |
| `src/collaborative.py` | CB-backed context mode |
| `config/swarm.yaml` | gpu_scheduler, cost_tracking, mcp sections |

---

## Implementation Phases

### Phase 1: GPU + Cost Foundation (5 days, ~800 LOC)
1. **GPU discovery** — SSH nvidia-smi probe across fleet, structured inventory
2. **VRAM-aware scheduler** — SQLite-backed allocation replacing lockfile slots
3. **Wire into dispatch** — Check VRAM before GPU tasks
4. **Per-task cost tracking** — SWARM_TASK_ID injection, Hydra Pulse query
5. **Budget enforcement** — Dashboard cost column, Prometheus metrics

### Phase 2: Core Backports (5 days, ~700 LOC)
6. **Git worktree isolation** — Create before dispatch, merge after, cleanup
7. **Wire worktree into dispatch** — Pre/post hooks in dispatch path
8. **PostgreSQL backend** — Backport from NAI Swarm, optional
9. **Unified model router** — Replace 3 scattered implementations
10. **Integration testing** — Full dispatch cycle end-to-end

### Phase 3: Advanced Patterns (5 days, ~650 LOC)
11. **Generator-verifier loop** — Extend pipeline.py with loop_to
12. **Self-correcting test loop** — pytest → parse → fix → repeat
13. **Living spec via Context Bridge** — cb_ingest/cb_peek for pipeline context
14. **KV-cache routing** — Query Ollama /api/ps, warm model scoring
15. **Hierarchical planning** — Planner→Manager→Worker pipeline

### Phase 4: Platform (5 days, ~600 LOC)
16-17. **Swarm MCP server** — 7 tools: task_create, task_list, task_claim, dispatch, status, gpu_status, pipeline_run
18. **CLI additions** — gpu discover, costs, dispatch --worktree
19. **Dashboard upgrades** — GPU scheduler view, cost graphs
20. **Docs + public fork sync**

---

## Public Fork Strategy

**Public (swarm_base/):** GPU discovery, GPU scheduler, model router, worktree dispatch, generator-verifier, pipeline loop_to, PostgreSQL backend, MCP server, all pipeline definitions, all tests for these modules.

**Private:** cost_tracker (Hydra Pulse), living_spec (Context Bridge), KV-cache routing (fleet-specific Ollama endpoints), swarm.yaml (fleet IPs), dashboard cost integration.

Adopt NAI Swarm's `swarm_base/` package layout: shared modules in `src/swarm_base/`, private integrations at `src/` top level.

---

## Verification

1. `pytest` — target 1,057 tests (matching NAI Swarm)
2. Full dispatch cycle: create task → auto-dispatch with worktree → verify cost tracked → merge PR
3. GPU discovery: `swarm gpu discover` returns all 4 GPU nodes with VRAM
4. Model routing: haiku-tier task → dispatches with haiku model, complex → opus
5. Gen-verify: dispatch a feature task, verify loop runs tests and fixes
6. MCP: `curl -X POST /mcp` with tools/call swarm_task_create
7. Dashboard: cost column populated, GPU scheduler shows allocations

---

## Critical Files

- `/opt/claude-swarm/src/hydra_dispatch.py` — dispatch engine (worktree + cost + router)
- `/opt/claude-swarm/src/task_queue.py` — Task dataclass (cost fields)
- `/opt/claude-swarm/src/gpu_slots.py` — GPU management (dynamic discovery)
- `/opt/claude-swarm/src/pipeline.py` — pipeline engine (loop_to)
- `/opt/claude-swarm/src/backend.py` — backend switcher (postgres)
- `/opt/nai-swarm/src/task_queue.py` — PostgreSQL queue to backport
- `/opt/nai-swarm/src/model_router.py` — model routing to backport
- `/opt/nai-swarm/src/gpu_scheduler.py` — GPU scheduler to adapt
- `/var/lib/hydra-pulse/src/main.rs` — cost query API
- `/opt/context-bridge-v2/src/tools/` — MCP tools for living spec
