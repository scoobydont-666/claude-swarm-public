"""Unified task queue with capability matching and lifecycle management.

Provides a single interface over Redis (primary) and NFS/filesystem (fallback).
Tasks flow through: pending → claimed → running → completed/failed.
Stale claimed tasks are auto-requeued after TTL expiry.

Part of Swarm v2 Stage 2 (s2-queue + s2-lifecycle).
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

# Priority tiers: named tiers (0-5) inspired by NAI Swarm fair-share scheduling
# Lower number = higher priority
PRIORITY_TIERS = {
    "production": 0,  # Tier 0: production inference / critical path
    "cicd": 1,  # Tier 1: CI/CD pipelines
    "lead": 2,  # Tier 2: team lead / high-priority manual
    "standard": 3,  # Tier 3: standard work (default)
    "batch": 4,  # Tier 4: batch jobs, background work
    "sandbox": 5,  # Tier 5: dev/sandbox, lowest priority
}

# Legacy priority mapping (backward compatible)
PRIORITY_MAP = {
    "critical": 0,
    "high": 1,
    "medium": 3,
    "low": 4,
    # Also accept tier names
    **PRIORITY_TIERS,
}

CLAIM_TTL_SECONDS = 600  # 10 minutes — requeue if uncompleted

# Preemption: tasks with priority <= this can preempt tasks with priority >= PREEMPT_TARGET_MIN
PREEMPT_SOURCE_MAX = 2  # P0-P2 can preempt
PREEMPT_TARGET_MIN = 4  # P4-P5 can be preempted
PREEMPT_GAP = 2  # Must be at least 2 tiers apart


@dataclass
class Task:
    """A work item in the queue."""

    id: str
    title: str
    description: str = ""
    project: str = ""
    priority: int = 3  # standard tier
    requires: list[str] = field(default_factory=list)
    state: str = "pending"  # pending, claimed, running, completed, failed
    created_by: str = ""
    created_at: float = 0.0
    claimed_by: str = ""
    claimed_at: float = 0.0
    completed_at: float = 0.0
    result: str = ""
    error: str = ""
    estimated_minutes: int = 0

    def to_dict(self) -> dict:
        """Convert Task to dict representation.

        Returns:
            Dict with all task fields.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict, *, strict: bool = False) -> "Task":
        """Reconstruct Task from dict (e.g., from YAML/Redis).

        Args:
            data: Dict with task fields. Handles string requires list.
            strict: If True, raise ValueError on (a) non-dict input,
                (b) missing required fields (id or title), (c) invalid
                state. If False (default), silently default as before —
                preserves backward-compat for legacy files that omit
                optional fields.

        Returns:
            Task instance.

        Raises:
            ValueError: in strict mode on malformed input.
        """
        # E3: strict-mode input validation. Non-dict => reject.
        if not isinstance(data, dict):
            msg = f"Task.from_dict expected dict, got {type(data).__name__}"
            if strict:
                raise ValueError(msg)

        data = data if isinstance(data, dict) else {}

        # E3 strict: required fields = id + title. In lax mode these get
        # defaulted (uuid4 + empty string) for backward compat.
        if strict:
            if not data.get("id"):
                raise ValueError("Task requires 'id' field (strict mode)")
            if not data.get("title"):
                raise ValueError("Task requires 'title' field (strict mode)")

        # E3 strict: state must be in the known set.
        _VALID_STATES = {"pending", "claimed", "running", "completed", "failed"}
        state = data.get("state", "pending")
        if strict and state not in _VALID_STATES:
            raise ValueError(
                f"Task state '{state}' not in allowed set {sorted(_VALID_STATES)}"
            )

        # Handle requires as string (from YAML/Redis)
        requires = data.get("requires", [])
        if isinstance(requires, str):
            requires = [r.strip() for r in requires.split(",") if r.strip()] if requires else []
        return cls(
            id=data.get("id", str(uuid4())),
            title=data.get("title", ""),
            description=data.get("description", ""),
            project=data.get("project", ""),
            priority=_normalize_priority(data.get("priority", 3)),
            requires=requires,
            state=state,
            created_by=data.get("created_by", ""),
            created_at=float(data.get("created_at", 0)),
            claimed_by=data.get("claimed_by", ""),
            claimed_at=float(data.get("claimed_at", 0)),
            completed_at=float(data.get("completed_at", 0)),
            result=data.get("result", ""),
            error=data.get("error", ""),
            estimated_minutes=int(data.get("estimated_minutes", 0)),
        )


def _normalize_priority(p: Any) -> int:
    """Convert string or numeric priority to int (0=highest, 5=lowest).

    Accepts:
    - int: clamped to 0-5
    - str: looked up in PRIORITY_MAP (tier names + legacy names)
    - "P0"-"P5": parsed directly
    """
    if isinstance(p, int):
        return max(0, min(5, p))
    if isinstance(p, str):
        s = p.lower().strip()
        # Handle "P0"-"P5" format
        if s.startswith("p") and len(s) == 2 and s[1].isdigit():
            return max(0, min(5, int(s[1])))
        return PRIORITY_MAP.get(s, 3)
    return 3


class TaskQueue:
    """Unified priority queue with capability matching.

    Backend priority: SQLite (if configured) → Redis → filesystem (NFS).
    """

    def __init__(
        self,
        use_redis: bool = True,
        tasks_dir: str = "/opt/swarm/tasks",
        use_sqlite: bool = False,
        sqlite_path: str = "",
    ) -> None:
        """Initialize TaskQueue with backend selection.

        Args:
            use_redis: Whether to prefer Redis (default True).
            tasks_dir: Filesystem fallback directory.
            use_sqlite: Whether to use SQLite backend (opt-in).
            sqlite_path: Path to SQLite database file.
        """
        self._use_redis = use_redis
        self._tasks_dir = tasks_dir
        self._redis_available = False
        self._sqlite_backend = None

        # SQLite backend (opt-in, highest priority if enabled)
        if use_sqlite:
            try:
                from sqlite_backend import SQLiteTaskBackend

                db_path = sqlite_path or os.path.join(tasks_dir, "tasks.db")
                self._sqlite_backend = SQLiteTaskBackend(db_path)
                log.info("TaskQueue: SQLite backend active at %s", db_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("TaskQueue: SQLite init failed, falling back: %s", exc)
                self._sqlite_backend = None

        if not self._sqlite_backend and use_redis:
            try:
                from redis_client import get_client

                get_client().ping()
                self._redis_available = True
                log.info("TaskQueue: Redis backend active")
            except Exception as exc:  # noqa: BLE001
                log.debug("Suppressed: %s", exc)
                log.warning("TaskQueue: Redis unavailable, using filesystem fallback")
                self._redis_available = False

    @property
    def backend(self) -> str:
        """Return name of active backend ('sqlite', 'redis', or 'filesystem')."""
        if self._sqlite_backend:
            return "sqlite"
        return "redis" if self._redis_available else "filesystem"

    # -----------------------------------------------------------------------
    # Create
    # -----------------------------------------------------------------------

    def create(
        self,
        title: str,
        description: str = "",
        project: str = "",
        priority: int | str = 5,
        requires: list[str] | None = None,
        estimated_minutes: int = 0,
        created_by: str = "",
    ) -> Task:
        """Create a new pending task."""
        norm_priority = _normalize_priority(priority)

        if self._sqlite_backend:
            data = self._sqlite_backend.create(
                title=title,
                description=description,
                project=project,
                priority=norm_priority,
                requires=requires,
                estimated_minutes=estimated_minutes,
                created_by=created_by or os.uname().nodename,
            )
            return Task.from_dict(data)

        task = Task(
            id=f"task-{uuid4().hex[:12]}",
            title=title,
            description=description,
            project=project,
            priority=norm_priority,
            requires=requires or [],
            state="pending",
            created_by=created_by or os.uname().nodename,
            created_at=time.time(),
            estimated_minutes=estimated_minutes,
        )

        if self._redis_available:
            self._redis_create(task)
        else:
            self._fs_create(task)

        log.info(
            "Task created: %s (priority=%d, requires=%s)",
            task.id,
            task.priority,
            task.requires,
        )
        return task

    # -----------------------------------------------------------------------
    # Claim
    # -----------------------------------------------------------------------

    def claim(self, claimer: str, task_id: str | None = None) -> Task | None:
        """Claim a specific task or the highest-priority pending task."""
        if self._sqlite_backend:
            data = self._sqlite_backend.claim(claimer, task_id)
            return Task.from_dict(data) if data else None

        if task_id:
            return self._claim_specific(task_id, claimer)

        if self._redis_available:
            return self._redis_claim(claimer)
        return self._fs_claim(claimer)

    def claim_matching(
        self,
        capabilities: dict[str, bool] | list[str],
        claimer: str,
    ) -> Task | None:
        """Claim the highest-priority task matching the given capabilities.

        Args:
            capabilities: Either a dict {"gpu": True, "docker": True} or a list ["gpu", "docker"].
            claimer: Agent identifier (hostname:pid).

        Returns:
            Claimed Task or None if no matching tasks.
        """
        # Normalize to set of capability names
        if isinstance(capabilities, dict):
            cap_set = {k for k, v in capabilities.items() if v}
        else:
            cap_set = set(capabilities)

        pending = self.list_pending()
        # Sort by priority (lowest number = highest priority)
        pending.sort(key=lambda t: t.priority)

        for task in pending:
            if not task.requires or all(r in cap_set for r in task.requires):
                claimed = self._claim_specific(task.id, claimer)
                if claimed:
                    return claimed
        return None

    # -----------------------------------------------------------------------
    # State transitions
    # -----------------------------------------------------------------------

    def start(self, task_id: str) -> Task | None:
        """Transition a claimed task to running."""
        task = self.get(task_id)
        if not task or task.state != "claimed":
            return None
        task.state = "running"
        self._save(task)
        log.info("Task running: %s", task_id)
        return task

    def complete(self, task_id: str, result: str = "") -> Task | None:
        """Mark a task as completed."""
        task = self.get(task_id)
        if not task or task.state not in ("claimed", "running"):
            return None
        task.state = "completed"
        task.completed_at = time.time()
        task.result = result
        self._save(task)
        log.info("Task completed: %s", task_id)
        return task

    def fail(self, task_id: str, error: str = "") -> Task | None:
        """Mark a task as failed."""
        task = self.get(task_id)
        if not task or task.state not in ("claimed", "running"):
            return None
        task.state = "failed"
        task.completed_at = time.time()
        task.error = error
        self._save(task)
        log.info("Task failed: %s — %s", task_id, error)
        return task

    def requeue(self, task_id: str) -> Task | None:
        """Return a claimed/running/failed task to pending."""
        task = self.get(task_id)
        if not task or task.state == "completed":
            return None
        task.state = "pending"
        task.claimed_by = ""
        task.claimed_at = 0.0
        task.error = ""
        self._save(task)
        log.info("Task requeued: %s", task_id)
        return task

    # -----------------------------------------------------------------------
    # Query
    # -----------------------------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        if self._sqlite_backend:
            data = self._sqlite_backend.get(task_id)
            return Task.from_dict(data) if data else None
        if self._redis_available:
            return self._redis_get(task_id)
        return self._fs_get(task_id)

    def list_pending(self, limit: int = 50) -> list[Task]:
        """List pending tasks sorted by priority."""
        return self._list_by_state("pending", limit)

    def list_claimed(self, limit: int = 50) -> list[Task]:
        """List claimed tasks (in progress).

        Args:
            limit: Max tasks to return.

        Returns:
            List of Task objects in claimed state.
        """
        return self._list_by_state("claimed", limit)

    def list_all(self, limit: int = 100) -> list[Task]:
        """List all tasks across all states."""
        tasks = []
        for state in ("pending", "claimed", "running", "completed", "failed"):
            tasks.extend(self._list_by_state(state, limit))
        return tasks[:limit]

    def list_matching(self, capabilities: dict[str, bool] | list[str]) -> list[Task]:
        """List pending tasks matching capabilities (without claiming)."""
        if isinstance(capabilities, dict):
            cap_set = {k for k, v in capabilities.items() if v}
        else:
            cap_set = set(capabilities)

        return [
            t
            for t in self.list_pending()
            if not t.requires or all(r in cap_set for r in t.requires)
        ]

    # -----------------------------------------------------------------------
    # Priority tier helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def tier_name(priority: int) -> str:
        """Convert numeric priority to tier name.

        Args:
            priority: Numeric priority (0-5).

        Returns:
            Tier name string (e.g., "production", "standard").
        """
        reverse_map = {v: k for k, v in PRIORITY_TIERS.items()}
        return reverse_map.get(priority, f"tier-{priority}")

    def find_preemptable(self, new_priority: int) -> list[Task]:
        """Find claimed tasks that can be preempted by a new higher-priority task.

        Rules:
        - New task must be P0-P2 (PREEMPT_SOURCE_MAX)
        - Target tasks must be P4-P5 (PREEMPT_TARGET_MIN)
        - Gap must be >= PREEMPT_GAP tiers

        Args:
            new_priority: Priority of the incoming task.

        Returns:
            List of Tasks eligible for preemption.
        """
        if new_priority > PREEMPT_SOURCE_MAX:
            return []

        claimed = self.list_claimed()
        preemptable = []
        for task in claimed:
            if (
                task.priority >= PREEMPT_TARGET_MIN
                and (task.priority - new_priority) >= PREEMPT_GAP
            ):
                preemptable.append(task)
        return preemptable

    # -----------------------------------------------------------------------
    # Lifecycle: auto-requeue stale claims
    # -----------------------------------------------------------------------

    def requeue_stale(self, ttl: int = CLAIM_TTL_SECONDS) -> list[str]:
        """Find and requeue tasks that have been claimed longer than TTL."""
        if self._sqlite_backend:
            return self._sqlite_backend.requeue_stale(ttl)
        now = time.time()
        requeued = []
        for task in self.list_claimed():
            if task.claimed_at > 0 and (now - task.claimed_at) > ttl:
                self.requeue(task.id)
                requeued.append(task.id)
                log.warning(
                    "Auto-requeued stale task: %s (claimed %ds ago by %s)",
                    task.id,
                    int(now - task.claimed_at),
                    task.claimed_by,
                )
        return requeued

    # -----------------------------------------------------------------------
    # Redis backend
    # -----------------------------------------------------------------------

    def _redis_create(self, task: Task) -> None:
        """Create task in Redis sorted set.

        Args:
            task: Task to create.
        """
        from redis_client import get_client

        r = get_client()
        score = task.priority * 1000 + int(task.created_at)
        pipe = r.pipeline()
        pipe.hset(
            f"task:{task.id}",
            mapping={
                k: json.dumps(v) if isinstance(v, (list, dict)) else str(v)
                for k, v in task.to_dict().items()
            },
        )
        pipe.zadd("tasks:pending", {task.id: score})
        pipe.execute()

    def _redis_claim(self, claimer: str) -> Task | None:
        """Claim next available task from Redis.

        Args:
            claimer: ID of claiming agent.

        Returns:
            Claimed Task or None if none available.
        """
        from redis_client import claim_task

        task_id = claim_task(claimer)
        if not task_id:
            return None
        task = self._redis_get(task_id)
        if task:
            task.state = "claimed"
            task.claimed_by = claimer
            task.claimed_at = time.time()
            self._redis_save(task)
        return task

    def _redis_get(self, task_id: str) -> Task | None:
        """Fetch task from Redis by ID.

        Args:
            task_id: Task ID.

        Returns:
            Task or None if not found.
        """
        from redis_client import get_task

        data = get_task(task_id)
        if not data:
            return None
        # Parse JSON fields
        for key in ("requires",):
            if key in data and isinstance(data[key], str):
                try:
                    data[key] = json.loads(data[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return Task.from_dict(data)

    def _redis_save(self, task: Task) -> None:
        """Save task to Redis.

        Args:
            task: Task to save.
        """
        from redis_client import get_client

        r = get_client()
        r.hset(
            f"task:{task.id}",
            mapping={
                k: json.dumps(v) if isinstance(v, (list, dict)) else str(v)
                for k, v in task.to_dict().items()
            },
        )

    # -----------------------------------------------------------------------
    # Filesystem backend (NFS fallback)
    # -----------------------------------------------------------------------

    def _fs_create(self, task: Task) -> None:
        """Create task YAML file on NFS.

        Args:
            task: Task to create.
        """
        import yaml

        path = os.path.join(self._tasks_dir, "pending", f"{task.id}.yaml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(task.to_dict(), f, default_flow_style=False)

    def _fs_claim(self, claimer: str) -> Task | None:
        """Claim next available task from NFS pending directory.

        Args:
            claimer: ID of claiming agent.

        Returns:
            Claimed Task or None if none available.
        """
        pending = self._fs_list("pending")
        pending.sort(key=lambda t: t.priority)
        if not pending:
            return None
        task = pending[0]
        return self._claim_specific(task.id, claimer)

    def _fs_get(self, task_id: str) -> Task | None:
        """Fetch task from NFS by ID, searching all state directories.

        Args:
            task_id: Task ID.

        Returns:
            Task or None if not found.
        """
        import yaml

        for state_dir in ("pending", "claimed", "completed", "failed", "running"):
            path = os.path.join(self._tasks_dir, state_dir, f"{task_id}.yaml")
            if os.path.exists(path):
                with open(path) as f:
                    data = yaml.safe_load(f) or {}
                data["state"] = state_dir
                return Task.from_dict(data)
        return None

    def _fs_list(self, state: str, limit: int = 50) -> list[Task]:
        """List tasks in a given state directory on NFS.

        Args:
            state: State directory name (pending, claimed, etc.).
            limit: Max tasks to return.

        Returns:
            List of Task objects.
        """
        import yaml

        state_dir = os.path.join(self._tasks_dir, state)
        if not os.path.isdir(state_dir):
            return []
        tasks = []
        for fname in sorted(os.listdir(state_dir))[:limit]:
            if not fname.endswith(".yaml"):
                continue
            path = os.path.join(state_dir, fname)
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            data["state"] = state
            tasks.append(Task.from_dict(data))
        return tasks

    def _fs_save(self, task: Task) -> None:
        """Save task to NFS, moving between state directories as needed.

        Args:
            task: Task to save.
        """
        import yaml

        # Remove from old state dir, write to new
        for state_dir in ("pending", "claimed", "completed", "failed", "running"):
            old_path = os.path.join(self._tasks_dir, state_dir, f"{task.id}.yaml")
            if os.path.exists(old_path) and state_dir != task.state:
                os.remove(old_path)
        new_dir = os.path.join(self._tasks_dir, task.state)
        os.makedirs(new_dir, exist_ok=True)
        new_path = os.path.join(new_dir, f"{task.id}.yaml")
        with open(new_path, "w") as f:
            yaml.safe_dump(task.to_dict(), f, default_flow_style=False)

    # -----------------------------------------------------------------------
    # Shared helpers
    # -----------------------------------------------------------------------

    def _claim_specific(self, task_id: str, claimer: str) -> Task | None:
        """Claim a specific task by ID.

        Args:
            task_id: Task ID to claim.
            claimer: ID of claiming agent.

        Returns:
            Claimed Task or None if not found/not pending.
        """
        task = self.get(task_id)
        if not task or task.state != "pending":
            return None
        task.state = "claimed"
        task.claimed_by = claimer
        task.claimed_at = time.time()
        self._save(task)
        return task

    def _save(self, task: Task) -> None:
        """Save task to backend (SQLite, Redis, or FS) and update sorted sets.

        Args:
            task: Task to save.
        """
        if self._sqlite_backend:
            # SQLite transitions are handled directly via the backend methods
            # This is a fallback for generic saves
            if task.state == "completed":
                self._sqlite_backend.complete(task.id, result=task.result)
            elif task.state == "failed":
                self._sqlite_backend.fail(task.id, error=task.error)
            elif task.state == "pending":
                self._sqlite_backend.requeue(task.id)
            elif task.state == "running":
                self._sqlite_backend.start(task.id)
            return
        if self._redis_available:
            self._redis_save(task)
            # Also update sorted sets
            from redis_client import get_client

            r = get_client()
            for state in ("pending", "claimed", "completed"):
                r.zrem(f"tasks:{state}", task.id)
            if task.state in ("pending", "claimed", "completed"):
                score = task.priority * 1000 + int(task.created_at)
                r.zadd(f"tasks:{task.state}", {task.id: score})
        else:
            self._fs_save(task)

    def _list_by_state(self, state: str, limit: int = 50) -> list[Task]:
        """List tasks in a specific state via active backend.

        Args:
            state: State name (pending, claimed, etc.).
            limit: Max tasks to return.

        Returns:
            List of Task objects sorted by priority.
        """
        if self._sqlite_backend:
            rows = self._sqlite_backend.list_by_state(state, limit)
            return [Task.from_dict(r) for r in rows]
        if self._redis_available:
            from redis_client import list_tasks

            raw = list_tasks(state, limit)
            tasks = []
            for data in raw:
                for key in ("requires",):
                    if key in data and isinstance(data[key], str):
                        try:
                            data[key] = json.loads(data[key])
                        except (json.JSONDecodeError, TypeError):
                            pass
                tasks.append(Task.from_dict(data))
            return tasks
        return self._fs_list(state, limit)
