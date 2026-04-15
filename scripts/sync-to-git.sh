#!/usr/bin/env bash
set -euo pipefail

# sync-to-git.sh — Push swarm state to claude-config git repo
# Run via cron or on-demand. Never force-push.

SWARM_ROOT="/opt/swarm"
CLAUDE_CONFIG="/opt/claude-configs/claude-config"

# Verify paths exist
if [[ ! -d "${SWARM_ROOT}" ]]; then
    echo "[ERROR] Swarm root not found: ${SWARM_ROOT}"
    exit 1
fi

if [[ ! -d "${CLAUDE_CONFIG}/.git" ]]; then
    echo "[ERROR] claude-config repo not found: ${CLAUDE_CONFIG}"
    exit 1
fi

# Create swarm directory in claude-config if needed
mkdir -p "${CLAUDE_CONFIG}/swarm"

# Copy swarm state (status, tasks, config — NOT artifacts or messages)
for dir in status tasks config; do
    if [[ -d "${SWARM_ROOT}/${dir}" ]]; then
        rsync -a --delete "${SWARM_ROOT}/${dir}/" "${CLAUDE_CONFIG}/swarm/${dir}/"
    fi
done

# Commit and push
cd "${CLAUDE_CONFIG}"

# Pull first (never force-push)
git pull --rebase origin main 2>/dev/null || true

# Check for changes
if git diff --quiet HEAD -- swarm/ 2>/dev/null && \
   [[ -z "$(git ls-files --others --exclude-standard swarm/)" ]]; then
    echo "No swarm changes to sync."
    exit 0
fi

git add swarm/
git commit -m "swarm: sync $(date +%Y-%m-%dT%H:%M:%S) from $(hostname)"
git push origin main

echo "Swarm state synced to git at $(date +%Y-%m-%dT%H:%M:%S)"
