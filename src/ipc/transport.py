"""Low-level Redis transport operations for IPC streams."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure claude-swarm src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from redis_client import get_client

# Stream limits
INBOX_MAXLEN = 5000
CHANNEL_MAXLEN = 10000
DLQ_MAXLEN = 5000


def stream_add(key: str, fields: dict[str, str], maxlen: int | None = None) -> str:
    """XADD to a stream. Returns the stream entry ID."""
    r = get_client()
    kwargs: dict[str, Any] = {}
    if maxlen is not None:
        kwargs["maxlen"] = maxlen
        kwargs["approximate"] = True
    return r.xadd(key, fields, **kwargs)


def stream_read_group(
    key: str,
    group: str,
    consumer: str,
    count: int = 10,
    block_ms: int | None = None,
    pending: bool = False,
) -> list[tuple[str, dict]]:
    """XREADGROUP from a stream.

    Args:
        key: Stream key
        group: Consumer group name
        consumer: Consumer name (agent_id)
        count: Max entries to return
        block_ms: Block timeout in ms (None = don't block)
        pending: If True, read pending (unacked) messages first

    Returns:
        List of (stream_id, fields_dict) tuples
    """
    r = get_client()
    entry_id = "0" if pending else ">"
    streams = {key: entry_id}
    kwargs: dict[str, Any] = {"count": count}
    if block_ms is not None:
        kwargs["block"] = block_ms
    result = r.xreadgroup(group, consumer, streams, **kwargs)
    if not result:
        return []
    # result is [(key, [(id, fields), ...])]
    return result[0][1]


def stream_ack(key: str, group: str, *message_ids: str) -> int:
    """XACK messages in a consumer group. Returns count acknowledged."""
    if not message_ids:
        return 0
    r = get_client()
    return r.xack(key, group, *message_ids)


def stream_pending(
    key: str, group: str, count: int = 100, min_idle_ms: int = 0
) -> list[dict]:
    """XPENDING detail: messages that haven't been acked.

    Returns list of dicts with: message_id, consumer, idle_ms, delivery_count.
    """
    r = get_client()
    try:
        raw = r.xpending_range(key, group, "-", "+", count)
    except Exception:
        return []
    results = []
    for entry in raw:
        idle = entry.get("time_since_delivered", 0)
        if min_idle_ms and idle < min_idle_ms:
            continue
        results.append({
            "message_id": entry.get("message_id", ""),
            "consumer": entry.get("consumer", ""),
            "idle_ms": idle,
            "delivery_count": entry.get("times_delivered", 0),
        })
    return results


def stream_claim(
    key: str, group: str, consumer: str, min_idle_ms: int, *message_ids: str
) -> list[tuple[str, dict]]:
    """XCLAIM: take ownership of pending messages from another consumer."""
    if not message_ids:
        return []
    r = get_client()
    return r.xclaim(key, group, consumer, min_idle_ms, list(message_ids))


def stream_len(key: str) -> int:
    """XLEN: number of entries in a stream."""
    r = get_client()
    return r.xlen(key)


def stream_trim(key: str, maxlen: int, approximate: bool = True) -> int:
    """XTRIM: trim a stream to maxlen."""
    r = get_client()
    return r.xtrim(key, maxlen=maxlen, approximate=approximate)


def ensure_consumer_group(key: str, group: str) -> bool:
    """Create a consumer group on a stream, creating the stream if needed.

    Returns True if created, False if already exists.
    """
    r = get_client()
    try:
        r.xgroup_create(key, group, id="0", mkstream=True)
        return True
    except Exception as e:
        if "BUSYGROUP" in str(e):
            return False
        raise


def delete_consumer_group(key: str, group: str) -> bool:
    """Delete a consumer group."""
    r = get_client()
    try:
        r.xgroup_destroy(key, group)
        return True
    except Exception:
        return False


def list_wait(key: str, timeout: int = 0) -> str | None:
    """BLPOP on a list key. Returns the value or None on timeout."""
    r = get_client()
    result = r.blpop(key, timeout=timeout)
    if result:
        return result[1]
    return None


def list_push(key: str, value: str) -> int:
    """LPUSH to a list. Returns list length."""
    r = get_client()
    return r.lpush(key, value)


def eval_lua(script: str, num_keys: int, *args: str) -> Any:
    """Execute a Lua script."""
    r = get_client()
    return r.eval(script, num_keys, *args)
