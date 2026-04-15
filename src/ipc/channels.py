"""Channel-based pub/sub messaging via Redis Streams."""

from __future__ import annotations

from . import transport
from .agent import get_current_agent_id
from .envelope import Envelope

# Key patterns
_K_CHANNEL = "ipc:channel:"
_K_CHANNELS_INDEX = "ipc:channels:index"
_K_CHANNEL_SUBS = "ipc:channel:subs:"

_CHANNEL_MAXLEN = 10000


def create(name: str) -> bool:
    """Create a channel. Idempotent.

    Returns True if created, False if already exists.
    """
    r = transport.get_client()
    added = r.sadd(_K_CHANNELS_INDEX, name)
    # Ensure stream exists
    transport.ensure_consumer_group(f"{_K_CHANNEL}{name}", f"cg:{name}")
    return bool(added)


def delete(name: str) -> bool:
    """Delete a channel and its subscribers."""
    r = transport.get_client()
    pipe = r.pipeline()
    pipe.srem(_K_CHANNELS_INDEX, name)
    pipe.delete(f"{_K_CHANNEL}{name}")
    pipe.delete(f"{_K_CHANNEL_SUBS}{name}")
    results = pipe.execute()
    return bool(results[0])


def subscribe(channel: str, agent_id: str | None = None) -> bool:
    """Subscribe to a channel.

    Creates a consumer group entry for this agent.
    """
    agent_id = agent_id or get_current_agent_id()
    if not agent_id:
        raise RuntimeError("Not registered — call ipc.register() first")

    r = transport.get_client()

    # Ensure channel exists
    create(channel)

    # Add to subscriber set
    r.sadd(f"{_K_CHANNEL_SUBS}{channel}", agent_id)
    return True


def unsubscribe(channel: str, agent_id: str | None = None) -> bool:
    """Unsubscribe from a channel."""
    agent_id = agent_id or get_current_agent_id()
    if not agent_id:
        return False

    r = transport.get_client()
    return bool(r.srem(f"{_K_CHANNEL_SUBS}{channel}", agent_id))


def publish(
    channel: str,
    payload: dict | str,
    priority: int = 3,
    ttl: int = 0,
    sender: str | None = None,
) -> str:
    """Publish a message to a channel.

    Returns the envelope ID.
    """
    sender = sender or get_current_agent_id()
    if not sender:
        raise RuntimeError("Not registered — call ipc.register() first")

    if isinstance(payload, str):
        payload = {"text": payload}

    env = Envelope(
        sender=sender,
        recipient=channel,
        message_type="channel",
        payload=payload,
        priority=priority,
        ttl=ttl,
    )

    transport.stream_add(
        f"{_K_CHANNEL}{channel}",
        {"envelope": env.to_json()},
        maxlen=_CHANNEL_MAXLEN,
    )

    # Increment metrics
    r = transport.get_client()
    r.incr("ipc:metrics:sent")
    r.incr("ipc:metrics:delivered")

    return env.id


def consume(
    channel: str,
    count: int = 10,
    block_ms: int | None = None,
    agent_id: str | None = None,
    auto_ack: bool = True,
) -> list[Envelope]:
    """Consume messages from a subscribed channel.

    Uses XREADGROUP with a shared consumer group — each message is
    delivered to exactly one subscriber (competing consumers pattern).

    For broadcast to ALL subscribers, use broadcast channels where
    each subscriber has its own consumer group.
    """
    agent_id = agent_id or get_current_agent_id()
    if not agent_id:
        raise RuntimeError("Not registered — call ipc.register() first")

    stream_key = f"{_K_CHANNEL}{channel}"
    group = f"cg:{channel}"

    # Read pending first, then new
    entries = transport.stream_read_group(
        stream_key, group, agent_id, count=count, pending=True
    )
    remaining = count - len(entries)
    if remaining > 0:
        new = transport.stream_read_group(
            stream_key, group, agent_id, count=remaining, block_ms=block_ms
        )
        entries.extend(new)

    if not entries:
        return []

    envelopes = []
    ack_ids = []
    for stream_id, fields in entries:
        raw = fields.get("envelope", "")
        if not raw:
            ack_ids.append(stream_id)
            continue
        try:
            env = Envelope.from_json(raw)
            if env.is_expired():
                ack_ids.append(stream_id)
                continue
            envelopes.append(env)
            ack_ids.append(stream_id)
        except Exception:
            ack_ids.append(stream_id)

    if auto_ack and ack_ids:
        transport.stream_ack(stream_key, group, *ack_ids)

    return envelopes


def list_channels() -> list[dict]:
    """List all channels with subscriber counts and stream lengths."""
    r = transport.get_client()
    names = r.smembers(_K_CHANNELS_INDEX)
    if not names:
        return []

    channels = []
    for name in sorted(names):
        sub_count = r.scard(f"{_K_CHANNEL_SUBS}{name}")
        stream_len = transport.stream_len(f"{_K_CHANNEL}{name}")
        channels.append({
            "name": name,
            "subscribers": sub_count,
            "messages": stream_len,
        })
    return channels


def get_subscribers(channel: str) -> set[str]:
    """Get set of agent IDs subscribed to a channel."""
    r = transport.get_client()
    return r.smembers(f"{_K_CHANNEL_SUBS}{channel}")
