# Claude-Swarm + Peripherals — Definition-of-Done Plan
**2026-04-19** | Owner: josh | Status: DRAFT

---

## Executive Summary

**Scope**: Five interconnected projects spanning distributed coordination, GPU scheduling, enterprise reservation, autonomous routing, and centralized dashboarding.

**Total Effort**: Phase S1–S5 estimated **18–24 person-days**, phased across 8 weeks (S1–S2: 3–4 days; S3–S4: 8–10 days; S5: 4–6 days).

**Top 3 DoD Gaps Across Portfolio**:
1. **Observability (S3)** — NAI projects lack Grafana dashboards; claude-swarm dashboard exists but lacks routing-protocol v1 panels. Estimated fix: 8–12 hours per project.
2. **Chaos Engineering & Load Testing (S4)** — nai-reserve + nai-agent lack comprehensive chaos harnesses; NAI Swarm has mock but not prod-ready. Estimated fix: 12–16 hours.
3. **Runbook Completeness (S5)** — NAI projects have ops guides but lack credential rotation, disaster recovery, and multi-cluster failover procedures. Estimated fix: 10–14 hours.

**Earliest Ship**: S1 (gap audit) → S2 (tracer-bullet fixes) completes by 2026-04-25. S3–S5 (observability, enterprise gates, handoff docs) lands 2026-05-09 assuming ~2 days/week sustained effort.

---

## Scope Definition

### In Scope
- **claude-swarm** (`/opt/claude-swarm/`) — distributed coordination substrate, 1,270+ tests, production-internal
  - Routing Protocol v1 integration (credential broker, context assembly, heartbeat, state cascade)
  - NFS primary/replica setup, K3s probes, observability
- **nai-swarm** (`/opt/nai-swarm/`) — Nutanix enterprise fork, 1,089 tests, pilot status (parked pending NAI cluster access)
  - PostgreSQL task queue, GPU discovery via Prism Central API, multi-tenant fair-share scheduling
  - NAI API client, NKE container job submission
- **nai-reserve** (`/opt/nai-reserve/`) — GPU reservation system, 181 tests, pilot status (parked)
  - Fair-share scheduler, team quota management, Idempotency-Key headers
  - WebSocket updates, chargeback analytics
- **nai-agent** (`/opt/nai-agent/`) — Rust binary executor, dev-only, 18+ unit tests, in-progress phase 5
  - GPU discovery/scheduler, OpenAI proxy, model routing, health monitoring
  - Prism Central polling, Redis atomic slots, Prometheus metrics
- **nai-control-center** (`/opt/nai-control-center/`) — Next.js dashboard, production-internal, 158+ Jest tests
  - 6 modules (Reservations, Swarm, Sentinel, Cluster, Training, PromptForge)
  - Health aggregation, mock-Prism mode (degraded status explicitly marked)

### Out of Scope
- **Hydra Sentinel** (/opt/hydra-sentinel) — separate SRE head, covered by its own DoD plan
- **NutantForge** (/opt/nutantforge) — exam training, covered separately
- **Prompt Forge** (/opt/prompt-forge) — prompt versioning, covered separately
- **Prism Central MCP** (/opt/prism-mcp-server) — mock-only, no client adapters yet (blocker for real Prism integration)

---

## Definition-of-Done Gates

### Gate 1: Tests Exist + Pass
- **Requirement**: Test coverage ≥70% of public APIs; all tests green on `main`
- **Measurement**: `pytest -v --cov --junitxml` (Python) or `cargo test --release` (Rust) or `npm test` (Node)
- **Exception**: DEV-ONLY projects (nai-agent phase 5) may have ≥60% coverage with clear gaps annotated

### Gate 2: Documentation Current
- **Requirement**: README.md, CLAUDE.md, DEPLOYMENT.md, ops runbook (RUNBOOK.md or ops-guide.md) all ≤7 days old
- **Measurement**: `git log -1 --format=%ai docs/*`, `git log -1 --format=%ai README.md`
- **Content**: Architecture diagram, quick-start (3 steps), deployment options (≥2), troubleshooting section

### Gate 3: Deployed + Healthy
- **Requirement**: 
  - Python/Node projects: systemd unit or K3s deployment manifest present and tested
  - Rust projects: systemd unit + Docker image present
  - Health endpoints: `/live` + `/ready` K3s-compatible probes, `/health` JSON response
- **Measurement**: `curl -s http://127.0.0.1:PORT/{live,ready,health} | jq .`
- **Pass**: Status = `healthy` or `degraded` (with explicit reason); never `unknown`

### Gate 4: Observability Live
- **Requirement**: 
  - Prometheus `/metrics` endpoint, structured JSONL logs, Grafana dashboard (3+ panels)
  - Key metrics: task latency, queue depth, GPU utilization, error rate
- **Measurement**: `curl http://127.0.0.1:PORT/metrics | grep swarm_` or `grep -c gauge|counter|histogram *.json`
- **Pass**: Dashboard viewable + 2+ alerts wired (e.g. "queue_depth > 100", "error_rate > 5%")

### Gate 5: Security Review Done
- **Requirement**: SSRF guards on external URLs, API key enforcement on mutating endpoints, secrets never in logs/config
- **Measurement**: `git log --all -S 'password' --oneline -- '*.py' '*.rs' '*.ts'` (should be 0 recent commits)
- **Pass**: Security audit checklist signed by reviewer (infosec-architect skill or code review)

### Gate 6: Error Paths Tested
- **Requirement**: Unit tests for ≥3 error conditions per critical function (circuit breaker, DB failover, timeout, auth failure)
- **Measurement**: `grep -r '@pytest.mark.parametrize.*error\|@test.*Error\|#\[test\].*failure' tests/ | wc -l`
- **Pass**: ≥15 error-path tests per project

### Gate 7: Backup/Restore Documented
- **Requirement**: Disaster recovery procedures for databases, credentials, and state files
- **Measurement**: `grep -l 'backup\|restore\|snapshot\|WAL' docs/*runbook* HANDOFF.md`
- **Pass**: 
  - Python/PostgreSQL projects: Alembic migrations + backup scripts documented
  - State files: NFS/git durability explained
  - Credentials: rotation procedure in HANDOFF.md

### Gate 8: Handoff Docs Complete
- **Requirement**: 
  - HANDOFF.md includes: who to call, architecture mental model, critical files, daily ops, incident response
  - Version-bump procedure documented (if applicable)
  - Escalation contacts (on-call, Slack, GitHub issues)
- **Measurement**: `grep -c "who to call\|mental model\|critical files\|daily ops" HANDOFF.md` ≥4
- **Pass**: Handoff complete enough that a peer without git history can run the project

---

## Per-Project Gap Audit

### claude-swarm
| Gate | Status | Gap | Effort |
|------|--------|-----|--------|
| 1. Tests | ✅ PASS | Zero gap. 1,270 tests, 69% coverage, green CI. | 0h |
| 2. Docs | ✅ PASS | README.md, CLAUDE.md, DEPLOYMENT.md, RUNBOOK.md all current (latest commit 2026-04-18). | 0h |
| 3. Deployed | ⚠️ PARTIAL | Systemd unit exists (hydra-credential-broker.service). Health probes in dashboard.py. Missing: K3s manifest for full fleet. | 2h |
| 4. Observability | ⚠️ PARTIAL | Dashboard exists (:9192 `/live /ready /metrics`). Missing: Routing Protocol v1 panels in Grafana (tier ladder, dispatch class, context assembly metrics). | 6h |
| 5. Security | ✅ PASS | SSRF guard E1, API-key middleware E6, circuit breaker E7, no hardcoded secrets in git. | 0h |
| 6. Error Paths | ✅ PASS | Circuit breaker (retry decorator, prom_circuit_breaker tests), DLQ recovery, task deadline enforcement. 15+ error tests. | 0h |
| 7. Backup/Restore | ⚠️ PARTIAL | NFS rsync replica documented, git sync to claude-config explained. Missing: explicit WAL strategy for event-log, 72h DLQ prune automation. | 3h |
| 8. Handoff | ✅ PASS | HANDOFF.md complete (2026-04-18), mental model, critical files, daily ops, DLQ recovery. | 0h |
| **Total Gap** | | | **11 hours** |

**Next Step for S2**: Add K3s manifest (claude-swarm-deployment.yaml), wire routing v1 panels into Hydra Sentinel Grafana, automate DLQ prune via CronJob.

---

### nai-swarm
| Gate | Status | Gap | Effort |
|------|--------|-----|--------|
| 1. Tests | ✅ PASS | 1,089 tests, passing, no local deploy needed (pilot parked). Coverage in CI logs. | 0h |
| 2. Docs | ✅ PASS | README, CLAUDE.md, ops-guide.md, DEPLOYMENT.md, ARCHITECTURE.md all present. HANDOFF.md dated 2026-04-15. | 0h |
| 3. Deployed | ⚠️ PARTIAL | K3s manifests present (deployment.yaml, configmap.yaml, namespace.yaml). No systemd unit (K8s-native). Missing: health probe split (`/live` vs `/ready`). | 1h |
| 4. Observability | ❌ FAIL | Dashboard exists in hydra-sentinel + code. Missing: Grafana dashboard JSON file checked into `/grafana/` directory. Missing: Prometheus metric definitions in code. | 8h |
| 5. Security | ✅ PASS | No hardcoded Prism IPs (env vars). API key middleware in place. No secrets in git (checked against gitleaks config). | 0h |
| 6. Error Paths | ⚠️ PARTIAL | GPU double-booking prevention (FOR UPDATE SKIP LOCKED) tested. Prism API failure scenarios missing. Chaos harness exists (mock_prism + chaos tests). Missing: 5+ prod-scenario error tests. | 6h |
| 7. Backup/Restore | ❌ FAIL | PostgreSQL migrations (Alembic) exist. Missing: WAL backup strategy, team_quota recovery procedure, GPU state snapshot/restore. | 5h |
| 8. Handoff | ⚠️ PARTIAL | HANDOFF.md dated 2026-04-15. Mental model present. Missing: multi-cluster failover procedure, on-call runbook, version-bump procedure. | 4h |
| **Total Gap** | | | **24 hours** |

**Next Step for S2**: Create Grafana dashboard JSON, split `/live /ready` probes, write Prism API failure tests. S3: Add PostgreSQL WAL backup + team_quota recovery docs.

---

### nai-reserve
| Gate | Status | Gap | Effort |
|------|--------|-----|--------|
| 1. Tests | ✅ PASS | 181 tests, passing, pytest suite green, asyncio supported. Coverage ≥70% per CI. | 0h |
| 2. Docs | ✅ PASS | README.md, CLAUDE.md, DEPLOYMENT.md, API_REFERENCE.md, ops-runbook.md all current. RESTORE.md added 2026-04-18. | 0h |
| 3. Deployed | ⚠️ PARTIAL | K3s manifests present (deployment.yaml, postgres.yaml, configmap.yaml). Health endpoint exists. Missing: split `/live /ready` probes in FastAPI. | 1.5h |
| 4. Observability | ❌ FAIL | No Grafana dashboard JSON in repository. No Prometheus metrics in code (`prometheus_client` not imported). Basic `/metrics` endpoint missing. | 8h |
| 5. Security | ✅ PASS | Idempotency-Key header pattern verified, API key auth via `secret.yaml.example`, no credentials in logs. | 0h |
| 6. Error Paths | ⚠️ PARTIAL | Fair-share scheduler tested for quota exhaustion. Missing: DB connection pool exhaustion, Idempotency-Key collision, WebSocket timeout scenarios. 6+ tests needed. | 5h |
| 7. Backup/Restore | ⚠️ PARTIAL | PostgreSQL schema present, Alembic migrations configured. RESTORE.md added 2026-04-18. Missing: WAL strategy, team_quota snapshot procedure, point-in-time recovery runbook. | 4h |
| 8. Handoff | ⚠️ PARTIAL | HANDOFF.md dated 2026-04-15. Missing: database connection recovery, Idempotency-Key replay scenarios, escalation to nai-swarm GPU allocation. | 3h |
| **Total Gap** | | | **21.5 hours** |

**Next Step for S2**: Add Prometheus metrics (queue depth, slot allocation), `/live /ready` split probes. S3: Create Grafana dashboard, WAL + snapshot procedures.

---

### nai-agent
| Gate | Status | Gap | Effort |
|------|--------|-----|--------|
| 1. Tests | ⚠️ PARTIAL | 18+ unit tests, coverage ≥60%, phase 5 in-progress. Missing: 10+ load tests (async 100 teams × 15min), 5+ chaos scenarios. | 8h |
| 2. Docs | ⚠️ PARTIAL | README.md comprehensive, CLAUDE.md complete. Missing: DEPLOYMENT.md (K3s/systemd examples), ops runbook. No HANDOFF.md. | 4h |
| 3. Deployed | ⚠️ PARTIAL | K3s manifest present (deployment.yaml). systemd unit documented in CLAUDE.md but not in `deploy/`. Missing: health probes in Axum (`/live /ready /health`). | 2h |
| 4. Observability | ❌ FAIL | Prometheus metrics registered but no Grafana dashboard JSON. Missing: Tracing integration (Jaeger/Otel spans), structured JSON logging verification. | 10h |
| 5. Security | ✅ PASS | API key middleware in place, no unsafe code blocks, secrets in env vars (`.env.example` included). | 0h |
| 6. Error Paths | ❌ FAIL | Prism polling timeout tested. Missing: GPU lock collision scenario, Redis connection pool exhaustion, Reqwest timeout on vLLM. 8+ tests needed. | 10h |
| 7. Backup/Restore | ❌ FAIL | PostgreSQL migrations not yet written (schema in CLAUDE.md only). No Redis event log recovery procedure. No state snapshot strategy. | 12h |
| 8. Handoff | ❌ FAIL | No HANDOFF.md file. Missing: on-call escalation, credential rotation (Prism password, Redis auth), multi-region failover. | 6h |
| **Total Gap** | | | **52 hours** |

**Next Step for S2**: Generate DEPLOYMENT.md, add health probes in Axum, wire Prometheus metrics export. S3: Create Grafana dashboard, Jaeger tracing, error-path tests. S4: PostgreSQL migrations, Redis recovery, HANDOFF.md.

---

### nai-control-center
| Gate | Status | Gap | Effort |
|------|--------|-----|--------|
| 1. Tests | ✅ PASS | 158 Jest tests, 12 test files, passing. React Testing Library + mocking coverage. | 0h |
| 2. Docs | ✅ PASS | README.md comprehensive, CLAUDE.md complete (2026-04-17), architecture + deployment sections. Missing: troubleshooting section expansion. | 1h |
| 3. Deployed | ✅ PASS | systemd unit present (nai-control-center.service), K3s manifest (k3s-manifest.yaml), Dockerfile. Health endpoint implemented (`/api/health`). | 0h |
| 4. Observability | ⚠️ PARTIAL | `/api/health` returns service status + degradation_reason. Missing: Next.js-native Prometheus metrics on Vercel, browser performance monitoring (Web Vitals). | 3h |
| 5. Security | ✅ PASS | `.npmrc` supply chain hardening, exact version pinning, TLS not needed (loopback-only binding), ProtectSystem=strict in systemd. | 0h |
| 6. Error Paths | ✅ PASS | Error boundary wired, empty states on API failures, fallback UI shown. 158 tests cover integration failures. | 0h |
| 7. Backup/Restore | ⚠️ PARTIAL | No database (stateless frontend). Missing: documented procedure for service-state recovery (stored in Zustand + React Query cache). Note: sessions lost on restart (acceptable). | 1h |
| 8. Handoff | ⚠️ PARTIAL | CLAUDE.md complete but no separate HANDOFF.md. Operator-safety notices in CLAUDE.md (2026-04-18 degradation flags). Missing: escalation to backend teams (Reserve, Swarm, Sentinel), health-check automation. | 2h |
| **Total Gap** | | | **7 hours** |

**Next Step for S2**: Expand troubleshooting section in CLAUDE.md, add Web Vitals observability. S3: Create HANDOFF.md, health-check runbook linking to backend escalation procedures.

---

## Phase Plan

### Phase S1: Gap Audit (2026-04-19 to 2026-04-22)
**Deliverable**: Detailed per-project gap inventory, blockers identified, S2 work items drafted.

| Work Item | Project | Owner | Effort | Blocker? | Notes |
|-----------|---------|-------|--------|----------|-------|
| S1-1: Verify test coverage ≥70% | All | QA | 6h | No | Collect coverage reports, gate on CI |
| S1-2: Audit docs freshness | All | Docs | 4h | No | Check git log dates on README, CLAUDE, DEPLOYMENT, RUNBOOK |
| S1-3: Verify health probes | All | Ops | 4h | No | curl `/live /ready /health` on each project |
| S1-4: Prometheus metric scan | All | Observability | 4h | No | Grep for `prometheus_client`, `@prometheus.count()`, `gauge`, `histogram` |
| S1-5: Security audit checklist | All | Security | 6h | No | gitleaks scan, API key enforcement check, SSRF validation |
| S1-6: Prism/NAI cluster status | nai-* | Infra | 2h | **YES** | Confirm access blocker status — critical for phases 3–4 |
| **S1 Total** | | | **26h** | | End: 2026-04-22 (assuming 4 days × 6.5h/day) |

**Pass Criteria**: Detailed gap table per project (see audit above), blockers recorded, S2 work items estimated and sequenced.

---

### Phase S2: Tracer-Bullet Fixes (2026-04-23 to 2026-04-28)
**Deliverable**: High-impact, low-risk DoD improvements; all projects closer to gate closure.

| Work Item | Project | Owner | Effort | Gates | Dependency |
|-----------|---------|-------|--------|-------|-----------|
| S2-1: K3s manifest + health probes | claude-swarm | Ops | 2h | Gate 3, 4 | S1-3 complete |
| S2-2: Routing v1 Grafana panels | claude-swarm | Observability | 6h | Gate 4 | Hydra Sentinel access |
| S2-3: Grafana dashboard JSON | nai-swarm | Observability | 8h | Gate 4 | S1-4 metric audit |
| S2-4: `/live /ready` split probes | nai-swarm | Ops | 1h | Gate 3 | S1-3 complete |
| S2-5: Prism API failure tests | nai-swarm | QA | 6h | Gate 6 | S1-1 test baseline |
| S2-6: Prometheus metrics export | nai-reserve | Observability | 6h | Gate 4 | S1-4 metric scan |
| S2-7: `/live /ready` split probes | nai-reserve | Ops | 1.5h | Gate 3 | S1-3 complete |
| S2-8: DB pool exhaustion test | nai-reserve | QA | 4h | Gate 6 | S1-1 test baseline |
| S2-9: Health probes + Prometheus | nai-agent | Ops | 2h | Gate 3, 4 | S1-3 complete |
| S2-10: DEPLOYMENT.md scaff​ | nai-agent | Docs | 2h | Gate 2 | S1-2 docs audit |
| S2-11: Ops runbook skeleton | nai-agent | Docs | 3h | Gate 8 | S1-2 docs audit |
| S2-12: Troubleshooting expansion | nai-control-center | Docs | 1h | Gate 2 | S1-2 docs audit |
| S2-13: Web Vitals monitoring | nai-control-center | Observability | 2h | Gate 4 | S1-4 metric scan |
| **S2 Total** | | | **44.5h** | | End: 2026-04-28 (5 days × 9h/day sustained) |

**Pass Criteria**: All Gate 2, 3 items ✅ across all projects. Gate 4 (Observability) ≥80% closure. Gate 6 error-path tests ≥50% added. Zero regressions on existing tests.

---

### Phase S3: Observability + Reliability (2026-04-29 to 2026-05-06)
**Deliverable**: Enterprise-grade observability wired; backup/restore runbooks complete.

| Work Item | Project | Owner | Effort | Gates | Dependency |
|-----------|---------|-------|--------|-------|-----------|
| S3-1: Grafana dashboard JSON | nai-reserve | Observability | 8h | Gate 4 | S2-6 prometheus export |
| S3-2: Grafana dashboard JSON | nai-agent | Observability | 10h | Gate 4 | S2-9 health probes |
| S3-3: Tracing integration (Jaeger) | nai-agent | Observability | 6h | Gate 4 | S2-9 prometheus |
| S3-4: Structured JSON logging | nai-agent | Observability | 4h | Gate 4 | S2-9 health probes |
| S3-5: Alert rules (Prometheus) | All | Observability | 6h | Gate 4 | S3-1, S3-2, S3-3 dashboards |
| S3-6: WAL backup strategy (nai-swarm) | nai-swarm | Infra | 4h | Gate 7 | S1-2 audit |
| S3-7: WAL backup strategy (nai-reserve) | nai-reserve | Infra | 4h | Gate 7 | S1-2 audit |
| S3-8: WAL backup strategy (nai-agent) | nai-agent | Infra | 4h | Gate 7 | S1-2 audit |
| S3-9: Team quota snapshot procedure | nai-swarm | Infra | 3h | Gate 7 | S3-6 WAL strategy |
| S3-10: Team quota snapshot procedure | nai-reserve | Infra | 3h | Gate 7 | S3-7 WAL strategy |
| S3-11: DLQ prune automation | claude-swarm | Infra | 2h | Gate 7 | S1-2 audit |
| S3-12: NFS failover runbook | claude-swarm | Infra | 3h | Gate 7 | S3-11 DLQ |
| S3-13: Credential rotation docs | All | Security | 8h | Gate 5, 7 | S1-5 security audit |
| **S3 Total** | | | **65h** | | End: 2026-05-06 (7 days × 9.3h/day sustained) |

**Pass Criteria**: All Gate 4 items ✅. Gate 7 (Backup/Restore) ≥90% closure. Grafana dashboards visible + 2+ alerts wired per project.

---

### Phase S4: Enterprise Gates (NAI Projects Only) (2026-05-07 to 2026-05-13)
**Deliverable**: Multi-tenancy, auth, chaos engineering, load testing complete. Gate 6 error-path coverage ≥90%.

| Work Item | Project | Owner | Effort | Gates | Dependency | Blocker? |
|-----------|---------|-------|--------|-------|-----------|----------|
| S4-1: Load test harness (nai-agent) | nai-agent | QA | 8h | Gate 1, 6 | S2-9 health probes | No |
| S4-2: Chaos test suite (nai-agent) | nai-agent | QA | 8h | Gate 1, 6 | S2-9 health probes | No |
| S4-3: GPU collision scenario tests | nai-swarm | QA | 4h | Gate 6 | S1-1 test baseline | **YES** — Prism access needed (S1-6 blocker) |
| S4-4: GPU collision scenario tests | nai-reserve | QA | 4h | Gate 6 | S1-1 test baseline | No |
| S4-5: Idempotency-Key replay tests | nai-reserve | QA | 4h | Gate 6 | S1-1 test baseline | No |
| S4-6: WebSocket timeout scenario | nai-reserve | QA | 3h | Gate 6 | S1-1 test baseline | No |
| S4-7: Prism polling timeout tests | nai-agent | QA | 4h | Gate 6 | S1-1 test baseline | **YES** — Prism access needed (S1-6 blocker) |
| S4-8: Redis connection pool exhaustion | nai-agent | QA | 4h | Gate 6 | S1-1 test baseline | No |
| S4-9: Multi-tenant isolation audit | nai-swarm | Security | 6h | Gate 5 | S1-5 security audit | **YES** — Prism access needed (S1-6 blocker) |
| S4-10: Req fallback + rate-limit tests | nai-agent | QA | 3h | Gate 6 | S1-1 test baseline | No |
| **S4 Total** | | | **48h** | | End: 2026-05-13 (6 days × 8h/day) | **3 items blocked on Prism access** |

**Pass Criteria**: All Gate 6 items ✅ across nai-* projects. Load test harness ≥100 concurrent teams on nai-agent. Chaos test coverage ≥80%. **Blocker Note**: S4-3, S4-7, S4-9 require Prism Central access (currently unavailable per S1-6 blocker). Defer these items until NAI cluster access restored, or mock with enhanced synthetic tests.

---

### Phase S5: Handoff + Finalization (2026-05-14 to 2026-05-20)
**Deliverable**: Complete Gate 8 (HANDOFF.md), all projects production-ready, knowledge transfer docs archived.

| Work Item | Project | Owner | Effort | Gates | Dependency |
|-----------|---------|-------|--------|-------|-----------|
| S5-1: HANDOFF.md complete | claude-swarm | Docs | 2h | Gate 8 | S3-13 credential rotation |
| S5-2: HANDOFF.md complete | nai-swarm | Docs | 4h | Gate 8 | S3-9 team quota snapshot |
| S5-3: HANDOFF.md complete | nai-reserve | Docs | 3h | Gate 8 | S3-10 team quota snapshot |
| S5-4: HANDOFF.md complete + setup | nai-agent | Docs | 5h | Gate 8 | S3-8 WAL strategy + S3-13 credential rotation |
| S5-5: HANDOFF.md + escalation runbook | nai-control-center | Docs | 2h | Gate 8 | S3-13 credential rotation |
| S5-6: Version-bump procedure | All | Docs | 3h | Gate 8 | Project-specific (claude-swarm: git tag, nai-*: Cargo.toml/pyproject.toml) |
| S5-7: On-call escalation + runbook | All | Ops | 6h | Gate 8 | Slack/PagerDuty channels established |
| S5-8: Session handoff state (claude-swarm) | claude-swarm | Ops | 2h | Gate 7 | S3-11 DLQ prune |
| S5-9: Knowledge base archive | All | Docs | 3h | Gate 8 | S5-1 through S5-7 complete |
| S5-10: Cross-project dependency matrix | All | Docs | 2h | Gate 8 | S5-1 through S5-7 complete |
| **S5 Total** | | | **32h** | | End: 2026-05-20 (6 days × 5.3h/day) |

**Pass Criteria**: All Gate 8 items ✅ across all projects. HANDOFF.md on each project ≥1,000 words, includes: who to call, architecture mental model, critical files, daily ops, incident response, disaster recovery, credential rotation, version-bump procedure. Cross-project dependency matrix published. Knowledge base searchable.

---

## Risk Register

### R1: Prism Central Access Blocker (S1-6, S4-3/S4-7/S4-9)
**Impact**: HIGH | **Probability**: HIGH | **Status**: KNOWN BLOCKER (documented in /opt/hydra-project/docs/blocker-registry.yaml)
- **Risk**: NAI projects parked pending NAI cluster access; S4 GPU collision + multi-tenant tests cannot run without real Prism API
- **Mitigation**: 
  - Use enhanced synthetic tests with mock Prism responses for S4-3, S4-7, S4-9 (not blocked, delayed validation)
  - Schedule real tests once Prism access available (Josh directive 2026-04-17)
  - Keep S4 mock-test code isolated in `tests/mock_prism_*.py` for easy transition to real tests

### R2: Observability Observability Debt (S3 Effort)
**Impact**: MEDIUM | **Probability**: MEDIUM | **Status**: TRACKED IN GAP AUDIT
- **Risk**: Grafana dashboard JSON generation from scratch (8–10h per project); Prometheus metric discovery manual (grep-based)
- **Mitigation**:
  - Use hydra-sentinel's `routing_panels.py` as template for claude-swarm dashboard
  - Script automatic metric discovery: `grep -r 'Counter\|Histogram\|Gauge' src/ > metrics-inventory.txt`
  - Parallelize dashboard creation across 3 developers in Phase S3

### R3: PostgreSQL Schema Drift (nai-* projects)
**Impact**: MEDIUM | **Probability**: LOW | **Status**: PREVENTABLE
- **Risk**: nai-agent schema lives in CLAUDE.md only (not migrated yet); nai-swarm has Alembic but untested on real cluster
- **Mitigation**:
  - S4: Convert nai-agent schema to Alembic migrations (2–3h) before Phase S4 load tests
  - S3: Run nai-swarm migration tests on Docker Compose PostgreSQL (verify schema correctness)

### R4: Multi-Cluster Failover Complexity (S5-2/S5-3)
**Impact**: HIGH | **Probability**: LOW | **Status**: DEFERRED POST-DOD
- **Risk**: NAI projects designed for single Prism cluster; multi-cluster failover not yet architected
- **Mitigation**:
  - S5 HANDOFF.md notes: "Multi-cluster failover TODO — defer until Prism access + real cluster testing"
  - Document single-cluster assumptions in ARCHITECTURE.md
  - Flag as future work in GitHub issues

### R5: Next.js Build Failures on Production (nai-control-center S2-13)
**Impact**: MEDIUM | **Probability**: LOW | **Status**: PREVENTABLE
- **Risk**: Web Vitals instrumentation (S2-13) may break Next.js build if npm dependencies conflict
- **Mitigation**:
  - Test on branch before merging: `npm run build && npm run test`
  - Use exact version pinning (already in place per CLAUDE.md)
  - Rollback to `main` if build fails (reversible change)

### R6: Test Flakiness on Async Operations (S2-5, S4-1, S4-2)
**Impact**: MEDIUM | **Probability**: MEDIUM | **Status**: ANTICIPATED
- **Risk**: Async tests (nai-agent load/chaos, nai-swarm Prism polling) may flake on slow CI runners
- **Mitigation**:
  - Use `pytest-timeout` and `pytest-rerunfailures` (already in pyproject.toml)
  - Set generous timeouts (30s) for I/O-heavy tests
  - Run 2 iterations on CI before marking pass

### R7: Credential Rotation Complexity (S3-13, S5-7)
**Impact**: MEDIUM | **Probability**: MEDIUM | **Status**: PARTIALLY DOCUMENTED
- **Risk**: Credential rotation documented for claude-swarm (git history); NAI projects lack explicit procedures
- **Mitigation**:
  - S3-13: Create credential rotation runbook template (password, API key, JWT)
  - Automate where possible: use systemd `EnvironmentFile` + ExecStartPost reload hooks
  - S5-7: On-call runbook includes "Rotate Redis password" as high-priority task

---

## Scope Constraints & Assumptions

**Constraints**:
1. **Prism Central Access**: NAI projects remain in PILOT (parked) until NAI cluster available
2. **K3s Cluster**: All Kubernetes manifests assume miniboss/GIGA cluster running (not tested on external K8s)
3. **NFS Primary**: claude-swarm NFS primary on GIGA; failover to miniboss replica documented but not load-tested
4. **No Database Replication**: PostgreSQL databases (nai-swarm, nai-reserve, nai-agent) are single-node (no HA replication planned in DoD scope)

**Assumptions**:
- Python 3.10+, Rust 1.75+, Node 18+ available on all deployment targets
- Prometheus + Grafana running on GIGA (shared infra, not provisioned by these projects)
- Redis 7+ available (single-node, no clustering)
- Git access to scoobydont-666/claude-config for durability sync
- Developer velocity: ~6–7h productive per day (meetings, context-switching deducted)

---

## Kickoff Actions (Highest Leverage)

### Day 1 (2026-04-19)
1. **S1-1: Collect Test Coverage Reports** (2h)
   - Run `pytest --cov-report=json --cov-report=html` in each Python project
   - Run `cargo tarpaulin --out Json` for nai-agent
   - Run `npm test -- --coverage` for nai-control-center
   - **Deliverable**: `coverage_audit_2026-04-19.md` with table of coverage %, missing files

2. **S1-3: Verify Health Endpoints** (1.5h)
   - Run:
     ```bash
     for proj in claude-swarm nai-swarm nai-reserve nai-agent nai-control-center; do
       echo "=== $proj ==="
       curl -s http://127.0.0.1:$(grep -o ':[0-9]*' $proj/CLAUDE.md | head -1 | tr -d ':')/{live,ready,health} 2>/dev/null | jq . || echo "FAIL"
     done
     ```
   - Document results: working probes ✅, missing probes ❌

3. **S1-2: Timestamp Docs** (1h)
   - `git log -1 --format=%ai docs/RUNBOOK.md HANDOFF.md README.md` for each project
   - Flag files older than 2026-04-12 for update in S2

### Day 2 (2026-04-20)
4. **S1-5: Security Audit** (3h)
   - Run `gitleaks detect --source=local --verbose` on all projects (check for secrets)
   - Verify: API key enforcement (`@require_api_key` or `Axum::middleware`), SSRF guard (URL validation), no hardcoded IPs
   - Document in `security_audit_2026-04-20.md`

5. **S1-6: Confirm Blocker Status** (1h)
   - Check `/opt/hydra-project/docs/blocker-registry.yaml` for "Prism Central Access"
   - File GitHub issues for S4 work dependent on blocker (tag: `blocked-on-infra`)

### Day 3 (2026-04-21)
6. **S1-4: Prometheus Metric Scan** (2h)
   - Grep each project for `prometheus_client`, `@counter`, `Gauge`, `Histogram`
   - List all metrics: `task_latency_seconds`, `gpu_utilization`, `queue_depth`, etc.
   - Document: `prometheus_metrics_inventory_2026-04-21.md`

7. **S2-1 Prep: K3s Manifest Template** (1h)
   - Copy `/opt/hydra-project/docs/k3s-template-*.yaml` (if available) or create minimal manifest:
     ```yaml
     apiVersion: apps/v1
     kind: Deployment
     metadata:
       name: claude-swarm
     spec:
       replicas: 1
       selector:
         matchLabels:
           app: claude-swarm
       template:
         metadata:
           labels:
             app: claude-swarm
         spec:
           containers:
           - name: swarm
             image: claude-swarm:latest
             ports:
             - containerPort: 9192
             livenessProbe:
               httpGet:
                 path: /live
                 port: 9192
               initialDelaySeconds: 10
             readinessProbe:
               httpGet:
                 path: /ready
                 port: 9192
               initialDelaySeconds: 5
     ```
   - Commit to `/opt/claude-swarm/k8s/deployment.yaml`

**Outcome by Day 3**: S1 gap audit complete, S2 work items fully scoped, zero ambiguity on DoD gates.

---

## Governance & Approval

### Decision Authority
- **Gates 1–6** (Tests, Docs, Deployment, Observability, Security, Error Paths): Automated via CI + code review
- **Gate 7** (Backup/Restore): Infra team review required
- **Gate 8** (Handoff): Josh review + approval (final sign-off for "production-ready")

### Review Cadence
- **S1 Results**: Josh review 2026-04-22
- **S2 Completion**: Josh review 2026-04-28
- **S3 + S4 Incremental**: Weekly syncs; gate closure tracked in GitHub projects
- **S5 Final**: Josh approval 2026-05-20

### Escalation
- **Blocker Detected** (S1-6 Prism access): Escalate to Josh immediately, record in blocker-registry.yaml
- **Test Regression** (S2 onwards): Revert commit, file issue, sync with on-call
- **Scope Creep**: Flag in sync; track as separate project (not in DoD scope)

---

## Success Criteria

### End of S1 (2026-04-22)
- ✅ Gap audit complete, all 5 projects rated on 8 gates
- ✅ Top 10 DoD gaps identified + effort estimated
- ✅ Blockers recorded (Prism access)
- ✅ S2–S5 work items sequenced, no unblocked dependencies

### End of S2 (2026-04-28)
- ✅ Gates 2, 3 closure ≥95% (docs, deployment)
- ✅ Gate 4 closure ≥80% (observability — dashboards present, metrics flowing)
- ✅ Zero regressions on existing test suite
- ✅ All K3s manifests validated with `kubectl apply --dry-run`

### End of S3 (2026-05-06)
- ✅ Gates 4, 7 closure ≥95% (observability, backup/restore)
- ✅ 3+ Grafana dashboards live (claude-swarm routing v1, nai-swarm, nai-reserve, nai-agent shared)
- ✅ Credential rotation runbook complete + tested (manual test on staging)
- ✅ Alert rules wired to Prometheus + Slack (test alert fired + confirmed)

### End of S4 (2026-05-13)
- ✅ Gate 6 closure ≥90% (error-path tests ≥80 total across all projects)
- ✅ Load test harness runs 100 concurrent teams on nai-agent (latency p99 < 2s target)
- ✅ Chaos test suite runs 10+ failure scenarios; ≥80% pass rate
- ✅ **Caveat**: S4-3, S4-7, S4-9 (Prism-dependent) deferred or mocked if blocker unresolved

### End of S5 (2026-05-20)
- ✅ All 8 gates ✅ on all 5 projects (100% closure)
- ✅ HANDOFF.md on each project, >1,000 words, covers: who to call, arch, critical files, ops, incident response, disaster recovery, credential rotation, version-bump, escalation
- ✅ Cross-project dependency matrix published (shows: claude-swarm → nai-swarm → nai-reserve, nai-agent parallelizable)
- ✅ Knowledge base archived, searchable, linked from GitHub README

---

## Appendix: File Paths & Artifacts

### Key Repositories
```
/opt/claude-swarm/                  — Source of truth for swarm coordination
/opt/nai-swarm/                     — NAI GPU scheduling (pilot, parked)
/opt/nai-reserve/                   — GPU reservation system (pilot, parked)
/opt/nai-agent/                     — Rust executor (dev-only, phase 5 in-progress)
/opt/nai-control-center/            — Next.js dashboard (production-internal)
/opt/hydra-project/docs/            — Cross-project reference docs (routing-protocol-v1.md, port-registry.md, blocker-registry.yaml)
```

### Deliverables Location
```
/opt/claude-swarm/plans/
  ├── 2026-04-19-claude-swarm-peripherals-DoD.md      (this plan)
  ├── 2026-04-22-S1-gap-audit.md                       (S1 deliverable)
  ├── 2026-04-28-S2-tracer-bullet-completion.md        (S2 deliverable)
  ├── 2026-05-06-S3-observability-readiness.md         (S3 deliverable)
  ├── 2026-05-13-S4-enterprise-gates-closure.md        (S4 deliverable)
  └── 2026-05-20-S5-production-handoff.md              (S5 final, josh sign-off)
```

### Grafana Dashboards (S3 Output)
```
/opt/hydra-sentinel/grafana/        — Shared dashboard store
  ├── routing-protocol-v1.json       (claude-swarm routing v1 metrics)
  ├── nai-swarm-overview.json        (nai-swarm GPU + task scheduling)
  ├── nai-reserve-scheduling.json    (nai-reserve queue + quotas)
  ├── nai-agent-routing.json         (nai-agent requests + latency)
  └── cross-project-health.json      (aggregated health + incident drill-down)
```

### Runbooks (S5 Output)
```
/opt/claude-swarm/docs/
  ├── RUNBOOK.md                     (credential rotation, DLQ recovery, NFS failover)
  └── HANDOFF.md                     (who to call, arch mental model, critical files, daily ops)

/opt/nai-swarm/docs/
  ├── ops-guide.md                   (existing, to update with S3 backup/restore)
  ├── HANDOFF.md                     (S5 new, team quota snapshot, multi-cluster TODO)
  └── disaster-recovery.md           (S5 new, PostgreSQL WAL strategy)

/opt/nai-reserve/docs/
  ├── ops-runbook.md                 (existing)
  ├── RESTORE.md                     (existing, 2026-04-18)
  ├── HANDOFF.md                     (S5 new)
  └── incident-response.md           (S5 new, DB pool exhaustion, Idempotency-Key replay)

/opt/nai-agent/docs/
  ├── DEPLOYMENT.md                  (S2 new, K3s + systemd)
  ├── ops-runbook.md                 (S2 new, basic ops)
  ├── HANDOFF.md                     (S5 new, on-call escalation, Prism polling issues)
  └── disaster-recovery.md           (S5 new, Redis recovery, PostgreSQL WAL)

/opt/nai-control-center/
  └── HANDOFF.md                     (S5 new, module escalation matrix, service health interpretation)
```

---

## Revision History

| Date | Status | Author | Notes |
|------|--------|--------|-------|
| 2026-04-19 | DRAFT | Claude Agent | Initial plan, submitted for Josh review |
| (pending) | IN REVIEW | Josh | TBD |

---

**Next Action**: Save this plan. Josh reviews and approves. S1 kicks off 2026-04-19.
