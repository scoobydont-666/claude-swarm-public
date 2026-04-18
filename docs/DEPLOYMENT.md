# claude-swarm — Deployment

> Skeleton seeded in P1. Full content populated in P5 (includes final deploy decision from P4).

## Current State (2026-04-18)

Deployed as **6 systemd units on miniboss**. K3s manifest (`k8s/deployment.yaml`) exists but is NOT applied. The deploy model will be finalized in Phase 4 of the DoD plan (Option A systemd-only vs Option B K3s migration).

## Systemd Units (miniboss)

| Unit | Role |
|---|---|
| `celery-swarm-worker.service` | Celery worker (4 concurrency, queues cpu,default) |
| `celery-swarm-beat.service` | Celery Beat scheduler |
| `celery-swarm-flower.service` | Flower UI |
| `swarm-dashboard.service` | FastAPI dashboard — 127.0.0.1:9192 |
| `swarm-health-monitor.service` | Health daemon |
| `swarm-metrics-exporter.service` | Prometheus exporter — :9191 |

All drop-in: `10-hydra-redis.conf` — injects `SWARM_REDIS_*` env from `/opt/claude-swarm/.env`.

## Dependencies

- **Redis** — miniboss:6379 with `SWARM_REDIS_PASSWORD`
- **NFS mount** — `<primary-node-ip>:/opt/swarm` → `/opt/swarm` (NFS4.2, `root_squash`)
- **Python 3.12+**, uv-managed venv

## Bootstrap — Fresh Host

```bash
# 1. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone + sync
git clone https://github.com/scoobydont-666/claude-swarm.git /opt/claude-swarm
cd /opt/claude-swarm
uv sync --extra dev

# 3. Config (P2 — Ansible-rendered from fleet inventory)
ansible-playbook -i inventory site.yml --tags claude_swarm_config
# OR manual dev render:
./scripts/render-swarm-config.sh

# 4. Enable units (on orchestrator hosts only)
sudo systemctl enable --now celery-swarm-worker swarm-dashboard swarm-metrics-exporter
```

## Container Build

Uses BuildKit with git-token secret for private `hydra-ipc` install:

```bash
DOCKER_BUILDKIT=1 docker build \
    --secret id=gh_token,env=GH_TOKEN \
    -t claude-swarm .
```

## Cron

- `*/15 * * * *` — `scripts/sync-to-git.sh`
- `*/30 * * * *` — `scripts/sync-claude-env.sh`
- `* * * * *` — `~/.claude/hooks/swarm-heartbeat-fast.sh`
- `* * * * *` — `/usr/local/bin/swarm-replica-sync.sh`
- `0 */2 * * *` — `scripts/swarm-task-poll.sh`
- `0 3 * * 0` — weekly artifact cleanup (mtime > 30d)

## Verification (post-deploy)

```bash
systemctl is-active celery-swarm-worker swarm-dashboard swarm-metrics-exporter
curl -sf http://127.0.0.1:9192/health          # /health endpoint added in P4
curl -sf http://127.0.0.1:9191/metrics | head
findmnt /opt/swarm                             # NFS mount verify
```

## Rollback

- Systemd: `systemctl stop <unit>` + `systemctl disable <unit>`; `.env` backup at `/opt/claude-swarm/.env.bak`.
- K3s (if/when adopted): `kubectl delete -f k8s/deployment.yaml`; re-enable systemd units from standby.

## See Also

- `docs/ARCHITECTURE.md`
- `ansible/roles/claude_swarm_config/` (P2)
- `config/swarm.yaml.example` — structural template for runtime config
