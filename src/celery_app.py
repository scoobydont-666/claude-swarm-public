"""Celery application for claude-swarm task orchestration.

Replaces custom auto_dispatch.py with Celery workers + beat scheduler.
Redis on node_primary (<orchestration-node-ip>:6379) serves as both broker and result backend.

Queues:
  gpu     — GPU workloads (node_gpu, node_reserve2 only)
  cpu     — CPU workloads (all hosts)
  default — fallback

Usage:
  # Start worker on a GPU host:
  celery -A celery_app worker -Q gpu,cpu,default -c 2 --hostname=worker@node_gpu

  # Start worker on a CPU host:
  celery -A celery_app worker -Q cpu,default -c 4 --hostname=worker@node_primary

  # Start beat scheduler:
  celery -A celery_app beat

  # Start Flower dashboard:
  celery -A celery_app flower --port=5555
"""

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

# Ensure src is on path for redis_client imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from celery import Celery

# Redis connection — uses same DB as redis_client (default 0).
# Fail-closed: in non-dev mode, empty SWARM_REDIS_PASSWORD is a fatal config error.
REDIS_HOST = os.environ.get("SWARM_REDIS_HOST", "127.0.0.1")
REDIS_PORT = os.environ.get("SWARM_REDIS_PORT", "6379")
REDIS_PASSWORD = os.environ.get("SWARM_REDIS_PASSWORD", "")
REDIS_DB = os.environ.get("SWARM_REDIS_DB", "0")
HYDRA_ENV = os.environ.get("HYDRA_ENV", "prod").lower()

if not REDIS_PASSWORD and HYDRA_ENV != "dev":
    raise RuntimeError(
        "SWARM_REDIS_PASSWORD is required in non-dev environments. "
        "Set HYDRA_ENV=dev to allow unauthenticated Redis for local development."
    )

REDIS_URL = (
    f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
    if REDIS_PASSWORD
    else f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
)

app = Celery("claude-swarm", broker=REDIS_URL, backend=REDIS_URL)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_routes={
        "celery_app.gpu_task": {"queue": "gpu"},
        "celery_app.cpu_task": {"queue": "cpu"},
        "celery_app.dispatch_task": {"queue": "default"},
        "celery_app.cleanup_stale": {"queue": "default"},
        "celery_app.health_check": {"queue": "default"},
        "celery_app.generate_work": {"queue": "default"},
        "celery_app.auto_dispatch_scan": {"queue": "default"},
    },
    beat_schedule={
        "cleanup-stale-every-5m": {
            "task": "celery_app.cleanup_stale",
            "schedule": 300.0,
        },
        "health-check-every-2m": {
            "task": "celery_app.health_check",
            "schedule": 120.0,
        },
        "generate-work-every-30m": {
            "task": "celery_app.generate_work",
            "schedule": 1800.0,
        },
        "auto-dispatch-every-2m": {
            "task": "celery_app.auto_dispatch_scan",
            "schedule": 120.0,
        },
    },
)

# Command allowlist — only these prefixes are permitted for task execution
ALLOWED_CMD_PREFIXES = (
    "claude",
    "examforge",
    "nutantforge",
    "python3",
    "python",
    "pytest",
    "git",
    "bash",
    "sh",
    "uv",
    "npm",
    "node",
)


def _validate_and_split_cmd(cmd: str) -> list[str]:
    """Validate command against allowlist and return argv list (no shell)."""
    argv = shlex.split(cmd)
    if not argv:
        raise ValueError("Empty command")
    binary = os.path.basename(argv[0])
    if not any(
        binary == prefix or binary.startswith(prefix + ".") for prefix in ALLOWED_CMD_PREFIXES
    ):
        raise ValueError(f"Command '{binary}' not in allowlist: {ALLOWED_CMD_PREFIXES}")
    return argv


@app.task(bind=True, name="celery_app.gpu_task")
def gpu_task(self, task_data: dict) -> dict:
    """Execute a GPU-bound task (generation, inference, QA)."""
    task_id = task_data.get("id", "unknown")
    project = task_data.get("project", "")
    cmd = task_data.get("command", "")

    if not cmd:
        return {"status": "error", "message": "No command specified"}

    try:
        argv = _validate_and_split_cmd(cmd)
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=3600,
            cwd=project or None,
        )
        return {
            "status": "success" if result.returncode == 0 else "failed",
            "task_id": task_id,
            "returncode": result.returncode,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "task_id": task_id}
    except Exception as e:
        return {"status": "error", "task_id": task_id, "message": str(e)}


@app.task(bind=True, name="celery_app.cpu_task")
def cpu_task(self, task_data: dict) -> dict:
    """Execute a CPU-bound task (tests, builds, docs)."""
    task_id = task_data.get("id", "unknown")
    project = task_data.get("project", "")
    cmd = task_data.get("command", "")

    if not cmd:
        return {"status": "error", "message": "No command specified"}

    try:
        argv = _validate_and_split_cmd(cmd)
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=3600,
            cwd=project or None,
        )
        return {
            "status": "success" if result.returncode == 0 else "failed",
            "task_id": task_id,
            "returncode": result.returncode,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "task_id": task_id}
    except Exception as e:
        return {"status": "error", "task_id": task_id, "message": str(e)}


@app.task(name="celery_app.dispatch_task")
def dispatch_task(task_data: dict) -> str:
    """Route a task to the appropriate queue based on requirements."""
    requires = task_data.get("requires", [])

    if "gpu" in requires:
        return gpu_task.apply_async(args=[task_data], queue="gpu").id
    else:
        return cpu_task.apply_async(args=[task_data], queue="cpu").id


CONFIG_PATH = Path("/opt/claude-swarm/config/swarm.yaml")


def _load_config() -> dict:
    """Load swarm.yaml config."""
    import yaml

    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


@app.task(name="celery_app.generate_work")
def generate_work() -> dict:
    """Periodic work generation -- scans projects for actionable tasks."""
    try:
        from auto_dispatch import AutoDispatcher

        config = _load_config()
        ad = AutoDispatcher(config)
        if not ad.auto_dispatch_enabled:
            return {"status": "skipped", "reason": "auto_dispatch disabled"}
        created = ad.generate_and_create(config, apply=True)
        return {
            "status": "ok",
            "tasks_created": len(created),
            "timestamp": time.time(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.task(name="celery_app.auto_dispatch_scan")
def auto_dispatch_scan() -> dict:
    """Periodic dispatch scan -- routes pending tasks to best-matching hosts."""
    try:
        from auto_dispatch import AutoDispatcher

        config = _load_config()
        ad = AutoDispatcher(config)
        if not ad.auto_dispatch_enabled:
            return {"status": "skipped", "reason": "auto_dispatch disabled"}
        dispatched = ad.process_pending_tasks()
        return {
            "status": "ok",
            "dispatched": len(dispatched),
            "details": dispatched,
            "timestamp": time.time(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.task(name="celery_app.cleanup_stale")
def cleanup_stale() -> dict:
    """Periodic cleanup of stale agents and tasks."""
    try:
        import redis_client as rc

        agents = rc.list_agents()
        # Redis TTL handles agent staleness automatically
        # Just report current state
        return {
            "live_agents": len(agents),
            "timestamp": time.time(),
        }
    except Exception as e:
        return {"error": str(e)}


@app.task(name="celery_app.health_check")
def health_check() -> dict:
    """Periodic health check."""
    try:
        import redis_client as rc

        redis_ok = rc.health_check()
        return {
            "redis": redis_ok,
            "timestamp": time.time(),
            "hostname": os.uname().nodename,
        }
    except Exception as e:
        return {"error": str(e)}
