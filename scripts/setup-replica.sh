#!/usr/bin/env bash
set -euo pipefail

# setup-replica.sh — Run on orchestration-node (10.0.0.5)
# Mounts gpu-server-1's NFS share and sets up local replica + backup NFS export
# Requires: sudo

gpu-server-1-ip="10.0.0.1"
SWARM_MOUNT="/var/lib/swarm"
REPLICA_DIR="/var/lib/swarm-replica"
SUBNET="10.0.0.0/23"

echo "=== claude-swarm: NFS Replica Setup (orchestration-node) ==="

# Install NFS packages
if ! dpkg -l | grep -q nfs-common; then
    echo "Installing nfs-common..."
    sudo apt-get update && sudo apt-get install -y nfs-common
fi

if ! dpkg -l | grep -q nfs-kernel-server; then
    echo "Installing nfs-kernel-server..."
    sudo apt-get update && sudo apt-get install -y nfs-kernel-server
fi

# Create mount point
sudo mkdir -p "${SWARM_MOUNT}"

# Mount gpu-server-1's NFS share (idempotent)
if mountpoint -q "${SWARM_MOUNT}" 2>/dev/null; then
    echo "NFS already mounted at ${SWARM_MOUNT}."
else
    echo "Mounting ${gpu-server-1-ip}:/var/lib/swarm at ${SWARM_MOUNT}..."
    sudo mount -t nfs "${gpu-server-1-ip}:/var/lib/swarm" "${SWARM_MOUNT}"
fi

# Add to fstab (idempotent)
FSTAB_LINE="${gpu-server-1-ip}:/var/lib/swarm ${SWARM_MOUNT} nfs defaults,_netdev,soft,timeo=30 0 0"
if ! grep -qF "${gpu-server-1-ip}:/var/lib/swarm" /etc/fstab 2>/dev/null; then
    echo "Adding to /etc/fstab..."
    echo "${FSTAB_LINE}" | sudo tee -a /etc/fstab
else
    echo "fstab entry already exists."
fi

# Create replica directory
echo "Creating replica directory at ${REPLICA_DIR}..."
sudo mkdir -p "${REPLICA_DIR}"
sudo chown -R aisvc:aisvc "${REPLICA_DIR}"

# Initial rsync
echo "Syncing from NFS mount to replica..."
sudo rsync -a --delete "${SWARM_MOUNT}/" "${REPLICA_DIR}/"

# Set up cron for rsync every 30 seconds (two cron entries, offset by 30s)
CRON_CMD="rsync -a --delete ${SWARM_MOUNT}/ ${REPLICA_DIR}/ 2>/dev/null"
CRON_LINE="* * * * * ${CRON_CMD}"
CRON_LINE_OFFSET="* * * * * sleep 30 && ${CRON_CMD}"

CRON_TEMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "swarm-replica" | grep -v "${REPLICA_DIR}" > "${CRON_TEMP}" || true
echo "${CRON_LINE}  # swarm-replica sync" >> "${CRON_TEMP}"
echo "${CRON_LINE_OFFSET}  # swarm-replica sync (offset)" >> "${CRON_TEMP}"
crontab "${CRON_TEMP}"
rm -f "${CRON_TEMP}"
echo "Cron jobs installed for 30-second replica sync."

# Export replica via NFS as backup
EXPORT_LINE="${REPLICA_DIR} ${SUBNET}(rw,sync,no_subtree_check,root_squash,all_squash,anonuid=1001,anongid=1001)"
if ! grep -qF "${REPLICA_DIR}" /etc/exports 2>/dev/null; then
    echo "Adding replica NFS export..."
    echo "${EXPORT_LINE}" | sudo tee -a /etc/exports
else
    echo "Replica NFS export already configured."
fi

sudo exportfs -ra
sudo systemctl enable --now nfs-kernel-server

echo ""
echo "=== Replica setup complete ==="
echo "NFS mount: ${gpu-server-1-ip}:/var/lib/swarm -> ${SWARM_MOUNT}"
echo "Replica: ${REPLICA_DIR} (rsync every 30s)"
echo "Backup export: ${EXPORT_LINE}"
