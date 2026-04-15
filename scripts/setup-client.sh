#!/usr/bin/env bash
set -euo pipefail

# setup-client.sh — Run on any host joining the swarm
# Mounts NFS (gpu-server-1 primary, orchestration-node fallback) and installs hooks
# Requires: sudo for NFS mount

# Load config from environment or defaults
gpu-server-1-ip="${gpu-server-1-ip:-10.0.0.1}"
MINIBOSS_IP="${MINIBOSS_IP:-10.0.0.5}"
SWARM_MOUNT="${SWARM_MOUNT:-/var/lib/swarm}"
ALLOW_LOCAL="${ALLOW_LOCAL:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

echo "=== claude-swarm: Client Setup ==="

# Install NFS client if needed
if ! dpkg -l | grep -q nfs-common; then
    echo "Installing nfs-common..."
    sudo apt-get update && sudo apt-get install -y nfs-common
fi

# Create mount point
sudo mkdir -p "${SWARM_MOUNT}"

# Try gpu-server-1 first, fall back to orchestration-node replica
if mountpoint -q "${SWARM_MOUNT}" 2>/dev/null; then
    echo "NFS already mounted at ${SWARM_MOUNT}."
else
    echo "Attempting NFS mount from gpu-server-1 (${gpu-server-1-ip})..."
    if sudo mount -t nfs -o soft,timeo=10 "${gpu-server-1-ip}:/var/lib/swarm" "${SWARM_MOUNT}" 2>/dev/null; then
        echo "Mounted from gpu-server-1."
        FSTAB_SRC="${gpu-server-1-ip}:/var/lib/swarm"
    else
        echo "gpu-server-1 unavailable. Trying orchestration-node replica (${MINIBOSS_IP})..."
        if sudo mount -t nfs -o soft,timeo=10 "${MINIBOSS_IP}:/var/lib/swarm-replica" "${SWARM_MOUNT}" 2>/dev/null; then
            echo "Mounted from orchestration-node replica."
            FSTAB_SRC="${MINIBOSS_IP}:/var/lib/swarm-replica"
        else
            echo "[ERROR] Could not mount NFS from gpu-server-1 or orchestration-node."
            if [[ "${ALLOW_LOCAL}" == "1" ]]; then
                echo "Local fallback enabled. Creating ~/.swarm/ directory."
                mkdir -p "${HOME}/.swarm"/{status,tasks/{pending,claimed,completed},artifacts,messages/{inbox,archive},config}
                exit 0
            else
                echo "Use ALLOW_LOCAL=1 to enable local fallback:"
                echo "  ALLOW_LOCAL=1 $0"
                exit 1
            fi
        fi
    fi

    # Add to fstab (idempotent)
    if ! grep -qF "/var/lib/swarm" /etc/fstab 2>/dev/null; then
        echo "${FSTAB_SRC} ${SWARM_MOUNT} nfs defaults,_netdev,soft,timeo=30 0 0" | sudo tee -a /etc/fstab
    fi
fi

# Show hook installation instructions (do NOT auto-install)
echo ""
echo "=== Client setup complete ==="
echo "NFS mounted at ${SWARM_MOUNT}"
echo ""
echo "To install hooks (when ready):"
echo "  mkdir -p ~/.claude/hooks"
echo "  cp ${PROJECT_DIR}/hooks/swarm-*.sh ~/.claude/hooks/"
echo "  chmod +x ~/.claude/hooks/swarm-*.sh"
echo ""
echo "Then add hook entries to your Claude Code settings.json."
