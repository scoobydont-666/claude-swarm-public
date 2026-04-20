"""F3: TypedDict response schemas for dashboard /api/* endpoints.

Covers <hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase F3.

Purpose: document the response shape each endpoint returns so callers
(dashboard JS, external monitoring scripts, NAI-suite integrations) have
a single authoritative reference. TypedDict is zero-runtime-cost — pure
static typing. Callers that want runtime validation can use these as
the source of truth for their own pydantic models.

This module complements src/cb_schema.py (CB→Swarm contract) and
src/events_schema.py (Swarm→Sentinel events) — together they cover the
three external contracts the swarm exposes.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------


class NodeStatus(TypedDict, total=False):
    """One node's current heartbeat + state (all fields optional except id)."""

    id: str
    hostname: str
    state: Literal["active", "idle", "offline", "busy", "unknown"]
    updated_at: str  # ISO 8601
    _color: str  # UI-only
    _dot: str  # UI-only
    _heartbeat_age: str  # humanized


class StatusResponse(TypedDict):
    """GET /api/status — fleet roll-up + backend degradation flag (E5)."""

    nodes: list[NodeStatus]
    backend: Literal["redis", "nfs", "unknown"]
    degraded: bool
    degradation_reason: str | None


# ---------------------------------------------------------------------------
# /api/tasks
# ---------------------------------------------------------------------------


class TaskEntry(TypedDict, total=False):
    """One task in the queue."""

    id: str
    title: str
    state: Literal["pending", "claimed", "running", "completed", "failed"]
    priority: int
    project: str
    created_by: str
    created_at: float
    claimed_by: str
    claimed_at: float
    _age: str  # humanized


class TasksResponse(TypedDict):
    """GET /api/tasks — queue roll-up per lifecycle stage."""

    pending: list[TaskEntry]
    claimed: list[TaskEntry]
    completed: list[TaskEntry]
    counts: dict[str, int]


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


class HealthResponse(TypedDict):
    """GET /health — liveness probe response (k8s-compatible)."""

    status: Literal["ok", "degraded"]
    degradation_reason: str | None
    checks: dict[str, bool]


# ---------------------------------------------------------------------------
# /live, /ready (E6)
# ---------------------------------------------------------------------------


class LivenessResponse(TypedDict):
    status: Literal["alive"]
    probe: Literal["liveness"]


class ReadinessResponse(TypedDict, total=False):
    status: Literal["ready", "not_ready"]
    probe: Literal["readiness"]
    checks: dict[str, bool]
    not_ready_reasons: list[str]


# ---------------------------------------------------------------------------
# /api/dispatches
# ---------------------------------------------------------------------------


class DispatchEntry(TypedDict, total=False):
    session_id: str
    host: str
    status: str
    created_at: str
    duration_s: float


class DispatchesResponse(TypedDict):
    """GET /api/dispatches — recent dispatch records."""

    entries: list[DispatchEntry]
    count: int


# ---------------------------------------------------------------------------
# /api/metrics
# ---------------------------------------------------------------------------


class MetricsResponse(TypedDict):
    """GET /api/metrics — Prometheus-style metrics in structured form.

    Note: the Prometheus scrape endpoint (text exposition) is typically
    served separately; /api/metrics returns a JSON snapshot for the
    dashboard UI.
    """

    metrics: dict[str, Any]
    timestamp: str


# ---------------------------------------------------------------------------
# /api/events
# ---------------------------------------------------------------------------


class EventEntry(TypedDict, total=False):
    timestamp: str
    rule: str
    host: str
    severity: Literal["info", "warning", "critical"]
    details: dict[str, Any]


class EventsResponse(TypedDict):
    """GET /api/events — recent health events from SQLite log."""

    events: list[EventEntry]
    count: int


# ---------------------------------------------------------------------------
# Error response (consistent across 4xx/5xx)
# ---------------------------------------------------------------------------


class ErrorResponse(TypedDict, total=False):
    error: str
    message: str
    existing_reservation_id: str  # used by nai-reserve idempotency replays
    existing_status: str
