"""Centralized config loader — single source of truth for claude-swarm configuration.

Loads config/swarm.yaml with environment variable overrides using ${VAR} syntax.
Provides typed accessors for common config sections.

NAI Swarm backport: consolidates scattered config reads into one module.

Usage:
    from config_loader import get_config, get_node_config, get_fleet

    config = get_config()
    fleet = get_fleet()
    node = get_node_config("GIGA")
"""

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

LOG = logging.getLogger(__name__)

# Config search paths in priority order
CONFIG_PATHS = [
    Path("/opt/swarm/config/swarm.yaml"),
    Path("/opt/claude-swarm/config/swarm.yaml"),
]

# Environment variable override pattern: ${VAR_NAME} or ${VAR_NAME:-default}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

_cached_config: dict | None = None
_cached_mtime: float = 0.0
_cached_path: Path | None = None


def _resolve_env_vars(value: str) -> str:
    """Resolve ${VAR} and ${VAR:-default} patterns in a string.

    Args:
        value: String potentially containing env var references.

    Returns:
        String with env vars resolved.
    """
    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)
        env_val = os.environ.get(var_name)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        return match.group(0)  # Leave unresolved if no default

    return _ENV_PATTERN.sub(_replace, value)


def _walk_resolve(obj: Any) -> Any:
    """Recursively resolve env vars in all string values in a nested structure.

    Args:
        obj: Any YAML-parsed object (dict, list, str, int, etc.).

    Returns:
        Same structure with env vars resolved in string values.
    """
    if isinstance(obj, dict):
        return {k: _walk_resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_resolve(item) for item in obj]
    if isinstance(obj, str):
        return _resolve_env_vars(obj)
    return obj


def _find_config() -> Path | None:
    """Find the first existing config file from CONFIG_PATHS.

    Also checks SWARM_CONFIG env var for override.
    """
    env_path = os.environ.get("SWARM_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    for p in CONFIG_PATHS:
        if p.exists():
            return p
    return None


def load_config(force_reload: bool = False) -> dict:
    """Load swarm configuration with env var resolution.

    Caches the result and reloads if the file has been modified.

    Args:
        force_reload: Force re-read from disk.

    Returns:
        Resolved config dict.
    """
    global _cached_config, _cached_mtime, _cached_path

    config_path = _find_config()
    if config_path is None:
        LOG.warning("No swarm config found in: %s", CONFIG_PATHS)
        return {}

    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return _cached_config or {}

    if not force_reload and _cached_config is not None and _cached_path == config_path and mtime == _cached_mtime:
        return _cached_config

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        LOG.error("Failed to load config from %s: %s", config_path, e)
        return _cached_config or {}

    resolved = _walk_resolve(raw)
    _cached_config = resolved
    _cached_mtime = mtime
    _cached_path = config_path
    LOG.debug("Loaded swarm config from %s", config_path)
    return resolved


def get_config() -> dict:
    """Get the current swarm configuration (cached, auto-reloads on file change)."""
    return load_config()


# ── Typed Accessors ──────────────────────────────────────────────────────────


def get_nodes() -> dict[str, dict]:
    """Get all node definitions from config."""
    return get_config().get("nodes", {})


def get_node_config(hostname: str) -> dict:
    """Get config for a specific node by hostname (case-insensitive).

    Args:
        hostname: Node hostname (e.g., "GIGA", "miniboss").

    Returns:
        Node config dict, or empty dict if not found.
    """
    nodes = get_nodes()
    # Exact match first
    if hostname in nodes:
        return nodes[hostname]
    # Case-insensitive
    for name, info in nodes.items():
        if name.lower() == hostname.lower():
            return info
    return {}


def get_fleet() -> dict[str, dict]:
    """Get fleet node definitions suitable for dispatch routing.

    Returns:
        Dict of hostname → {ip, user, capabilities, projects, role}.
    """
    nodes = get_nodes()
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


def get_auto_dispatch_config() -> dict:
    """Get auto_dispatch section."""
    return get_config().get("auto_dispatch", {})


def get_health_monitor_config() -> dict:
    """Get health_monitor section."""
    return get_config().get("health_monitor", {})


def get_nfs_config() -> dict:
    """Get NFS section."""
    return get_config().get("nfs", {})


def get_capability_routing() -> dict[str, list[str]]:
    """Get capability → hosts routing table from decomposition config."""
    decomp = get_config().get("decomposition", {})
    return decomp.get("capability_routing", {})


def get_swarm_root() -> Path:
    """Get the swarm root path from NFS config."""
    nfs = get_nfs_config()
    mount = nfs.get("mount_point", "/opt/swarm")
    p = Path(mount)
    if p.is_dir():
        return p
    return Path("/opt/swarm")
