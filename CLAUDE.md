# claude-swarm — Distributed Claude Code Coordination

## Status
- `status: PRODUCTION-INTERNAL`
- `last_verified: 2026-04-18`
- `owner: josh`
- `tier: infra`
- `peer_fork: nai-swarm (Nutanix-track, diverged 2026-03-22 — DO NOT conflate; see /home/josh/.claude/projects/-opt-hydra-project/memory/feedback_claude_swarm_vs_nai_swarm.md)`

## Routing Protocol v1 (2026-04-18)
claude-swarm is the substrate for `routing-protocol-v1` — the coordinator ↔ worker contract between Claude Code terminal sessions and fleet workers. **Spec:** `/opt/hydra-project/docs/routing-protocol-v1.md`.

Modules added 2026-04-18:
- `config/routing.yaml` — tier_ladder, dispatch_class, host_slots, cascade config (§3-7)
- `src/credential_broker.py` + `src/broker_client.py` + systemd unit — workers never hold keys (§12)
- `src/context_assembly.py` — CB-augmented prompt packaging, per-tier token budget (§5, coordinator-side)
- `src/worker_context_assembly.py` — **NEW (Phase 4)** — Worker-side CB delta-mode context assembly, tier budgets: worker-sm (8k), worker-md (16k), worker-lg (32k)
- `src/heartbeat.py` — 30s ping / 90s timeout / 5min stuck detection (§7,§10)
- `src/session_report.py` — end-of-session markdown + CB-index (§15)
- `src/state_store_cascade.py` — Redis → CB → SQLite → fail-closed (§6)

Dashboard panels live in hydra-sentinel (`routing_panels.py` at `/routing/*`).
Enforcement hooks in `~/.claude/hooks/routing_*.py` (coordinator-side).

## Project Location
/opt/claude-swarm/

## Purpose
Multi-instance Claude Code awareness and task sharing via NFS + git.
Advisory coordination system — never forces action, human in the loop.

## Architecture
- NFS primary: GIGA (192.168.200.163) exports /opt/swarm/
- NFS replica: miniboss (192.168.200.213) mirrors to /opt/swarm-replica/ and re-exports
- Git: claude-config repo (scoobydont-666/claude-config) for remote sync + durability
- Local instances: instant coordination via NFS mount at /opt/swarm/
- Remote instances: git sync every 60s or on-demand

## Key Rules
- File locking: `fcntl.flock()` on task files to prevent race conditions
- Status files: atomic write (write to .tmp, rename)
- Git sync: never force-push, always pull-rebase first
- Auto-claim is OFF — human decides task ownership
- NFS setup requires sudo on target hosts — scripts provided but not auto-run

## CLI
```bash
swarm status                      # Show all nodes
swarm tasks                       # List tasks
swarm tasks create "title"        # New task
swarm tasks claim <id>            # Claim task for this host
swarm tasks complete <id>         # Mark done
swarm message <host> "text"       # Direct message
swarm message --broadcast "text"  # Broadcast
swarm inbox                       # Check messages
swarm artifacts list              # List shared artifacts
swarm artifacts share <file>      # Share a file
swarm health                      # Health check
swarm sync                        # Force git sync
```

## Dependencies
- Python 3.10+
- typer, pyyaml, rich (pip install)
- NFS mount at /opt/swarm/ (setup scripts provided)

## Phases
| Phase | Scope |
|-------|-------|
| Phase 1 | NFS mount setup + swarm CLI skeleton |
| Phase 2 | Task board (create, claim, complete) |
| Phase 3 | Messaging (direct + broadcast) |
| Phase 4 | Artifact sharing + health checks |
| Phase 5 | ✅ Complete — 1,270 tests, 69% coverage (2026-04-18 DoD pass) |
| v2 S1-S5 | ✅ Complete — registry, events, worktrees, GPU slots, auto-dispatch, auto-scale, rate-limit detection |

## Worker Context Assembly (Phase 4, 2026-04-18)
Extends coordinator-side CB context assembly to worker subagents. Workers now receive CB-assembled context instead of full file reads, achieving token savings comparable to coordinator (97.5% savings per project_cb_model_routing.md).

### Configuration
- **Default mode:** `delta` (CB-assembled context, **enabled by default**)
- **Opt-out:** `context_mode=full` for legacy full-file dispatch (deprecation warning logged)
- **Environment variable:** `SWARM_WORKER_CONTEXT_MODE=delta|full|disabled`

### Worker Tiers
| Tier | Context Window | CB Budget | Task Budget | File Budget |
|------|---|---|---|---|
| `worker-sm` | 8k | 1k | 500 | 2k |
| `worker-md` | 16k | 3k | 1k | 4k |
| `worker-lg` | 32k | 8k | 2k | 8k |

Workers auto-select tier based on task complexity ("small"/"quick" → sm, "large"/"complex" → lg, default → md).

### Metrics
Prometheus gauges emitted per dispatch:
- `routing_worker_context_bytes{dispatch_id,worker_tier}` — assembled context size
- `routing_worker_context_savings_pct{dispatch_id,worker_tier}` — size reduction vs. full context

### Testing
26 unit tests in `tests/test_worker_context_assembly.py`:
- Tier budget compliance (3 tests)
- Delta mode (5 tests)
- Legacy full mode (2 tests)
- Template rendering (4 tests)
- CB fallback + caching (3 tests)
- Context savings metrics (2 tests)
- Default tier selection (1 test)

### Integration
Worker context is assembled in `hydra_dispatch.dispatch()` before task is sent to worker. Falls back gracefully to original task on any error (degraded context, no failure).

## Hooks
Hooks in `hooks/` directory — install to `~/.claude/hooks/` when ready.
Do NOT auto-install; Josh integrates manually.
