#!/usr/bin/env bash
set -euo pipefail

# sync-from-git.sh — Pull swarm state from claude-config git repo
# For remote hosts without NFS access

CLAUDE_CONFIG="${HOME}/claude-configs/claude-config"
SWARM_TARGET="/opt/swarm"
SWARM_LOCAL="${HOME}/.swarm"

# Check for claude-config repo
if [[ ! -d "${CLAUDE_CONFIG}/.git" ]]; then
    echo "[ERROR] claude-config repo not found: ${CLAUDE_CONFIG}"
    echo "Clone it first: git clone git@github.com:your-github-user/claude-config.git ${CLAUDE_CONFIG}"
    exit 1
fi

# Pull latest
cd "${CLAUDE_CONFIG}"
git pull --rebase origin main

# Check if swarm directory exists in repo
if [[ ! -d "${CLAUDE_CONFIG}/swarm" ]]; then
    echo "No swarm state in claude-config repo yet."
    exit 0
fi

# Apply to NFS mount if available, otherwise local directory
if [[ -d "${SWARM_TARGET}" ]] && mountpoint -q "${SWARM_TARGET}" 2>/dev/null; then
    DEST="${SWARM_TARGET}"
    echo "Syncing to NFS mount: ${DEST}"
else
    DEST="${SWARM_LOCAL}"
    mkdir -p "${DEST}"/{status,tasks/{pending,claimed,completed},artifacts,messages/{inbox,archive},config}
    echo "Syncing to local: ${DEST}"
fi

for dir in status tasks config; do
    if [[ -d "${CLAUDE_CONFIG}/swarm/${dir}" ]]; then
        rsync -a "${CLAUDE_CONFIG}/swarm/${dir}/" "${DEST}/${dir}/"
    fi
done

echo "Swarm state synced from git at $(date +%Y-%m-%dT%H:%M:%S)"
