"""Direct agent-to-agent messaging via Redis Streams."""

from __future__ import annotations

import time

from . import _lua_scripts, transport
from .agent import _K_INBOX, _K_INBOX_GROUP, _K_INDEX, get_current_agent_id
from .envelope import Envelope

# Stream limits
_INBOX_MAXLEN = 5000
_DLQ_MAXLEN = 5000


def send(
    recipient: str,
    payload: dict | str,
    priority: int = 3,
    ttl: int = 300,
    sender: str | None = None,
) -> tuple[bool, str]:
    """Send a direct message to another agent.

    Args:
        recipient: Target agent_id
        payload: Message content (dict or string, auto-wrapped)
        priority: 0-5
        ttl: Seconds until expiry (0 = never)
        sender: Override sender ID (defaults to current agent)

    Returns:
        (delivered, envelope_id) — delivered is True if recipient was online
    """
    sender = sender or get_current_agent_id()
    if not sender:
        raise RuntimeError("Not registered — call ipc.register() first")

    if isinstance(payload, str):
        payload = {"text": payload}

    env = Envelope(
        sender=sender,
        recipient=recipient,
        message_type="direct",
        payload=payload,
        priority=priority,
        ttl=ttl,
    )

    result = transport.eval_lua(
        _lua_scripts.SEND_ATOMIC,
        6,
        _K_INDEX,
        f"{_K_INBOX}{recipient}",
        "ipc:dlq",
        "ipc:metrics:sent",
        "ipc:metrics:delivered",
        "ipc:metrics:dlq",
        env.to_json(),
        recipient,
        str(_INBOX_MAXLEN),
        str(_DLQ_MAXLEN),
    )
    return (result == 1, env.id)


def recv(
    agent_id: str | None = None,
    count: int = 10,
    block_ms: int | None = None,
    auto_ack: bool = True,
) -> list[Envelope]:
    """Receive messages from own inbox.

    Args:
        agent_id: Override agent ID (defaults to current)
        count: Max messages to return
        block_ms: Block timeout in ms (None = don't block)
        auto_ack: Automatically acknowledge messages

    Returns:
        List of Envelope objects
    """
    agent_id = agent_id or get_current_agent_id()
    if not agent_id:
        raise RuntimeError("Not registered — call ipc.register() first")

    inbox_key = f"{_K_INBOX}{agent_id}"

    # First read any pending (unacked) messages
    entries = transport.stream_read_group(
        inbox_key, _K_INBOX_GROUP, agent_id, count=count, pending=True
    )

    # Then read new messages
    remaining = count - len(entries)
    if remaining > 0:
        new_entries = transport.stream_read_group(
            inbox_key,
            _K_INBOX_GROUP,
            agent_id,
            count=remaining,
            block_ms=block_ms,
        )
        entries.extend(new_entries)

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
            # Skip expired messages
            if env.is_expired():
                ack_ids.append(stream_id)
                continue
            envelopes.append(env)
            ack_ids.append(stream_id)
        except Exception:
            ack_ids.append(stream_id)

    if auto_ack and ack_ids:
        transport.stream_ack(inbox_key, _K_INBOX_GROUP, *ack_ids)

    return envelopes


def recv_iter(
    agent_id: str | None = None,
    timeout: float = 30.0,
    batch_size: int = 5,
):
    """Generator that yields envelopes until timeout.

    Yields Envelope objects as they arrive.
    """
    agent_id = agent_id or get_current_agent_id()
    if not agent_id:
        raise RuntimeError("Not registered — call ipc.register() first")

    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining_ms = int((deadline - time.time()) * 1000)
        if remaining_ms <= 0:
            break
        block = min(remaining_ms, 2000)  # Poll every 2s max
        messages = recv(agent_id, count=batch_size, block_ms=block)
        yield from messages


def broadcast(
    payload: dict | str,
    priority: int = 3,
    ttl: int = 300,
    sender: str | None = None,
    project: str | None = None,
) -> int:
    """Broadcast a message to all agents (or all agents on a project).

    Returns count of agents delivered to.
    """
    sender = sender or get_current_agent_id()
    if not sender:
        raise RuntimeError("Not registered — call ipc.register() first")

    if isinstance(payload, str):
        payload = {"text": payload}

    if project:
        # Send to project members individually
        r = transport.get_client()
        from .agent import _K_PROJECT

        members = r.smembers(f"{_K_PROJECT}{project}")
        count = 0
        for member in members:
            if member != sender:
                delivered, _ = send(member, payload, priority, ttl, sender)
                if delivered:
                    count += 1
        return count

    env = Envelope(
        sender=sender,
        recipient="*",
        message_type="broadcast",
        payload=payload,
        priority=priority,
        ttl=ttl,
    )

    result = transport.eval_lua(
        _lua_scripts.BROADCAST_ATOMIC,
        3,
        _K_INDEX,
        "ipc:metrics:sent",
        "ipc:metrics:delivered",
        env.to_json(),
        sender,
        str(_INBOX_MAXLEN),
    )
    return int(result)


def inbox_depth(agent_id: str | None = None) -> int:
    """Get the number of messages in an agent's inbox stream."""
    agent_id = agent_id or get_current_agent_id()
    if not agent_id:
        return 0
    return transport.stream_len(f"{_K_INBOX}{agent_id}")
