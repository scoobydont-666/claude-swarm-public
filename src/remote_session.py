"""Remote Claude Code session orchestration — intelligent dispatch decisions.

Determines WHEN and HOW to execute work across the fleet:

1. **Local execution** — run it here (tools, bash, subagents)
2. **Remote dispatch** — fire-and-forget `claude -p` on another host
3. **Remote interactive** — spawn a full Claude Code session on another host
   for complex reasoning that benefits from the remote host's context/resources
4. **Collaborative** — spawn remote session, monitor progress, exchange context

Decision factors:
- Does the task need resources only available on another host? (GPU, Ollama, Docker Swarm)
- Is the task simple enough for a one-shot prompt? (dispatch)
- Does it need multi-turn reasoning or debugging? (interactive session)
- Does it need the remote host's CLAUDE.md / project context? (interactive)
- Is there a time constraint? (dispatch is faster to start, interactive is richer)
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── Execution Strategies ────────────────────────────────────────────────────


class ExecutionStrategy(StrEnum):
    """How to execute a task."""

    LOCAL = "local"  # Run here — tools, bash, subagents
    REMOTE_DISPATCH = "remote_dispatch"  # One-shot `claude -p` via SSH
    REMOTE_SESSION = "remote_session"  # Full interactive Claude Code on remote
    COLLABORATIVE = "collaborative"  # Remote session with context exchange


class TaskComplexity(StrEnum):
    """Estimated complexity of a task."""

    TRIVIAL = "trivial"  # Single command, status check
    SIMPLE = "simple"  # Few files, known pattern
    MODERATE = "moderate"  # Multi-file, some reasoning
    COMPLEX = "complex"  # Architecture, debugging, multi-step
    EXPLORATORY = "exploratory"  # Unknown scope, needs investigation


@dataclass
class ExecutionPlan:
    """The swarm's decision about how to execute a task."""

    strategy: ExecutionStrategy
    host: str  # Where to run (hostname)
    model: str  # Which Claude model
    reasoning: str  # Why this strategy was chosen
    complexity: TaskComplexity
    estimated_minutes: int = 15
    project_dir: str = ""
    prompt: str = ""
    max_turns: int = 0  # 0 = unlimited for interactive
    needs_project_context: bool = False  # Load CLAUDE.md on remote
    needs_gpu: bool = False
    needs_ollama: bool = False
    needs_docker: bool = False


# ── Fleet Knowledge ─────────────────────────────────────────────────────────

FLEET: dict[str, dict[str, Any]] = {
    "node_primary": {
        "ip": os.environ.get("MINIBOSS_HOST", "<orchestration-node-ip>"),
        "ssh_user": "josh",
        "claude_path": "/home/josh/.local/bin/claude",
        "capabilities": {"docker", "tailscale", "nfs_replica", "monero"},
        "strengths": "Fullnode relay, lightweight services, orchestration, CPU-only tasks",
        "is_primary": True,  # Usually the swarm controller
    },
    "node_gpu": {
        "ip": os.environ.get("GIGA_HOST", "<primary-node-ip>"),
        "ssh_user": "josh",
        "claude_path": "/home/josh/.local/bin/claude",
        "capabilities": {
            "gpu",
            "docker",
            "ollama",
            "nfs_primary",
            "chromadb",
            "swarm_manager",
        },
        "strengths": "GPU inference, Docker Swarm management, heavy computation, model serving",
        "is_primary": False,
    },
}

# Resource → required host mapping
RESOURCE_HOST_MAP = {
    "gpu": "node_gpu",
    "ollama": "node_gpu",
    "chromadb": "node_gpu",
    "swarm_manager": "node_gpu",
    "monero": "node_primary",
}


# ── Strategy Decision Engine ────────────────────────────────────────────────

# Keywords that indicate complexity
_TRIVIAL_KEYWORDS = {"status", "check", "list", "count", "version", "ping", "uptime"}
_SIMPLE_KEYWORDS = {
    "install",
    "copy",
    "move",
    "rename",
    "delete",
    "restart",
    "enable",
    "disable",
}
_MODERATE_KEYWORDS = {
    "implement",
    "add",
    "create",
    "write",
    "test",
    "fix",
    "update",
    "build",
}
_COMPLEX_KEYWORDS = {
    "architect",
    "design",
    "debug complex",
    "refactor",
    "migrate",
    "security audit",
    "investigate",
    "root cause",
    "performance",
    "optimize",
}
_EXPLORATORY_KEYWORDS = {
    "explore",
    "research",
    "evaluate",
    "assess",
    "prototype",
    "spike",
    "figure out",
    "why does",
    "how does",
}

# Keywords indicating remote host resources needed
_GPU_KEYWORDS = {
    "gpu",
    "cuda",
    "nvidia",
    "ollama",
    "inference",
    "model",
    "embedding",
    "chromadb",
    "project-a",
    "comfyui",
    "vram",
}
_DOCKER_KEYWORDS = {"docker", "swarm", "stack", "service", "container", "traefik"}

# Keywords indicating multi-turn reasoning needed
_INTERACTIVE_KEYWORDS = {
    "debug",
    "investigate",
    "figure out",
    "why",
    "root cause",
    "explore codebase",
    "chat through",
    "reason about",
    "step through",
    "walk through",
    "complex problem",
    "architectural decision",
}


def _classify_complexity(task: str) -> TaskComplexity:
    """Classify task complexity from description."""
    lower = task.lower()

    if any(kw in lower for kw in _EXPLORATORY_KEYWORDS):
        return TaskComplexity.EXPLORATORY
    if any(kw in lower for kw in _COMPLEX_KEYWORDS):
        return TaskComplexity.COMPLEX
    if any(kw in lower for kw in _MODERATE_KEYWORDS):
        return TaskComplexity.MODERATE
    if any(kw in lower for kw in _SIMPLE_KEYWORDS):
        return TaskComplexity.SIMPLE
    if any(kw in lower for kw in _TRIVIAL_KEYWORDS):
        return TaskComplexity.TRIVIAL

    return TaskComplexity.MODERATE  # Default


def _needs_remote_resources(task: str) -> tuple[bool, str]:
    """Check if task needs resources only available on a specific host.
    Returns (needs_remote, host_name)."""
    lower = task.lower()

    if any(kw in lower for kw in _GPU_KEYWORDS):
        return True, "node_gpu"
    if any(kw in lower for kw in _DOCKER_KEYWORDS):
        return True, "node_gpu"

    return False, ""


def _needs_interactive(task: str, complexity: TaskComplexity) -> bool:
    """Determine if a task needs multi-turn interactive reasoning."""
    lower = task.lower()

    # Explicitly interactive keywords
    if any(kw in lower for kw in _INTERACTIVE_KEYWORDS):
        return True

    # Complex and exploratory tasks benefit from interactive sessions
    if complexity in (TaskComplexity.COMPLEX, TaskComplexity.EXPLORATORY):
        return True

    return False


def _select_model(complexity: TaskComplexity, needs_interactive: bool) -> str:
    """Select model based on complexity and interaction mode."""
    if complexity == TaskComplexity.TRIVIAL:
        return "haiku"
    if complexity == TaskComplexity.SIMPLE:
        return "sonnet"
    if complexity == TaskComplexity.MODERATE:
        return "sonnet"
    if complexity in (TaskComplexity.COMPLEX, TaskComplexity.EXPLORATORY):
        return "opus" if needs_interactive else "sonnet"
    return "sonnet"


def plan_execution(
    task: str,
    current_host: str = "",
    project_dir: str = "",
    force_host: str = "",
    force_strategy: str = "",
) -> ExecutionPlan:
    """Decide how and where to execute a task.

    This is the core intelligence of the swarm orchestrator.

    Args:
        task: Natural language task description
        current_host: Hostname of the calling instance
        project_dir: Project directory (helps determine host affinity)
        force_host: Override host selection
        force_strategy: Override strategy selection

    Returns:
        ExecutionPlan with strategy, host, model, and reasoning.
    """
    import socket

    current_host = current_host or socket.gethostname()

    # 1. Classify complexity
    complexity = _classify_complexity(task)

    # 2. Check resource requirements
    needs_remote, preferred_host = _needs_remote_resources(task)

    # 3. Check if interactive reasoning needed
    interactive = _needs_interactive(task, complexity)

    # 4. Determine host
    if force_host:
        target_host = force_host
    elif needs_remote and preferred_host:
        target_host = preferred_host
    elif project_dir:
        # Project affinity: GPU projects → node_gpu, others → current host
        gpu_projects = {"<project-a-path>", "<ai-project-path>"}
        if project_dir in gpu_projects:
            target_host = "node_gpu"
        else:
            target_host = current_host
    else:
        target_host = current_host

    # 5. Determine strategy
    is_local = target_host == current_host

    if force_strategy:
        strategy = ExecutionStrategy(force_strategy)
    elif is_local:
        strategy = ExecutionStrategy.LOCAL
    elif interactive:
        strategy = ExecutionStrategy.REMOTE_SESSION
    elif complexity in (TaskComplexity.TRIVIAL, TaskComplexity.SIMPLE):
        strategy = ExecutionStrategy.REMOTE_DISPATCH
    elif complexity == TaskComplexity.MODERATE:
        strategy = ExecutionStrategy.REMOTE_DISPATCH
    elif complexity in (TaskComplexity.COMPLEX, TaskComplexity.EXPLORATORY):
        strategy = ExecutionStrategy.REMOTE_SESSION
    else:
        strategy = ExecutionStrategy.REMOTE_DISPATCH

    # 6. Select model
    model = _select_model(complexity, interactive)

    # 7. Estimate time
    time_map = {
        TaskComplexity.TRIVIAL: 2,
        TaskComplexity.SIMPLE: 10,
        TaskComplexity.MODERATE: 20,
        TaskComplexity.COMPLEX: 45,
        TaskComplexity.EXPLORATORY: 60,
    }
    est_minutes = time_map.get(complexity, 20)

    # 8. Build reasoning
    reasons = []
    if needs_remote:
        reasons.append(f"needs {preferred_host} resources (GPU/Ollama/Docker)")
    if interactive:
        reasons.append("multi-turn reasoning benefits from full Claude Code session")
    if is_local:
        reasons.append("can execute locally — no remote dispatch needed")
    if not is_local and not interactive:
        reasons.append("straightforward task — one-shot dispatch is sufficient")
    if complexity in (TaskComplexity.COMPLEX, TaskComplexity.EXPLORATORY):
        reasons.append(f"high complexity ({complexity.value}) warrants careful approach")

    # Max turns: unlimited for interactive, scaled for dispatch
    # Trivial tasks get 3 turns, simple get 10, moderate 25, complex/exploratory unlimited
    turn_map = {
        TaskComplexity.TRIVIAL: 3,
        TaskComplexity.SIMPLE: 10,
        TaskComplexity.MODERATE: 25,
        TaskComplexity.COMPLEX: 0,  # unlimited
        TaskComplexity.EXPLORATORY: 0,  # unlimited
    }
    max_turns = 0 if interactive else turn_map.get(complexity, 15)

    return ExecutionPlan(
        strategy=strategy,
        host=target_host,
        model=model,
        reasoning="; ".join(reasons),
        complexity=complexity,
        estimated_minutes=est_minutes,
        project_dir=project_dir,
        prompt=task,
        max_turns=max_turns,
        needs_project_context=(strategy != ExecutionStrategy.LOCAL),
        needs_gpu=any(kw in task.lower() for kw in _GPU_KEYWORDS),
        needs_ollama="ollama" in task.lower(),
        needs_docker=any(kw in task.lower() for kw in _DOCKER_KEYWORDS),
    )


# ── Execution Engine ────────────────────────────────────────────────────────


@dataclass
class SessionResult:
    """Result of a remote session."""

    dispatch_id: str
    host: str
    strategy: str
    model: str
    status: str  # running, completed, failed, timeout
    exit_code: int = -1
    output: str = ""
    output_file: str = ""
    started_at: str = ""
    completed_at: str = ""
    error: str = ""
    pid: int = 0
    estimated_cost_usd: float = 0.0


DISPATCH_DIR = Path("/opt/swarm/artifacts/dispatches")

# Model pricing per 1M tokens (input/output average) — from Claude API
MODEL_COSTS = {
    "haiku": 0.80,  # $0.80 per 1M input, $4 per 1M output tokens
    "sonnet": 3.00,  # $3 per 1M input, $15 per 1M output tokens
    "opus": 15.00,  # $15 per 1M input, $75 per 1M output tokens
}


def _estimate_cost(model: str, output_length: int) -> float:
    """Estimate cost from output length. Uses ~0.3 tokens per character."""
    cost_per_1m = MODEL_COSTS.get(model, 3.0)
    # Rough estimate: 0.3 tokens per character
    estimated_tokens = output_length * 0.3
    return (estimated_tokens / 1_000_000) * cost_per_1m


def _auto_requeue_task(dispatch_id: str, task_id: str = "") -> bool:
    """Auto-requeue a failed task back to pending if applicable.

    Args:
        dispatch_id: The dispatch ID
        task_id: Optional task ID (extracted from dispatch if not provided)

    Returns:
        True if successfully requeued, False otherwise.
    """
    import os
    from pathlib import Path

    import yaml

    # Try to extract task_id from dispatch plan if not provided
    if not task_id:
        plan_file = DISPATCH_DIR / f"{dispatch_id}.plan.yaml"
        if plan_file.exists():
            try:
                with open(plan_file) as f:
                    plan_data = yaml.safe_load(f) or {}
                task_id = plan_data.get("task_id", "")
            except (yaml.YAMLError, OSError):
                pass

    if not task_id:
        return False

    try:
        claimed_dir = Path("/opt/swarm/tasks/claimed")
        task_file = claimed_dir / f"{task_id}.yaml"

        if not task_file.exists():
            return False

        with open(task_file) as f:
            task = yaml.safe_load(f) or {}

        retries = task.get("_retries", 0)
        if retries >= 3:
            logger.warning("Task %s exceeded max retries — escalating instead", task_id)
            return False

        # Requeue: move to pending, increment retries
        task["_retries"] = retries + 1
        task.pop("claimed_by", None)
        task.pop("claimed_at", None)

        pending_dir = Path("/opt/swarm/tasks/pending")
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_file = pending_dir / f"{task_id}.yaml"

        with open(pending_file, "w") as f:
            yaml.dump(task, f, default_flow_style=False, sort_keys=False)

        os.remove(task_file)
        logger.info(
            "Auto-requeued task %s (retry %d/3) after dispatch failure",
            task_id,
            retries + 1,
        )
        return True

    except Exception as exc:
        logger.warning("Failed to auto-requeue task %s: %s", task_id, exc)
        return False


def check_dispatch_status(dispatch_id: str) -> SessionResult | None:
    """Check status of a background dispatch.

    For background dispatches, reads PID file and checks if process is alive.
    If dead and no completion record, marks as failed and attempts auto-requeue.

    Args:
        dispatch_id: The dispatch ID to check

    Returns:
        SessionResult with updated status, or None if dispatch not found.
    """
    import psutil

    dispatch_dir = DISPATCH_DIR
    pid_file = dispatch_dir / f"{dispatch_id}.pid"
    plan_file = dispatch_dir / f"{dispatch_id}.plan.yaml"
    output_file = dispatch_dir / f"{dispatch_id}.output"

    if not pid_file.exists():
        return None

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None

    # Check if process is alive
    try:
        proc = psutil.Process(pid)
        if proc.is_running():
            # Process still running
            return None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        # Process is dead
        pass

    # Process is dead — mark as failed if no completion record
    output = ""
    if output_file.exists():
        try:
            output = output_file.read_text()
        except OSError:
            pass

    result = SessionResult(
        dispatch_id=dispatch_id,
        host="",
        strategy="",
        model="",
        status="failed",
        exit_code=1,
        output=output,
        output_file=str(output_file),
        error="Process terminated unexpectedly",
    )

    # Try to extract task_id and requeue
    if plan_file.exists():
        try:
            import yaml

            with open(plan_file) as f:
                plan_data = yaml.safe_load(f) or {}
            task_id = plan_data.get("task_id", "")
            _auto_requeue_task(dispatch_id, task_id)
        except (yaml.YAMLError, OSError):
            pass

    logger.warning(
        "Dispatch %s process dead — marked failed and requeued if applicable",
        dispatch_id,
    )
    return result


def execute_plan(plan: ExecutionPlan, background: bool = True) -> SessionResult:
    """Execute an ExecutionPlan — dispatches to the appropriate host and mode.

    For LOCAL strategy, returns immediately (caller should execute).
    For REMOTE_DISPATCH, fires a one-shot `claude -p`.
    For REMOTE_SESSION, fires `claude -p` with higher max_turns.
    For COLLABORATIVE, fires session and sets up context exchange.
    """
    DISPATCH_DIR.mkdir(parents=True, exist_ok=True)
    dispatch_id = f"session-{int(time.time())}-{plan.host}"
    output_file = str(DISPATCH_DIR / f"{dispatch_id}.output")

    result = SessionResult(
        dispatch_id=dispatch_id,
        host=plan.host,
        strategy=plan.strategy.value,
        model=plan.model,
        status="pending",
        output_file=output_file,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    if plan.strategy == ExecutionStrategy.LOCAL:
        result.status = "local"
        return result  # Caller handles local execution

    host_cfg = FLEET.get(plan.host)
    if not host_cfg:
        result.status = "failed"
        result.error = f"Unknown host: {plan.host}"
        return result

    # Build remote Claude command
    claude_path = host_cfg["claude_path"]
    parts = [
        "export PATH=$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.cargo/bin:$PATH",
    ]
    if plan.project_dir:
        parts.append(f"cd {plan.project_dir}")

    # Build claude arguments
    claude_args = [claude_path]

    if plan.strategy == ExecutionStrategy.REMOTE_DISPATCH:
        # One-shot: use -p for print mode, limited turns
        claude_args.extend(
            [
                "--permission-mode",
                "bypassPermissions",
                "--model",
                plan.model,
                "-p",
                plan.prompt,
            ]
        )
        if plan.max_turns > 0:
            claude_args.extend(["--max-turns", str(plan.max_turns)])
    elif plan.strategy in (
        ExecutionStrategy.REMOTE_SESSION,
        ExecutionStrategy.COLLABORATIVE,
    ):
        # Full session: more turns, project context loaded automatically
        claude_args.extend(
            [
                "--permission-mode",
                "bypassPermissions",
                "--model",
                plan.model,
                "-p",
                plan.prompt,
            ]
        )
        # No max-turns limit for interactive sessions (or high limit)
        if plan.max_turns > 0:
            claude_args.extend(["--max-turns", str(plan.max_turns)])

    # Build the claude command with a proper argv-quoted string.
    # Fix 2026-04-22 (dogfood test): the prior version did `claude_args[:-1]`
    # (dropping the last arg — which was the prompt for REMOTE_DISPATCH but
    # was `--max-turns VALUE`'s VALUE for REMOTE_SESSION/COLLABORATIVE), then
    # appended a heredoc-wrapped prompt AND re-appended `--max-turns` on the
    # session paths. Result: two prompts + duplicate --max-turns + broken outer
    # single-quotes from `'PROMPT_EOF'` inside `bash -c '...'`.
    #
    # Correct pattern: shlex.quote every argv element, join with spaces, emit
    # a single well-formed shell command line. No heredoc, no re-append tricks.
    import shlex as _shlex

    claude_cmdline = " ".join(_shlex.quote(a) for a in claude_args)
    parts.append(claude_cmdline)
    setup_cmd = " && ".join(parts)
    remote_script = setup_cmd

    ssh_cmd = [
        "ssh",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=60",  # Keep alive for long sessions
        f"{host_cfg['ssh_user']}@{host_cfg['ip']}",
        f"bash -c {repr(remote_script)}",
    ]

    # Save plan for reference (will be updated with cost after completion)
    plan_path = DISPATCH_DIR / f"{dispatch_id}.plan.yaml"
    with open(plan_path, "w") as f:
        yaml.dump(
            {
                "dispatch_id": dispatch_id,
                "strategy": plan.strategy.value,
                "host": plan.host,
                "model": plan.model,
                "complexity": plan.complexity.value,
                "reasoning": plan.reasoning,
                "project_dir": plan.project_dir,
                "estimated_minutes": plan.estimated_minutes,
                "prompt": plan.prompt[:500],
                "estimated_cost_usd": 0.0,  # Will be updated after completion
            },
            f,
        )

    if background:
        # Wrap with stdbuf for line-buffered output streaming
        stdbuf_cmd = ["stdbuf", "-oL"] + ssh_cmd

        # Capture output locally too (tee on remote writes to NFS, this is the local copy)
        local_output_f = open(output_file, "w")
        try:
            proc = subprocess.Popen(
                stdbuf_cmd,
                stdout=local_output_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Suppressed: %s", exc)
            local_output_f.close()
            raise
        result.status = "running"
        result.pid = proc.pid

        pid_path = DISPATCH_DIR / f"{dispatch_id}.pid"
        pid_path.write_text(str(proc.pid))

        logger.info(
            "Dispatched [%s] to %s (%s, %s) — PID %d, streaming to %s",
            dispatch_id,
            plan.host,
            plan.strategy.value,
            plan.model,
            proc.pid,
            output_file,
        )
    else:
        try:
            proc = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=plan.estimated_minutes * 60 * 2,  # 2x safety margin
            )
            result.exit_code = proc.returncode
            result.status = "completed" if proc.returncode == 0 else "failed"
            result.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            result.output = proc.stdout
            result.error = proc.stderr[:500] if proc.returncode != 0 else ""

            # Estimate cost
            result.estimated_cost_usd = _estimate_cost(plan.model, len(result.output))

            # Update plan file with actual cost
            plan_data = {
                "dispatch_id": dispatch_id,
                "strategy": plan.strategy.value,
                "host": plan.host,
                "model": plan.model,
                "complexity": plan.complexity.value,
                "reasoning": plan.reasoning,
                "project_dir": plan.project_dir,
                "estimated_minutes": plan.estimated_minutes,
                "prompt": plan.prompt[:500],
                "estimated_cost_usd": result.estimated_cost_usd,
            }
            with open(plan_path, "w") as f:
                yaml.dump(plan_data, f)

        except subprocess.TimeoutExpired:
            result.status = "timeout"
            result.error = f"Timeout after {plan.estimated_minutes * 2} minutes"

        # Auto-requeue if dispatch was for a swarm task and failed
        if result.status in ("failed", "timeout"):
            # Check if this dispatch was for a swarm task
            task_id = plan.prompt.split()[-1] if plan.prompt else ""
            # Extract task ID if it's in the plan prompt (format: "... task_id")
            import re

            task_match = re.search(r"(task-\d+)", plan.prompt)
            if task_match:
                task_id = task_match.group(1)
                if _auto_requeue_task(dispatch_id, task_id):
                    result.error += " [auto-requeued]"

    return result


def get_dispatch_output(dispatch_id: str, tail_lines: int = 50) -> str:
    """Retrieve the output from a dispatch, optionally tailing the last N lines.

    Args:
        dispatch_id: The dispatch ID (e.g., 'session-1774378670-node_gpu')
        tail_lines: Number of lines to return from the end. If 0, return all.

    Returns:
        Output text from the dispatch, or error message if not found.
    """
    output_file = DISPATCH_DIR / f"{dispatch_id}.output"
    if not output_file.exists():
        return f"Output file not found: {output_file}"

    try:
        with open(output_file) as f:
            if tail_lines > 0:
                lines = f.readlines()
                return "".join(lines[-tail_lines:])
            else:
                return f.read()
    except OSError as exc:
        return f"Error reading output: {exc}"


def smart_dispatch(
    task: str,
    project_dir: str = "",
    force_host: str = "",
    background: bool = True,
) -> tuple[ExecutionPlan, SessionResult | None]:
    """High-level entry point: plan + execute in one call.

    Returns (plan, result). Result is None if strategy is LOCAL.
    """
    import socket

    plan = plan_execution(
        task=task,
        current_host=socket.gethostname(),
        project_dir=project_dir,
        force_host=force_host,
    )

    if plan.strategy == ExecutionStrategy.LOCAL:
        return plan, None

    result = execute_plan(plan, background=background)
    return plan, result
