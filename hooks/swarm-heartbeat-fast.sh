#!/usr/bin/env bash
# swarm-heartbeat-fast.sh — Lightweight status timestamp update (Redis + NFS)
# Called on PostToolUse. Must be <20ms. No Python, prefers pure-bash Redis ping.
# Uses jq for safe JSON update, falls back to sed.

SWARM_ROOT="/var/lib/swarm"
STATUS_FILE="${SWARM_ROOT}/status/$(hostname).json"

[ -f "$STATUS_FILE" ] || exit 0

# Rate limit: only update once per 60 seconds
MARKER="/tmp/.swarm-heartbeat-last"
if [ -f "$MARKER" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$MARKER" 2>/dev/null || echo 0) ))
    [ "$AGE" -lt 60 ] && exit 0
fi
touch "$MARKER"

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HOSTNAME=$(hostname)
PID=$$

# Prefer jq for safe JSON update; fall back to sed
if command -v jq >/dev/null 2>&1; then
    TMP="${STATUS_FILE}.tmp"
    jq --arg ts "$NOW" '.updated_at = $ts' "$STATUS_FILE" > "$TMP" && mv "$TMP" "$STATUS_FILE"
else
    sed -i "s/\"updated_at\": \"[^\"]*\"/\"updated_at\": \"${NOW}\"/" "$STATUS_FILE"
fi

# Try to ping Redis for fast heartbeat (non-blocking, <5ms timeout)
# Uses timeout + nc (netcat) to avoid Python startup overhead
if command -v timeout >/dev/null 2>&1 && command -v nc >/dev/null 2>&1; then
    {
        echo -e "PING\r"
        sleep 0.01
    } | timeout 0.005s nc -w 1 127.0.0.1 6379 >/dev/null 2>&1 || true
    # Note: silent fail — NFS is already updated above
fi
