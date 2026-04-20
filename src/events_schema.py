"""Swarm→Sentinel event schema — B2 contract typing.

Closes the "no shared types" finding from the 2026-04-18 audit. Previously:
- claude-swarm ``emit(event_type, project, details)`` accepted any dict as
  ``details``; no field-name validation.
- hydra-sentinel ``routing_panels.py`` consumed the JSONL stream with no
  schema check — silent silent drop if a field was renamed.

This module defines every event type as a frozen dataclass. ``validate()``
returns the canonical details dict for emission (rejects unknown fields
in strict mode, logs WARN in lax mode). Sentinel imports and reuses the
same types via vendored copy (see /opt/hydra-sentinel/src/hydra_sentinel/
events_schema.py — kept in sync manually until contract CI arrives in B4).

Plan: <hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase B2
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

# Bump when event detail shapes change.
# - Major: field shape change (breaking)
# - Minor: additive field (consumers tolerate unknown additive fields)
EVENT_SCHEMA_VERSION = "1.0.0"

# Registry: event_type → dataclass. Populated by @register_event decorator.
_REGISTRY: dict[str, type[BaseEvent]] = {}


def register_event(cls: type[BaseEvent]) -> type[BaseEvent]:
    """Decorator: register an event class by its ``event_type`` ClassVar."""
    et = cls.event_type
    if et in _REGISTRY:
        raise ValueError(f"event_type '{et}' already registered to {_REGISTRY[et].__name__}")
    _REGISTRY[et] = cls
    return cls


@dataclass(frozen=True, slots=True)
class BaseEvent:
    """Base class for all Swarm→Sentinel events.

    Subclass fields become the allowed ``details`` keys. Unknown fields
    in incoming dicts are flagged by ``validate()``.
    """

    event_type: ClassVar[str] = "base"

    def to_details(self) -> dict[str, Any]:
        """Serialize fields (excluding event_type) as a details dict."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


@register_event
@dataclass(frozen=True, slots=True)
class CommitEvent(BaseEvent):
    event_type: ClassVar[str] = "commit"
    commit: str
    message: str
    files_changed: int = 0


@register_event
@dataclass(frozen=True, slots=True)
class TestResultEvent(BaseEvent):
    event_type: ClassVar[str] = "test_result"
    passed: int
    failed: int
    total: int
    all_green: bool = False  # derived by caller as (failed == 0)
    duration_s: float = 0.0

    # Opt out of pytest collection — class name starts with Test* which
    # pytest auto-discovers, but this is a dataclass not a test class.
    __test__: ClassVar[bool] = False


@register_event
@dataclass(frozen=True, slots=True)
class TaskCompletedEvent(BaseEvent):
    event_type: ClassVar[str] = "task_completed"
    task_id: str
    result: str = ""
    duration_s: float = 0.0


@register_event
@dataclass(frozen=True, slots=True)
class TaskClaimedEvent(BaseEvent):
    event_type: ClassVar[str] = "task_claimed"
    task_id: str


@register_event
@dataclass(frozen=True, slots=True)
class TaskFailedEvent(BaseEvent):
    event_type: ClassVar[str] = "task_failed"
    task_id: str
    error: str
    retry_count: int = 0


@register_event
@dataclass(frozen=True, slots=True)
class RateLimitEvent(BaseEvent):
    event_type: ClassVar[str] = "rate_limit"
    profile: str
    limit_type: str
    reset_hint: str


@register_event
@dataclass(frozen=True, slots=True)
class BlockerFoundEvent(BaseEvent):
    event_type: ClassVar[str] = "blocker_found"
    blocker_type: str
    description: str
    severity: str = "medium"


@register_event
@dataclass(frozen=True, slots=True)
class SessionStartEvent(BaseEvent):
    event_type: ClassVar[str] = "session_start"
    session_id: str = ""
    model: str = ""


@register_event
@dataclass(frozen=True, slots=True)
class SessionEndEvent(BaseEvent):
    event_type: ClassVar[str] = "session_end"
    session_id: str = ""
    items_completed: int = 0
    duration_s: float = 0.0


@register_event
@dataclass(frozen=True, slots=True)
class ContextHandoffEvent(BaseEvent):
    event_type: ClassVar[str] = "context_handoff"
    handoff_path: str
    session_id: str = ""


@register_event
@dataclass(frozen=True, slots=True)
class ConfigSyncEvent(BaseEvent):
    event_type: ClassVar[str] = "config_sync"
    host: str
    result: str  # "pushed" | "drift_detected" | "error"


def validate(
    event_type: str,
    details: dict[str, Any] | None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Validate a details dict against the registered event class.

    Args:
        event_type: Registered event type string.
        details: Detail payload (may be None for events without fields).
        strict: If True, raise ValueError on unknown event_type, missing
            required fields, or unknown extra fields. If False (default),
            log WARN and pass through details unchanged — backward-compat.

    Returns:
        A details dict with only fields declared on the registered class,
        augmented with default values for optional fields. In lax mode and
        on unregistered event_type, returns the input unchanged.
    """
    details = dict(details or {})

    cls = _REGISTRY.get(event_type)
    if cls is None:
        msg = f"unregistered event_type '{event_type}' — valid: {sorted(_REGISTRY)}"
        if strict:
            raise ValueError(msg)
        logger.warning(msg)
        return details

    # Extract declared field names (via dataclass fields)
    declared = {f.name for f in fields(cls)}
    unknown = set(details) - declared
    if unknown:
        msg = f"event '{event_type}' has unknown fields: {sorted(unknown)} — allowed: {sorted(declared)}"
        if strict:
            raise ValueError(msg)
        logger.warning(msg)
        # Drop unknown fields in lax mode so the output shape is clean
        details = {k: v for k, v in details.items() if k in declared}

    # Check required fields (those without default or default_factory)
    required = {
        f.name
        for f in fields(cls)
        if f.default is field(default=None).default.__class__  # has no literal default
        and f.default_factory is field(default_factory=dict).default_factory.__class__
    }
    # Simpler requirement check: any field whose default is MISSING is required
    from dataclasses import MISSING

    required = {f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING}
    missing = required - set(details)
    if missing:
        msg = f"event '{event_type}' missing required fields: {sorted(missing)}"
        if strict:
            raise ValueError(msg)
        logger.warning(msg)

    return details


def registered_event_types() -> list[str]:
    """Return the list of registered event_type strings (for tests / docs)."""
    return sorted(_REGISTRY)
