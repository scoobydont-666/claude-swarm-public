#!/usr/bin/env python3
"""hydra-ipc CLI — agent-to-agent communication for Claude Code instances."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipc import (
    Envelope,
    broadcast as ipc_broadcast,
    channels,
    cleanup_stale,
    deregister as ipc_deregister,
    dlq,
    get_current_agent_id,
    inbox_depth,
    list_agents,
    metrics,
    recv as ipc_recv,
    register as ipc_register,
    rpc,
    send as ipc_send,
    update_status,
)

app = typer.Typer(help="hydra-ipc — agent-to-agent communication")
channels_app = typer.Typer(help="Channel management")
dlq_app = typer.Typer(help="Dead-letter queue")
app.add_typer(channels_app, name="channels")
app.add_typer(dlq_app, name="dlq")

console = Console()


def _relative_time(ts: float) -> str:
    """Format a timestamp as relative time."""
    diff = time.time() - ts
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    return f"{int(diff / 3600)}h ago"


def _auto_register() -> str:
    """Ensure agent is registered, return agent_id."""
    agent_id = get_current_agent_id()
    if agent_id:
        return agent_id
    return ipc_register(auto_heartbeat=False)


# ── Registration ──────────────────────────────────────────────────


@app.command()
def register(
    project: str = typer.Option("", help="Project path this agent is working on"),
    model: str = typer.Option("", help="Model name (e.g. opus-4-6)"),
) -> None:
    """Register as an IPC agent."""
    agent_id = ipc_register(project=project, model=model, auto_heartbeat=False)
    console.print(f"Registered as [bold green]{agent_id}[/]")


@app.command()
def deregister() -> None:
    """Deregister from IPC."""
    agent_id = _auto_register()
    ipc_deregister(agent_id)
    console.print(f"Deregistered [bold red]{agent_id}[/]")


# ── Status ────────────────────────────────────────────────────────


@app.command()
def status(
    project: Optional[str] = typer.Option(None, help="Filter by project"),
) -> None:
    """Show all online IPC agents."""
    cleanup_stale()
    agents = list_agents(project=project)

    if not agents:
        console.print("[dim]No agents online[/]")
        return

    table = Table(title="IPC Agents")
    table.add_column("Agent ID", style="bold")
    table.add_column("Status")
    table.add_column("Project")
    table.add_column("Model")
    table.add_column("Inbox")
    table.add_column("Last Heartbeat")

    for a in agents:
        aid = a.get("agent_id", "?")
        st = a.get("status", "?")
        proj = a.get("project", "")
        if proj:
            proj = proj.split("/")[-1]  # Show just dir name
        model = a.get("model", "")
        depth = inbox_depth(aid)
        hb = a.get("last_heartbeat", "0")
        try:
            hb_str = _relative_time(float(hb))
        except (ValueError, TypeError):
            hb_str = "?"

        status_style = {"online": "green", "busy": "yellow", "away": "dim"}.get(
            st, "white"
        )
        table.add_row(
            aid,
            f"[{status_style}]{st}[/]",
            proj,
            model,
            str(depth),
            hb_str,
        )

    console.print(table)


# ── Direct Messaging ─────────────────────────────────────────────


@app.command()
def send(
    agent_id: str = typer.Argument(help="Target agent ID"),
    message: str = typer.Argument(help="Message (text or JSON)"),
    priority: int = typer.Option(3, help="Priority 0-5"),
    ttl: int = typer.Option(300, help="TTL in seconds"),
) -> None:
    """Send a direct message to an agent."""
    _auto_register()

    # Try to parse as JSON, fallback to text
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        payload = message

    delivered, env_id = ipc_send(agent_id, payload, priority=priority, ttl=ttl)
    if delivered:
        console.print(f"[green]Delivered[/] {env_id[:12]}… → {agent_id}")
    else:
        console.print(f"[red]Dead-lettered[/] {env_id[:12]}… (recipient not found)")


@app.command()
def recv(
    wait: int = typer.Option(0, help="Block for N seconds"),
    count: int = typer.Option(10, help="Max messages"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
) -> None:
    """Read messages from your inbox."""
    _auto_register()

    block_ms = wait * 1000 if wait > 0 else None
    messages = ipc_recv(count=count, block_ms=block_ms)

    if not messages:
        if not as_json:
            console.print("[dim]No messages[/]")
        else:
            print("[]")
        return

    if as_json:
        print(json.dumps([json.loads(m.to_json()) for m in messages], indent=2))
        return

    for msg in messages:
        age = _relative_time(msg.timestamp)
        console.print(
            f"\n[bold cyan]FROM[/]: {msg.sender} ({age}) "
            f"[dim]{msg.message_type}[/] [dim]id={msg.id[:12]}…[/]"
        )
        if isinstance(msg.payload, dict) and "text" in msg.payload:
            console.print(f"  {msg.payload['text']}")
        else:
            console.print(f"  {json.dumps(msg.payload, indent=2)}")


@app.command()
def broadcast(
    message: str = typer.Argument(help="Message to broadcast"),
    project: Optional[str] = typer.Option(None, help="Limit to project"),
) -> None:
    """Broadcast a message to all agents."""
    _auto_register()
    count = ipc_broadcast(message, project=project)
    console.print(f"[green]Broadcast[/] to {count} agent(s)")


# ── RPC ──────────────────────────────────────────────────────────


@app.command(name="rpc")
def rpc_cmd(
    agent_id: str = typer.Argument(help="Target agent ID"),
    method: str = typer.Argument(help="RPC method name"),
    params: str = typer.Argument("{}", help="JSON params"),
    timeout: int = typer.Option(30, help="Timeout in seconds"),
) -> None:
    """Send an RPC request and wait for response."""
    _auto_register()

    try:
        params_dict = json.loads(params)
    except json.JSONDecodeError:
        console.print("[red]Invalid JSON params[/]")
        raise typer.Exit(1)

    try:
        start = time.time()
        response = rpc.request(agent_id, method, params_dict, timeout=timeout)
        elapsed = time.time() - start
        console.print(f"[green]Response[/] ({elapsed:.2f}s):")
        console.print(json.dumps(response.payload, indent=2))
    except rpc.RPCTimeout as e:
        console.print(f"[red]Timeout[/]: {e}")
        raise typer.Exit(2)
    except rpc.RPCError as e:
        console.print(f"[red]Error[/]: {e}")
        raise typer.Exit(1)


@app.command()
def ping(
    agent_id: str = typer.Argument(help="Target agent ID"),
    count: int = typer.Option(3, help="Number of pings"),
) -> None:
    """Ping an agent to test connectivity and measure latency."""
    _auto_register()

    latencies = []
    for i in range(count):
        try:
            start = time.time()
            resp = rpc.request(agent_id, "ping", {"seq": i}, timeout=5)
            elapsed = (time.time() - start) * 1000
            latencies.append(elapsed)
            console.print(f"  pong from {agent_id}: seq={i} time={elapsed:.1f}ms")
        except rpc.RPCTimeout:
            console.print(f"  [red]timeout[/] from {agent_id}: seq={i}")
        except rpc.RPCError as e:
            console.print(f"  [red]error[/] from {agent_id}: {e}")

    if latencies:
        avg = sum(latencies) / len(latencies)
        mn = min(latencies)
        mx = max(latencies)
        console.print(
            f"\n--- {agent_id} ping statistics ---\n"
            f"{count} sent, {len(latencies)} received, "
            f"{100 * (count - len(latencies)) / count:.0f}% loss\n"
            f"rtt min/avg/max = {mn:.1f}/{avg:.1f}/{mx:.1f} ms"
        )


# ── Watch ────────────────────────────────────────────────────────


@app.command()
def watch(
    inbox: bool = typer.Option(True, help="Watch own inbox"),
    channel: Optional[str] = typer.Option(None, help="Watch a channel instead"),
) -> None:
    """Live tail of messages. Ctrl+C to stop."""
    _auto_register()
    console.print("[dim]Watching for messages… (Ctrl+C to stop)[/]\n")

    try:
        while True:
            if channel:
                msgs = channels.consume(channel, count=5, block_ms=2000)
            else:
                msgs = ipc_recv(count=5, block_ms=2000)

            for msg in msgs:
                age = _relative_time(msg.timestamp)
                source = channel or "inbox"
                console.print(
                    f"[{source}] [bold cyan]{msg.sender}[/] ({age}) "
                    f"[dim]{msg.message_type}[/]"
                )
                if isinstance(msg.payload, dict) and "text" in msg.payload:
                    console.print(f"  {msg.payload['text']}")
                else:
                    console.print(f"  {json.dumps(msg.payload)}")
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped[/]")


# ── Channels ─────────────────────────────────────────────────────


@channels_app.command("list")
def channels_list() -> None:
    """List all channels."""
    chans = channels.list_channels()
    if not chans:
        console.print("[dim]No channels[/]")
        return

    table = Table(title="IPC Channels")
    table.add_column("Channel", style="bold")
    table.add_column("Subscribers")
    table.add_column("Messages")

    for ch in chans:
        table.add_row(ch["name"], str(ch["subscribers"]), str(ch["messages"]))
    console.print(table)


@channels_app.command("create")
def channels_create(name: str = typer.Argument(help="Channel name")) -> None:
    """Create a channel."""
    created = channels.create(name)
    if created:
        console.print(f"[green]Created[/] channel '{name}'")
    else:
        console.print(f"Channel '{name}' already exists")


@channels_app.command("subscribe")
def channels_subscribe(name: str = typer.Argument(help="Channel name")) -> None:
    """Subscribe to a channel."""
    _auto_register()
    channels.subscribe(name)
    console.print(f"[green]Subscribed[/] to '{name}'")


@channels_app.command("publish")
def channels_publish(
    name: str = typer.Argument(help="Channel name"),
    message: str = typer.Argument(help="Message"),
) -> None:
    """Publish to a channel."""
    _auto_register()
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        payload = message
    env_id = channels.publish(name, payload)
    console.print(f"[green]Published[/] {env_id[:12]}… → {name}")


@channels_app.command("consume")
def channels_consume(
    name: str = typer.Argument(help="Channel name"),
    wait: int = typer.Option(0, help="Block for N seconds"),
    count: int = typer.Option(10, help="Max messages"),
) -> None:
    """Consume messages from a channel."""
    _auto_register()
    block_ms = wait * 1000 if wait > 0 else None
    msgs = channels.consume(name, count=count, block_ms=block_ms)

    if not msgs:
        console.print("[dim]No messages[/]")
        return

    for msg in msgs:
        age = _relative_time(msg.timestamp)
        console.print(f"[bold cyan]{msg.sender}[/] ({age}): ", end="")
        if isinstance(msg.payload, dict) and "text" in msg.payload:
            console.print(msg.payload["text"])
        else:
            console.print(json.dumps(msg.payload))


# ── DLQ ──────────────────────────────────────────────────────────


@dlq_app.command("list")
def dlq_list(limit: int = typer.Option(50, help="Max entries")) -> None:
    """Show dead-letter queue."""
    entries = dlq.list_dlq(limit=limit)
    if not entries:
        console.print("[dim]DLQ empty[/]")
        return

    table = Table(title=f"Dead-Letter Queue ({dlq.dlq_depth()} entries)")
    table.add_column("ID", style="dim")
    table.add_column("Reason")
    table.add_column("Sender")
    table.add_column("Recipient")
    table.add_column("Type")

    for e in entries:
        env = e.get("envelope")
        if isinstance(env, Envelope):
            table.add_row(
                e["stream_id"],
                e["reason"],
                env.sender,
                env.recipient,
                env.message_type,
            )
        else:
            table.add_row(e["stream_id"], e["reason"], "?", "?", "?")
    console.print(table)


@dlq_app.command("requeue")
def dlq_requeue(
    message_id: str = typer.Argument(help="Stream ID to requeue"),
    to: Optional[str] = typer.Option(None, help="Override recipient"),
) -> None:
    """Requeue a dead-lettered message."""
    ok = dlq.requeue(message_id, new_recipient=to)
    if ok:
        console.print(f"[green]Requeued[/] {message_id}")
    else:
        console.print(f"[red]Failed[/] to requeue {message_id}")


@dlq_app.command("purge")
def dlq_purge(
    older_than: int = typer.Option(3600, help="Purge entries older than N seconds"),
) -> None:
    """Purge old DLQ entries."""
    count = dlq.purge(older_than_seconds=older_than)
    console.print(f"Purged {count} entries")


# ── Metrics ──────────────────────────────────────────────────────


@app.command()
def show_metrics() -> None:
    """Show IPC metrics."""
    data = metrics.collect()

    table = Table(title="IPC Metrics")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    for key, value in sorted(data.items()):
        table.add_row(key, str(value))
    console.print(table)


# ── Entry point ──────────────────────────────────────────────────


def main() -> None:
    app()


if __name__ == "__main__":
    main()
