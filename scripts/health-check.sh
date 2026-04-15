#!/usr/bin/env bash
set -euo pipefail

# health-check.sh — Check swarm health across all nodes

SWARM_ROOT="/opt/swarm"
STALE_THRESHOLD=300  # seconds

echo "=== claude-swarm Health Check ==="
echo "Time: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# Check NFS mount
if mountpoint -q "${SWARM_ROOT}" 2>/dev/null; then
    echo "[OK] NFS mounted at ${SWARM_ROOT}"
elif [[ -d "${SWARM_ROOT}" ]]; then
    echo "[OK] Local swarm directory at ${SWARM_ROOT} (not NFS)"
else
    echo "[FAIL] No swarm root at ${SWARM_ROOT}"
    exit 1
fi

# Check nodes
echo ""
echo "--- Nodes ---"
NOW=$(date +%s)
STALE_COUNT=0

for status_file in "${SWARM_ROOT}"/status/*.json; do
    [[ -f "${status_file}" ]] || continue

    HOSTNAME=$(python3 -c "import json; print(json.load(open('${status_file}')).get('hostname', '?'))")
    STATE=$(python3 -c "import json; print(json.load(open('${status_file}')).get('state', '?'))")
    UPDATED=$(python3 -c "import json; print(json.load(open('${status_file}')).get('updated_at', ''))")
    TASK=$(python3 -c "import json; print(json.load(open('${status_file}')).get('current_task', '') or '-')")

    # Calculate age
    if [[ -n "${UPDATED}" ]]; then
        UPDATED_EPOCH=$(date -d "${UPDATED}" +%s 2>/dev/null || echo "0")
        AGE=$(( NOW - UPDATED_EPOCH ))
    else
        AGE=999999
    fi

    if [[ "${STATE}" != "offline" && "${AGE}" -gt "${STALE_THRESHOLD}" ]]; then
        echo "[STALE] ${HOSTNAME}: ${STATE}, age=${AGE}s, task=${TASK}"
        STALE_COUNT=$((STALE_COUNT + 1))
    else
        echo "[OK]    ${HOSTNAME}: ${STATE}, age=${AGE}s, task=${TASK}"
    fi
done

# Check tasks
echo ""
echo "--- Tasks ---"
PENDING=$(find "${SWARM_ROOT}/tasks/pending" -name "task-*.yaml" 2>/dev/null | wc -l)
CLAIMED=$(find "${SWARM_ROOT}/tasks/claimed" -name "task-*.yaml" 2>/dev/null | wc -l)
COMPLETED=$(find "${SWARM_ROOT}/tasks/completed" -name "task-*.yaml" 2>/dev/null | wc -l)
echo "Pending: ${PENDING}, Claimed: ${CLAIMED}, Completed: ${COMPLETED}"

# Check NFS connectivity to both servers
echo ""
echo "--- NFS Connectivity ---"
if ping -c 1 -W 2 <primary-node-ip> &>/dev/null; then
    echo "[OK] GIGA (<primary-node-ip>) reachable"
else
    echo "[FAIL] GIGA (<primary-node-ip>) unreachable"
fi

if ping -c 1 -W 2 <orchestration-node-ip> &>/dev/null; then
    echo "[OK] miniboss (<orchestration-node-ip>) reachable"
else
    echo "[FAIL] miniboss (<orchestration-node-ip>) unreachable"
fi

# Summary
echo ""
if [[ "${STALE_COUNT}" -gt 0 ]]; then
    echo "=== ${STALE_COUNT} stale node(s) detected ==="
    exit 1
else
    echo "=== All checks passed ==="
fi
