# Claude-Swarm Operations Runbook

**Version**: 1.0 | **Last updated**: 2026-04-18 | **Scope**: on-call response to every alert in `deploy/swarm-alerts.yml` and `/opt/ai-project/config/prometheus/routing_protocol_v1_alerts.yml`.

## How to use this runbook

Every alert rule references a `# Runbook:` anchor (e.g., `#swarm-node-offline`) that maps to a section below. The section tells you:

1. **What triggered the alert** (plain-language)
2. **First diagnostic command** to run (copy-paste ready)
3. **Common causes** and their resolutions
4. **Escalation** if initial steps don't resolve

Keep this document short and action-oriented. If a section grows beyond ~30 lines, split into a dedicated runbook.

## Quick reference — who owns what

| Subsystem | Owner | Escalation |
|---|---|---|
| Redis (state store) | miniboss local service | SSH miniboss; `systemctl status redis` |
| NFS share `/opt/swarm` | miniboss NFS export | `showmount -e miniboss`; check mount on each host |
| Routing Protocol v1 | `~/.claude/hooks/routing_*.py` + `~/.claude/state/routing.db` | `sqlite3 ~/.claude/state/routing.db` |
| Prometheus textfile | `/tmp/node_exporter_textfile/routing_protocol.prom` | `crontab -l | grep routing_metrics` |
| Grafana dashboards | `/opt/ai-project/config/grafana/dashboards/` | GIGA:3000 |
| NAI-suite derivatives | `/opt/nai-swarm`, `/opt/nai-reserve` (own runbooks) | See nai-swarm/docs/ops-guide.md |

---

## Alert → Action map

### `SwarmNodeOffline` {#swarm-node-offline}

**Triggers**: one or more swarm nodes have state=offline for 5+ minutes.

**First command**:
```bash
python3 /opt/claude-swarm/src/swarm_cli.py status
for h in giga mega mecha mongo; do echo "=== $h ==="; ssh -o ConnectTimeout=5 $h "hostname; uptime" 2>&1 | head -3; done
```

**Common causes**:
- **Host powered off** → wake via `wakeonlan` if WoL configured, else physical power
- **SSH key issue** → `ssh -vv <host>` to diagnose; miniboss `.ssh/known_hosts` may need refresh
- **Heartbeat agent crashed** → SSH in, `systemctl status swarm-heartbeat` (or the equivalent service name on that host)
- **Network segmentation** → `ping <host>` vs `ssh <host>`

**Escalation**: if multiple nodes offline simultaneously → likely network/switch issue. Check miniboss router logs.

### `SwarmTaskQueueBacklog` {#swarm-task-queue-backlog}

**Triggers**: >5 pending tasks for 30+ minutes — nobody is claiming work.

**First command**:
```bash
ls /opt/swarm/tasks/pending/*.yaml | wc -l
python3 /opt/claude-swarm/src/swarm_cli.py inbox
```

**Common causes**:
- **No active workers** → check `SwarmNodeOffline` status; start a worker
- **All workers blocked on the same blocker** → inspect `inbox` output, identify the blocker, unblock
- **Tasks require capabilities no worker has** → check task yaml `required_capabilities`; either provision the cap or reassign

**Escalation**: if queue is growing faster than consumption → ops capacity problem, not software. Escalate to Josh.

### `SwarmHeartbeatStale` {#swarm-heartbeat-stale}

**Triggers**: a host hasn't sent a heartbeat in 10+ minutes (was 5+ for alert to fire).

**First command**:
```bash
HOST=<affected-host>  # from alert labels
ssh $HOST "ps aux | grep -i heartbeat | grep -v grep" 2>&1 | head -5
ssh $HOST "cat /opt/swarm/agents/*${HOST}*.yaml" 2>&1 | head -20
```

**Common causes**:
- **Agent process crashed / hung** → restart the swarm agent on that host
- **NFS mount stale on the host** → `ssh $HOST "stat /opt/swarm/.swarm_alive"`; if hangs, remount NFS
- **Redis unreachable from that host** → `ssh $HOST "redis-cli -h miniboss ping"` (expect PONG)

**Escalation**: critical severity — paged. If host is unrecoverable, drain + replace via NAI-Reserve scheduling.

### `SwarmHighDispatchCost` {#swarm-high-dispatch-cost}

**Triggers**: >$5/hour dispatch cost sustained for 10 minutes.

**First command**:
```bash
python3 /opt/claude-swarm/src/cost_tracker.py --last-hour
```

**Common causes**:
- **Runaway loop** — a task stuck in Opus retry → find the task, kill it, inspect root cause
- **Legitimate heavy work** (large planning session) — ack the alert, monitor for return to baseline
- **Cost routing misconfigured** — check `/opt/hydra-project/libs/agent_bridge/model_tier.py` for tier pins that should be sonnet but got set to opus

**Escalation**: if cost crosses $20/hour without a known heavy workload → kill all dispatches, call Josh.

### `SwarmDispatchCostByHost` {#swarm-dispatch-cost-by-host}

**Triggers**: a single host is accruing >$2/hour.

**First command**: same as `SwarmHighDispatchCost` but filter by `{hostname="<affected>"}`.

**Common causes**: usually same root cause as fleet-wide — just attributed to a specific host. Investigate per above.

### `RoutingAutoDowngradeFired` {#routing-auto-downgrade-fired}

**Triggers**: >5 routing false-positive blocks in the trailing hour; enforcement auto-downgrades to warn-only.

**First command**:
```bash
sqlite3 ~/.claude/state/routing.db 'SELECT * FROM dlq WHERE resolved_at IS NULL ORDER BY created_at DESC LIMIT 20'
grep "FP" /tmp/routing_metrics.log | tail -20
```

**Common causes**:
- **Pause-ask-scanner too aggressive** — false-positive on a legitimate coordinator message. Inspect recent `pause-ask` BLOCK events, tune the regex in `~/.claude/hooks/routing_enforcement.py`.
- **Dispatch-rate-limit too strict** — legitimate bursty parallel dispatches. Adjust limit in `~/.claude/hooks/lib/routing_state_db.py`.

**Resolution**:
1. Fix the FP pattern
2. Mark DLQ entries resolved: `sqlite3 ~/.claude/state/routing.db 'UPDATE dlq SET resolved_at=datetime("now") WHERE resolved_at IS NULL'`
3. Re-enforce: `/routing-mode enforce`

### `RoutingDLQGrowing` {#routing-dlq-growing}

**Triggers**: 20+ unresolved DLQ entries for 30+ minutes.

**First command**:
```bash
sqlite3 ~/.claude/state/routing.db 'SELECT hook_name, COUNT(*) FROM dlq WHERE resolved_at IS NULL GROUP BY hook_name ORDER BY 2 DESC'
```

**Resolution**: triage by hook. Most common culprits:
- `pause-ask-scanner` — FP regex tuning (see `RoutingAutoDowngradeFired`)
- `dispatch-rate-limit` — increase limit or fix parallel pattern
- `cb-context-assembly` — CB unreachable; check `systemctl status context-bridge-mcp`

### `RoutingMetricsStale` {#routing-metrics-stale}

**Triggers**: no metrics refresh in 5 minutes — cron emitter stopped.

**First command**:
```bash
crontab -l | grep routing_metrics
tail /tmp/routing_metrics.log
python3 ~/.claude/hooks/lib/routing_metrics.py  # test run manually
```

**Resolution**:
- If cron entry missing, restore it:
  ```
  * * * * * /usr/bin/python3 /home/josh/.claude/hooks/lib/routing_metrics.py >> /tmp/routing_metrics.log 2>&1
  ```
- If script errors, fix the bug then re-enable the cron.

### `RoutingModeNotEnforce` {#routing-mode-not-enforce}

**Triggers**: routing is in warn-only or off for 15+ minutes.

**First command**:
```bash
cat /tmp/routing-mode
```

**Resolution**:
- If auto-downgrade fired (see `RoutingAutoDowngradeFired`) → fix the FP cause, then `/routing-mode enforce`
- If manually disabled for debugging → remember to re-enable when done

---

## Backend failure playbooks

### Redis down

Redis runs on miniboss. Swarm falls back to NFS-backed mode (slower, still functional).

```bash
systemctl status redis
systemctl restart redis
```

Verify swarm backend switched:
```bash
curl -s http://127.0.0.1:9192/api/status | jq '.backend'
# expects: "redis" normally, "nfs" when degraded
```

Degraded mode is a known graceful state — not an emergency unless sustained >1 hour.

### NFS mount stale on a worker

```bash
HOST=<affected>
ssh $HOST "stat /opt/swarm/.swarm_alive"  # should return quickly
# If hangs:
ssh $HOST "sudo umount -lf /opt/swarm && sudo mount /opt/swarm"
```

### Dashboard 503

The swarm dashboard at `127.0.0.1:9192` returns 503 when `/ready` detects a degraded backend. Check:

1. Redis: `redis-cli -h miniboss ping`
2. NFS: `stat /opt/swarm/.swarm_alive`
3. Log: `journalctl -u swarm-dashboard -n 50`

Both must be up for `/ready` to return 200. `/live` only requires the process to be up.

---

## Credentials — Redis password

**Source of truth**: `/etc/systemd/system/hydra-redis.env` (root:root, 0600). Loaded via drop-in:
```
[Service]
EnvironmentFile=/etc/systemd/system/hydra-redis.env
```
at `/etc/systemd/system/swarm-{dashboard,health-monitor,metrics-exporter}.service.d/10-hydra-redis.conf`.

The file `/opt/claude-swarm/.env` is a **dev convenience fallback** only. Services do NOT call `load_dotenv()` — env vars come from the systemd drop-ins. The in-repo `.env` should never contain the actual password (scrubbed 2026-04-18 E2; see comment in the file).

**To rotate the password**:

1. Generate new: `openssl rand -hex 32`
2. Update Redis config:
   ```bash
   sudo redis-cli CONFIG SET requirepass <new>
   sudo sed -i "s/^requirepass .*/requirepass <new>/" /etc/redis/redis.conf
   ```
3. Update systemd env: `sudo nano /etc/systemd/system/hydra-redis.env` (replace `SWARM_REDIS_PASSWORD=`)
4. Restart consumers:
   ```bash
   sudo systemctl daemon-reload  # only needed if drop-in files changed
   sudo systemctl restart swarm-dashboard swarm-health-monitor swarm-metrics-exporter
   ```
5. Verify: `redis-cli -a <new> ping` → `PONG`; dashboard `/api/status` shows `backend: "redis"`
6. Old password is now invalid; any service still using it will fall back to NFS mode.

**Known risk**: the old password hash appeared in public git history at commits `3645d14`, `e168fdc`, `0d7bc88` before the "remove" commit (which didn't actually rewrite history). Repo is PRIVATE so blast radius is limited to anyone who has cloned it. **Rotating is the cheap fix**; `git filter-repo` cleanup is deferred (requires force-push + breaks clones).

## Schema drift triage

Silent-drift protection ships via the Phase B1+B2 schemas. If you suspect drift:

```bash
# CB contract
pytest /opt/claude-swarm/tests/test_cb_schema.py -v
# Event schema
pytest /opt/claude-swarm/tests/test_events_schema.py -v
# Strict mode smoke
SWARM_EVENT_SCHEMA_STRICT=1 python3 -c "from src.events import emit; emit('unknown_type', details={'foo': 1})"
# (should raise ValueError)
```

If tests fail, revert the offending change and inspect the schema diff in the commit.

## Contacts

- **Primary on-call**: Josh (scoobydont-666)
- **Project home**: `/opt/hydra-project`
- **This runbook**: `/opt/claude-swarm/docs/RUNBOOK.md`
- **Alerts file**: `/opt/claude-swarm/deploy/swarm-alerts.yml`
- **Routing alerts**: `/opt/ai-project/config/prometheus/routing_protocol_v1_alerts.yml`
