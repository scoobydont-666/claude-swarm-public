#!/usr/bin/env bash
set -euo pipefail

# swarm-heartbeat.sh — Periodic heartbeat (Redis + NFS fallback)
# Publishes heartbeat to Redis Streams (hydra:ipc:heartbeat).
# Falls back to NFS if Redis unavailable.
# Updates own status, marks stale nodes, checks inbox.

# Activate venv if available
if [[ -f "/opt/claude-swarm/.venv/bin/activate" ]]; then
    source "/opt/claude-swarm/.venv/bin/activate"
fi

SWARM_ROOT="/opt/swarm"

if [[ ! -d "${SWARM_ROOT}/status" ]]; then
    if [[ -d "${HOME}/.swarm/status" ]]; then
        SWARM_ROOT="${HOME}/.swarm"
    else
        exit 0
    fi
fi

OUTPUT=$(python3 -c "
import sys, json, os, socket, time
sys.path.insert(0, '/opt/claude-swarm/src')
import swarm_lib as lib
try:
    import redis_client as rc
except ImportError:
    rc = None

hostname = socket.gethostname()
pid = os.getpid()

# Update own timestamp (keep current state)
current = lib.get_status(hostname)
if current:
    lib.update_status(
        state=current.get('state', 'active'),
        current_task=current.get('current_task', ''),
        project=current.get('project', ''),
        session_id=current.get('session_id', ''),
        model=current.get('model', ''),
        pid=pid,
    )

# Emit heartbeat to Redis Streams (primary) + NFS (fallback)
heartbeat_data = {
    'hostname': hostname,
    'pid': pid,
    'timestamp': time.time(),
    'state': current.get('state', 'active') if current else 'active',
    'current_task': current.get('current_task', '') if current else '',
    'model': current.get('model', '') if current else '',
}

redis_ok = False
if rc:
    try:
        if rc.health_check():
            # Publish to Redis Streams (hydra:ipc:heartbeat)
            rc.get_client().xadd(
                'hydra:ipc:heartbeat',
                {'hostname': hostname, 'pid': str(pid), 'payload': json.dumps(heartbeat_data)},
                maxlen=10000
            )
            redis_ok = True
    except Exception as e:
        pass

if not redis_ok:
    # Fallback: also write to NFS status (already done above via lib.update_status)
    pass

# Mark stale nodes
stale = lib.mark_stale_nodes()

# Check inbox
msgs = lib.read_inbox()

parts = []
if stale:
    parts.append(f'Marked stale: {\" \".join(stale)}')
if msgs:
    parts.append(f'{len(msgs)} unread message(s)')
if redis_ok:
    parts.append('Redis: OK')

if parts:
    print(json.dumps({'systemMessage': 'Swarm heartbeat: ' + '. '.join(parts)}))
")

if [[ -n "${OUTPUT}" ]]; then
    echo "${OUTPUT}"
fi
