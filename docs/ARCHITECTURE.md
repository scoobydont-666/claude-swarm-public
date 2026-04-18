# claude-swarm — Architecture

> Skeleton seeded in P1 of DoD plan. Full content populated in P5.

## Purpose

Distributed Claude Code coordination — multi-instance awareness and task sharing across the Hydra fleet. Advisory system; never forces action. Human remains in the loop.

## Topology

- **Primary NFS export**: GIGA (`<primary-node-ip>`) serves `/opt/swarm/`
- **Replica**: miniboss (`<orchestration-node-ip>`) mounts + mirrors to `/opt/swarm-replica/`
- **Orchestrator**: miniboss hosts the 6 systemd units (dashboard, metrics exporter, health monitor, celery worker/beat/flower)
- **Workers**: all fleet members (GIGA, MECHA, MEGA, MONGO) register into the swarm registry

## Subsystems

| Module | Role |
|---|---|
| `src/registry.py` / `registry_redis.py` | Agent registry (NFS and Redis backends) |
| `src/task_queue.py` | Priority + capability matching queue |
| `src/events.py` / `events_redis.py` | Append-only event bus |
| `src/gpu_slots.py` / `gpu_slots_redis.py` | GPU slot leasing |
| `src/gpu_scheduler_v2.py` | VRAM-aware SQLite-backed scheduler |
| `src/sync_engine.py` | NFS↔git bidirectional sync |
| `src/hydra_dispatch.py` / `auto_dispatch.py` | Smart dispatch routing |
| `src/model_router.py` | Canonical 4-tier routing (Opus/Sonnet/Haiku/local) |
| `src/cost_tracker.py` | Per-task cost correlation via hydra-pulse |
| `src/pipelines/` | 12 specialized pipelines (bug_fix, feature_build, etc.) |
| `src/ipc/` | Redis Streams IPC (hydra-ipc-compatible) |
| `src/dashboard.py` | FastAPI dashboard (port 9192) |
| `src/swarm_metrics.py` | Prometheus exporter (port 9191) |

## Data Flow

```
claude-code hook  →  swarm-heartbeat  →  Redis Streams (miniboss:6379)
                                        ↓
                                 agent_registry (Redis)
                                        ↓
                              task_queue (Redis SKIP-LOCKED)
                                        ↓
                        hydra_dispatch → model_router → Claude API
                                        ↓
                            SWARM_TASK_ID → hydra-pulse (cost tracking)
                                        ↓
                                  result → NFS artifacts
                                        ↓
                              git sync → claude-config remote
```

## Fork Lineage

claude-swarm is the parent of `/opt/nai-swarm/` (forked 2026-03-22 for Nutanix NAI+NKP). The two tracks share an ancestor but evolve independently. Backports flow NAI → claude-swarm when patterns are general-purpose (precedent: commit `0cec9cb`). Nutanix-specific code (Prism RBAC, NAI vLLM, AFS, NKE) does NOT backport.

## Persistence

- **Hot**: Redis (miniboss:6379) — agent registry, task queue, event streams
- **Warm**: SQLite (`data/agents.db`, `data/health-events.db`) — historical queryability
- **Cold**: NFS artifacts (`/opt/swarm/artifacts/`) — session handoffs, checkpoints, dispatch records
- **Durable**: git (`claude-config` repo) — cron-synced every 15 min

## See Also

- `docs/DEPLOYMENT.md` — how to stand up a new node
- `docs/RELIABILITY_FEATURES.md` — failure modes + recovery
- `docs/HANDOFF.md` — session handoff protocol
- `docs/remote-orchestration.md` — smart dispatch deep dive
