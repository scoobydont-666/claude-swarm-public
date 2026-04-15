#!/usr/bin/env bash
set -euo pipefail

# swarm-task-check.sh — Check for available tasks matching this node's capabilities
# Does NOT auto-claim — human decides.

SWARM_ROOT="/opt/swarm"

if [[ ! -d "${SWARM_ROOT}/status" ]]; then
    if [[ -d "${HOME}/.swarm/status" ]]; then
        SWARM_ROOT="${HOME}/.swarm"
    else
        exit 0
    fi
fi

OUTPUT=$(python3 -c "
import sys, json
sys.path.insert(0, '/opt/claude-swarm/src')
import swarm_lib as lib

matching = lib.get_matching_tasks()
if not matching:
    sys.exit(0)

lines = []
for t in matching:
    pri = t.get('priority', 'medium')
    lines.append(f'  [{pri}] {t[\"id\"]}: {t[\"title\"]}')

msg = f'Swarm: {len(matching)} task(s) available for this node:\n' + '\n'.join(lines)
msg += '\nUse \"swarm tasks claim <id>\" to claim one.'
print(json.dumps({'systemMessage': msg}))
")

if [[ -n "${OUTPUT}" ]]; then
    echo "${OUTPUT}"
fi
