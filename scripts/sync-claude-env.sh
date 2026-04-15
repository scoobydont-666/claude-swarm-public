#!/usr/bin/env bash
set -euo pipefail

# sync-claude-env.sh — Push orchestration-node Claude Code environment to all fleet hosts
# Run before dispatching work, or on cron for continuous sync.
# Source of truth: orchestration-node ~/.claude/

HOSTS=(giga mecha mega mongo)
TIMEOUT=10

echo "[$(date -Iseconds)] Claude env sync starting"

for host in "${HOSTS[@]}"; do
    echo "  → $host"

    # Ensure target dirs exist
    ssh -o ConnectTimeout=$TIMEOUT "$host" \
        "mkdir -p ~/.claude/projects/-opt-hydra-project/memory ~/.claude/skills" 2>/dev/null || {
        echo "    SKIP: $host unreachable"
        continue
    }

    # Skills (delete stale ones on remote)
    rsync -a --delete ~/.claude/skills/ "$host":~/.claude/skills/ 2>/dev/null

    # Settings, MCP config, global CLAUDE.md
    scp -o ConnectTimeout=$TIMEOUT -q \
        ~/.claude/settings.json \
        ~/.claude/.mcp.json \
        ~/.claude/CLAUDE.md \
        "$host":~/.claude/ 2>/dev/null

    # Memory files (don't delete — remote may have generated its own)
    rsync -a ~/.claude/projects/-opt-hydra-project/memory/ \
        "$host":~/.claude/projects/-opt-hydra-project/memory/ 2>/dev/null

    # Verify
    remote_skills=$(ssh -o ConnectTimeout=$TIMEOUT "$host" "ls ~/.claude/skills/ | wc -l" 2>/dev/null)
    remote_memory=$(ssh -o ConnectTimeout=$TIMEOUT "$host" "ls ~/.claude/projects/-opt-hydra-project/memory/ | wc -l" 2>/dev/null)
    echo "    OK: $remote_skills skills, $remote_memory memories"
done

echo "[$(date -Iseconds)] Claude env sync complete"
