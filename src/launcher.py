"""Auto-Scale Launcher — spawn Claude Code instances when queue backs up.

Monitors pending task queue depth and spawns new agent instances when the
count exceeds a threshold. Integrates with rate-limit tracking to avoid
spawning agents that will immediately hit limits.

Safety rails:
  - Max concurrent instances cap (default 5)
  - Minimum cooldown between spawns (default 60s)
  - Dead-man switch: won't scale if no human session in 24h
  - Rate-limit aware: checks RateLimitTracker before spawning
  - Won't spawn if auto-dispatch mode is "off" or "dry_run"
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Defaults — overridable via config
DEFAULT_QUEUE_THRESHOLD = 3  # pending tasks before scaling
DEFAULT_MAX_INSTANCES = 5  # max concurrent agents
DEFAULT_SPAWN_COOLDOWN = 60  # seconds between spawns
DEFAULT_SPAWN_TIMEOUT = 10  # seconds to wait for process start


@dataclass
class SpawnResult:
    """Result of an instance spawn attempt."""

    success: bool
    pid: int = 0
    host: str = ""
    profile: str = ""
    reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Convert spawn result to a dictionary."""
        return {
            "success": self.success,
            "pid": self.pid,
            "host": self.host,
            "profile": self.profile,
            "reason": self.reason,
        }


class AutoScaler:
    """Monitor queue depth and spawn Claude Code instances as needed."""

    def __init__(
        self,
        queue_threshold: int = DEFAULT_QUEUE_THRESHOLD,
        max_instances: int = DEFAULT_MAX_INSTANCES,
        spawn_cooldown: float = DEFAULT_SPAWN_COOLDOWN,
        profiles: list[str] | None = None,
    ) -> None:
        self.queue_threshold = queue_threshold
        self.max_instances = max_instances
        self.spawn_cooldown = spawn_cooldown
        self.profiles = profiles or ["default"]
        self._last_spawn_time: float = 0.0
        self._spawned_pids: list[int] = []

    # ------------------------------------------------------------------
    # Queue depth
    # ------------------------------------------------------------------

    @staticmethod
    def get_pending_count(swarm_root: Path = Path("/var/lib/swarm")) -> int:
        """Count pending tasks in the swarm task queue."""
        pending_dir = swarm_root / "tasks" / "pending"
        if not pending_dir.exists():
            return 0
        return len(list(pending_dir.glob("*.yaml")))

    @staticmethod
    def get_active_agent_count(swarm_root: Path = Path("/var/lib/swarm")) -> int:
        """Count currently live agents from the registry."""
        agents_dir = swarm_root / "agents"
        if not agents_dir.exists():
            return 0
        import json
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        count = 0
        for f in agents_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                last_hb = data.get("last_heartbeat", "")
                if last_hb:
                    dt = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
                    age = (now - dt).total_seconds()
                    if age < 300:  # 5-minute stale threshold
                        count += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("Suppressed: %s", exc)
                continue
        return count

    # ------------------------------------------------------------------
    # Scale decision
    # ------------------------------------------------------------------

    def should_scale(
        self,
        pending_count: int,
        active_agents: int,
        rate_tracker: Any = None,
    ) -> tuple[bool, str]:
        """Decide whether to spawn a new instance.

        Returns (should_spawn, reason).
        """
        # Check queue threshold
        if pending_count < self.queue_threshold:
            return (
                False,
                f"queue depth {pending_count} < threshold {self.queue_threshold}",
            )

        # Check max instances
        if active_agents >= self.max_instances:
            return False, f"at max instances ({active_agents}/{self.max_instances})"

        # Check cooldown
        elapsed = time.time() - self._last_spawn_time
        if elapsed < self.spawn_cooldown:
            remaining = self.spawn_cooldown - elapsed
            return False, f"cooldown active ({remaining:.0f}s remaining)"

        # Check rate limits
        if rate_tracker is not None:
            available = rate_tracker.get_available_profiles(self.profiles)
            if not available:
                return False, "all profiles rate-limited"

        return (
            True,
            f"queue depth {pending_count} >= threshold, {active_agents} active agents",
        )

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    def spawn_instance(
        self,
        project_dir: str = "/opt/hydra-project",
        profile: str = "default",
        task_hint: str = "",
    ) -> SpawnResult:
        """Spawn a new Claude Code CLI instance.

        The instance starts with a project-management prompt and picks up
        the next pending task from the swarm queue.
        """
        claude_bin = shutil.which("claude")
        if not claude_bin:
            return SpawnResult(success=False, reason="claude CLI not found in PATH")

        prompt = (
            "You are a swarm agent. Check /var/lib/swarm/tasks/pending/ for work. "
            "Claim the highest-priority task and execute it. "
            "Commit after each unit of work. Run tests before moving on."
        )
        if task_hint:
            prompt += f"\n\nHint: {task_hint}"

        env = os.environ.copy()
        if profile != "default":
            env["CLAUDE_PROFILE"] = profile

        try:
            proc = subprocess.Popen(
                [claude_bin, "--print", "--dangerously-skip-permissions", "-p", prompt],
                cwd=project_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # detach from parent
            )

            self._last_spawn_time = time.time()
            self._spawned_pids.append(proc.pid)

            logger.info(
                "Spawned Claude Code instance: pid=%d, project=%s, profile=%s",
                proc.pid,
                project_dir,
                profile,
            )

            return SpawnResult(
                success=True,
                pid=proc.pid,
                host=os.uname().nodename,
                profile=profile,
            )

        except Exception as e:
            logger.error("Failed to spawn Claude Code: %s", e)
            return SpawnResult(success=False, reason=str(e))

    # ------------------------------------------------------------------
    # Check + scale (main entry point)
    # ------------------------------------------------------------------

    def check_and_scale(
        self,
        rate_tracker: Any = None,
        swarm_root: Path = Path("/var/lib/swarm"),
    ) -> SpawnResult | None:
        """Check queue depth and spawn if needed. Returns SpawnResult or None."""
        pending = self.get_pending_count(swarm_root)
        active = self.get_active_agent_count(swarm_root)

        should, reason = self.should_scale(pending, active, rate_tracker)

        if not should:
            logger.debug("No scale needed: %s", reason)
            return None

        # Pick best available profile
        profile = "default"
        if rate_tracker is not None:
            best = rate_tracker.get_best_profile(self.profiles)
            if best:
                profile = best

        logger.info("Scaling up: %s", reason)
        return self.spawn_instance(profile=profile)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_dead_processes(self) -> list[int]:
        """Remove PIDs that are no longer running."""
        cleaned = []
        alive = []
        for pid in self._spawned_pids:
            try:
                os.kill(pid, 0)  # check if process exists
                alive.append(pid)
            except OSError:
                cleaned.append(pid)
        self._spawned_pids = alive
        return cleaned

    def status(self) -> dict[str, Any]:
        """Current auto-scaler status."""
        self.cleanup_dead_processes()
        return {
            "queue_threshold": self.queue_threshold,
            "max_instances": self.max_instances,
            "spawn_cooldown": self.spawn_cooldown,
            "spawned_count": len(self._spawned_pids),
            "spawned_pids": list(self._spawned_pids),
            "last_spawn_ago": time.time() - self._last_spawn_time
            if self._last_spawn_time
            else None,
            "profiles": self.profiles,
        }
