"""Claude Swarm TaskQueue adapter for Swarm Spec conformance tests.

Maps the internal Task/TaskQueue to the TaskQueueBackend protocol from swarm_spec.protocols.
"""

import logging
from typing import Optional
from uuid import UUID

log = logging.getLogger(__name__)


class ClaudeSwarmTaskQueueAdapter(TaskQueueBackend):
    """Adapts claude-swarm's TaskQueue to the Swarm Spec TaskQueueBackend protocol.

    Handles impedance mismatch:
    - spec uses TaskDescriptor (UUID task_id, dict payload, list[str] capabilities)
    - claude-swarm uses Task (str id, str title, requires list[str])

    Storage: in-memory dict during conformance tests (no Redis/SQLite).
    """

    def __init__(self):
        """Initialize with in-memory task store for testing."""
        self._tasks = {}  # task_id (UUID) -> (TaskDescriptor, state_str)
        self._dequeued = {}  # task_id -> worker_id (for ack/nack tracking)
        self._discarded = set()  # task_id set for nacked tasks with retry=False
        log.debug("ClaudeSwarmTaskQueueAdapter initialized (in-memory)")

    def enqueue(self, task: TaskDescriptor) -> UUID:
        """Enqueue a task. Enforces idempotency_key deduplication."""
        # Deduplication: if idempotency_key already exists, return existing task_id
        if task.idempotency_key:
            for tid, (stored_task, _) in self._tasks.items():
                if (
                    stored_task.idempotency_key == task.idempotency_key
                    and tid not in self._discarded
                ):
                    log.debug(
                        "Task with idempotency_key=%s already exists, skipping",
                        task.idempotency_key,
                    )
                    return tid

        # Store task
        self._tasks[task.task_id] = (task, "pending")
        log.debug("Enqueued task %s (priority=%d)", task.task_id, task.priority)
        return task.task_id

    def dequeue(
        self, worker_id: str, capabilities: list[str], timeout_s: float = 30.0
    ) -> Optional[TaskDescriptor]:
        """Dequeue a task matching capabilities, respecting priority order.

        For testing, we ignore timeout_s (no actual blocking).
        Returns highest-priority task matching any of the requested capabilities.
        """
        # Filter: tasks in pending state, not discarded, matching at least one capability
        candidates = []
        for tid, (task, state) in self._tasks.items():
            if state == "pending" and tid not in self._discarded:
                # Check if task requires any of the worker's capabilities
                if any(cap in task.capabilities for cap in capabilities):
                    candidates.append((task.priority, tid, task))

        if not candidates:
            log.debug("No tasks available for worker=%s capabilities=%s", worker_id, capabilities)
            return None

        # Sort by priority (lower = higher priority), then stable order
        candidates.sort(key=lambda x: (x[0], str(x[1])))
        priority, task_id, task = candidates[0]

        # Mark as dequeued (move to "claimed" state)
        self._tasks[task_id] = (task, "claimed")
        self._dequeued[task_id] = worker_id
        log.debug("Dequeued task %s for worker %s", task_id, worker_id)
        return task

    def ack(self, task_id: UUID) -> None:
        """Acknowledge task completion. Remove from queue."""
        if task_id in self._tasks:
            task, _ = self._tasks[task_id]
            self._tasks[task_id] = (task, "completed")
            self._dequeued.pop(task_id, None)
            log.debug("Acked task %s", task_id)
        else:
            log.warning("Ack on unknown task %s", task_id)

    def nack(self, task_id: UUID, reason: str, retry: bool = True) -> None:
        """Negative acknowledge. Re-queue if retry=True, discard if retry=False."""
        if task_id not in self._tasks:
            log.warning("Nack on unknown task %s", task_id)
            return

        task, _ = self._tasks[task_id]
        if retry:
            # Re-queue: move back to pending
            self._tasks[task_id] = (task, "pending")
            self._dequeued.pop(task_id, None)
            log.debug("Nacked task %s with retry=True, re-queued", task_id)
        else:
            # Discard: mark as failed and remove from circulation
            self._tasks[task_id] = (task, "failed")
            self._discarded.add(task_id)
            self._dequeued.pop(task_id, None)
            log.debug("Nacked task %s with retry=False, discarded. Reason: %s", task_id, reason)

    def peek(self, filter_capabilities: list[str] = [], limit: int = 10) -> list[TaskDescriptor]:
        """Peek at pending tasks without dequeuing. Filter by capabilities if provided."""
        candidates = []
        for tid, (task, state) in self._tasks.items():
            if state == "pending" and tid not in self._discarded:
                # If filter_capabilities provided, check match
                if filter_capabilities:
                    if any(cap in task.capabilities for cap in filter_capabilities):
                        candidates.append((task.priority, tid, task))
                else:
                    # No filter: include all pending
                    candidates.append((task.priority, tid, task))

        # Sort by priority
        candidates.sort(key=lambda x: (x[0], str(x[1])))
        result = [task for _, _, task in candidates[:limit]]
        log.debug("Peeked %d tasks (limit=%d)", len(result), limit)
        return result
