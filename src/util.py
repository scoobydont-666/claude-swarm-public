"""Shared utilities for claude-swarm — single source of truth for common functions."""

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ts() -> float:
    """Return current UTC time as Unix timestamp."""
    return datetime.now(timezone.utc).timestamp()


def hostname() -> str:
    """Return the current hostname."""
    return socket.gethostname()


def relative_time(ts) -> str:
    """Convert timestamp (ISO string or Unix float) to human-readable relative time."""
    if not ts:
        return "?"
    try:
        if isinstance(ts, (int, float)):
            age = time.time() - ts
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).total_seconds()
        if age < 0:
            return "just now"
        if age < 60:
            return f"{int(age)}s ago"
        if age < 3600:
            return f"{int(age // 60)}m ago"
        if age < 86400:
            return f"{age / 3600:.1f}h ago"
        return f"{age / 86400:.1f}d ago"
    except (ValueError, TypeError, AttributeError):
        return str(ts) if ts else "?"


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: write to .tmp, then rename."""
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.rename(tmp_path, path)


def atomic_write_yaml(path: Path, data: dict) -> None:
    """Write YAML atomically: write to .tmp, then rename."""
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.rename(tmp_path, path)


def swarm_root() -> Path:
    """Return the swarm root directory. Checks NFS mount first, falls back to local."""
    nfs_path = Path("/var/lib/swarm")
    if nfs_path.is_dir():
        return nfs_path
    local_path = Path.home() / ".swarm"
    local_path.mkdir(parents=True, exist_ok=True)
    return local_path


def load_swarm_config() -> dict[str, Any]:
    """Load swarm.yaml configuration."""
    config_paths = [
        swarm_root() / "config" / "swarm.yaml",
        Path("/opt/claude-swarm/config/swarm.yaml"),
    ]
    for p in config_paths:
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f) or {}
    return {}


def projects_for_host(host: str = "") -> list[str]:
    """Get the list of project paths for a given host from swarm.yaml.

    Returns absolute paths (/opt/<project>) for the specified host.
    Falls back to all unique projects across all nodes if host not found.
    """
    host = host or hostname()
    config = load_swarm_config()
    nodes = config.get("nodes", {})

    # Try exact match (case-insensitive)
    for name, info in nodes.items():
        if name.lower() == host.lower():
            return [f"/opt/{p}" for p in info.get("projects", [])]

    # Fallback: union of all projects
    all_projects: set[str] = set()
    for info in nodes.values():
        for p in info.get("projects", []):
            all_projects.add(f"/opt/{p}")
    return sorted(all_projects)


def fleet_from_config() -> dict[str, dict[str, Any]]:
    """Load fleet node definitions from swarm.yaml. Single source of truth."""
    config = load_swarm_config()
    nodes = config.get("nodes", {})
    fleet = {}
    for name, info in nodes.items():
        ip = info.get("ip", "")
        if ip and ip != "TBD":
            fleet[name] = {
                "ip": ip,
                "user": "josh",
                "capabilities": info.get("capabilities", []),
                "projects": info.get("projects", []),
                "role": info.get("role", "client"),
            }
    return fleet
