# claude-swarm

Multi-instance Claude Code coordination system — NFS-backed task board, messaging, artifact
sharing, worktree management, GPU slot allocation, and auto-dispatch across the Hydra cluster.

## Features

### Coordination primitives
- **NFS-backed task board** — atomic file ops (`pending/claimed/done`) with `fcntl.flock`
  for race-free claims across nodes
- **Event bus** — append-only JSONL event log with schema versioning and 30-day prune
- **Messaging** — per-node inbox (`messages/<host>/`) with archive + search
- **Artifacts** — shared file store with aliased retrieval across instances
- **Status heartbeats** — per-node `status/<host>.json` with configurable stale thresholds

### Orchestration
- **Auto-dispatch** — claims unblocked tasks matching this node's capabilities
- **Pipelines** — chained task graphs with dependency resolution + retry
- **Worktrees** — parallel branch checkouts via `git worktree`
- **GPU slot allocation** — reserve-and-release model for shared GPU nodes
- **Credential broker** — scoped, short-lived secret lending between agents
- **Conflict detection** — flags overlapping writes across active worktrees

### Reliability & ops
- **Circuit breaker on Prometheus** — 5-of-10 failure window opens breaker for 30s–5m
  exponential cooldown so health_monitor doesn't hammer a down Prom instance
- **TTL cache on hot paths** — 5s identical-query cache cuts PromQL outbound volume 10–50x
- **DLQ prune** — 72h rolling window on failed-task DLQ (spans weekends)
- **Event log prune** — 30-day retention, append-only, bounded size
- **K3s probes** — split `/live` vs `/ready` semantics; ready gates on NFS + DB health
- **API-key middleware** — `SWARM_API_KEY` header enforcement on dashboard endpoints
- **SSRF guard** — prometheus_url validated against loopback/private-range allowlist
- **Centralized `with_retry`** — shared decorator with jitter, max-retries, backoff
- **Contract CI** — schema validation for CB + event-bus payloads wired into CI
- **Idempotency-Key** — opt-in header support on mutating endpoints (nai-reserve)
- **NFS drift detection** — primary-vs-replica mtime comparison, flags at 120s drift
- **Task deadline enforcement** — tasks exceeding `claimed_at + 2×estimated` get re-queued

### Observability
- **Prometheus metrics** — task counts, dispatch latency, event-bus throughput, breaker state
- **Structured logs** — JSONL to `/opt/swarm/artifacts/logs/` + stdout
- **Dashboard** — `/live /ready /metrics /events /tasks /status` on port 9192
- **Cost tracker** — API token usage per agent/task with budget alerts

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │         /opt/swarm/  (NFS share)     │
                    │                                       │
                    │  status/      per-node heartbeat JSON │
                    │  tasks/       pending/claimed/done    │
                    │  artifacts/   shared files            │
                    │  messages/    inbox/ + archive/       │
                    │  events/      append-only event log   │
                    └───────────┬─────────────┬────────────┘
                                │             │
              ┌─────────────────┘             └──────────────────┐
              │                                                    │
   node_gpu (<primary-node-ip>)                        node_primary (<orchestration-node-ip>)
   NFS primary                                   NFS replica
   /opt/swarm/ (export)                          /opt/swarm-replica/ (rsync)
   primary inference + swarm mgr                 monitoring + git sync
              │                                    │
              └──────────── git sync ───────────────┘
                          your-github-user/claude-config
                          (remote durability, 60s interval)

   node_reserve2 (<gpu-worker-ip>) ──── NFS client ──── mounts /opt/swarm/
   (worker node, ComfyUI, inference)

Claude Code instances on any host:
  └── hooks/swarm-*.sh → swarm CLI → atomic file ops on NFS share
```

- NFS provides sub-second coordination for local instances
- Git provides durability and sync for remote/offline instances
- All writes are atomic (write to `.tmp`, then rename)
- File locking via `fcntl.flock()` prevents race conditions on task files

## Prerequisites

- Python 3.10+
- `typer`, `pyyaml`, `rich` Python packages
- NFS mount at `/opt/swarm/` (setup scripts provided — requires sudo)
- `git` with access to `your-github-user/claude-config`

## Quick Start

### 1. Set Up NFS

```bash
# On node_gpu (NFS primary):
sudo bash scripts/setup-primary.sh

# On node_primary (NFS replica):
sudo bash scripts/setup-replica.sh

# On any other cluster host:
sudo bash scripts/setup-client.sh
```

### 2. Install Python Dependencies

```bash
pip install typer pyyaml rich
```

### 3. Alias the CLI

```bash
alias swarm='python3 /opt/claude-swarm/src/swarm_cli.py'
```

Add to `.bashrc` or `.zshrc` for persistence.

### 4. Verify

```bash
swarm health         # full cluster health check
swarm status         # show all nodes + their state
```

### 5. Install Hooks (Manual)

```bash
cp hooks/swarm-*.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/swarm-*.sh
```

Then add hook entries to Claude Code `settings.json`. Do not auto-install — integrate manually.

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| CLI | Python 3.10+ | Task dispatch, heartbeat polling, status queries |
| Coordination | NFS (primary) | Sub-second atomic file operations on node_gpu |
| Durability | Git + rsync | Replica on node_primary, remote sync to claude-config |
| Task Queue | YAML files | Structured state in `/opt/swarm/tasks/` |
| Registry | JSON (fcntl) | Per-node heartbeat with file locking |
| Database | SQLite | Agent tracking, session state |
| Scheduler | APScheduler | Health monitor daemon, sync cadence |
| Metrics | Prometheus | Task queue depth, GPU slot utilization, rate-limit events |
| Config | YAML | `swarm.yaml` for shared settings |

## NFS Share Structure

```
/opt/swarm/
├── status/                  # Per-node JSON heartbeat files
│   └── <hostname>.json
├── tasks/
│   ├── pending/             # Unclaimed tasks (YAML)
│   ├── claimed/             # In-progress tasks
│   └── completed/           # Done tasks (archived)
├── artifacts/               # Shared files between instances
├── messages/
│   ├── inbox/               # Per-node + broadcast directories
│   └── archive/             # Read messages
├── events/                  # Append-only event log
└── config/
    └── swarm.yaml           # Shared swarm configuration
```

## Configuration

Config file: `config/swarm.yaml`

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `swarm.name` | `hydra-swarm` | Swarm identifier |
| `nfs.primary` | `<primary-node-ip>:/opt/swarm` | NFS primary export |
| `nfs.replica` | `<orchestration-node-ip>:/opt/swarm-replica` | Replica for HA |
| `nfs.mount_point` | `/opt/swarm` | Local mount path |
| `nfs.sync_interval_seconds` | `30` | NFS→replica rsync interval |
| `git.repo` | `your-github-user/claude-config` | Remote durability repo |
| `git.sync_interval_seconds` | `60` | Git sync cadence |
| `git.sync_on_task_complete` | `true` | Sync immediately on task done |
| `heartbeat.interval_seconds` | `60` | Node heartbeat cadence |

### Claude Backend (CLAUDE_BACKEND)

The swarm can call Claude via multiple backends. Set `CLAUDE_BACKEND` to control which one:

| Value | Behavior | Requirements |
|-------|----------|--------------|
| `cli` (forced) | Uses `claude` CLI (OAuth-authenticated, no API key) | Claude Code installed on PATH |
| `sdk` (forced) | Uses Anthropic SDK | `ANTHROPIC_API_KEY` env var set |
| `auto` (default) | Tries CLI first; falls back to SDK if CLI unavailable | Either Claude Code or API key |

Example:
```bash
# Use CLI (Claude Code auth)
export CLAUDE_BACKEND=cli
python3 src/claude_backend.py

# Use SDK (API key)
export CLAUDE_BACKEND=sdk
export ANTHROPIC_API_KEY=sk-ant-...
python3 src/claude_backend.py

# Auto-fallback (tries CLI, uses SDK as fallback)
export CLAUDE_BACKEND=auto
python3 src/claude_backend.py
```

Related environment variables:
- `CLAUDE_CLI_TIMEOUT_SEC` — timeout for CLI calls (default: 180)

## CLI Commands

### Node Status

| Command | Description |
|---------|-------------|
| `swarm status` | Show all nodes with state, last heartbeat, active task |
| `swarm health` | Full cluster health check (NFS, git, node reachability) |

### Task Board

| Command | Description |
|---------|-------------|
| `swarm tasks` | List all tasks (pending, claimed, completed) |
| `swarm tasks create "title"` | Create a new task |
| `swarm tasks claim <id>` | Claim a task for this host |
| `swarm tasks complete <id>` | Mark a task done |
| `swarm tasks decompose <id>` | Break a task into subtasks |

### Messaging

| Command | Description |
|---------|-------------|
| `swarm message <host> "text"` | Direct message to a node |
| `swarm message --broadcast "text"` | Broadcast to all nodes |
| `swarm inbox` | Read incoming messages |

### Artifacts

| Command | Description |
|---------|-------------|
| `swarm artifacts` | List shared artifacts |
| `swarm artifacts list` | Explicit list command |
| `swarm artifacts share <file>` | Copy file to shared artifact store |

### Worktrees

| Command | Description |
|---------|-------------|
| `swarm worktrees` | List active git worktrees across nodes |

### Summaries and Context

| Command | Description |
|---------|-------------|
| `swarm summaries` | Show session summaries from all nodes |
| `swarm context` | Aggregate context for current task |

### Pipelines and Dispatch

| Command | Description |
|---------|-------------|
| `swarm pipeline` | List registered pipelines |
| `swarm pipeline run <name>` | Run a named pipeline |
| `swarm pipeline status` | Show pipeline execution status |
| `swarm pipeline history` | Show pipeline run history |
| `swarm dispatches` | List auto-dispatch records |
| `swarm dispatches show <id>` | Show dispatch detail |
| `swarm dispatches tail` | Tail live dispatch log |

### Collaboration

| Command | Description |
|---------|-------------|
| `swarm collab start` | Start collaborative session |
| `swarm collab status` | Show collaboration state |
| `swarm collab resolve` | Resolve a conflict |
| `swarm collab blockers` | List blocking issues |

### Maintenance

| Command | Description |
|---------|-------------|
| `swarm sync` | Force git sync now |
| `swarm cleanup` | Remove stale files and expired tasks |
| `swarm dashboard` | Live terminal dashboard (rich TUI) |
| `swarm generate` | AI-assisted task generation |

## Node States

| State | Meaning |
|-------|---------|
| `active` | Instance running, available for coordination |
| `idle` | No active session |
| `offline` | No heartbeat for more than 5 minutes |
| `busy` | Active — do not interrupt |

## Testing

```bash
cd /opt/claude-swarm
pytest tests/ -v                 # 1,270 tests (69% coverage, gate ≥65%)
pytest tests/ -k "gpu"           # filter by keyword
pytest tests/ --tb=short         # compact failure output
```

Test files mirror source modules (e.g., `test_gpu_slots.py` → `gpu_slots.py`).

## Development

### Source Structure

```
/opt/claude-swarm/
├── src/
│   ├── swarm_cli.py          # typer CLI — all user-facing commands
│   ├── swarm_lib.py          # core coordination primitives
│   ├── registry.py           # node registry (status file R/W)
│   ├── events.py             # event log (append-only)
│   ├── event_log.py          # event log reader/query
│   ├── session.py            # session lifecycle management
│   ├── agent_db.py           # agent tracking DB
│   ├── gpu_slots.py          # GPU slot allocation across nodes
│   ├── auto_dispatch.py      # auto task dispatch engine
│   ├── hydra_dispatch.py     # Hydra-specific dispatch rules
│   ├── rate_limiter.py       # Claude API rate-limit detection
│   ├── pipeline.py           # pipeline execution engine
│   ├── pipeline_registry.py  # pipeline YAML registry
│   ├── collaborative.py      # collaborative session primitives
│   ├── conflicts.py          # conflict detection + resolution
│   ├── sync_engine.py        # NFS↔git sync engine
│   ├── health_monitor.py     # cluster health monitoring daemon
│   ├── health_rules.py       # alerting rules engine
│   ├── remediations.py       # auto-remediation actions
│   ├── crash_handler.py      # crash detection + recovery
│   ├── dashboard.py          # rich terminal dashboard
│   ├── swarm_metrics.py      # Prometheus metrics export
│   ├── remote_session.py     # remote host session management
│   ├── work_generator.py     # AI-assisted work item generation
│   ├── launcher.py           # process launcher
│   └── util.py               # shared utilities
│
│   └── pipelines/
│       ├── bug_fix.py          # bug-fix pipeline definition
│       ├── feature_build.py    # feature build pipeline
│       ├── question_generation.py  # ExamForge question gen pipeline
│       └── security_audit.py   # security audit pipeline
│
├── tests/                    # 1,270 tests (69% coverage, gate ≥65%) (42 test files)
├── hooks/                    # Claude Code hook scripts
│   ├── swarm-session-start.sh
│   ├── swarm-session-end.sh
│   ├── swarm-heartbeat.sh
│   ├── swarm-heartbeat-fast.sh
│   └── swarm-task-check.sh
├── config/
│   └── swarm.yaml            # shared cluster configuration
└── deploy/                   # systemd units + install scripts
```

### Design Principles

- Advisory only — coordinates but never forces action on a Claude instance
- Human in the loop — auto-claim is OFF; human decides task ownership
- Atomic writes — write to `.tmp`, rename (prevents partial reads)
- File locking — `fcntl.flock()` on task files (prevents race conditions)
- Git safety — never force-push, always pull-rebase first
- Belt and suspenders — NFS primary on node_gpu, replica on node_primary, git for durability

## Deployment

### Systemd Services

```bash
# Install health monitor and metrics exporter
sudo cp deploy/swarm-health-monitor.service /etc/systemd/system/
sudo cp deploy/swarm-metrics-exporter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now swarm-health-monitor swarm-metrics-exporter
```

Available units in `deploy/`:

| Unit | Description |
|------|-------------|
| `swarm-health-monitor.service` | Cluster health polling daemon |
| `swarm-health.service` | One-shot health check |
| `swarm-dashboard.service` | Terminal dashboard (tmux session) |
| `swarm-metrics-exporter.service` | Prometheus metrics endpoint |

### Monitoring

Prometheus alerts config: `deploy/swarm-alerts.yml`

Metrics exposed include: node online status, task queue depth, dispatch counts,
GPU slot utilization, rate-limit events.

## v2 Sprint Status

| Sprint | Scope | Status |
|--------|-------|--------|
| Phase 1 | NFS mount setup, CLI skeleton | Complete |
| Phase 2 | Task board (create, claim, complete) | Complete |
| Phase 3 | Messaging (direct + broadcast) | Complete |
| Phase 4 | Artifact sharing, health checks | Complete |
| Phase 5 | Event log, worktrees, summaries | Complete |
| v2 S1 | Registry v2, structured events | Complete |
| v2 S2 | Worktree awareness + git integration | Complete |
| v2 S3 | GPU slot allocation | Complete |
| v2 S4 | Auto-dispatch + smart dispatch | Complete |
| v2 S5 | Auto-scale + rate-limit detection | Complete |
| v2 S6 | Parallel sync, priority queues, metrics | Complete |
| v2 S7 | Redis backend, Celery integration | Complete |
| v2 S8 | Performance rating, scored dispatch | Complete |
| v2 S9 | Backend parity polish — 1,270 tests (69% coverage, gate ≥65%) | Complete |

## Related Projects

| Project | Location | Relationship |
|---------|----------|-------------|
| Project Hydra | <hydra-project-path>/ | Umbrella — swarm coordinates all Hydra heads |
| hydra-pulse | /opt/hydra-pulse/ | Consumes SWARM_TASK_ID for cost-per-task analytics |
| claude-config | /opt/claude-configs/claude-config/ | Hook scripts + swarm config synced here |
| ProjectA | <project-a-path>/ | Primary beneficiary of multi-agent dispatch |
| ExamForge | /opt/examforge/ | question_generation pipeline runs via swarm |

## License

MIT. See [LICENSE](LICENSE).
