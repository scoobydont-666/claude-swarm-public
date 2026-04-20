# claude-swarm — Session Handoff Protocol

> Skeleton seeded in P1. Full content populated in P5.

Each claude-code session writes a session artifact into `/opt/swarm/artifacts/handoffs/<host>-<timestamp>.md` via the `swarm-session-end` hook.

## Artifact Structure

```markdown
---
session_id: <uuid>
host: node_primary | giga | mecha | mega | mongo
started: <ISO-8601>
ended: <ISO-8601>
model: claude-opus-4-7 | claude-sonnet-4-6 | ...
tool_use_count: <int>
token_cost_usd: <float>   # from hydra-pulse
---

## Summary
<1-2 sentence session outcome>

## Commits
- <repo>:<hash> — <message>

## Next Session
<what the next session should pick up>
```

## Reading Handoffs

```bash
ls /opt/swarm/artifacts/handoffs/ | tail -5
swarm handoffs list --host node_primary --since 1d
swarm handoffs show <host>-<timestamp>
```

## Related

- `docs/ARCHITECTURE.md` — where artifacts fit in the data flow
- `src/swarm_cli.py` handoffs subcommand
- `hooks/swarm-session-end` — the writer hook

---

## Credential Rotation

All secrets used by claude-swarm services must be rotated on a **quarterly cadence** (or immediately on incident — compromise, public-repo exposure, or personnel change). The table below lists every secret, its source of truth, and rotation procedure.

### Rotation cadence

| Trigger | Action |
|---------|--------|
| Quarterly (first Monday of each quarter) | Rotate all secrets below per their procedures |
| Incident — credential confirmed or suspected leaked | Rotate immediately, then post-mortem within 48 h |
| Personnel change (access revoked) | Rotate any shared secrets that person held |
| Public-repo exposure (private repo accidentally flipped public, git history leak) | Rotate all secrets immediately |

### 1 — Redis password

**Source of truth**: `/etc/systemd/system/hydra-redis.env` (root:root, mode 0600).
**Cross-reference**: Full procedure at `docs/RUNBOOK.md#credentials-redis-password`.

Summary:

1. Generate: `openssl rand -hex 32`
2. Apply to Redis live: `sudo redis-cli CONFIG SET requirepass <new>`
3. Persist to `/etc/redis/redis.conf`: `sudo sed -i "s/^requirepass .*/requirepass <new>/" /etc/redis/redis.conf`
4. Update `/etc/systemd/system/hydra-redis.env` — set `SWARM_REDIS_PASSWORD=<new>`
5. Reload + restart consumers:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart swarm-dashboard swarm-health-monitor swarm-metrics-exporter
   ```
6. Verify: `redis-cli -a <new> ping` → `PONG`; `curl http://127.0.0.1:8560/api/status` shows `backend: "redis"`
7. Invalidate old password — any service still using it degrades gracefully to NFS read-only mode.

Note: commits `3645d14`, `e168fdc`, `0d7bc88` contain a historical password hash (repo is private; rotation is the mitigation; `git filter-repo` cleanup deferred).

### 2 — NFS SSH/rsync keys (replica-sync)

Used by the node_gpu replica-sync cron (`<ai-project-path>/scripts/replica-sync.sh`) to rsync swarm artifacts from node_primary NFS export → node_gpu `/mnt/swarm-replica/`.

**Source of truth**: `/home/aisvc/.ssh/id_ed25519_swarm_replica` on node_gpu; `~/.ssh/authorized_keys` on node_primary (entry tagged `# swarm-replica`).

Rotation procedure:

1. Generate a new key pair on node_gpu as `aisvc`:
   ```bash
   sudo -u aisvc ssh-keygen -t ed25519 -f /home/aisvc/.ssh/id_ed25519_swarm_replica_new -N "" -C "swarm-replica-$(date +%Y%m)"
   ```
2. Append the new public key to node_primary `~/.ssh/authorized_keys` (tagged line `# swarm-replica`):
   ```bash
   ssh node_primary "echo '$(cat /home/aisvc/.ssh/id_ed25519_swarm_replica_new.pub) # swarm-replica' >> ~/.ssh/authorized_keys"
   ```
3. Test the new key: `sudo -u aisvc ssh -i /home/aisvc/.ssh/id_ed25519_swarm_replica_new node_primary 'echo ok'`
4. Swap the symlink / update the cron to reference the new key.
5. Remove the old public key from node_primary `authorized_keys`.
6. Delete the old key pair on node_gpu.

### 3 — MCP server auth tokens

If MCP server auth is enabled (via `SWARM_MCP_AUTH_TOKEN` in systemd env or `.env`):

**Source of truth**: `/etc/systemd/system/hydra-redis.env` (same drop-in as Redis) or a dedicated `/etc/systemd/system/swarm-mcp.env`.

Rotation procedure:

1. Generate: `openssl rand -hex 32`
2. Update the env file holding `SWARM_MCP_AUTH_TOKEN=<new>`.
3. Restart the MCP server service: `sudo systemctl restart swarm-mcp-server` (or equivalent).
4. Update any callers (agents, hooks, CI) that embed the token.
5. Verify: hit an authenticated endpoint with the new token and confirm 200; confirm the old token is rejected with 401.

If MCP auth is currently disabled, document the decision here and re-evaluate before exposing the MCP server outside the loopback interface.

### 4 — claude-config repo PAT / SSH key (git sync)

Used by `<hydra-project-path>/claude-sync/sync.sh` and the collect script to push/pull the claude-config repo (`your-github-user/claude-config`).

**Source of truth**: `~/.ssh/id_ed25519_claude_config` (or a GitHub PAT stored in the system keychain / environment, never in a dotfile committed to any repo).

Rotation procedure (SSH key variant):

1. Generate new key: `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_claude_config_new -N "" -C "claude-config-$(date +%Y%m)"`
2. Add the new public key to GitHub → Settings → SSH and GPG keys (or repo deploy keys if scoped).
3. Update `~/.ssh/config` to reference the new key for `github.com` (or the relevant Host block).
4. Test: `ssh -T git@github.com`
5. Remove the old public key from GitHub.
6. Delete the old key file.

Rotation procedure (PAT variant):

1. Generate a new fine-grained PAT scoped to `your-github-user/claude-config` with `contents: read+write`.
2. Update the credential store (`git credential approve` or the relevant secret manager).
3. Revoke the old PAT in GitHub → Developer Settings → Personal access tokens.

### Rotation verification checklist

After any rotation, confirm end-to-end before closing the incident:

- [ ] `redis-cli -a <new> ping` → `PONG`
- [ ] `curl http://127.0.0.1:8560/api/status` → `backend: "redis"`
- [ ] Swarm dashboard loads without Redis error banner
- [ ] `swarm handoffs list --since 1h` returns entries (write path works)
- [ ] Replica-sync cron next run succeeds (check `/var/log/swarm-replica-sync.log`)
- [ ] MCP server authenticated call returns 200 with new token
- [ ] `git -C <hydra-project-path>/claude-sync pull` succeeds with new PAT/key
