"""
Worktree Dispatch — Git worktree isolation per agent dispatch.

Creates a temporary git worktree for each dispatch so agents work on isolated
branches. On completion, merges back to the main branch and cleans up.

Patterns from Cursor Background Agents and Claude Code Agent Teams.
"""

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

WORKTREE_BASE = "/tmp/swarm-worktrees"
BRANCH_PREFIX = "swarm"


@dataclass
class WorktreeInfo:
    """Info about a created worktree."""

    path: str
    branch: str
    repo_path: str
    dispatch_id: str
    created_at: float


def create_worktree(
    repo_path: str,
    dispatch_id: str,
    host: str = "localhost",
    base_branch: str = "main",
    worktree_base: str = WORKTREE_BASE,
) -> WorktreeInfo | None:
    """Create a git worktree for isolated agent work.

    Args:
        repo_path: Path to the git repository
        dispatch_id: Unique dispatch identifier (used in branch name)
        host: Target host (localhost = local, otherwise SSH)
        base_branch: Branch to base the worktree on
        worktree_base: Base directory for worktrees

    Returns:
        WorktreeInfo if successful, None if failed
    """
    # Sanitize dispatch_id for use as branch name
    safe_id = dispatch_id.replace("/", "-").replace(" ", "-")[:60]
    branch = f"{BRANCH_PREFIX}/{safe_id}"
    worktree_path = f"{worktree_base}/{safe_id}"

    cmds = [
        f"mkdir -p {shlex.quote(worktree_base)}",
        f"cd {shlex.quote(repo_path)}",
        f"git worktree add {shlex.quote(worktree_path)} -b {shlex.quote(branch)} {shlex.quote(base_branch)} 2>&1",
    ]
    full_cmd = " && ".join(cmds)

    try:
        if host == "localhost" or host.lower() in ("mega", "$(hostname)"):
            result = subprocess.run(
                ["bash", "-c", full_cmd],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", f"josh@{host}", full_cmd],
                capture_output=True,
                text=True,
                timeout=30,
            )

        if result.returncode == 0:
            logger.info(f"Worktree created: {host}:{worktree_path} (branch {branch})")
            return WorktreeInfo(
                path=worktree_path,
                branch=branch,
                repo_path=repo_path,
                dispatch_id=dispatch_id,
                created_at=time.time(),
            )
        else:
            logger.warning(f"Worktree creation failed on {host}: {result.stderr.strip()}")
            return None

    except subprocess.TimeoutExpired:
        logger.warning(f"Worktree creation timed out on {host}")
        return None
    except Exception as e:
        logger.warning(f"Worktree creation error on {host}: {e}")
        return None


def merge_worktree(
    worktree: WorktreeInfo,
    host: str = "localhost",
    target_branch: str = "main",
    auto_merge: bool = True,
) -> bool:
    """Merge worktree branch back to target and clean up.

    Args:
        worktree: WorktreeInfo from create_worktree
        host: Target host
        target_branch: Branch to merge into
        auto_merge: If True, attempt automatic merge. If False, just create a PR-ready branch.

    Returns:
        True if merge successful, False otherwise
    """
    if auto_merge:
        cmds = [
            f"cd {shlex.quote(worktree.repo_path)}",
            f"git checkout {shlex.quote(target_branch)}",
            f"git merge --no-ff {shlex.quote(worktree.branch)} -m 'swarm: merge {worktree.dispatch_id}'",
        ]
    else:
        # Just leave the branch for manual review/PR
        cmds = [
            f"cd {shlex.quote(worktree.repo_path)}",
            f"git checkout {shlex.quote(target_branch)}",
        ]

    full_cmd = " && ".join(cmds)

    try:
        if host == "localhost" or host.lower() in ("mega", "$(hostname)"):
            result = subprocess.run(
                ["bash", "-c", full_cmd],
                capture_output=True,
                text=True,
                timeout=60,
            )
        else:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", f"josh@{host}", full_cmd],
                capture_output=True,
                text=True,
                timeout=60,
            )

        if result.returncode == 0:
            logger.info(f"Worktree merged: {worktree.branch} → {target_branch}")
            # Clean up worktree
            cleanup_worktree(worktree, host)
            return True
        else:
            logger.warning(f"Merge failed: {result.stderr.strip()}")
            # Don't cleanup on merge failure — preserve for manual resolution
            return False

    except Exception as e:
        logger.warning(f"Merge error: {e}")
        return False


def cleanup_worktree(
    worktree: WorktreeInfo,
    host: str = "localhost",
) -> bool:
    """Remove a worktree and its branch."""
    cmds = [
        f"cd {shlex.quote(worktree.repo_path)}",
        f"git worktree remove {shlex.quote(worktree.path)} --force 2>/dev/null || rm -rf {shlex.quote(worktree.path)}",
        f"git branch -D {shlex.quote(worktree.branch)} 2>/dev/null || true",
    ]
    full_cmd = " && ".join(cmds)

    try:
        if host == "localhost" or host.lower() in ("mega", "$(hostname)"):
            subprocess.run(["bash", "-c", full_cmd], capture_output=True, timeout=15)
        else:
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", f"josh@{host}", full_cmd],
                capture_output=True,
                timeout=15,
            )
        logger.info(f"Worktree cleaned: {worktree.path}")
        return True
    except Exception as e:
        logger.warning(f"Worktree cleanup failed: {e}")
        return False


def list_worktrees(repo_path: str, host: str = "localhost") -> list[str]:
    """List active worktrees for a repo."""
    cmd = f"cd {shlex.quote(repo_path)} && git worktree list --porcelain"
    try:
        if host == "localhost":
            result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=10)
        else:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", f"josh@{host}", cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
        if result.returncode == 0:
            return [
                line.split(" ", 1)[1]
                for line in result.stdout.splitlines()
                if line.startswith("worktree ")
            ]
    except Exception:
        pass
    return []
