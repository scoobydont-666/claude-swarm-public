"""IPC observability — Prometheus metric collection."""

from __future__ import annotations

from . import transport
from .agent import _K_INDEX, _K_INBOX, list_agents
from .channels import _K_CHANNELS_INDEX, _K_CHANNEL_SUBS, list_channels
from .dlq import dlq_depth


def collect() -> dict:
    """Collect all IPC metrics.

    Returns a dict of metric_name -> value, suitable for Prometheus exposition.
    """
    r = transport.get_client()
    metrics = {}

    # Counter metrics from Redis
    for key in (
        "ipc:metrics:sent",
        "ipc:metrics:delivered",
        "ipc:metrics:dlq",
        "ipc:metrics:rpc_sent",
        "ipc:metrics:rpc_timeout",
    ):
        val = r.get(key)
        name = key.replace("ipc:metrics:", "ipc_") + "_total"
        metrics[name] = int(val) if val else 0

    # Gauge: online agents
    agents = list_agents()
    metrics["ipc_agents_online"] = len(agents)

    # Gauge: per-agent inbox depth
    for agent in agents:
        aid = agent.get("agent_id", "")
        depth = transport.stream_len(f"{_K_INBOX}{aid}")
        metrics[f'ipc_inbox_depth{{agent_id="{aid}"}}'] = depth

    # Gauge: DLQ depth
    metrics["ipc_dlq_depth"] = dlq_depth()

    # Gauge: channel stats
    channels = list_channels()
    for ch in channels:
        name = ch["name"]
        metrics[f'ipc_channel_subscribers{{channel="{name}"}}'] = ch["subscribers"]
        metrics[f'ipc_channel_messages{{channel="{name}"}}'] = ch["messages"]

    # Gauge: pending RPCs
    pending_count = r.zcard("ipc:rpc:pending")
    metrics["ipc_rpc_pending"] = pending_count

    return metrics


def prometheus_text() -> str:
    """Format metrics as Prometheus text exposition."""
    data = collect()
    lines = []
    for key, value in sorted(data.items()):
        # Extract base metric name for HELP/TYPE (strip labels)
        base = key.split("{")[0]
        lines.append(f"{key} {value}")
    return "\n".join(lines) + "\n"


def reset_counters() -> None:
    """Reset all IPC metric counters. Useful for testing."""
    r = transport.get_client()
    keys = r.keys("ipc:metrics:*")
    if keys:
        r.delete(*keys)
