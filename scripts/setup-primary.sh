#!/usr/bin/env bash
set -euo pipefail

# setup-primary.sh — Run on GIGA (192.168.200.163) to set up NFS primary
# Requires: sudo

SWARM_ROOT="/opt/swarm"
SUBNET="192.168.200.0/23"

echo "=== claude-swarm: NFS Primary Setup (GIGA) ==="

# Create directory structure
echo "Creating swarm directory structure at ${SWARM_ROOT}..."
sudo mkdir -p "${SWARM_ROOT}"/{status,tasks/{pending,claimed,completed},artifacts,messages/{inbox/{miniboss,GIGA,MEGA,MECHA,broadcast},archive},config}
sudo chown -R aisvc:aisvc "${SWARM_ROOT}"
sudo chmod -R 775 "${SWARM_ROOT}"

# Copy config if not present
if [[ ! -f "${SWARM_ROOT}/config/swarm.yaml" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "${SCRIPT_DIR}/../config/swarm.yaml" ]]; then
        cp "${SCRIPT_DIR}/../config/swarm.yaml" "${SWARM_ROOT}/config/swarm.yaml"
        echo "Copied swarm.yaml to ${SWARM_ROOT}/config/"
    fi
fi

# Install NFS server if needed
if ! dpkg -l | grep -q nfs-kernel-server; then
    echo "Installing nfs-kernel-server..."
    sudo apt-get update && sudo apt-get install -y nfs-kernel-server
fi

# Add NFS export (idempotent)
EXPORT_LINE="${SWARM_ROOT} ${SUBNET}(rw,sync,no_subtree_check,root_squash,all_squash,anonuid=1001,anongid=1001)"
if ! grep -qF "${SWARM_ROOT}" /etc/exports 2>/dev/null; then
    echo "Adding NFS export..."
    echo "${EXPORT_LINE}" | sudo tee -a /etc/exports
else
    echo "NFS export already configured."
fi

# Apply exports
sudo exportfs -ra
echo "NFS exports applied."

# Ensure NFS server is running
sudo systemctl enable --now nfs-kernel-server

echo ""
echo "=== Primary setup complete ==="
echo "Export: ${EXPORT_LINE}"
echo "Verify with: showmount -e 127.0.0.1"
