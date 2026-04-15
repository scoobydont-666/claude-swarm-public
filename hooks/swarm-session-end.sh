#!/usr/bin/env bash
set -euo pipefail

# swarm-session-end.sh — Stop hook
# Marks this instance as idle, warns about uncompleted claimed tasks,
# and generates a session summary for cross-instance context sharing.

SWARM_ROOT="/opt/swarm"
HOSTNAME=$(hostname)

# Ensure swarm root exists
if [[ ! -d "${SWARM_ROOT}/status" ]]; then
    if [[ -d "${HOME}/.swarm/status" ]]; then
        SWARM_ROOT="${HOME}/.swarm"
    else
        exit 0
    fi
fi

# Generate session summary and update status
python3 -c "
import sys
sys.path.insert(0, '/opt/claude-swarm/src')
import swarm_lib as lib

hostname = '${HOSTNAME}'
session_id = '${CLAUDE_SESSION_ID:-unknown}'

# Get current status to determine project and task
current = lib.get_status(hostname)
project = current.get('project', '') if current else ''
task_id = current.get('current_task', '') if current else ''

# Generate and share session summary
if project:
    summary = lib.generate_session_summary(
        project=project,
        session_id=session_id,
        task_id=task_id if task_id else None,
        context_for_next='Session ended normally. Check git log for recent changes.',
    )
    lib.share_session_summary(summary)

# Update status to idle
lib.update_status(state='idle', current_task='', project='')
"

# Check for uncompleted claimed tasks
python3 -c "
import sys
sys.path.insert(0, '/opt/claude-swarm/src')
import swarm_lib as lib

hostname = '${HOSTNAME}'
claimed = lib.list_tasks('claimed')
my_claimed = [t for t in claimed if t.get('claimed_by') == hostname]
if my_claimed:
    ids = ', '.join(t.get('id', '?') for t in my_claimed)
    print(f'WARNING: {len(my_claimed)} claimed task(s) not completed: {ids}', file=sys.stderr)
" 2>&1 || true
