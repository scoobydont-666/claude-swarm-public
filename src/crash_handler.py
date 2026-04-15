#!/usr/bin/env python3
"""Crash Handler — graceful shutdown and session cleanup.

Registers signal handlers for SIGTERM, SIGINT, SIGHUP to ensure:
1. Node is marked idle
2. Any claimed tasks are requeued
3. Session summary is written
4. Process exits cleanly

Should be called early in session initialization:
    from crash_handler import install_crash_handlers
    install_crash_handlers()
"""

import atexit
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Callable, Optional

from util import now_iso as _now_iso, atomic_write_yaml as _atomic_write_yaml

logger = logging.getLogger("swarm.crash_handler")

# Global state
_session_info: dict = {}
_crash_callbacks: list[Callable] = []


def register_crash_callback(callback: Callable[[], None]) -> None:
    """Register a callback to run during crash/shutdown.

    Callbacks are executed in LIFO order (last registered, first executed).
    """
    _crash_callbacks.append(callback)


def set_session_info(info: dict) -> None:
    """Set session metadata to include in crash report."""
    global _session_info
    _session_info.update(info)


def _mark_node_idle() -> None:
    """Mark this node's status as idle in the swarm."""
    try:
        from swarm_lib import update_status, get_status

        current = get_status()
        if current:
            update_status(
                state="idle",
                current_task="",
                project="",
                session_id="",
                model="",
            )
            logger.info("Node marked idle")
    except Exception as exc:
        logger.warning("Failed to mark node idle: %s", exc)


def _release_claimed_tasks() -> list[str]:
    """Requeue any claimed tasks back to pending.

    Returns list of requeued task IDs.
    """
    requeued = []
    try:
        import yaml
        from pathlib import Path

        claimed_dir = Path("/var/lib/swarm/tasks/claimed")
        if not claimed_dir.is_dir():
            return requeued

        for task_file in claimed_dir.glob("*.yaml"):
            try:
                with open(task_file) as f:
                    task = yaml.safe_load(f) or {}

                task_id = task.get("id", task_file.stem)
                task.pop("claimed_by", None)
                task.pop("claimed_at", None)
                retries = task.get("_retries", 0) + 1
                task["_retries"] = retries

                pending_dir = Path("/var/lib/swarm/tasks/pending")
                pending_dir.mkdir(parents=True, exist_ok=True)
                pending_file = pending_dir / f"{task_id}.yaml"

                # Write atomically to pending/ FIRST (safe ordering)
                _atomic_write_yaml(pending_file, task)

                os.remove(task_file)
                requeued.append(task_id)
                logger.info("Requeued task %s on crash", task_id)
            except Exception as exc:
                logger.warning("Failed to requeue %s: %s", task_file, exc)

        return requeued
    except Exception as exc:
        logger.warning("Failed to release claimed tasks: %s", exc)
        return requeued


def _write_session_summary(signal_num: Optional[int] = None) -> None:
    """Write a session summary to disk when exiting.

    Location: /opt/claude-swarm/data/crash-summaries/<timestamp>-<pid>.yaml
    """
    try:
        import yaml

        summary_dir = Path("/opt/claude-swarm/data/crash-summaries")
        summary_dir.mkdir(parents=True, exist_ok=True)

        pid = os.getpid()
        hostname = os.uname().nodename
        timestamp = _now_iso().replace(":", "").replace("-", "")[:14]

        summary = {
            "hostname": hostname,
            "pid": pid,
            "exit_time": _now_iso(),
            "exit_signal": signal_num,
            "session_info": _session_info,
        }

        summary_file = summary_dir / f"{timestamp}-{pid}.yaml"
        with open(summary_file, "w") as f:
            yaml.dump(summary, f, default_flow_style=False, sort_keys=False)

        logger.info("Session summary written to %s", summary_file)
    except Exception as exc:
        logger.error("Failed to write session summary: %s", exc)


def _handle_crash(signum: int, frame: Optional[object]) -> None:
    """Signal handler for SIGTERM, SIGINT, SIGHUP.

    Gracefully shuts down: mark idle, release tasks, write summary.
    """
    logger.info("Received signal %d — initiating graceful shutdown", signum)

    # Run registered callbacks in reverse order
    for callback in reversed(_crash_callbacks):
        try:
            callback()
        except Exception as exc:
            logger.warning("Crash callback failed: %s", exc)

    # Core shutdown sequence
    _mark_node_idle()
    requeued = _release_claimed_tasks()
    _write_session_summary(signum)

    logger.info("Graceful shutdown complete — requeued %d tasks", len(requeued))
    sys.exit(0)


def _handle_atexit() -> None:
    """atexit handler as fallback for normal exits."""
    try:
        # Only write summary if we haven't already done so via signal handler
        summary_dir = Path("/opt/claude-swarm/data/crash-summaries")
        if summary_dir.exists():
            # Check if a summary was written recently (within 5 seconds)
            import time

            now = time.time()
            for f in sorted(summary_dir.glob("*.yaml"), reverse=True)[:1]:
                try:
                    mtime = f.stat().st_mtime
                    if now - mtime < 5:
                        # Recently written — don't write again
                        return
                except OSError:
                    pass

        # No recent summary — write one for normal exit
        _write_session_summary()
    except Exception as exc:
        logger.warning("atexit handler failed: %s", exc)


def install_crash_handlers() -> None:
    """Install signal handlers and atexit hook for graceful shutdown."""
    signal.signal(signal.SIGTERM, _handle_crash)
    signal.signal(signal.SIGINT, _handle_crash)
    signal.signal(signal.SIGHUP, _handle_crash)

    atexit.register(_handle_atexit)

    logger.info(
        "Crash handlers installed — SIGTERM/SIGINT/SIGHUP will trigger graceful shutdown"
    )
