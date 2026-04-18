# claude-swarm — Session Handoff Protocol

> Skeleton seeded in P1. Full content populated in P5.

Each claude-code session writes a session artifact into `/opt/swarm/artifacts/handoffs/<host>-<timestamp>.md` via the `swarm-session-end` hook.

## Artifact Structure

```markdown
---
session_id: <uuid>
host: node_primary | giga | mecha | mega | mongo
started: <ISO-8601>
ended: <ISO-8601>
model: claude-opus-4-7 | claude-sonnet-4-6 | ...
tool_use_count: <int>
token_cost_usd: <float>   # from hydra-pulse
---

## Summary
<1-2 sentence session outcome>

## Commits
- <repo>:<hash> — <message>

## Next Session
<what the next session should pick up>
```

## Reading Handoffs

```bash
ls /opt/swarm/artifacts/handoffs/ | tail -5
swarm handoffs list --host node_primary --since 1d
swarm handoffs show <host>-<timestamp>
```

## Related

- `docs/ARCHITECTURE.md` — where artifacts fit in the data flow
- `src/swarm_cli.py` handoffs subcommand
- `hooks/swarm-session-end` — the writer hook
