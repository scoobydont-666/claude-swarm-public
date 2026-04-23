# Framework Sync Ledger — claude-swarm ↔ nai-swarm

**Date Established**: 2026-04-23
**Scope**: Formalized cherry-pick sync for shared coordination framework patterns
**Decision Ref**: Grill Decision 4.3 (intentional duplication, not library merge) + lineage-manifest.yaml pair entry (2026-04-21)

---

## Rationale

`claude-swarm` and `nai-swarm` share coordination framework patterns (agent registry, work queue, event bus, heartbeat, GPU slot management) but diverge on domain concerns:

- **claude-swarm** (Hydra platform, open-source path): multi-project orchestration across 30+ Hydra heads, NFS-backed state, Redis-streams IPC; free-form experiment validation
- **nai-swarm** (NAI production path): Nutanix GPU scheduling, Volcano/Kueue integration, tenant isolation, SLO-backed dispatch; enterprise product requirements

Drift audit (2026-04-21): descendant (`nai-swarm`) is 1.4× larger by file count and has 83+ meaningful commits ahead of ancestor at time of the 2026-04-22 sync-bot run. Lineage-sync-phase-1 SOP run escalated this pair as "intentional divergence — not a traditional fork"; automatic cherry-pick was deferred.

Extracting to a shared library risks polluting both codebases with unwanted coupling (Hydra shouldn't depend on Volcano; Nutanix shouldn't depend on NFS state). Instead, we **cherry-pick improvements across repos** and **log each sync event** in this ledger to enable easy review and revert.

**Canonical direction**: per manifest, normally `claude-swarm` (ancestor) → `nai-swarm` (descendant). Reverse cherry-picks are acceptable if descendant-originated patterns prove generically useful to Hydra — record direction in each row.

---

## Ledger

| Date | Direction | Source Commit SHA | Source Repo | Target Commit SHA | Reviewer | Notes |
|------|-----------|-------------------|-------------|-------------------|----------|-------|
| 2026-04-23 | — | — | — | — | — | **Ledger established; first real cherry-pick replaces this row** |

---

## Process: Adding a Row

1. **Source repo**: Land your improvement in a PR. Capture merged commit SHA.
2. **Target repo**: Create a new branch, adapt the code (NOT `git cherry-pick` across the repo boundary — copy-adapt preserving intent; different architectures can't blindly share SHAs).
3. **Target PR**: Reference the source commit SHA in PR body.
4. **Before merge**: Update THIS ledger (on both source and target repo if both carry a copy) with the new row.
5. **Merge target PR**.

**Why log target SHA, not source?** The target maintainer owns the change; their SHA is the auditable record.

---

## Cherry-Pick Evaluation Rubric

For each candidate:
- **Alignment**: Does the pattern solve the same problem in both repos?
- **Context isolation**: Adaptable without dragging in Hydra-specific (NFS, Redis Streams) or NAI-specific (Volcano, Kueue, K8s) infrastructure?
- **Testing**: Source-side tests present? Do they port?
- **Rollback**: Reversible without architectural consequences?

If uncertain, defer to `PENDING_SYNCS.md` (created on first deferral).

---

## Pattern Families Eligible for Sync

Based on the 2026-04-21 drift audit + sentinel cherry-pick matrix methodology:

1. **Agent registry / heartbeat** — polling cadence, staleness detection, ghost-agent eviction
2. **Work queue / priority + capability matching** — priority_tier * 1000 + usage_ratio * 100 scoring pattern
3. **Event bus / cross-agent context** — schema evolution, versioning, consumer group semantics
4. **GPU slot accounting** — VRAM-budget tracking, eviction scoring (see P1-NAI-CKPT on nai-reserve side)
5. **Session lifecycle protocol** — start/heartbeat/end hooks, crash recovery
6. **Observability / metrics surface** — Prometheus exporters, Grafana dashboards

**NOT eligible for sync** (architectural divergence):
- Volcano / Kueue scheduler adapters (NAI only)
- Redis Streams IPC wire protocol (Hydra only — NAI uses direct K8s API)
- Tenant RBAC + credit-ledger integration (NAI only — F.1 scope)

---

## Deferred pending architectural decisions

- **Message bus canonicalization**: claude-swarm uses Redis Streams; nai-swarm uses a mix of Kafka-style (via nai-ipc) + direct K8s watches. Sync bot flagged 2231 file diffs on 2026-04-21 as "despite identical commit timestamps" — this is the diff surface. Needs a separate grill before cherry-pick attempts.
- **State store**: claude-swarm NFS + Redis; nai-swarm PostgreSQL (via nai-reserve). No sync planned; architecturally disjoint.

---

**Document Version**: 1.0
**Last Updated**: 2026-04-23
**Pair manifest entry**: `<hydra-project-path>/docs/lineage-manifest.yaml` → established_pairs[0]
