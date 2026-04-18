# claude-swarm — Handoff Guide

Who to call, where the bodies are buried, how to run this thing without crying.

## TL;DR — 60-second orientation

1. **Start here**: `README.md` for architecture, `plans/` for roadmap history.
2. **Live state**: `/opt/swarm/` on the NFS primary (node_gpu). `status/<host>.json`
   shows which nodes are alive. `tasks/pending/` is the inbox.
3. **Health dashboard**: `http://node_primary:9192` — `/live /ready /metrics`
4. **Runbook**: `docs/RUNBOOK.md` — credential rotation, DLQ recovery, NFS failover.
5. **Active plan**: `<hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md`

## Mental model

```
NFS (node_gpu primary ─rsync→ node_primary replica)
   ├── Coordination layer        (tasks/, messages/, events/, artifacts/)
   ├── Observability layer       (health_monitor, dashboard, event_log)
   ├── Reliability layer         (circuit breakers, ttl_cache, with_retry, DLQ)
   └── Integration layer         (auto-dispatch, pipelines, worktrees, GPU slots)
```

All writes are atomic (`.tmp` + `rename`). All claims use `fcntl.flock`. When in
doubt about concurrency, trust the file semantics — they're the contract.

## Critical files you should know

| Path | Role |
|------|------|
| `src/swarm_cli.py` | CLI entrypoint — every `swarm <cmd>` goes through here |
| `src/swarm_lib.py` | Core library — atomic file ops, lock primitives |
| `src/health_monitor.py` | 1s loop running all RULES in `health_rules.py` |
| `src/auto_dispatch.py` | Claims unblocked tasks for this node's capabilities |
| `src/dashboard.py` | FastAPI app on :9192, split `/live` vs `/ready` probes |
| `src/event_log.py` | Append-only JSONL event bus with schema versioning |
| `src/ttl_cache.py` | 5s TTL decorator — wraps `_prom_query` hot path (F5) |
| `src/prom_circuit_breaker.py` | 5-of-10 failure breaker on Prometheus calls (E7) |
| `src/retry.py` | Centralized `with_retry(…)` — do NOT hand-roll retries anymore |
| `src/cb_schema.py` | Context-Bridge payload schema (v1.0.0) |
| `src/events_schema.py` | Event-bus payload schema + validators |
| `swarm.yaml` | Runtime config — thresholds, endpoints, health rules |

## Where things live on disk

```
/opt/swarm/                              # NFS primary (node_gpu)
├── status/<host>.json                   # heartbeat (updated every 30s)
├── tasks/{pending,claimed,done,dlq}/    # task board
├── messages/<host>/{inbox,archive}/     # per-node messaging
├── artifacts/                           # shared files (alias-addressable)
├── events/                              # event-bus JSONL
├── checkpoints/                         # session handoff state
├── summaries/                           # session summaries
└── logs/                                # structured logs

/opt/swarm-replica/                      # node_primary rsync replica (every 60s)
/opt/claude-swarm/                       # this repo (source of truth)
```

## Daily ops

### Check fleet health
```bash
python3 /opt/claude-swarm/src/swarm_cli.py status
curl -s http://node_primary:9192/ready | jq .
curl -s http://node_primary:9192/metrics | grep swarm_
```

### Work the task board
```bash
swarm inbox                              # messages for this host
swarm tasks list                         # all pending
swarm tasks claim <task-id>              # claim + start
swarm tasks done <task-id> --artifacts=<alias>
```

### Investigate a hung task
```bash
cat /opt/swarm/tasks/claimed/<task-id>.yaml
# Check claimed_at vs now + 2×estimated_minutes (auto-reap threshold)
tail -50 /opt/swarm/logs/health_monitor.jsonl | grep <task-id>
```

### DLQ inspection + replay
```bash
ls /opt/swarm/tasks/dlq/                 # 72h rolling window
swarm dlq show <task-id>
swarm dlq replay <task-id>               # re-enqueues with retries=0
```

### NFS replica drift
```bash
# Health monitor auto-flags at 120s drift; manual check:
stat -c '%Y' /opt/swarm /opt/swarm-replica
# Re-seed replica (requires stop + rsync + start):
sudo systemctl stop claude-swarm-replica-sync
sudo rsync -aH --delete /opt/swarm/ /opt/swarm-replica/
sudo systemctl start claude-swarm-replica-sync
```

### Credentials rotation
- Redis password: `docs/RUNBOOK.md` §Credentials (2-min procedure — **rotate soon**,
  old password leaked to git history on a prior commit; private repo limits blast
  radius but defense-in-depth says rotate).
- `SWARM_API_KEY`: dashboard middleware checks `X-Swarm-Api-Key`. Rotate via systemd
  env file + `systemctl restart claude-swarm-dashboard`.

## Failure modes + fixes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `/ready` returns 503 | NFS mount stale OR Prom unreachable | Check `mount /opt/swarm`, `curl $PROM/api/v1/labels` |
| Dashboard 401 on all endpoints | `SWARM_API_KEY` missing/rotated | Set header `X-Swarm-Api-Key: ...` |
| Tasks stuck in `claimed/` | Claimer crashed before ack | Auto-reaped at `claimed_at + 2×estimated` |
| Events missing after rollback | 30-day prune cut old window | Restore from `/opt/swarm-replica/events/` |
| Prometheus queries all returning `[]` | Circuit breaker OPEN | Check `/metrics` for `prom_breaker_state`; waits 30s→5m backoff |
| Duplicate rule fires per 1s tick | Cache miss on `_prom_query` | Verify TTL cache stats: `cache_stats()` should show >0.5 hit rate |
| High API costs | Unthrottled auto-dispatch loop | Check `cost_tracker` budget; set `SWARM_DAILY_BUDGET_USD` |
| Worktree merge conflicts | Two agents edited same file | `conflicts.py` flags pre-merge; review `/opt/swarm/logs/conflicts.jsonl` |

## Contracts — do not break these

- **Task YAML schema** — `src/cb_schema.py` + `tests/test_cb_schema.py`. Changes require
  a `schema_version` bump + back-compat shim for 1 release.
- **Event payload schema** — `src/events_schema.py`. All producers must round-trip
  through `validate_event()`. CI enforces this.
- **Idempotency-Key header** — `nai-reserve` endpoints. Clients SHOULD send a UUID;
  server dedupes within 24h window using a partial unique index.
- **Atomic writes** — NEVER `open(path, "w")` directly in `/opt/swarm/`. Use
  `swarm_lib.atomic_write(path, content)` which writes `.tmp` + `os.rename`.

## Performance notes

- Health monitor loop is **1s**. Add expensive checks to the 10s cadence, not the 1s.
- Prometheus TTL cache: 5s. If you need fresher data, bypass via `_prom_query.cache_clear()`
  BEFORE your query — do NOT change the TTL.
- NFS fsync is slow (~5-20ms on node_gpu). Batch writes when possible.
- Event log append is lock-free (O_APPEND atomic for lines <4KB).

## Runbooks

| Scenario | File |
|----------|------|
| Full cluster restart | `docs/RUNBOOK.md` §Cold start |
| Node reintegration (was offline >24h) | `docs/RUNBOOK.md` §Reintegration |
| DLQ flood (>100 in 24h) | `docs/RUNBOOK.md` §DLQ flood |
| Replica re-seed | see "NFS replica drift" above |
| Credential rotation | `docs/RUNBOOK.md` §Credentials |
| Dashboard unreachable | `docs/RUNBOOK.md` §Dashboard recovery |

## Conventions

- **Branch naming**: `feat/<scope>`, `fix/<scope>`, `docs/<scope>`, `chore/<scope>`.
- **Commit trailer**: always include `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
  when this assistant touches it.
- **Tests**: mirrors `src/` under `tests/`. Run `pytest tests/` before every PR.
- **Lint**: ruff pre-commit hook auto-formats. Don't fight it.
- **Type hints**: required on new public functions. Mypy is not wired yet but coming.
- **`127.0.0.1` not `localhost`** — IPv6 cluster-ism. Always.

## Owners

- **Primary**: Josh (admin@example.com)
- **Co-author**: Claude Code (Opus 4.7 sessions)
- **Fleet**: node_primary (orchestrator), node_gpu (NFS primary + GPU), mecha/mega/mongo (workers)

## When stuck

1. `/opt/swarm/artifacts/checkpoints/<hostname>-current.yaml` is the tactical truth.
2. `cb_peek alias="memory-next-FIRST-*" namespace=<latest session-*>` has Josh's standing orders.
3. `docs/RUNBOOK.md` for procedures.
4. If nothing works: `pkill -f swarm; bash scripts/cold-start.sh`.

Good luck. It's mostly boring. When it's not boring, write a runbook entry so the
next person doesn't have to rediscover the same cliff.
