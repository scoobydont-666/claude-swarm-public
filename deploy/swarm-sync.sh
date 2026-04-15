#!/usr/bin/env bash
set -euo pipefail
# swarm-sync.sh — Bidirectional swarm state sync between miniboss and GIGA.
#
# Architecture: Both hosts maintain local /opt/swarm/ directories. This script
# merges state bidirectionally using rsync --update (newer files win). Each host
# writes its own status/*.json and both share tasks/messages/artifacts.
#
# Run via cron every 30s or via systemd timer.
# Install: sudo cp /opt/claude-swarm/deploy/swarm-sync.sh /usr/local/bin/swarm-sync.sh

GIGA_HOST="<primary-node-ip>"
GIGA_USER="josh"
LOCAL_SWARM="/opt/swarm"
REMOTE_SWARM="/opt/swarm"
REPLICA_BACKUP="/opt/swarm-replica"

LOGFILE="/opt/claude-swarm/data/swarm-sync.log"
LOCKFILE="/tmp/swarm-sync.lock"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOGFILE"; }

# Prevent concurrent runs
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    exit 0  # Another sync is running, skip silently
fi

# Ensure all expected directories exist locally
for dir in status tasks/pending tasks/claimed tasks/completed tasks/preempted tasks/decomposed \
           messages/inbox messages/archive artifacts/dispatches artifacts/shared \
           gpu collaborative config events; do
    mkdir -p "${LOCAL_SWARM}/${dir}"
done

# Ensure remote directories exist too
ssh -o ConnectTimeout=5 -o BatchMode=yes "${GIGA_USER}@${GIGA_HOST}" \
    "mkdir -p ${REMOTE_SWARM}/{status,tasks/{pending,claimed,completed,preempted,decomposed},messages/{inbox,archive},artifacts/{dispatches,shared},gpu,collaborative,config,events}" \
    2>/dev/null || {
        log "ERROR: Cannot reach GIGA at ${GIGA_HOST} — skipping sync"
        exit 1
    }

# Phase 1: Pull from GIGA → local (newer files win, no delete)
rsync -rlpt --update --timeout=30 \
    "${GIGA_USER}@${GIGA_HOST}:${REMOTE_SWARM}/" \
    "${LOCAL_SWARM}/" \
    >> "$LOGFILE" 2>&1 || {
        rc=$?
        log "WARNING: Pull from GIGA failed (rc=${rc})"
    }

# Phase 2: Push local → GIGA (newer files win, no delete)
rsync -rlpt --update --timeout=30 \
    "${LOCAL_SWARM}/" \
    "${GIGA_USER}@${GIGA_HOST}:${REMOTE_SWARM}/" \
    >> "$LOGFILE" 2>&1 || {
        rc=$?
        log "WARNING: Push to GIGA failed (rc=${rc})"
    }

# Phase 3: Update local backup replica
rsync -a --delete "${LOCAL_SWARM}/" "${REPLICA_BACKUP}/" 2>/dev/null || true

log "OK: sync complete"
