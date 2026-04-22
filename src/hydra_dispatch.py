#!/usr/bin/env python3
"""
Hydra Dispatch — Orchestrate Claude Code sessions across the fleet.

The main head (node_primary) dispatches tasks to fleet members by starting
Claude Code sessions via SSH. Each dispatch is a full autonomous session
with all skills, hooks, and context.

Usage:
    python3 hydra_dispatch.py dispatch --host node_gpu --task "Kin index <project-a-path>"
    python3 hydra_dispatch.py dispatch --task-id task-001  # auto-routes by capabilities
    python3 hydra_dispatch.py status                        # show all running dispatches
    python3 hydra_dispatch.py recall --host node_gpu             # get output from last dispatch
"""

import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Add swarm lib
sys.path.insert(0, str(Path(__file__).parent))
try:
    from backend import lib as swarm
except ImportError:
    import swarm_lib as swarm
import re

from util import fleet_from_config

# ── Worker Context Assembly (CB delta mode) ────────────────────────────────
try:
    from worker_context_assembly import build_worker_dispatch_prompt
    WORKER_CONTEXT_ASSEMBLY_AVAILABLE = True
except ImportError:
    WORKER_CONTEXT_ASSEMBLY_AVAILABLE = False
    logger.warning("worker_context_assembly not available; worker CB assembly disabled")

# Try to import metrics
try:
    from prom_boilerplate import make_gauge
    _prom_registry = None
    def _get_metrics():
        global _prom_registry
        if _prom_registry is None:
            from prom_boilerplate import make_registry
            _prom_registry = make_registry()
        return {
            "worker_context_bytes": make_gauge(
                "routing_worker_context_bytes",
                "Worker dispatch assembled context size in bytes",
                labelnames=["dispatch_id", "worker_tier"],
                registry=_prom_registry,
            ),
            "worker_context_savings": make_gauge(
                "routing_worker_context_savings_pct",
                "Worker dispatch context size savings percentage",
                labelnames=["dispatch_id", "worker_tier"],
                registry=_prom_registry,
            ),
        }
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False
    logger.debug("prom_boilerplate not available; metrics disabled")

# ── Model-Size Routing ──────────────────────────────────────────────────────


def _load_routing_config() -> dict:
    """Load config/routing.yaml for model-size-aware dispatch."""
    routing_paths = [
        Path("/opt/claude-swarm/config/routing.yaml"),
        Path(__file__).parent.parent / "config" / "routing.yaml",
    ]
    for p in routing_paths:
        if p.exists():
            try:
                with open(p) as f:
                    return yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError):
                pass
    return {}


_ROUTING_CONFIG: dict | None = None


def get_routing_config() -> dict:
    """Get cached routing config (lazy-loaded)."""
    global _ROUTING_CONFIG
    if _ROUTING_CONFIG is None:
        _ROUTING_CONFIG = _load_routing_config()
    return _ROUTING_CONFIG


def classify_model_size(task_description: str) -> dict | None:
    """Classify a task's model size requirements using routing rules.

    Args:
        task_description: Task description or model name to classify.

    Returns:
        Dict with keys: model_size, gpu_count, min_vram_gb, hosts, gpu_required.
        None if no rule matches.
    """
    config = get_routing_config()
    rules = config.get("rules", [])
    models = config.get("models", {})

    desc_lower = task_description.lower()

    for rule in rules:
        pattern = rule.get("pattern", "")
        if pattern and re.search(pattern, desc_lower):
            model_size = rule.get("model_size")
            if model_size and model_size in models:
                model_info = models[model_size]
                return {
                    "model_size": model_size,
                    "gpu_count": model_info.get("gpu_count", 1),
                    "min_vram_gb": model_info.get("min_vram_gb", 0),
                    "hosts": model_info.get("hosts", []),
                    "gpu_required": rule.get("gpu_required", True),
                    "tensor_parallel": model_info.get("tensor_parallel", False),
                    "rule_name": rule.get("name", ""),
                }
            # Rule matched but no model_size (e.g., code-analysis)
            return {
                "model_size": None,
                "gpu_count": 0,
                "min_vram_gb": 0,
                "hosts": [],
                "gpu_required": rule.get("gpu_required", False),
                "tensor_parallel": False,
                "rule_name": rule.get("name", ""),
            }

    return None


def get_model_gpu_requirements(model_name: str) -> dict | None:
    """Look up GPU requirements for a specific model name.

    Args:
        model_name: Model name (e.g., "qwen2.5-coder-7b", "llama3.3-70b").

    Returns:
        Dict from models registry, or None if not found.
    """
    config = get_routing_config()
    models = config.get("models", {})

    name_lower = model_name.lower()

    # Direct size match (e.g., "70b" in model name)
    for size_key, info in models.items():
        examples = [e.lower() for e in info.get("examples", [])]
        if name_lower in examples:
            return {"model_size": size_key, **info}
        # Check if size key appears in model name
        if size_key.lower() in name_lower:
            return {"model_size": size_key, **info}

    return None


# ── Fleet Configuration ──────────────────────────────────────────────────────


# Single source of truth: swarm.yaml. Fallback to hardcoded for offline use.
def _get_fleet() -> dict:
    fleet = fleet_from_config()
    if fleet:
        # Normalize to dispatch format
        result = {}
        for name, info in fleet.items():
            result[name] = {
                "ip": info["ip"],
                "ssh_user": info.get("user", "josh"),
                "claude_path": "claude",  # Resolved via PATH on remote host
                "capabilities": info.get("capabilities", []),
                "default_model": "sonnet",
            }
        return result
    # Fallback: env vars with hardcoded defaults
    return {
        "node_primary": {
            "ip": os.environ.get("MINIBOSS_HOST", "<orchestration-node-ip>"),
            "ssh_user": "josh",
            "claude_path": "/home/josh/.local/bin/claude",
            "capabilities": ["docker", "tailscale", "nfs_replica"],
            "default_model": "sonnet",
        },
        "node_gpu": {
            "ip": os.environ.get("GIGA_HOST", "<primary-node-ip>"),
            "ssh_user": "josh",
            "claude_path": "/home/josh/.npm-global/bin/claude",
            "capabilities": ["gpu", "docker", "ollama", "nfs_primary"],
            "default_model": "sonnet",
        },
    }


FLEET = _get_fleet()

DISPATCH_DIR = Path("/opt/swarm/artifacts/dispatches")
DISPATCH_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class DispatchSpec:
    """Typed specification for a dispatch request.

    Use instead of raw dicts when building dispatch requests programmatically.

    Attributes:
        task: Natural language task description / prompt
        host: Target fleet member (auto-routes if None)
        model: Claude model override (auto-selects if None)
        project_dir: Working directory on remote host
        timeout_minutes: Max runtime before killing
        background: Run in background (non-blocking)
        requires: Capability requirements for auto-routing
        task_id: Optional swarm task ID to link dispatch to
        track: Register with BackgroundRegistry for async tracking
    """

    task: str
    host: str | None = None
    model: str | None = None
    project_dir: str | None = None
    timeout_minutes: int = 30
    background: bool = True
    requires: list[str] = field(default_factory=list)
    task_id: str | None = None
    track: bool = True


@dataclass
class DispatchResult:
    """Result of a dispatch operation to a fleet member.

    Attributes:
        dispatch_id: Unique identifier for the dispatch
        host: Target host/fleet member name
        task: Task description
        model: Claude model used (opus/sonnet/haiku)
        status: Current status (pending, running, completed, failed)
        started_at: ISO timestamp when dispatch started
        completed_at: ISO timestamp when dispatch completed
        exit_code: Process exit code (-1 if not yet run)
        output_file: Path to output file on local disk
        error: Error message if dispatch failed
    """

    dispatch_id: str
    host: str
    task: str
    model: str
    status: str  # pending, running, completed, failed
    started_at: str = ""
    completed_at: str = ""
    exit_code: int = -1
    output_file: str = ""
    error: str = ""


def _model_for_task(task_description: str) -> str:
    """Apply session-miser logic to pick the right model for a task."""
    desc = task_description.lower()

    # Opus-tier: architecture, security, complex reasoning
    opus_patterns = [
        "architect",
        "design",
        "security audit",
        "complex",
        "debug",
        "plan",
        "review code",
        "analyze",
    ]
    if any(p in desc for p in opus_patterns):
        return "opus"

    # Haiku-tier: mechanical, status, search
    haiku_patterns = [
        "status",
        "check",
        "list",
        "search",
        "grep",
        "find",
        "count",
        "verify",
    ]
    if any(p in desc for p in haiku_patterns):
        return "haiku"

    # Sonnet: default for most work
    return "sonnet"


def _assemble_worker_context(
    task: str,
    target_files: list[str] | None = None,
    repo_name: str | None = None,
    language: str = "python",
    worker_tier: str | None = None,
    context_mode: str = "delta",
) -> tuple[str, dict]:
    """Assemble worker dispatch prompt using CB context assembly.

    Args:
        task: Task description
        target_files: Optional list of file paths to include
        repo_name: Repository name for CB scoping
        language: Programming language
        worker_tier: Worker tier (auto-selects if None)
        context_mode: "delta" (CB-assembled, default) or "full" (legacy, opt-out)

    Returns:
        Tuple of (assembled_prompt, metadata_dict) for metrics emission.
        Falls back to original task on any error.
    """
    if not WORKER_CONTEXT_ASSEMBLY_AVAILABLE:
        return task, {}

    try:
        # Auto-select worker tier based on task complexity
        if worker_tier is None:
            if "small" in task.lower() or "quick" in task.lower():
                worker_tier = "worker-sm"
            elif "large" in task.lower() or "complex" in task.lower():
                worker_tier = "worker-lg"
            else:
                worker_tier = "worker-md"  # default

        result = build_worker_dispatch_prompt(
            task_description=task,
            target_files=target_files,
            repo_name=repo_name,
            language=language,
            worker_tier=worker_tier,
            context_mode=context_mode,
        )

        # Emit metrics if available
        if METRICS_AVAILABLE and "dispatch_id" in globals():
            try:
                metrics = _get_metrics()
                meta = result.get("metadata", {})
                dispatch_id = globals().get("current_dispatch_id", "unknown")
                metrics["worker_context_bytes"].labels(
                    dispatch_id=dispatch_id, worker_tier=worker_tier
                ).set(meta.get("assembled_context_bytes", 0))
                metrics["worker_context_savings"].labels(
                    dispatch_id=dispatch_id, worker_tier=worker_tier
                ).set(meta.get("context_savings_pct", 0))
            except Exception as e:
                logger.debug("Failed to emit worker context metrics: %s", e)

        # Return assembled system+user prompt
        assembled_task = (
            result.get("system", "") + "\n\n---\n\n" + result.get("user", "")
        )
        return assembled_task, result.get("metadata", {})
    except Exception as e:
        logger.warning("Worker context assembly failed (falling back to original task): %s", e)
        return task, {}


def _find_best_host(requires: list[str], task_complexity: str = "") -> str | None:
    """Find the best host for a task using scored performance-based routing.

    Uses model-size routing (if task involves GPU inference) + performance ratings
    + capability matching + task complexity to rank hosts.
    Falls back to first-match if no ratings exist (backward compatible).
    """
    # Model-size routing: if task description mentions a model size, use routing config
    if task_complexity:
        model_info = classify_model_size(task_complexity)
        if model_info and model_info.get("hosts"):
            # Filter to hosts that are in FLEET and have required capabilities
            for host in model_info["hosts"]:
                if host in FLEET:
                    caps = set(FLEET[host]["capabilities"])
                    if set(requires).issubset(caps):
                        return host

    try:
        from performance_rating import scored_host_selection

        candidates = scored_host_selection(FLEET, requires, task_complexity)
        if candidates:
            return candidates[0][0]  # Highest-scored host
    except ImportError:
        pass

    # Fallback: original first-match behavior
    for hostname, config in FLEET.items():
        caps = set(config["capabilities"])
        if set(requires).issubset(caps):
            return hostname
    return None


def dispatch(
    host: str,
    task: str,
    model: str | None = None,
    project_dir: str | None = None,
    timeout_minutes: int = 30,
    background: bool = True,
) -> DispatchResult:
    """Dispatch a Claude Code session to a fleet member.

    Args:
        host: Fleet member hostname (e.g., "node_gpu")
        task: Natural language task description / prompt
        model: Override model (sonnet/opus/haiku). Auto-selects if None.
        project_dir: Working directory on the remote host
        timeout_minutes: Max runtime before killing
        background: Run in background (non-blocking)

    Returns:
        DispatchResult with dispatch_id and status
    """
    # CS2 fix: case-insensitive hostname resolution
    from util import resolve_host_key
    canonical = resolve_host_key(host, FLEET)
    if canonical is None:
        raise ValueError(f"Unknown host: {host}. Available: {list(FLEET.keys())}")
    host = canonical

    config = FLEET[host]
    dispatch_id = f"dispatch-{int(time.time())}-{host}"
    output_file = str(DISPATCH_DIR / f"{dispatch_id}.output")

    # ── Worker Context Assembly (routing-protocol-v1 §5) ──────────────────
    # Assemble CB-augmented context for worker dispatch (delta mode by default)
    # Pass context_mode="full" to opt-out and use legacy full-file dispatch
    worker_context_mode = os.environ.get("SWARM_WORKER_CONTEXT_MODE", "delta")
    if worker_context_mode != "disabled":
        task, wca_metadata = _assemble_worker_context(
            task=task,
            target_files=None,  # workers get inline context, not file references
            repo_name=project_dir.split("/")[-1] if project_dir else "unknown",
            language="python",  # default; could be inferred from task
            context_mode=worker_context_mode,
        )
        if wca_metadata:
            logger.info(
                "Worker context assembled: %d bytes, %d%% savings, tier=%s",
                wca_metadata.get("assembled_context_bytes", 0),
                wca_metadata.get("context_savings_pct", 0),
                wca_metadata.get("worker_tier", "unknown"),
            )

    # Auto-select model using unified model router (v3)
    if model is None:
        try:
            from model_router import get_model_for_task, route_task

            decision = route_task(task)
            model = decision.model
            logger.info(f"Model router: {decision.tier} → {model} (rule: {decision.rule_name})")
            # Emit routing decision via IPC
            try:
                from ipc_bridge import emit_routing_decision

                emit_routing_decision(task[:200], decision.tier, model, decision.rule_name)
            except Exception:
                pass
        except ImportError:
            model = _model_for_task(task)  # fallback to old logic

    # ── GPU Scheduler Integration (v3) ──────────────────────────────────────
    # If the task needs GPU (inference, embedding, etc.), try to allocate
    # a GPU slot via the VRAM-aware scheduler before dispatching.
    gpu_allocation = None
    try:
        from gpu_scheduler_v2 import GpuScheduler

        scheduler = GpuScheduler(exclude_hosts=os.environ.get("SWARM_EXCLUDE_HOSTS", "").split(","))
        # Check if model needs GPU
        model_needs_gpu = any(
            kw in model.lower() for kw in ["qwen", "devstral", "deepseek", "llama", "project-a"]
        )
        if model_needs_gpu or "gpu" in task.lower() or "inference" in task.lower():
            # KV-cache-aware routing: prefer host with model already warm
            prefer = host
            try:
                from ipc_bridge import find_host_with_warm_model

                warm_host = find_host_with_warm_model(model)
                if warm_host:
                    logger.info(f"KV-cache hit: {model} warm on {warm_host}")
                    prefer = warm_host
            except Exception:
                pass
            gpu_allocation = scheduler.schedule(
                task_id=dispatch_id,
                model_name=model if model_needs_gpu else "",
                prefer_host=prefer,
            )
            if gpu_allocation.success:
                logger.info(
                    f"GPU allocated: {gpu_allocation.host} GPU {gpu_allocation.gpu_indices} for {model}"
                )
                # Route to the host with the allocated GPU
                if gpu_allocation.host != host and gpu_allocation.host in FLEET:
                    logger.info(
                        f"Re-routing dispatch from {host} to {gpu_allocation.host} (GPU available)"
                    )
                    host = gpu_allocation.host
                    config = FLEET[host]
            else:
                logger.warning(f"No GPU available for {model}: {gpu_allocation.reason}")
    except ImportError:
        pass  # gpu_scheduler_v2 not available, continue without GPU scheduling
    except Exception as e:
        logger.warning(f"GPU scheduler error (continuing without): {e}")

    # ── Worktree isolation (v3) ──────────────────────────────────────────
    worktree_info = None
    if project_dir:
        try:
            from worktree_dispatch import create_worktree

            worktree_info = create_worktree(
                repo_path=project_dir,
                dispatch_id=dispatch_id,
                host=host,
            )
            if worktree_info:
                logger.info(
                    f"Worktree created: {worktree_info.path} (branch {worktree_info.branch})"
                )
                project_dir = worktree_info.path  # redirect dispatch to worktree
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Worktree creation failed (continuing without): {e}")

    # Build the remote command
    claude_cmd = config["claude_path"]
    # Inject SWARM_TASK_ID for cost tracking via Hydra Pulse
    env_exports = "export PATH=$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.cargo/bin:/usr/bin:/usr/local/bin:$PATH"
    env_exports += f" && export SWARM_TASK_ID={shlex.quote(dispatch_id)}"
    if gpu_allocation and gpu_allocation.success:
        env_exports += f" && export CUDA_VISIBLE_DEVICES={','.join(str(i) for i in gpu_allocation.gpu_indices)}"
    remote_parts = [
        env_exports,
        f"cd {shlex.quote(project_dir)}" if project_dir else "cd ~",
        f"{claude_cmd} --dangerously-skip-permissions --model {shlex.quote(model)} -p {shlex.quote(task)}",
    ]
    remote_cmd = " && ".join(remote_parts)

    ssh_cmd = [
        "ssh",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
        f"{config['ssh_user']}@{config['ip']}",
        remote_cmd,
    ]

    # Record dispatch
    result = DispatchResult(
        dispatch_id=dispatch_id,
        host=host,
        task=task,
        model=model,
        status="running",
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        output_file=output_file,
    )

    # Record performance metric start
    try:
        from performance_rating import record_dispatch_start

        record_dispatch_start(
            task_id=dispatch_id,
            hostname=host,
            model=model,
            estimated_minutes=timeout_minutes,
        )
    except ImportError:
        pass

    # Save dispatch record
    record_path = DISPATCH_DIR / f"{dispatch_id}.yaml"
    with open(record_path, "w") as f:
        yaml.dump(asdict(result), f)

    # Update swarm status on remote host
    try:
        swarm.send_message(
            host, f"Dispatch from {swarm._hostname()}: {task}", sender=swarm._hostname()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Suppressed: %s", exc)
        pass  # Best effort

    if background:
        # Run in background, capture output to file
        with open(output_file, "w") as out_f:
            proc = subprocess.Popen(
                ssh_cmd,
                stdout=out_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # Detach from parent
            )
        result.status = "running"
        # Save PID for monitoring
        pid_path = DISPATCH_DIR / f"{dispatch_id}.pid"
        pid_path.write_text(str(proc.pid))

        # v3: Emit IPC event (primary coordination layer)
        try:
            from ipc_bridge import emit_dispatch_started

            emit_dispatch_started(dispatch_id, host, model, task)
        except Exception:
            pass  # IPC is best-effort; NFS is the fallback

        print(f"Dispatched [{dispatch_id}] to {host} ({model})")
        print(f"  Task: {task}")
        print(f"  Output: {output_file}")
        print(f"  PID: {proc.pid}")
    else:
        # Run synchronously
        try:
            proc = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout_minutes * 60,
            )
            result.exit_code = proc.returncode
            result.status = "completed" if proc.returncode == 0 else "failed"
            result.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            result.error = proc.stderr[:500] if proc.returncode != 0 else ""

            # Save output
            with open(output_file, "w") as f:
                f.write(proc.stdout)

        except subprocess.TimeoutExpired:
            result.status = "failed"
            result.error = f"Timeout after {timeout_minutes} minutes"

        # Record performance metric end (sync dispatches)
        try:
            from performance_rating import record_dispatch_end

            record_dispatch_end(
                task_id=dispatch_id,
                hostname=host,
                success=(result.status == "completed"),
                error_type="timeout" if "Timeout" in result.error else result.error[:100],
            )
        except ImportError:
            pass

    # ── Release GPU allocation (v3) ───────────────────────────────────────
    if gpu_allocation and gpu_allocation.success:
        try:
            scheduler.release(gpu_allocation.host, gpu_allocation.gpu_indices)
            logger.info(f"GPU released: {gpu_allocation.host} GPU {gpu_allocation.gpu_indices}")
            from ipc_bridge import emit_gpu_released

            for idx in gpu_allocation.gpu_indices:
                emit_gpu_released(gpu_allocation.host, idx, dispatch_id)
        except Exception as e:
            logger.warning(f"Failed to release GPU: {e}")

    # ── Merge worktree on completion (v3) ────────────────────────────────
    if worktree_info:
        try:
            from worktree_dispatch import merge_worktree

            merged = merge_worktree(worktree_info, host=host)
            if merged:
                logger.info(f"Worktree merged: {worktree_info.branch} → main")
            else:
                logger.warning(
                    f"Worktree merge failed — branch {worktree_info.branch} preserved for manual review"
                )
        except Exception as e:
            logger.warning(f"Worktree merge error: {e}")

    # ── Query cost on completion (v3) ─────────────────────────────────────
    try:
        from cost_tracker import get_task_cost

        cost = get_task_cost(dispatch_id)
        if cost and cost.total_cost_usd > 0:
            result.actual_cost_usd = cost.total_cost_usd
            logger.info(
                f"Dispatch cost: ${cost.total_cost_usd:.4f} ({cost.total_input_tokens}+{cost.total_output_tokens} tokens)"
            )
    except Exception:
        pass

    # ── Emit dispatch completion IPC event ─────────────────────────────────
    try:
        from ipc_bridge import emit_dispatch_completed

        cost_usd = getattr(result, "actual_cost_usd", 0.0)
        emit_dispatch_completed(dispatch_id, host, result.status, cost_usd=cost_usd)
    except Exception:
        pass

    # Update dispatch record
    with open(record_path, "w") as f:
        yaml.dump(asdict(result), f)

    return result


def dispatch_from_spec(spec: DispatchSpec) -> DispatchResult:
    """Dispatch a task from a typed DispatchSpec.

    Auto-routes to the best host if spec.host is None.
    Registers with BackgroundRegistry if spec.track and spec.background.

    Args:
        spec: Typed dispatch specification

    Returns:
        DispatchResult with dispatch_id and status
    """
    host = spec.host
    if host is None:
        host = _find_best_host(spec.requires, spec.task)
        if host is None:
            raise RuntimeError(f"No host matches requirements: {spec.requires}")

    result = dispatch(
        host=host,
        task=spec.task,
        model=spec.model,
        project_dir=spec.project_dir,
        timeout_minutes=spec.timeout_minutes,
        background=spec.background,
    )

    # Register with background registry for async tracking
    if spec.background and spec.track:
        try:
            from background_registry import BackgroundRegistry

            registry = BackgroundRegistry()
            registry.register(
                dispatch_id=result.dispatch_id,
                host=result.host,
                description=spec.task[:200],
                task_id=spec.task_id,
                model=result.model,
                output_file=result.output_file,
            )
        except Exception as e:
            logger.warning("Failed to register with background registry: %s", e)

    return result


def dispatch_swarm_task(task_id: str, model: str | None = None) -> DispatchResult:
    """Dispatch a swarm task to the best-matching host."""
    # Read the task
    pending_path = Path("/opt/swarm/tasks/pending") / f"{task_id}.yaml"
    if not pending_path.exists():
        raise FileNotFoundError(f"Task {task_id} not found in pending/")

    with open(pending_path) as f:
        task = yaml.safe_load(f)

    # Find best host
    requires = task.get("requires", [])
    host = _find_best_host(requires)
    if not host:
        raise RuntimeError(f"No host matches requirements: {requires}")

    # Claim the task
    swarm.claim_task(task_id)

    # Build prompt from task
    prompt = f"""You have a swarm task to complete:

Title: {task["title"]}
Description: {task.get("description", "")}
Project: {task.get("project", "")}

Complete this task. When done, report what you did.
"""

    # Dispatch
    result = dispatch(
        host=host,
        task=prompt,
        model=model,
        project_dir=task.get("project"),
        background=True,
    )

    return result


def list_dispatches(active_only: bool = False) -> list[dict]:
    """List all dispatch records."""
    results = []
    for f in DISPATCH_DIR.glob("dispatch-*.yaml"):
        if f.name.endswith(".yaml"):
            with open(f) as fh:
                d = yaml.safe_load(fh)
                if active_only and d.get("status") != "running":
                    continue
                # Check if still running
                pid_file = f.with_suffix(".pid")
                if d.get("status") == "running" and pid_file.exists():
                    pid = int(pid_file.read_text().strip())
                    try:
                        os.kill(pid, 0)  # Check if alive
                    except ProcessLookupError:
                        d["status"] = "completed"
                results.append(d)
    return sorted(results, key=lambda x: x.get("started_at", ""), reverse=True)


def recall(dispatch_id: str) -> str:
    """Get the output from a completed dispatch."""
    output_file = DISPATCH_DIR / f"{dispatch_id}.output"
    if output_file.exists():
        return output_file.read_text()
    return f"No output found for {dispatch_id}"


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point for hydra_dispatch.

    Supports dispatch, status, and recall subcommands.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Hydra Dispatch — orchestrate Claude Code across the fleet"
    )
    sub = parser.add_subparsers(dest="command")

    # dispatch
    dp = sub.add_parser("dispatch", help="Dispatch a task to a fleet member")
    dp.add_argument("--host", help="Target host (auto-selects if not specified)")
    dp.add_argument("--task", required=True, help="Task description / prompt")
    dp.add_argument("--task-id", help="Claim and dispatch a swarm task by ID")
    dp.add_argument("--model", choices=["haiku", "sonnet", "opus"], help="Override model")
    dp.add_argument("--project", help="Working directory on remote host")
    dp.add_argument("--sync", action="store_true", help="Run synchronously (wait for result)")
    dp.add_argument("--timeout", type=int, default=30, help="Timeout in minutes (sync only)")

    # status
    sub.add_parser("status", help="Show all dispatches")

    # recall
    rc = sub.add_parser("recall", help="Get output from a dispatch")
    rc.add_argument("dispatch_id", help="Dispatch ID")

    # swarm-dispatch
    sd = sub.add_parser("swarm-dispatch", help="Dispatch a pending swarm task")
    sd.add_argument("task_id", help="Swarm task ID")
    sd.add_argument("--model", choices=["haiku", "sonnet", "opus"])

    args = parser.parse_args()

    if args.command == "dispatch":
        if args.task_id:
            result = dispatch_swarm_task(args.task_id, model=args.model)
        elif args.host:
            result = dispatch(
                host=args.host,
                task=args.task,
                model=args.model,
                project_dir=args.project,
                background=not args.sync,
                timeout_minutes=args.timeout,
            )
        else:
            # Auto-route: infer host from task
            print("Error: specify --host or --task-id")
            sys.exit(1)

        print(f"\nDispatch: {result.dispatch_id}")
        print(f"Status: {result.status}")
        if result.output_file:
            print(f"Output: {result.output_file}")

    elif args.command == "status":
        dispatches = list_dispatches()
        if not dispatches:
            print("No dispatches found.")
            return
        print(f"{'ID':40s} {'Host':10s} {'Model':8s} {'Status':10s} {'Started':20s}")
        print("-" * 90)
        for d in dispatches:
            print(
                f"{d.get('dispatch_id', '?'):40s} {d.get('host', '?'):10s} {d.get('model', '?'):8s} {d.get('status', '?'):10s} {d.get('started_at', '?'):20s}"
            )

    elif args.command == "recall":
        output = recall(args.dispatch_id)
        print(output)

    elif args.command == "swarm-dispatch":
        result = dispatch_swarm_task(args.task_id, model=args.model)
        print(f"Dispatched swarm task {args.task_id} to {result.host} ({result.model})")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
