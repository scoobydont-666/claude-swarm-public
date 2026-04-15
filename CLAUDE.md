# claude-swarm — Distributed Claude Code Coordination

## Project Location
/opt/claude-swarm/

## Purpose
Multi-instance Claude Code awareness and task sharing via NFS + git.
Advisory coordination system — never forces action, human in the loop.

## Architecture
- NFS primary: gpu-server-1 (10.0.0.1) exports /var/lib/swarm/
- NFS replica: orchestration-node (10.0.0.5) mirrors to /var/lib/swarm-replica/ and re-exports
- Git: claude-config repo (your-github-user/claude-config) for remote sync + durability
- Local instances: instant coordination via NFS mount at /var/lib/swarm/
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
- NFS mount at /var/lib/swarm/ (setup scripts provided)

## Phases
| Phase | Scope |
|-------|-------|
| Phase 1 | NFS mount setup + swarm CLI skeleton |
| Phase 2 | Task board (create, claim, complete) |
| Phase 3 | Messaging (direct + broadcast) |
| Phase 4 | Artifact sharing + health checks |
| Phase 5 | ✅ Complete — 691 tests |
| v2 S1-S5 | ✅ Complete — registry, events, worktrees, GPU slots, auto-dispatch, auto-scale, rate-limit detection |

## Hooks
Hooks in `hooks/` directory — install to `~/.claude/hooks/` when ready.
Do NOT auto-install; Josh integrates manually.
