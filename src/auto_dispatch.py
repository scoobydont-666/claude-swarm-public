"""Auto Dispatcher — connects work generation to hydra dispatch.

Scans pending tasks and dispatches eligible ones to the best-matching host.
Model routing is delegated to session-miser/token-miser — no separate approval gates.

Auto-dispatch starts DISABLED. Graduated modes:
  off       — no auto-dispatch (default)
  dry_run   — log what would dispatch, no execution
  haiku_only — auto-dispatch only haiku-tier tasks
  sonnet    — haiku + sonnet tasks
  full      — all tiers, model selection via token-miser

Safety rails:
  - Dead-man switch: pause if no human session in 24 hours
  - Host quarantine: 3 consecutive failures = 1 hour quarantine
  - max_concurrent_dispatches honored

Task priority re-ranking: P0 can preempt P4/P5, marking lower priority tasks as preempted.
"""

import logging
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
try:
    from backend import lib as swarm
except ImportError:
    import swarm_lib as swarm
from hydra_dispatch import _find_best_host, dispatch
from work_generator import WorkGenerator, infer_model

logger = logging.getLogger(__name__)

# Valid auto-dispatch modes in escalating order
DISPATCH_MODES = ("off", "dry_run", "haiku_only", "sonnet", "full")

# Model tiers allowed per mode
MODE_ALLOWED_MODELS = {
    "off": set(),
    "dry_run": set(),  # logs only
    "haiku_only": {"haiku"},
    "sonnet": {"haiku", "sonnet"},
    "full": {"haiku", "sonnet", "opus"},
}


def _tier_of(model: str) -> str:
    """Map a model identifier (tier name or full ID) to its tier label.

    Accepts both short tier names (haiku/sonnet/opus) emitted by the legacy
    infer_model(), and full Anthropic model IDs (claude-haiku-4-5-20251001,
    claude-sonnet-4-6, claude-opus-4-7) emitted by the model_router.
    """
    if not model:
        return ""
    low = model.lower()
    if "haiku" in low:
        return "haiku"
    if "sonnet" in low:
        return "sonnet"
    if "opus" in low:
        return "opus"
    return low  # local models or unknown — return as-is

# Dead-man switch: max seconds since last human session before pausing
DEADMAN_THRESHOLD_SECONDS = 86400  # 24 hours

# Host quarantine: consecutive failures before quarantine
QUARANTINE_FAILURE_THRESHOLD = 3
QUARANTINE_DURATION_SECONDS = 3600  # 1 hour


class AutoDispatcher:
    """Automatically dispatches generated tasks to the best-matching host."""

    def __init__(self, config: dict) -> None:
        """Initialize AutoDispatcher with configuration.

        Args:
            config: Swarm config dict with 'auto_dispatch' and 'swarm_root' keys.
        """
        ad_cfg = config.get("auto_dispatch", {})
        self.mode: str = ad_cfg.get("mode", "off")
        # Backward compat: if 'enabled' is set but 'mode' isn't, derive mode
        if "mode" not in ad_cfg and ad_cfg.get("enabled", False):
            self.mode = "sonnet"
        self.max_concurrent: int = ad_cfg.get("max_concurrent_dispatches", 3)
        self.swarm_root = Path(config.get("swarm_root", "/opt/swarm"))

        # Host failure tracking (in-memory, resets on restart)
        self._host_failures: dict[str, int] = {}
        self._host_quarantine_until: dict[str, float] = {}

    @property
    def auto_dispatch_enabled(self) -> bool:
        """Backward-compatible property."""
        return self.mode not in ("off",)

    # -----------------------------------------------------------------------
    # Mode management
    # -----------------------------------------------------------------------

    def is_model_allowed(self, model: str) -> bool:
        """Check if a model is allowed under the current mode.

        Accepts both tier names and full model IDs via _tier_of().
        """
        return _tier_of(model) in MODE_ALLOWED_MODELS.get(self.mode, set())

    def set_mode(self, mode: str, config_path: Path) -> None:
        """Persist auto_dispatch.mode to swarm.yaml."""
        if mode not in DISPATCH_MODES:
            raise ValueError(f"Invalid mode '{mode}'. Valid: {DISPATCH_MODES}")
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            cfg = {}

        cfg.setdefault("auto_dispatch", {})["mode"] = mode
        # Also update 'enabled' for backward compat
        cfg["auto_dispatch"]["enabled"] = mode != "off"

        tmp = config_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        import os

        os.rename(tmp, config_path)
        self.mode = mode
        logger.info("Auto-dispatch mode set to: %s", mode)

    # -----------------------------------------------------------------------
    # Safety rails
    # -----------------------------------------------------------------------

    def _check_deadman_switch(self) -> bool:
        """Return True if a human session was active within the threshold.

        Returns True (safe to proceed) if:
        - No status directory exists (test/dev environment)
        - No status files exist (fresh swarm, no nodes registered)
        - Any node has heartbeat within threshold
        """
        status_dir = self.swarm_root / "status"
        if not status_dir.is_dir():
            return True

        status_files = list(status_dir.glob("*.json"))
        if not status_files:
            return True  # No nodes registered = fresh/test environment

        now = time.time()
        for status_file in status_dir.glob("*.json"):
            try:
                import json

                with open(status_file) as f:
                    status = json.load(f)
                updated = status.get("updated_at", "")
                if not updated:
                    continue
                from datetime import datetime

                dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age = now - dt.timestamp()
                if age < DEADMAN_THRESHOLD_SECONDS:
                    return True
            except (OSError, ValueError, KeyError):
                continue
        return False

    def _is_host_quarantined(self, host: str) -> bool:
        """Check if a host is in quarantine."""
        until = self._host_quarantine_until.get(host, 0)
        if time.time() < until:
            return True
        # Quarantine expired, reset
        if host in self._host_quarantine_until:
            del self._host_quarantine_until[host]
            self._host_failures.pop(host, None)
        return False

    def _record_host_failure(self, host: str) -> None:
        """Record a dispatch failure for a host. Quarantine after threshold."""
        self._host_failures[host] = self._host_failures.get(host, 0) + 1
        if self._host_failures[host] >= QUARANTINE_FAILURE_THRESHOLD:
            self._host_quarantine_until[host] = time.time() + QUARANTINE_DURATION_SECONDS
            logger.warning(
                "Host %s quarantined for %ds after %d consecutive failures",
                host,
                QUARANTINE_DURATION_SECONDS,
                self._host_failures[host],
            )

    def _record_host_success(self, host: str) -> None:
        """Reset failure counter on success."""
        self._host_failures.pop(host, None)
        self._host_quarantine_until.pop(host, None)

    # -----------------------------------------------------------------------
    # Priority Re-ranking
    # -----------------------------------------------------------------------

    def rerank_tasks(self) -> list[dict]:
        """Re-sort pending tasks by priority tier (0=highest, 5=lowest), then FIFO."""
        pending = swarm.list_tasks("pending")
        priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5}

        def _sort_key(t: dict) -> tuple:
            p = t.get("priority", "P5")
            if isinstance(p, int):
                tier = max(0, min(5, p))
            elif isinstance(p, str):
                tier = priority_order.get(p, 5)
            else:
                tier = 5
            # FIFO within same tier: sort by created_at ascending
            created = t.get("created_at", "")
            return (tier, created)

        pending.sort(key=_sort_key)
        return pending

    def interrupt_for_priority(self, task_id: str) -> bool:
        """Check if a new task should preempt any claimed tasks.

        Only tasks with priority 2+ levels higher can preempt.
        """
        all_tasks = swarm.list_tasks()
        new_task = None
        for t in all_tasks:
            if t.get("id") == task_id:
                new_task = t
                break

        if not new_task:
            return False

        new_priority_val = self._priority_value(new_task.get("priority", "P5"))

        # Only high-priority tasks (P0-P2) can preempt
        if new_priority_val > 2:
            return False

        claimed_tasks = swarm.list_tasks("claimed")
        preempted = False

        for task in claimed_tasks:
            claimed_priority_val = self._priority_value(task.get("priority", "P5"))
            if claimed_priority_val - new_priority_val >= 2:
                self._preempt_task(task["id"], task.get("claimed_by", ""))
                preempted = True

        return preempted

    def _priority_value(self, priority: str | int) -> int:
        """Convert priority string or int to numeric value (0-5).

        Args:
            priority: Priority as 'P0'-'P5', tier name, or int.

        Returns:
            Numeric priority (0=highest, 5=default/lowest).
        """
        if isinstance(priority, int):
            return max(0, min(5, priority))
        priority_map = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5}
        s = str(priority).strip()
        if s in priority_map:
            return priority_map[s]
        # Try tier names
        from task_queue import PRIORITY_TIERS

        return PRIORITY_TIERS.get(s.lower(), 5)

    def _preempt_task(self, task_id: str, claimed_by: str) -> None:
        """Mark a task as preempted and notify the claiming agent."""
        src = self.swarm_root / "tasks" / "claimed" / f"{task_id}.yaml"
        preempted_dir = self.swarm_root / "tasks" / "preempted"
        preempted_dir.mkdir(parents=True, exist_ok=True)
        dst = preempted_dir / f"{task_id}.yaml"

        if src.exists():
            import shutil

            shutil.move(str(src), str(dst))
            msg = (
                f"Task {task_id} has been preempted by a higher-priority task. "
                f"It will be re-queued when available."
            )
            swarm.send_message(claimed_by, msg, sender="auto_dispatcher")
            logger.info("Preempted task %s from %s", task_id, claimed_by)

    # -----------------------------------------------------------------------
    # Generate + apply
    # -----------------------------------------------------------------------

    def generate_and_create(self, config: dict, apply: bool = False) -> list[dict]:
        """Run WorkGenerator, deduplicate, optionally write task files."""
        wg = WorkGenerator(config)
        proposed = wg.generate_work()

        if not apply:
            return proposed

        created: list[dict] = []
        for t in proposed:
            task = swarm.create_task(
                title=t["title"],
                description=t.get("description", ""),
                project=t.get("project", ""),
                priority=t.get("priority", "medium"),
                requires=t.get("requires", []),
            )
            created.append(task)
        return created

    # -----------------------------------------------------------------------
    # Dispatch pending tasks
    # -----------------------------------------------------------------------

    def process_pending_tasks(self) -> list[dict]:
        """Scan pending tasks and dispatch eligible ones.

        Respects graduated mode, dead-man switch, and host quarantine.
        Returns list of dispatch result dicts for tasks that were dispatched.
        """
        if self.mode == "off":
            return []

        # Dead-man switch
        if not self._check_deadman_switch():
            logger.warning(
                "Dead-man switch: no human session in %ds — pausing auto-dispatch",
                DEADMAN_THRESHOLD_SECONDS,
            )
            return []

        dispatched: list[dict] = []

        # Re-rank pending tasks by priority
        pending = self.rerank_tasks()

        # Check if any P0 tasks should preempt
        for task in pending:
            if task.get("priority") == "P0":
                self.interrupt_for_priority(task["id"])

        # Check concurrent limit
        active_count = len(swarm.list_tasks("claimed"))
        if active_count >= self.max_concurrent:
            return []

        for task in pending:
            if active_count >= self.max_concurrent:
                break

            model = self._infer_model(task)

            # Mode gate: check if this model tier is allowed
            if not self.is_model_allowed(model):
                if self.mode == "dry_run":
                    logger.info(
                        "[DRY RUN] Would dispatch task %s to model %s",
                        task.get("id"),
                        model,
                    )
                continue

            requires = task.get("requires", [])
            host = _find_best_host(requires)
            if not host:
                continue

            # Host quarantine check
            if self._is_host_quarantined(host):
                logger.debug("Host %s is quarantined — skipping", host)
                continue

            # Claim before dispatch
            try:
                swarm.claim_task(task["id"])
            except FileNotFoundError:
                continue

            result = dispatch(
                host=host,
                task=self._build_prompt(task),
                model=model,
                project_dir=task.get("project") or None,
                background=True,
            )

            if result.status == "failed":
                self._record_host_failure(host)
            else:
                self._record_host_success(host)

            dispatched.append(
                {
                    "task_id": task["id"],
                    "host": host,
                    "model": model,
                    "dispatch_id": result.dispatch_id,
                    "mode": self.mode,
                }
            )
            active_count += 1

        return dispatched

    # -----------------------------------------------------------------------
    # Enable / disable helpers (backward compat)
    # -----------------------------------------------------------------------

    def set_enabled(self, enabled: bool, config_path: Path) -> None:
        """Backward-compatible enable/disable. Use set_mode() instead."""
        self.set_mode("sonnet" if enabled else "off", config_path)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _infer_model(self, task: dict) -> str:
        """Infer model from task using unified model router (v3).

        Falls back to legacy infer_model if model_router unavailable.
        """
        suggested = task.get("suggested_model", "")
        if suggested in ("haiku", "sonnet", "opus"):
            return suggested
        task_text = task.get("title", "") + " " + task.get("description", "")
        try:
            from model_router import get_model_for_task

            return get_model_for_task(task_text)
        except ImportError:
            return infer_model(task_text)

    @staticmethod
    def _build_prompt(task: dict) -> str:
        """Build a prompt for Claude to execute a swarm task.

        Args:
            task: Task dict with 'title', 'description', 'project' keys.

        Returns:
            Prompt string for Claude Code invocation.
        """
        return (
            f"You have a swarm task to complete:\n\n"
            f"Title: {task.get('title', '')}\n"
            f"Description: {task.get('description', '')}\n"
            f"Project: {task.get('project', '')}\n\n"
            f"Complete this task. When done, report what you did."
        )
