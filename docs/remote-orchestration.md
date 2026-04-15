# Remote Claude Code Session Orchestration

## The Problem

A single Claude Code session on orchestration-node can SSH to gpu-server-1 and run bash commands, but it can't **think** on gpu-server-1. When a task requires:
- GPU resources (Ollama, ChromaDB, CUDA)
- Docker Swarm context (services, stacks, Traefik)
- Project-local CLAUDE.md and .kin/ context
- Multi-turn debugging with tool use

...the current approach of `ssh gpu-server-1 "some command"` is insufficient. You need a full Claude Code brain on the remote host.

## The Solution: `smart-dispatch`

The swarm now has an intelligent dispatch engine that decides **how** to execute any task across the fleet:

### Execution Strategies

| Strategy | When | How | Cost |
|----------|------|-----|------|
| **LOCAL** | Task needs no remote resources | Execute in current session | Zero overhead |
| **REMOTE_DISPATCH** | Simple task, remote resources needed | `claude -p "task" --max-turns 5` via SSH | Low (one-shot) |
| **REMOTE_SESSION** | Complex task needing investigation | `claude -p "task"` with unlimited turns via SSH | Medium (full session) |
| **COLLABORATIVE** | Needs context exchange between hosts | Remote session + artifact sharing back | High (two sessions) |

### Decision Factors

```
1. Does it need remote resources?
   ├─ GPU/Ollama/ChromaDB → gpu-server-1
   ├─ Docker Swarm management → gpu-server-1
   ├─ Monero/P2Pool → orchestration-node
   └─ None → stay local

2. How complex is it?
   ├─ TRIVIAL (status, check, list) → dispatch, haiku, 3 turns
   ├─ SIMPLE (install, copy, restart) → dispatch, sonnet, 5 turns
   ├─ MODERATE (implement, test, fix) → dispatch, sonnet, 5 turns
   ├─ COMPLEX (debug, architect, refactor) → session, opus, unlimited
   └─ EXPLORATORY (investigate, figure out) → session, opus, unlimited

3. Does it need multi-turn reasoning?
   ├─ Debug keywords → yes → REMOTE_SESSION
   ├─ "figure out", "why does" → yes → REMOTE_SESSION
   ├─ Complex + needs reasoning → yes → REMOTE_SESSION
   └─ Otherwise → no → REMOTE_DISPATCH
```

### Project Affinity

Some projects have inherent host affinity:
- `/opt/christi-project` → gpu-server-1 (needs Ollama GPU)
- `/opt/ai-project` → gpu-server-1 (Docker Swarm manager)
- `/opt/monero-farm` → orchestration-node (fullnode)
- Everything else → current host (avoid unnecessary remote)

## Usage

### Plan without executing
```bash
swarm smart-dispatch "debug why Christi RAG is slow" -p /opt/christi-project --plan-only
```

### Fire and forget (background)
```bash
swarm smart-dispatch "restart Ollama and verify models load"
```

### Wait for result
```bash
swarm smart-dispatch "run full test suite on ExamForge backend" --sync
```

### Force host
```bash
swarm smart-dispatch "check disk usage" --host gpu-server-1
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│            smart-dispatch CLI                     │
│  "debug why Christi RAG returns stale results"   │
└────────────────────┬────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────┐
│          Strategy Decision Engine                 │
│                                                   │
│  1. Classify complexity    → COMPLEX              │
│  2. Check resources needed → GPU/ChromaDB → gpu-server-1  │
│  3. Needs interactive?     → "debug" → YES        │
│  4. Select model           → COMPLEX+interactive   │
│                               → opus               │
│  5. Pick strategy          → REMOTE_SESSION        │
│                                                   │
│  ExecutionPlan:                                    │
│    strategy=REMOTE_SESSION                         │
│    host=gpu-server-1, model=opus, turns=unlimited          │
└────────────────────┬────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────┐
│          SSH to gpu-server-1                              │
│  claude --permission-mode bypassPermissions       │
│    --model opus                                   │
│    -p "debug why Christi RAG returns stale..."    │
│                                                   │
│  Claude Code on gpu-server-1:                             │
│  - Loads /opt/christi-project/CLAUDE.md            │
│  - Has access to ChromaDB at 127.0.0.1:8100       │
│  - Can query Ollama at 127.0.0.1:11434            │
│  - Reads files, runs tests, checks logs           │
│  - Multi-turn reasoning until solved               │
└─────────────────────────────────────────────────┘
```

## Future: Collaborative Mode

The next evolution is **COLLABORATIVE** — where the orchestrating session and the remote session exchange context mid-flight:

1. Orchestrator spawns remote session with initial prompt
2. Remote session works, hits a blocker, writes to shared artifact
3. Orchestrator detects the artifact, reasons about the blocker
4. Orchestrator sends updated context to the remote session via swarm message
5. Remote session continues with new context

This enables **distributed debugging**: orchestration-node can reason about the architecture while gpu-server-1 investigates the running system, and they converge on a solution.

## Cost Implications

| Strategy | Model | Typical Turns | Est. Cost |
|----------|-------|--------------|-----------|
| LOCAL | inherited | — | $0 extra |
| REMOTE_DISPATCH (haiku) | haiku | 3 | ~$0.01 |
| REMOTE_DISPATCH (sonnet) | sonnet | 5 | ~$0.05 |
| REMOTE_SESSION (sonnet) | sonnet | 20-50 | ~$0.50 |
| REMOTE_SESSION (opus) | opus | 20-50 | ~$1.00 |
| COLLABORATIVE (opus) | opus×2 | 50+ | ~$2.00 |

The key insight: **most tasks are DISPATCH-tier** (trivial/simple). The engine correctly routes 80% of work to cheap one-shot calls, reserving expensive interactive sessions for genuinely complex problems.
