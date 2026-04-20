"""Compatibility shim: re-exports from event_schema (consolidated N+2 lib).

This module maintains backward compatibility by re-exporting canonical
event schemas from <hydra-project-path>/libs/event_schema. New code should
import directly from event_schema. Legacy callers (pre-N+2) can continue
importing from this module.

Plan: <hydra-project-path>/plans/ntnx-ai-factory-architecture-2026-04-20.md §7 N+2
"""

from __future__ import annotations

# Standard claude-swarm events still defined here for now (backward compat);
# GPU + task events prefer event_schema canonical definitions.

__all__ = [
    # Re-exports from event_schema
    "TaskCreatedEvent",
    "TaskCompletedEvent",
    "TaskFailedEvent",
    "TaskClaimedEvent",
    "GpuRestartEvent",
    "GpuVramHwmEvent",
    "GpuOomEvent",
    # Local definitions (kept for compat)
    "CommitEvent",
    "TestResultEvent",
    "RateLimitEvent",
    "BlockerFoundEvent",
    "SessionStartEvent",
    "SessionEndEvent",
    "ContextHandoffEvent",
    "ConfigSyncEvent",
]

try:
    # Prefer canonical event_schema
    from event_schema import (
        TaskClaimedEvent,
        TaskCompletedEvent,
        TaskCreatedEvent,
        TaskFailedEvent,
        GpuOomEvent,
        GpuRestartEvent,
        GpuVramHwmEvent,
    )
except ImportError:
    # Fallback if event_schema not installed
    from dataclasses import dataclass, field
    from typing import Any
    from uuid import UUID

    from .events_schema import BaseEvent

    @dataclass(frozen=True, slots=True)
    class TaskCreatedEvent(BaseEvent):
        task_id: UUID = field(default_factory=lambda: UUID("00000000-0000-0000-0000-000000000000"))
        worker_id: str = field(default="")
        payload: dict[str, Any] = field(default_factory=dict)

    @dataclass(frozen=True, slots=True)
    class TaskCompletedEvent(BaseEvent):
        task_id: UUID = field(default_factory=lambda: UUID("00000000-0000-0000-0000-000000000000"))
        result: str = field(default="")
        duration_s: float = field(default=0.0)

    @dataclass(frozen=True, slots=True)
    class TaskFailedEvent(BaseEvent):
        task_id: UUID = field(default_factory=lambda: UUID("00000000-0000-0000-0000-000000000000"))
        error: str = field(default="")
        retry_count: int = field(default=0)

    @dataclass(frozen=True, slots=True)
    class TaskClaimedEvent(BaseEvent):
        task_id: UUID = field(default_factory=lambda: UUID("00000000-0000-0000-0000-000000000000"))
        worker_id: str = field(default="")

    @dataclass(frozen=True, slots=True)
    class GpuRestartEvent(BaseEvent):
        pod_name: str = field(default="")
        namespace: str = field(default="")
        restart_count: int = field(default=0)

    @dataclass(frozen=True, slots=True)
    class GpuVramHwmEvent(BaseEvent):
        gpu_index: int = field(default=-1)
        vram_used_mib: int = field(default=0)
        vram_total_mib: int = field(default=0)
        vram_pct: float = field(default=0.0)

    @dataclass(frozen=True, slots=True)
    class GpuOomEvent(BaseEvent):
        gpu_index: int = field(default=-1)
        pod_name: str = field(default="")
        namespace: str = field(default="")
        vram_needed_mib: int = field(default=0)

# Local event definitions (not in event_schema yet)
from .events_schema import (
    BlockerFoundEvent,
    CommitEvent,
    ConfigSyncEvent,
    ContextHandoffEvent,
    RateLimitEvent,
    SessionEndEvent,
    SessionStartEvent,
    TestResultEvent,
)
