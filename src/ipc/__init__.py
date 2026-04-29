"""Hydra IPC — Agent-to-agent communication for Claude Code instances.

Public API:
    # Registration
    ipc.register(project="/opt/examforge", model="opus-4-6")
    ipc.deregister()
    ipc.get_current_agent_id()
    ipc.list_agents()

    # Direct messaging
    ipc.send(recipient, payload)
    ipc.recv(block_ms=5000)
    ipc.broadcast(payload)

    # Channels
    ipc.channels.create("my-channel")
    ipc.channels.subscribe("my-channel")
    ipc.channels.publish("my-channel", payload)
    ipc.channels.consume("my-channel")

    # RPC
    response = ipc.rpc.request(target, "method", params)
    ipc.rpc.respond(request_env, result)

    # Status
    ipc.status()
"""

from .agent import (
    cleanup_stale,
    deregister,
    get_agent,
    get_current_agent_id,
    list_agents,
    refresh_heartbeat,
    register,
    update_status,
)
from .direct import broadcast, inbox_depth, recv, recv_iter, send
from .envelope import Envelope
from .rpc import RPCError, RPCTimeout

from . import channels, dlq, metrics, rpc

__all__ = [
    # Registration
    "register",
    "deregister",
    "get_current_agent_id",
    "get_agent",
    "list_agents",
    "refresh_heartbeat",
    "update_status",
    "cleanup_stale",
    # Direct messaging
    "send",
    "recv",
    "recv_iter",
    "broadcast",
    "inbox_depth",
    # Envelope
    "Envelope",
    # RPC
    "RPCError",
    "RPCTimeout",
    # Sub-modules
    "channels",
    "dlq",
    "metrics",
    "rpc",
]
