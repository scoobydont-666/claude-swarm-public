#!/usr/bin/env bash
set -euo pipefail

# swarm-session-start.sh — SessionStart hook
# Registers this instance in the swarm, reports who else is online,
# and loads relevant session summaries for context continuity.
# Output goes to systemMessage for Claude Code context.

SWARM_ROOT="/var/lib/swarm"
HOSTNAME=$(hostname)

# Ensure swarm root exists (NFS or local)
if [[ ! -d "${SWARM_ROOT}/status" ]]; then
    if [[ -d "${HOME}/.swarm/status" ]]; then
        SWARM_ROOT="${HOME}/.swarm"
    else
        echo '{"systemMessage": "Swarm: not configured (no /var/lib/swarm or ~/.swarm)"}'
        exit 0
    fi
fi

# Write own status as active
python3 -c "
import sys
sys.path.insert(0, '/opt/claude-swarm/src')
import swarm_lib as lib
lib.update_status(state='active', session_id='${CLAUDE_SESSION_ID:-unknown}', model='${CLAUDE_MODEL:-unknown}')
"

# Mark stale nodes
python3 -c "
import sys
sys.path.insert(0, '/opt/claude-swarm/src')
import swarm_lib as lib
stale = lib.mark_stale_nodes()
if stale:
    print(f'Marked stale: {stale}', file=sys.stderr)
"

# Build status summary including session context
SUMMARY=$(python3 -c "
import sys, json
sys.path.insert(0, '/opt/claude-swarm/src')
import swarm_lib as lib

nodes = lib.get_all_status()
hostname = '${HOSTNAME}'
parts = []
for n in nodes:
    h = n.get('hostname', '?')
    if h == hostname:
        continue
    state = n.get('state', 'unknown')
    task = n.get('current_task', '') or 'no task'
    model = n.get('model', '')
    model_str = f' on {model}' if model else ''
    parts.append(f'{h} is {state} ({task}{model_str})')

# Check for pending tasks
pending = lib.list_tasks('pending')
matching = lib.get_matching_tasks()

msgs = lib.read_inbox()

summary = f'Swarm: {hostname} registered as active.'
if parts:
    summary += ' ' + '. '.join(parts) + '.'
if pending:
    summary += f' {len(pending)} pending task(s).'
if matching:
    summary += f' {len(matching)} task(s) match your capabilities.'
if msgs:
    summary += f' {len(msgs)} unread message(s) in inbox.'

# Load relevant session summaries for context continuity
current = lib.get_status(hostname)
project = current.get('project', '') if current else ''
if project:
    ctx = lib.get_latest_summary_context(project)
    if ctx:
        summary += f' {ctx}'

print(summary)
")

# Output as systemMessage JSON
python3 -c "
import json
print(json.dumps({'systemMessage': '''${SUMMARY}'''}))
"
