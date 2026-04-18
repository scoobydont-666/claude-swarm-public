"""Request/response RPC over IPC with timeout and streaming support."""

from __future__ import annotations

import time

from . import _lua_scripts, transport
from .agent import _K_INBOX, get_current_agent_id
from .envelope import Envelope

# Defaults
_DEFAULT_TIMEOUT = 30  # seconds
_INBOX_MAXLEN = 5000


class RPCTimeout(Exception):
    """Raised when an RPC request times out."""

    def __init__(self, correlation_id: str, timeout: float) -> None:
        self.correlation_id = correlation_id
        self.timeout = timeout
        super().__init__(f"RPC {correlation_id} timed out after {timeout}s")


class RPCError(Exception):
    """Raised when the RPC responder returns an error."""

    def __init__(self, correlation_id: str, error: str) -> None:
        self.correlation_id = correlation_id
        self.error = error
        super().__init__(f"RPC {correlation_id} error: {error}")


def request(
    target: str,
    method: str,
    params: dict | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    sender: str | None = None,
) -> Envelope:
    """Send an RPC request and wait for a response.

    Args:
        target: Target agent_id
        method: RPC method name
        params: Method parameters
        timeout: Seconds to wait for response
        sender: Override sender

    Returns:
        Response Envelope

    Raises:
        RPCTimeout: If no response within timeout
        RPCError: If responder returned an error
    """
    sender = sender or get_current_agent_id()
    if not sender:
        raise RuntimeError("Not registered — call ipc.register() first")

    env = Envelope(
        sender=sender,
        recipient=target,
        message_type="rpc_request",
        payload={"method": method, "params": params or {}},
        reply_to=sender,
        ttl=int(timeout) + 5,  # Slight buffer over timeout
    )
    env.correlation_id = env.id  # Use message ID as correlation

    resp_key = f"ipc:rpc:resp:{env.correlation_id}"
    deadline = time.time() + timeout

    # Atomic: create response slot + send to target + track pending
    transport.eval_lua(
        _lua_scripts.RPC_REQUEST_ATOMIC,
        3,
        resp_key,
        f"{_K_INBOX}{target}",
        "ipc:rpc:pending",
        env.to_json(),
        str(int(timeout) + 5),
        str(deadline),
        env.correlation_id,
        str(_INBOX_MAXLEN),
    )

    # Increment metrics
    r = transport.get_client()
    r.incr("ipc:metrics:rpc_sent")
    r.incr("ipc:metrics:sent")

    # Wait for response via BLPOP
    result = transport.list_wait(resp_key, timeout=int(timeout))

    # Clean up pending tracker
    r.zrem("ipc:rpc:pending", env.correlation_id)

    if result is None:
        r.incr("ipc:metrics:rpc_timeout")
        raise RPCTimeout(env.correlation_id, timeout)

    resp_env = Envelope.from_json(result)

    # Check for error responses
    if resp_env.payload.get("error"):
        raise RPCError(env.correlation_id, resp_env.payload["error"])

    return resp_env


def respond(request_env: Envelope, result: dict) -> bool:
    """Send a response to an RPC request.

    Args:
        request_env: The original request envelope
        result: Response payload

    Returns:
        True if response was sent
    """
    sender = get_current_agent_id()
    if not sender:
        raise RuntimeError("Not registered — call ipc.register() first")

    resp_env = Envelope(
        sender=sender,
        recipient=request_env.sender,
        message_type="rpc_response",
        payload=result,
        correlation_id=request_env.correlation_id,
    )

    resp_key = f"ipc:rpc:resp:{request_env.correlation_id}"
    transport.list_push(resp_key, resp_env.to_json())

    r = transport.get_client()
    r.incr("ipc:metrics:delivered")

    return True


def respond_error(request_env: Envelope, error: str) -> bool:
    """Send an error response to an RPC request."""
    return respond(request_env, {"error": error})


def respond_stream(request_env: Envelope, chunks) -> int:
    """Send a streaming RPC response.

    Args:
        request_env: The original request envelope
        chunks: Iterable of dicts, each becomes a response chunk

    Returns:
        Total number of chunks sent
    """
    sender = get_current_agent_id()
    if not sender:
        raise RuntimeError("Not registered — call ipc.register() first")

    resp_key = f"ipc:rpc:resp:{request_env.correlation_id}"
    seq = 0

    for chunk in chunks:
        seq += 1
        resp_env = Envelope(
            sender=sender,
            recipient=request_env.sender,
            message_type="rpc_stream",
            payload=chunk,
            correlation_id=request_env.correlation_id,
            sequence=seq,
            final=False,
        )
        transport.list_push(resp_key, resp_env.to_json())

    # Send final marker
    final_env = Envelope(
        sender=sender,
        recipient=request_env.sender,
        message_type="rpc_stream",
        payload={},
        correlation_id=request_env.correlation_id,
        sequence=seq + 1,
        final=True,
    )
    transport.list_push(resp_key, final_env.to_json())

    return seq


def request_stream(
    target: str,
    method: str,
    params: dict | None = None,
    timeout: float = 60.0,
    sender: str | None = None,
):
    """Send an RPC request and yield streaming response chunks.

    Yields Envelope objects for each chunk until final=True.
    """
    sender = sender or get_current_agent_id()
    if not sender:
        raise RuntimeError("Not registered — call ipc.register() first")

    env = Envelope(
        sender=sender,
        recipient=target,
        message_type="rpc_request",
        payload={"method": method, "params": params or {}, "stream": True},
        reply_to=sender,
        ttl=int(timeout) + 5,
    )
    env.correlation_id = env.id

    resp_key = f"ipc:rpc:resp:{env.correlation_id}"
    deadline = time.time() + timeout

    # Atomic setup
    transport.eval_lua(
        _lua_scripts.RPC_REQUEST_ATOMIC,
        3,
        resp_key,
        f"{_K_INBOX}{target}",
        "ipc:rpc:pending",
        env.to_json(),
        str(int(timeout) + 5),
        str(deadline),
        env.correlation_id,
        str(_INBOX_MAXLEN),
    )

    r = transport.get_client()
    r.incr("ipc:metrics:rpc_sent")
    r.incr("ipc:metrics:sent")

    # Yield chunks until final or timeout
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        result = transport.list_wait(resp_key, timeout=min(remaining, 5))
        if result is None:
            continue
        chunk_env = Envelope.from_json(result)
        yield chunk_env
        if chunk_env.final:
            r.zrem("ipc:rpc:pending", env.correlation_id)
            return

    r.zrem("ipc:rpc:pending", env.correlation_id)
    raise RPCTimeout(env.correlation_id, timeout)


def cleanup_expired_rpcs() -> int:
    """Remove expired RPC entries from the pending set.

    Returns count of cleaned entries.
    """
    r = transport.get_client()
    now = time.time()
    expired = r.zrangebyscore("ipc:rpc:pending", "-inf", str(now))
    if expired:
        r.zrem("ipc:rpc:pending", *expired)
        # Clean up response keys
        pipe = r.pipeline()
        for corr_id in expired:
            pipe.delete(f"ipc:rpc:resp:{corr_id}")
        pipe.execute()
    return len(expired) if expired else 0
