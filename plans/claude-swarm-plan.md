# claude-swarm — Implementation Plan

## Overview
Distributed Claude Code coordination system using NFS + git for multi-instance awareness and task sharing across the Hydra cluster.

## Phase 1: Core (this build)
- [x] Project structure and config
- [x] swarm_lib.py — status, tasks, artifacts, messages with file locking
- [x] swarm_cli.py — typer CLI for all operations
- [x] Hook scripts — session start/end, heartbeat, task check
- [x] NFS setup scripts — primary, replica, client
- [x] Git sync scripts — to/from claude-config repo
- [x] Health check script
- [x] Tests — lib and hooks
- [x] Skill file for Claude Code integration

## Phase 2: NFS Deployment (manual)
- [ ] Run setup-primary.sh on node_gpu (requires sudo)
- [ ] Run setup-replica.sh on node_primary (requires sudo)
- [ ] Run setup-client.sh on any additional hosts
- [ ] Verify NFS mounts and replication

## Phase 3: Hook Integration (manual)
- [ ] Josh reviews hooks and installs to ~/.claude/hooks/
- [ ] Update settings.json with hook triggers
- [ ] Test session start/end cycle

## Phase 4: Git Sync
- [ ] Add swarm/ directory to claude-config repo
- [ ] Set up cron for sync-to-git.sh on primary
- [ ] Test remote host sync-from-git.sh

## Phase 5: Multi-Node Testing
- [ ] Two concurrent Claude Code instances on different hosts
- [ ] Verify status visibility, task lifecycle, messaging
- [ ] Stress test: rapid task create/claim/complete cycles
