# Claude Swarm v2 — Distributed Autonomous Coordination

## Goal
Replace advisory swarm with autonomous execution engine. Multiple Claude Code
instances coordinate via shared NFS state, pick up work, avoid conflicts,
and hand off context — no human orchestration needed.

## Phases

### S1: Agent Registry + Heartbeat
```yaml
tasks:
  - id: s1-registry
    title: Agent registry module
    requires: []
    host: any
    acceptance: agents register/deregister, stale detection works
    files:
      - src/registry.py
      - tests/test_registry.py

  - id: s1-heartbeat
    title: Heartbeat daemon thread
    requires: []
    host: any
    depends_on: [s1-registry]
    acceptance: heartbeat updates every 60s, stale after 5 min

  - id: s1-hooks
    title: Session start/end hooks
    requires: []
    host: any
    depends_on: [s1-registry]
    acceptance: Claude Code auto-registers on start, deregisters on end
```

### S2: Work Queue
```yaml
tasks:
  - id: s2-queue
    title: Priority work queue with file locking
    requires: []
    host: any
    acceptance: atomic claim/release, priority ordering, capability matching
    files:
      - src/queue.py
      - tests/test_queue.py

  - id: s2-lifecycle
    title: Task lifecycle management
    requires: []
    host: any
    depends_on: [s2-queue]
    acceptance: pending→claimed→running→done/failed, auto-requeue on death

  - id: s2-cli
    title: CLI commands for queue management
    requires: []
    host: any
    depends_on: [s2-queue]
    acceptance: swarm tasks list/create/claim/complete/fail
```

### S3: Event Bus + Auto-Sync
```yaml
tasks:
  - id: s3-events
    title: Event emission and consumption
    requires: []
    host: any
    acceptance: events written to /var/lib/swarm/events/, queryable by time range
    files:
      - src/events.py
      - tests/test_events.py

  - id: s3-git-sync
    title: Auto git pull on commit events
    requires: []
    host: any
    depends_on: [s3-events]
    acceptance: when agent A commits+pushes, agent B auto-pulls within 60s

  - id: s3-session-summary
    title: Auto-generate session summary from events
    requires: []
    host: any
    depends_on: [s3-events]
    acceptance: session end produces summary YAML from event stream
```

### S4: Multi-Instance
```yaml
tasks:
  - id: s4-worktrees
    title: Git worktree support for parallel work
    requires: []
    host: any
    acceptance: two agents can work on same repo in different worktrees

  - id: s4-conflict
    title: Conflict detection
    requires: []
    host: any
    depends_on: [s4-worktrees, s3-events]
    acceptance: alert if two agents editing overlapping files

  - id: s4-gpu-slots
    title: GPU resource arbitration
    requires: [gpu]
    host: gpu-server-1
    depends_on: [s1-registry]
    acceptance: GPU slot claiming prevents two agents loading models simultaneously
```

### S5: Self-Orchestration
```yaml
tasks:
  - id: s5-work-gen
    title: Work generator from project plans
    requires: []
    host: any
    depends_on: [s2-queue, s3-events]
    acceptance: scans plans/*.md, creates tasks for incomplete items

  - id: s5-auto-dispatch
    title: Auto-dispatch tasks to matching agents
    requires: []
    host: any
    depends_on: [s5-work-gen, s1-registry]
    acceptance: idle agents auto-claim highest-priority matching task

  - id: s5-auto-scale
    title: Launch new Claude Code instances when queue backs up
    requires: []
    host: any
    depends_on: [s5-auto-dispatch]
    acceptance: if queue depth > threshold, spawn new instance via CLI
    status: COMPLETE — src/launcher.py (AutoScaler), 23 tests
```
