"""Sync Engine — automated git sync and config propagation.

Handles:
- Git auto-pull when commit events arrive from other hosts
- Config sync (collect → push → pull → install)
- Project state detection across hosts
"""

from __future__ import annotations

import concurrent.futures
import logging
import socket
import subprocess
from pathlib import Path
from typing import Any

try:
    from events_redis import emit
except (ImportError, Exception):
    from events import emit
from util import projects_for_host

logger = logging.getLogger(__name__)

CLAUDE_CONFIG_DIR = "/opt/claude-configs/claude-config"


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a command, return result. Never raises."""
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as e:
        return subprocess.CompletedProcess(cmd, returncode=1, stderr=str(e))


def _is_git_repo(path: str) -> bool:
    return Path(path).joinpath(".git").is_dir()


def git_pull(project_path: str) -> dict[str, Any]:
    """Pull latest changes for a project."""
    if not _is_git_repo(project_path):
        return {"status": "skip", "reason": "not a git repo"}

    result = _run(["git", "pull", "--rebase", "--autostash"], cwd=project_path)
    return {
        "status": "ok" if result.returncode == 0 else "error",
        "stdout": result.stdout.strip()[:200],
        "stderr": result.stderr.strip()[:200],
    }


def git_push(project_path: str) -> dict[str, Any]:
    """Push local commits for a project."""
    if not _is_git_repo(project_path):
        return {"status": "skip", "reason": "not a git repo"}

    # Check if there's anything to push
    result = _run(["git", "status", "--porcelain"], cwd=project_path)
    if result.stdout.strip():
        return {"status": "skip", "reason": "uncommitted changes"}

    result = _run(["git", "push"], cwd=project_path)
    if result.returncode == 0:
        # Emit commit event
        log = _run(["git", "log", "-1", "--format=%H %s"], cwd=project_path)
        if log.stdout.strip():
            parts = log.stdout.strip().split(" ", 1)
            emit(
                "commit",
                project=project_path,
                details={
                    "commit": parts[0],
                    "message": parts[1] if len(parts) > 1 else "",
                },
            )
    return {
        "status": "ok" if result.returncode == 0 else "error",
        "output": (result.stdout + result.stderr).strip()[:200],
    }


def pull_all_projects() -> dict[str, Any]:
    """Pull all projects on this host (parallel, up to 5 concurrent)."""
    projects = projects_for_host()

    existing = [p for p in projects if Path(p).is_dir()]
    results: dict[str, Any] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_proj = {executor.submit(git_pull, proj): proj for proj in existing}
        try:
            for future in concurrent.futures.as_completed(future_to_proj, timeout=120):
                proj = future_to_proj[future]
                try:
                    results[proj] = future.result()
                except Exception as exc:
                    logger.error("pull failed for %s: %s", proj, exc)
                    results[proj] = {"status": "error", "stderr": str(exc)}
        except concurrent.futures.TimeoutError:
            for future, proj in future_to_proj.items():
                if proj not in results:
                    future.cancel()
                    results[proj] = {
                        "status": "error",
                        "stderr": "timeout waiting for futures",
                    }

    return results


def push_all_dirty() -> dict[str, Any]:
    """Push all projects that have local commits ahead of remote (parallel, up to 5 concurrent)."""
    projects = projects_for_host()

    def _fetch_and_push(proj: str) -> tuple[str, dict[str, Any] | None]:
        """Fetch, check if ahead, push if so. Returns (proj, result_or_None)."""
        if not Path(proj).is_dir() or not _is_git_repo(proj):
            return proj, None
        _run(["git", "fetch", "--quiet"], cwd=proj, timeout=10)
        status = _run(["git", "status", "-sb"], cwd=proj)
        if "ahead" in status.stdout:
            return proj, git_push(proj)
        return proj, None

    results: dict[str, Any] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_proj = {executor.submit(_fetch_and_push, proj): proj for proj in projects}
        try:
            for future in concurrent.futures.as_completed(future_to_proj, timeout=120):
                proj = future_to_proj[future]
                try:
                    _, result = future.result()
                    if result is not None:
                        results[proj] = result
                except Exception as exc:
                    logger.error("push failed for %s: %s", proj, exc)
                    results[proj] = {"status": "error", "stderr": str(exc)}
        except concurrent.futures.TimeoutError:
            for future, proj in future_to_proj.items():
                if proj not in results:
                    future.cancel()
                    results[proj] = {
                        "status": "error",
                        "stderr": "timeout waiting for futures",
                    }

    return results


def sync_config() -> dict[str, Any]:
    """Collect, push, and install claude-config."""
    results = {}

    # Collect
    collect = _run(
        [f"{CLAUDE_CONFIG_DIR}/scripts/collect.sh"],
        cwd=CLAUDE_CONFIG_DIR,
    )
    results["collect"] = collect.returncode == 0

    # Git add + commit + push
    _run(["git", "add", "-A"], cwd=CLAUDE_CONFIG_DIR)
    diff = _run(["git", "diff", "--cached", "--stat"], cwd=CLAUDE_CONFIG_DIR)
    if diff.stdout.strip():
        _run(
            ["git", "commit", "-m", f"sync: auto-sync from {socket.gethostname()}"],
            cwd=CLAUDE_CONFIG_DIR,
        )
        push = _run(["git", "push"], cwd=CLAUDE_CONFIG_DIR)
        results["push"] = push.returncode == 0
    else:
        results["push"] = "no changes"

    emit("config_sync", details=results)
    return results


def get_dirty_repos() -> list[dict[str, Any]]:
    """List repos with uncommitted changes."""
    projects = projects_for_host()

    dirty = []
    for proj in projects:
        if not Path(proj).is_dir() or not _is_git_repo(proj):
            continue
        result = _run(["git", "status", "--porcelain"], cwd=proj)
        lines = result.stdout.strip().splitlines()
        if lines:
            dirty.append(
                {
                    "project": proj,
                    "files": len(lines),
                    "changes": lines[:5],
                }
            )
    return dirty


def process_commit_events(since: str) -> dict[str, list[str]]:
    """Pull repos that got commits from other hosts since a timestamp.

    Returns dict of {project: [commit messages pulled]}.
    """
    try:
        from events_redis import query
    except (ImportError, Exception):
        from events import query

    hostname = socket.gethostname()
    commits = query(since=since, event_type="commit")

    # Filter: only commits from OTHER hosts for repos WE have locally
    to_pull = set()
    for event in commits:
        if event.get("hostname") != hostname:
            proj = event.get("project", "")
            if proj and Path(proj).is_dir():
                to_pull.add(proj)

    pulled = {}
    for proj in to_pull:
        result = git_pull(proj)
        if result.get("status") == "ok":
            pulled[proj] = [result.get("stdout", "")]

    return pulled
