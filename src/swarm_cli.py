#!/usr/bin/env python3
"""claude-swarm CLI — distributed Claude Code coordination."""

import json
import logging
import sys
from pathlib import Path

LOG = logging.getLogger(__name__)

import typer
from rich.console import Console
from rich.table import Table

# Add parent to path so swarm_lib is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from backend import lib
except ImportError:
    import swarm_lib as lib
from datetime import UTC

from registry import STALE_THRESHOLD
from util import fleet_from_config
from util import relative_time as _relative_time

# work_generator and auto_dispatch are imported lazily inside commands to
# avoid import-time side-effects when the module is loaded in test contexts.

app = typer.Typer(help="claude-swarm — distributed Claude Code coordination")
tasks_app = typer.Typer(help="Task management")
artifacts_app = typer.Typer(help="Artifact sharing")
worktrees_app = typer.Typer(help="Worktree isolation")
summaries_app = typer.Typer(help="Session summaries")
pipeline_app = typer.Typer(help="Multi-head reasoning pipelines")
dispatches_app = typer.Typer(help="Dispatch history and monitoring")
app.add_typer(tasks_app, name="tasks")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(worktrees_app, name="worktrees")
app.add_typer(summaries_app, name="summaries")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(dispatches_app, name="dispatches")

# IPC subcommand — agent-to-agent communication
try:
    from ipc.cli import app as ipc_app

    app.add_typer(ipc_app, name="ipc")
except ImportError:
    pass  # IPC module not available

console = Console()


@app.command()
def status() -> None:
    """Show all nodes and their states."""
    nodes = lib.get_all_status()
    # Verify stale PIDs for accurate display (doesn't affect cleanup logic)
    lib.verify_stale_pids(nodes)
    if not nodes:
        console.print("[dim]No nodes registered yet.[/dim]")
        return

    table = Table(title="Swarm Status")
    table.add_column("Host", style="bold")
    table.add_column("State")
    table.add_column("Task")
    table.add_column("Project")
    table.add_column("Model")
    table.add_column("Updated")
    table.add_column("PID")

    state_colors = {
        "active": "green",
        "idle": "yellow",
        "offline": "red",
        "busy": "cyan",
    }

    # Flag nodes as stale if active/busy and >5 min since update
    from datetime import datetime

    now = datetime.now(UTC)

    for node in nodes:
        state = node.get("state", "unknown")
        updated = node.get("updated_at", "")

        # Detect stale: active/busy but no heartbeat in 5 min
        is_stale = False
        if state in ("active", "busy") and updated:
            try:
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age = (now - dt).total_seconds()
                if age > STALE_THRESHOLD:
                    is_stale = True
            except (ValueError, TypeError):
                pass

        if is_stale:
            color = "red"
            state_display = f"[{color}]{state} (STALE)[/{color}]"
        else:
            color = state_colors.get(state, "white")
            state_display = f"[{color}]{state}[/{color}]"

        table.add_row(
            node.get("hostname", "?"),
            state_display,
            node.get("current_task", "") or "-",
            node.get("project", "") or "-",
            node.get("model", "") or "-",
            _relative_time(updated),
            str(node.get("pid", "")) or "-",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@tasks_app.callback(invoke_without_command=True)
def tasks_list(ctx: typer.Context) -> None:
    """List all tasks (default when no subcommand given)."""
    if ctx.invoked_subcommand is not None:
        return
    all_tasks = lib.list_tasks()
    if not all_tasks:
        console.print("[dim]No tasks.[/dim]")
        return

    table = Table(title="Swarm Tasks")
    table.add_column("ID", style="bold")
    table.add_column("Stage")
    table.add_column("Priority")
    table.add_column("Title")
    table.add_column("Created By")
    table.add_column("Claimed By")

    stage_colors = {
        "pending": "yellow",
        "claimed": "cyan",
        "completed": "green",
        "decomposed": "magenta",
    }

    for task in all_tasks:
        stage = task.get("_stage", "?")
        color = stage_colors.get(stage, "white")
        table.add_row(
            task.get("id", "?"),
            f"[{color}]{stage}[/{color}]",
            task.get("priority", "medium"),
            task.get("title", ""),
            task.get("created_by", ""),
            task.get("claimed_by", "") or "-",
        )

    console.print(table)


@tasks_app.command("create")
def tasks_create(
    title: str = typer.Argument(..., help="Task title"),
    description: str = typer.Option("", "--desc", "-d", help="Task description"),
    project: str = typer.Option("", "--project", "-p", help="Project path"),
    priority: str = typer.Option("medium", "--priority", help="high/medium/low"),
    requires: list[str] | None = typer.Option(
        None, "--requires", "-r", help="Required capabilities"
    ),
    minutes: int = typer.Option(0, "--minutes", "-m", help="Estimated minutes"),
) -> None:
    """Create a new task."""
    task = lib.create_task(
        title=title,
        description=description,
        project=project,
        priority=priority,
        requires=requires,
        estimated_minutes=minutes,
    )
    console.print(f"[green]Created {task['id']}:[/green] {title}")


@tasks_app.command("claim")
def tasks_claim(
    task_id: str = typer.Argument(..., help="Task ID to claim"),
    isolate: bool = typer.Option(False, "--isolate", help="Create git worktree for isolated work"),
    project: str = typer.Option(
        "", "--project", "-p", help="Project path (required with --isolate)"
    ),
) -> None:
    """Claim a pending task for this host. Use --isolate to create a worktree."""
    try:
        task = lib.claim_task(task_id)
        console.print(f"[green]Claimed {task_id}[/green] for {task['claimed_by']}")

        if isolate:
            proj = project or task.get("project", "")
            if not proj:
                console.print("[red]Error:[/red] --project required with --isolate")
                raise typer.Exit(1)
            worktree_path = lib.create_worktree(proj, task_id)
            console.print(f"[green]Worktree created:[/green] {worktree_path}")
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]Worktree error:[/red] {e}")
        raise typer.Exit(1)


@tasks_app.command("complete")
def tasks_complete(
    task_id: str = typer.Argument(..., help="Task ID to complete"),
    artifact: str = typer.Option("", "--artifact", "-a", help="Result artifact path"),
    merge: bool = typer.Option(False, "--merge", help="Merge worktree branch to main"),
    branch: bool = typer.Option(False, "--branch", help="Push worktree branch only (no merge)"),
    project: str = typer.Option(
        "", "--project", "-p", help="Project path (for worktree operations)"
    ),
) -> None:
    """Mark a claimed task as done. Use --merge or --branch for worktree handling."""
    try:
        # Handle worktree completion first if requested
        if merge or branch:
            # Try to get project from task YAML
            claimed_path = lib._tasks_dir("claimed") / f"{task_id}.yaml"
            if claimed_path.exists():
                task_data = lib._locked_read_yaml(claimed_path)
                proj = project or task_data.get("project", "")
            else:
                proj = project
            if not proj:
                console.print("[red]Error:[/red] --project required for worktree operations")
                raise typer.Exit(1)
            wt_result = lib.complete_worktree(proj, task_id, merge=merge)
            action = wt_result.get("action", "unknown")
            console.print(f"[green]Worktree {action}:[/green] {wt_result.get('branch', '')}")

        task = lib.complete_task(task_id, result_artifact=artifact)
        console.print(f"[green]Completed {task_id}[/green] by {task['completed_by']}")
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]Worktree error:[/red] {e}")
        raise typer.Exit(1)


@tasks_app.command("decompose")
def tasks_decompose(
    task_id: str = typer.Argument(..., help="Task ID to decompose"),
    apply: bool = typer.Option(
        False, "--apply", help="Apply the decomposition (default: suggest only)"
    ),
) -> None:
    """Suggest or apply task decomposition."""
    # Find the task
    pending_path = lib._tasks_dir("pending") / f"{task_id}.yaml"
    if not pending_path.exists():
        console.print(f"[red]Error:[/red] Task {task_id} not found in pending/")
        raise typer.Exit(1)

    task = lib._locked_read_yaml(pending_path)
    suggestions = lib.TaskDecomposer.suggest(task)

    if not suggestions:
        console.print(f"[yellow]No decomposition suggested for {task_id}.[/yellow]")
        console.print("Task text does not match any decomposition patterns.")
        return

    if not apply:
        console.print(f"[bold]Suggested decomposition for {task_id}:[/bold]")
        table = Table()
        table.add_column("Subtask", style="bold")
        table.add_column("Title")
        table.add_column("Requires")
        for i, sub in enumerate(suggestions):
            sub_id = f"{task_id}-{chr(97 + i)}"
            table.add_row(
                sub_id,
                sub.get("title", ""),
                ", ".join(sub.get("requires", [])) or "-",
            )
        console.print(table)
        console.print("\nRun with [bold]--apply[/bold] to execute this decomposition.")
        return

    parent = lib.decompose_task(task_id, suggestions)
    console.print(
        f"[green]Decomposed {task_id}[/green] into {len(parent.get('subtasks', []))} subtasks:"
    )
    for sub_id in parent.get("subtasks", []):
        console.print(f"  - {sub_id}")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@app.command()
def message(
    target: str = typer.Argument(..., help="Target hostname or --broadcast"),
    text: str = typer.Argument(..., help="Message text"),
    broadcast: bool = typer.Option(False, "--broadcast", "-b", help="Broadcast to all"),
) -> None:
    """Send a message to another instance."""
    if broadcast or target == "--broadcast":
        lib.broadcast_message(text)
        console.print("[green]Broadcast sent[/green]")
    else:
        lib.send_message(target, text)
        console.print(f"[green]Message sent to {target}[/green]")


@app.command()
def inbox(
    clear: bool = typer.Option(False, "--clear", help="Archive all messages"),
    clear_rule: str | None = typer.Option(
        None, "--clear-rule", help="Archive messages matching a rule name"
    ),
) -> None:
    """Check messages for this host."""
    messages = lib.read_inbox()
    if not messages:
        console.print("[dim]No messages.[/dim]")
        return

    if clear or clear_rule:
        archived = 0
        for msg in messages:
            if clear_rule and clear_rule not in msg.get("text", ""):
                continue
            fpath = msg.get("_file")
            if fpath:
                lib.archive_message(fpath)
                archived += 1
        console.print(f"[green]Archived {archived} message(s).[/green]")
        return

    for msg in messages:
        source_tag = "[broadcast]" if msg.get("_source") == "broadcast" else ""
        console.print(
            f"[bold]{msg.get('from', '?')}[/bold] {source_tag} ({msg.get('sent_at', '?')}):"
        )
        console.print(f"  {msg.get('text', '')}")
        console.print()


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


@artifacts_app.callback(invoke_without_command=True)
def artifacts_default(ctx: typer.Context) -> None:
    """List artifacts (default)."""
    if ctx.invoked_subcommand is not None:
        return
    artifacts_list_cmd()


@artifacts_app.command("list")
def artifacts_list_cmd() -> None:
    """List shared artifacts."""
    artifacts = lib.list_artifacts()
    if not artifacts:
        console.print("[dim]No artifacts shared.[/dim]")
        return

    table = Table(title="Shared Artifacts")
    table.add_column("Name", style="bold")
    table.add_column("Size")
    table.add_column("Modified")

    for a in artifacts:
        size = a["size_bytes"]
        if size > 1024 * 1024:
            size_str = f"{size / 1024 / 1024:.1f} MB"
        elif size > 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size} B"
        table.add_row(a["name"], size_str, a["modified_at"])

    console.print(table)


@artifacts_app.command("share")
def artifacts_share(
    file: str = typer.Argument(..., help="File path to share"),
    name: str = typer.Option("", "--name", "-n", help="Override artifact name"),
) -> None:
    """Share a file with the swarm."""
    try:
        dst = lib.share_artifact(file, name=name or None)
        console.print(f"[green]Shared:[/green] {dst}")
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------


@worktrees_app.callback(invoke_without_command=True)
def worktrees_default(
    ctx: typer.Context,
    project: str = typer.Option(".", "--project", "-p", help="Project path"),
) -> None:
    """List active worktrees across the fleet."""
    if ctx.invoked_subcommand is not None:
        return
    worktrees = lib.list_worktrees(project)
    if not worktrees:
        console.print("[dim]No active worktrees.[/dim]")
        return

    table = Table(title="Active Worktrees")
    table.add_column("Path", style="bold")
    table.add_column("Branch")
    table.add_column("HEAD")

    for wt in worktrees:
        table.add_row(
            wt.get("path", "?"),
            wt.get("branch", wt.get("detached", "detached")),
            wt.get("head", "?")[:12],
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


@summaries_app.callback(invoke_without_command=True)
def summaries_default(
    ctx: typer.Context,
    project: str = typer.Option("", "--project", "-p", help="Filter by project path"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max summaries to show"),
) -> None:
    """List recent session summaries."""
    if ctx.invoked_subcommand is not None:
        return

    if project:
        summaries = lib.get_relevant_summaries(project, limit=limit)
    else:
        # List all summaries
        summaries_dir = lib._summaries_dir()
        summaries = []
        for f in sorted(summaries_dir.glob("*.yaml"), reverse=True):
            try:
                import yaml

                data = yaml.safe_load(open(f)) or {}
                data["_file"] = str(f)
                summaries.append(data)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Suppressed: %s", exc)
                continue
        summaries = summaries[:limit]

    if not summaries:
        console.print("[dim]No session summaries.[/dim]")
        return

    table = Table(title="Session Summaries")
    table.add_column("Host", style="bold")
    table.add_column("Timestamp")
    table.add_column("Project")
    table.add_column("Task")
    table.add_column("Duration")
    table.add_column("Files")
    table.add_column("Context")

    for s in summaries:
        ctx_text = s.get("context_for_next", "")
        if len(ctx_text) > 60:
            ctx_text = ctx_text[:57] + "..."
        table.add_row(
            s.get("hostname", "?"),
            s.get("timestamp", "?"),
            s.get("project", "") or "-",
            s.get("task_id", "") or "-",
            f"{s.get('duration_minutes', 0)}m",
            str(len(s.get("files_changed", []))),
            ctx_text or "-",
        )

    console.print(table)


@app.command()
def context(
    project: str = typer.Option(".", "--project", "-p", help="Project path"),
) -> None:
    """Show what the last instance on this project left for you."""
    ctx_text = lib.get_latest_summary_context(project)
    if not ctx_text:
        console.print("[dim]No prior session context available for this project.[/dim]")
        return
    console.print(f"[bold]Prior context:[/bold] {ctx_text}")


# ---------------------------------------------------------------------------
# Health & Sync
# ---------------------------------------------------------------------------


@app.command()
def health(
    rules: bool = typer.Option(False, "--rules", help="Show all rules and last trigger time"),
    events: bool = typer.Option(False, "--events", help="Show recent events from the event log"),
    start: bool = typer.Option(False, "--start", help="Start the health monitor daemon"),
    stop: bool = typer.Option(False, "--stop", help="Stop the health monitor daemon"),
    prune: bool = typer.Option(False, "--prune", help="Prune events older than --prune-days"),
    prune_days: int = typer.Option(
        30, "--prune-days", help="Days to keep (default: 30, used with --prune)"
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of events to show (with --events)"),
) -> None:
    """Full health check of the swarm. Use flags for rule status, event log, or daemon control."""
    import subprocess as _sp
    from pathlib import Path as _Path

    # ── Event log pruning ──────────────────────────────────────────────────
    if prune:
        import sys as _sys

        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from event_log import EventLog as _EventLog

        ev = _EventLog()
        before = ev.count()
        deleted = ev.prune(days=prune_days)
        after = ev.count()
        console.print(
            f"[green]Pruned {deleted} events[/green] older than {prune_days} days ({before} → {after} rows)"
        )
        return

    # ── Daemon control ──────────────────────────────────────────────────────
    if start:
        result = _sp.run(
            ["systemctl", "--user", "start", "swarm-health"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Try system unit
            result = _sp.run(
                ["sudo", "systemctl", "start", "swarm-health"],
                capture_output=True,
                text=True,
            )
        if result.returncode == 0:
            console.print("[green]swarm-health started[/green]")
        else:
            console.print(f"[red]Failed to start:[/red] {result.stderr.strip()}")
            raise typer.Exit(1)
        return

    if stop:
        result = _sp.run(
            ["systemctl", "--user", "stop", "swarm-health"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            result = _sp.run(
                ["sudo", "systemctl", "stop", "swarm-health"],
                capture_output=True,
                text=True,
            )
        if result.returncode == 0:
            console.print("[green]swarm-health stopped[/green]")
        else:
            console.print(f"[red]Failed to stop:[/red] {result.stderr.strip()}")
            raise typer.Exit(1)
        return

    # ── Rule status ─────────────────────────────────────────────────────────
    if rules:
        import sys as _sys

        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from event_log import EventLog as _EventLog
        from health_rules import RULES as _RULES

        ev = _EventLog()
        summary = {r["rule_name"]: r for r in ev.rule_summary()}

        table = Table(title="Health Rules")
        table.add_column("Name", style="bold")
        table.add_column("Check")
        table.add_column("Action")
        table.add_column("Auto?")
        table.add_column("Cooldown (m)")
        table.add_column("Last Trigger")
        table.add_column("Total Fires")

        for rule in _RULES:
            name = rule["name"]
            stat = summary.get(name, {})
            auto = "[green]yes[/green]" if rule.get("auto_remediate") else "[dim]no[/dim]"
            table.add_row(
                name,
                rule.get("check", "-"),
                rule.get("action", "-"),
                auto,
                str(rule.get("cooldown_minutes", "-")),
                stat.get("last_seen", "[dim]never[/dim]"),
                str(stat.get("total", 0)),
            )
        console.print(table)
        return

    # ── Event log ───────────────────────────────────────────────────────────
    if events:
        import sys as _sys

        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from event_log import EventLog as _EventLog

        ev = _EventLog()
        rows = ev.recent_events(limit=limit)
        if not rows:
            console.print("[dim]No health events recorded yet.[/dim]")
            return

        table = Table(title=f"Recent Health Events (last {len(rows)})")
        table.add_column("Time", style="dim")
        table.add_column("Rule", style="bold")
        table.add_column("Host")
        table.add_column("Severity")
        table.add_column("Action Taken")
        table.add_column("Result")

        severity_colors = {"high": "red", "medium": "yellow", "low": "dim"}
        for row in rows:
            sev = row.get("severity", "medium")
            color = severity_colors.get(sev, "white")
            result_text = row.get("action_result", "")
            if len(result_text) > 50:
                result_text = result_text[:47] + "..."
            table.add_row(
                row.get("timestamp", "?"),
                row.get("rule_name", "?"),
                row.get("host", "-"),
                f"[{color}]{sev}[/{color}]",
                row.get("action_taken", "-") or "-",
                result_text or "-",
            )
        console.print(table)
        return

    # ── Default: swarm health overview ─────────────────────────────────────
    result = lib.health_check()

    console.print(f"[bold]Swarm Health Check[/bold] — {result['timestamp']}")
    console.print(f"  Root: {result['swarm_root']}")
    nfs_status = "[green]yes[/green]" if result["nfs_available"] else "[red]no[/red]"
    console.print(f"  NFS available: {nfs_status}")
    console.print()

    if result["nodes"]:
        table = Table(title="Node Health")
        table.add_column("Host", style="bold")
        table.add_column("State")
        table.add_column("Age (s)")
        table.add_column("Stale?")
        table.add_column("Task")

        for hostname, info in result["nodes"].items():
            stale = "[red]YES[/red]" if info["stale"] else "[green]no[/green]"
            table.add_row(
                hostname,
                info["state"],
                str(info["age_seconds"]),
                stale,
                info["current_task"] or "-",
            )
        console.print(table)

    console.print()
    console.print(
        f"  Tasks: {result['pending_tasks']} pending, "
        f"{result['claimed_tasks']} claimed, "
        f"{result['completed_tasks']} completed"
    )

    if result["stale_nodes"]:
        console.print(f"  [red]Stale nodes: {', '.join(result['stale_nodes'])}[/red]")


@app.command()
def cleanup(
    threshold: int = typer.Option(
        300, "--threshold", "-t", help="Stale threshold in seconds (default: 300)"
    ),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip SSH PID verification"),
) -> None:
    """Detect and clean up stale nodes + orphaned tasks.

    Checks each active/busy node: if no heartbeat update within threshold AND
    the registered PID is dead (verified via SSH), resets node to idle and
    requeues any claimed tasks back to pending.
    """
    console.print(
        f"[bold]Cleaning up stale nodes[/bold] (threshold: {threshold}s, verify PID: {not no_verify})"
    )
    result = lib.cleanup_stale_nodes(threshold_seconds=threshold, verify_pid=not no_verify)

    cleaned = result.get("cleaned", [])
    orphaned = result.get("orphaned_tasks", [])

    if cleaned:
        for host in cleaned:
            console.print(f"  [green]Reset → idle:[/green] {host}")
    else:
        console.print("  [dim]No stale nodes found.[/dim]")

    if orphaned:
        for task_id in orphaned:
            console.print(f"  [yellow]Requeued → pending:[/yellow] {task_id}")
    elif cleaned:
        console.print("  [dim]No orphaned tasks.[/dim]")

    console.print(
        f"\n[bold]Done:[/bold] {len(cleaned)} nodes cleaned, {len(orphaned)} tasks requeued."
    )


@app.command()
def sync() -> None:
    """Force git sync (runs sync-to-git.sh)."""
    import subprocess

    script = Path("/opt/claude-swarm/scripts/sync-to-git.sh")
    if not script.exists():
        console.print("[red]sync-to-git.sh not found[/red]")
        raise typer.Exit(1)

    result = subprocess.run([str(script)], capture_output=True, text=True)
    if result.returncode == 0:
        console.print("[green]Git sync complete[/green]")
        if result.stdout.strip():
            console.print(result.stdout.strip())
    else:
        console.print(f"[red]Sync failed:[/red] {result.stderr.strip()}")
        raise typer.Exit(1)


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to"),
    port: int = typer.Option(9192, "--port", help="Port to bind to"),
) -> None:
    """Start the web dashboard for fleet monitoring.

    Open http://127.0.0.1:9192/ in your browser.
    Dashboard auto-refreshes every 10 seconds.
    """
    from dashboard import run_dashboard

    run_dashboard(host=host, port=port)


# ---------------------------------------------------------------------------
# Work Generation
# ---------------------------------------------------------------------------


@app.command()
def generate(
    apply: bool = typer.Option(
        False, "--apply", help="Create the proposed tasks (default: dry run)"
    ),
) -> None:
    """Run work generator: scan projects, alerts, git, and schedules for tasks.

    Without --apply, shows proposed tasks without creating them.
    With --apply, writes task files to the pending queue.
    """
    from work_generator import WorkGenerator

    config = lib.load_config()
    wg = WorkGenerator(config)
    proposed = wg.generate_work()

    if not proposed:
        console.print("[dim]No new tasks proposed.[/dim]")
        return

    if not apply:
        table = Table(title=f"Proposed Tasks ({len(proposed)} new)")
        table.add_column("Title", style="bold")
        table.add_column("Priority")
        table.add_column("Model")
        table.add_column("Requires")
        table.add_column("Source")
        table.add_column("Project")

        priority_colors = {"high": "red", "medium": "yellow", "low": "dim"}
        for t in proposed:
            pri = t.get("priority", "medium")
            color = priority_colors.get(pri, "white")
            proj = t.get("project", "") or "-"
            if len(proj) > 40:
                proj = "..." + proj[-37:]
            table.add_row(
                t.get("title", ""),
                f"[{color}]{pri}[/{color}]",
                t.get("suggested_model", "-"),
                ", ".join(t.get("requires", [])) or "-",
                t.get("source", "-"),
                proj,
            )
        console.print(table)
        console.print(f"\nRun with [bold]--apply[/bold] to create these {len(proposed)} tasks.")
        return

    # Apply: create tasks
    created_count = 0
    for t in proposed:
        task = lib.create_task(
            title=t["title"],
            description=t.get("description", ""),
            project=t.get("project", ""),
            priority=t.get("priority", "medium"),
            requires=t.get("requires", []),
        )
        console.print(f"[green]Created {task['id']}:[/green] {t['title']}")
        created_count += 1

    console.print(f"\n[green]Created {created_count} tasks.[/green]")


# ---------------------------------------------------------------------------
# Auto Dispatch
# ---------------------------------------------------------------------------


@app.command(name="auto-dispatch")
def auto_dispatch_cmd(
    mode: str = typer.Option(
        "", "--mode", "-m", help="Set mode: off|dry_run|haiku_only|sonnet|full"
    ),
    enable: bool = typer.Option(False, "--enable", help="Enable (shortcut for --mode sonnet)"),
    disable: bool = typer.Option(False, "--disable", help="Disable (shortcut for --mode off)"),
) -> None:
    """Run auto-dispatcher or set its mode.

    Without flags: run one pass of the auto-dispatcher.

    Modes (graduated rollout):
      off        — disabled (default)
      dry_run    — log what would dispatch, no execution
      haiku_only — auto-dispatch only haiku-tier tasks
      sonnet     — haiku + sonnet tasks
      full       — all tiers, model routing via token-miser
    """
    from auto_dispatch import DISPATCH_MODES, AutoDispatcher

    try:
        config = lib.load_config()
    except FileNotFoundError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(1)

    dispatcher = AutoDispatcher(config)

    # Handle mode setting
    if mode:
        if mode not in DISPATCH_MODES:
            console.print(f"[red]Invalid mode '{mode}'. Valid: {DISPATCH_MODES}[/red]")
            raise typer.Exit(1)
        config_path = lib._config_path()
        dispatcher.set_mode(mode, config_path)
        console.print(f"[green]Auto-dispatch mode set to: {mode}[/green]")
        return

    if enable:
        config_path = lib._config_path()
        dispatcher.set_mode("sonnet", config_path)
        console.print("[green]Auto-dispatch enabled (mode: sonnet).[/green]")
        return

    if disable:
        config_path = lib._config_path()
        dispatcher.set_mode("off", config_path)
        console.print("[yellow]Auto-dispatch disabled.[/yellow]")
        return

    # Show current mode and run one pass
    current_mode = config.get("auto_dispatch", {}).get("mode", "off")
    console.print(f"[bold]Auto-dispatch mode:[/bold] {current_mode}")

    if dispatcher.mode == "off":
        console.print(
            "[yellow]Auto-dispatch is off.[/yellow] "
            "Set mode with [bold]--mode dry_run[/bold] to start."
        )
        return

    dispatched = dispatcher.process_pending_tasks()
    if not dispatched:
        console.print("[dim]No eligible tasks to dispatch.[/dim]")
        return

    table = Table(title=f"Dispatched {len(dispatched)} tasks")
    table.add_column("Task ID", style="bold")
    table.add_column("Host")
    table.add_column("Model")
    table.add_column("Mode")
    table.add_column("Dispatch ID")

    for d in dispatched:
        table.add_row(
            d.get("task_id", "?"),
            d.get("host", "?"),
            d.get("model", "?"),
            d.get("mode", "?"),
            d.get("dispatch_id", "?"),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------


@pipeline_app.command("list")
def pipeline_list() -> None:
    """List available multi-head reasoning pipelines."""
    from pipeline_registry import list_pipelines as _list_pipelines

    pipelines = _list_pipelines()
    if not pipelines:
        console.print("[dim]No pipelines registered.[/dim]")
        return

    table = Table(title="Available Pipelines")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Stages")
    table.add_column("Timeout (m)")

    for p in pipelines:
        table.add_row(
            p["name"],
            p["description"],
            " → ".join(p["stages"]),
            str(p["timeout_minutes"]),
        )
    console.print(table)


@pipeline_app.command("run")
def pipeline_run(
    name: str = typer.Argument(..., help="Pipeline name (see 'pipeline list')"),
    input_task: str = typer.Option("", "--input", "-i", help="Task description / input"),
    project: str = typer.Option("", "--project", "-p", help="Project directory on remote hosts"),
    cert: str = typer.Option("", "--cert", help="CPA cert section (question-gen pipeline)"),
    domain: str = typer.Option("", "--domain", help="Domain (question-gen pipeline)"),
    count: str = typer.Option("10", "--count", help="Count (question-gen pipeline)"),
) -> None:
    """Start a multi-head reasoning pipeline.

    Examples:
      swarm pipeline run feature-build --input "Add CSV export to Audit Sentinel" --project /opt/audit-sentinel
      swarm pipeline run bug-fix --input "KeyError in line 42 of pipeline.py"
      swarm pipeline run security-audit
      swarm pipeline run question-gen --cert FAR --domain Revenue --count 20
    """
    from pipeline import PipelineExecutor
    from pipeline_registry import get_pipeline

    try:
        pipeline = get_pipeline(name)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    # Build input_data dict
    input_data: dict = {}
    if input_task:
        input_data["task"] = input_task
    if project:
        input_data["project"] = project
    if cert:
        input_data["cert"] = cert
    if domain:
        input_data["domain"] = domain
    if count:
        input_data["count"] = count

    console.print(f"[bold]Starting pipeline:[/bold] {pipeline.name}")
    console.print(f"  {pipeline.description}")
    console.print(f"  Stages: {' → '.join(s.name for s in pipeline.stages)}")
    if input_task:
        console.print(f"  Input: {input_task[:80]}")
    console.print()

    executor = PipelineExecutor()
    try:
        result = executor.execute(pipeline, input_data)
    except Exception as e:
        console.print(f"[red]Pipeline error:[/red] {e}")
        raise typer.Exit(1)

    # Display results
    status_color = "green" if result.status == "completed" else "red"
    console.print(
        f"[bold]Pipeline {result.pipeline_id}:[/bold] [{status_color}]{result.status}[/{status_color}]"
    )
    if result.error:
        console.print(f"  [red]Error:[/red] {result.error}")
    console.print()

    table = Table(title="Stage Results")
    table.add_column("Stage", style="bold")
    table.add_column("Status")
    table.add_column("Host")
    table.add_column("Model")
    table.add_column("Duration (s)")
    table.add_column("Output (preview)")

    for stage_name, sr in result.stage_results.items():
        st = sr.get("status", "?")
        color = "green" if st == "completed" else "red"
        preview = sr.get("output", "")[:60].replace("\n", " ")
        if len(sr.get("output", "")) > 60:
            preview += "..."
        table.add_row(
            stage_name,
            f"[{color}]{st}[/{color}]",
            sr.get("host", "-"),
            sr.get("model", "-"),
            str(sr.get("duration_seconds", 0)),
            preview or "-",
        )
    console.print(table)

    if result.status == "completed":
        console.print(f"\n[dim]Full outputs: swarm pipeline status {result.pipeline_id}[/dim]")


@pipeline_app.command("status")
def pipeline_status(
    pipeline_id: str = typer.Argument(..., help="Pipeline ID"),
    stage: str = typer.Option("", "--stage", "-s", help="Show output for a specific stage"),
) -> None:
    """Show pipeline progress and stage outputs.

    Examples:
      swarm pipeline status abc12345
      swarm pipeline status abc12345 --stage architect
    """
    from pipeline import get_pipeline_run, get_stage_output

    run = get_pipeline_run(pipeline_id)
    if not run:
        console.print(f"[red]Pipeline {pipeline_id!r} not found.[/red]")
        raise typer.Exit(1)

    if stage:
        output = get_stage_output(pipeline_id, stage)
        if output is None:
            console.print(f"[red]Stage '{stage}' not found in pipeline {pipeline_id}.[/red]")
            raise typer.Exit(1)
        console.print(f"[bold]Pipeline {pipeline_id} / Stage {stage}:[/bold]")
        console.print(output)
        return

    status_color = (
        "green"
        if run.get("status") == "completed"
        else ("yellow" if run.get("status") == "running" else "red")
    )
    console.print(f"[bold]Pipeline:[/bold] {run.get('pipeline_name', pipeline_id)}")
    console.print(f"[bold]ID:[/bold] {pipeline_id}")
    console.print(f"[bold]Status:[/bold] [{status_color}]{run.get('status', '?')}[/{status_color}]")
    console.print(f"[bold]Started:[/bold] {run.get('started_at', '?')}")
    if run.get("completed_at"):
        console.print(f"[bold]Completed:[/bold] {run.get('completed_at')}")
    if run.get("error"):
        console.print(f"[bold red]Error:[/bold red] {run['error']}")
    console.print()

    stage_results = run.get("stage_results", {})
    if stage_results:
        table = Table(title="Stages")
        table.add_column("Stage", style="bold")
        table.add_column("Status")
        table.add_column("Host")
        table.add_column("Model")
        table.add_column("Duration (s)")

        for sname, sr in stage_results.items():
            st = sr.get("status", "?")
            color = "green" if st == "completed" else ("yellow" if st == "running" else "red")
            table.add_row(
                sname,
                f"[{color}]{st}[/{color}]",
                sr.get("host", "-"),
                sr.get("model", "-"),
                str(sr.get("duration_seconds", 0)),
            )
        console.print(table)
        console.print()
        console.print("[dim]Use --stage <name> to view full output for a stage.[/dim]")


@pipeline_app.command("history")
def pipeline_history(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of runs to show"),
) -> None:
    """List completed pipeline runs."""
    from pipeline import list_pipeline_runs

    runs = list_pipeline_runs()[:limit]
    if not runs:
        console.print("[dim]No pipeline runs found.[/dim]")
        return

    table = Table(title="Pipeline History")
    table.add_column("ID", style="bold")
    table.add_column("Pipeline")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Completed")
    table.add_column("Stages")

    for run in runs:
        st = run.get("status", "?")
        color = "green" if st == "completed" else ("yellow" if st == "running" else "red")
        n_stages = len(run.get("stage_results", {}))
        table.add_row(
            run.get("pipeline_id", "?"),
            run.get("pipeline_name", "?"),
            f"[{color}]{st}[/{color}]",
            run.get("started_at", "?"),
            run.get("completed_at", "-") or "-",
            str(n_stages),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Smart Dispatch (remote session orchestration)
# ---------------------------------------------------------------------------


@app.command(name="smart-dispatch")
def smart_dispatch_cmd(
    task: str = typer.Argument(..., help="Task description in natural language"),
    project: str = typer.Option("", "--project", "-p", help="Project directory on remote host"),
    host: str = typer.Option("", "--host", help="Force dispatch to specific host"),
    strategy: str = typer.Option(
        "",
        "--strategy",
        "-s",
        help="Force strategy: local|remote_dispatch|remote_session|collaborative",
    ),
    plan_only: bool = typer.Option(
        False, "--plan-only", help="Show execution plan without running"
    ),
    sync: bool = typer.Option(False, "--sync", help="Wait for completion (default: background)"),
) -> None:
    """Intelligently dispatch a task — decides WHERE, HOW, and WHICH MODEL.

    Analyzes the task to determine:
    - LOCAL: run here (no remote needed)
    - REMOTE_DISPATCH: one-shot `claude -p` on another host
    - REMOTE_SESSION: full Claude Code session for complex reasoning
    - COLLABORATIVE: remote session with context exchange

    Examples:
        swarm smart-dispatch "check ollama model list"
        swarm smart-dispatch "debug why ProjectA RAG returns stale results" -p <project-a-path>
        swarm smart-dispatch "implement ExamForge Stripe integration" -p /opt/examforge --host node_gpu
    """
    import socket

    from remote_session import ExecutionStrategy, execute_plan, plan_execution

    plan = plan_execution(
        task=task,
        current_host=socket.gethostname(),
        project_dir=project,
        force_host=host,
        force_strategy=strategy,
    )

    # Display the plan
    strategy_colors = {
        "local": "green",
        "remote_dispatch": "cyan",
        "remote_session": "yellow",
        "collaborative": "magenta",
    }
    color = strategy_colors.get(plan.strategy.value, "white")

    console.print("\n[bold]Execution Plan[/bold]")
    console.print(f"  Strategy:   [{color}]{plan.strategy.value}[/{color}]")
    console.print(f"  Host:       {plan.host}")
    console.print(f"  Model:      {plan.model}")
    console.print(f"  Complexity: {plan.complexity.value}")
    console.print(f"  Est. time:  {plan.estimated_minutes}m")
    console.print(f"  Max turns:  {'unlimited' if plan.max_turns == 0 else plan.max_turns}")
    console.print(f"  Reasoning:  [dim]{plan.reasoning}[/dim]")
    if plan.project_dir:
        console.print(f"  Project:    {plan.project_dir}")
    console.print()

    if plan_only:
        return

    if plan.strategy == ExecutionStrategy.LOCAL:
        console.print(
            "[green]Strategy is LOCAL — execute this task in your current session.[/green]"
        )
        return

    # Execute
    result = execute_plan(plan, background=not sync)

    if result.status == "running":
        console.print(f"[green]Dispatched[/green] [{result.dispatch_id}]")
        console.print(f"  PID: {result.pid}")
        console.print(f"  Output: {result.output_file}")
        console.print(f"\n[dim]Monitor: tail -f {result.output_file}[/dim]")
    elif result.status == "completed":
        console.print(f"[green]Completed[/green] (exit code {result.exit_code})")
        if result.output:
            console.print(result.output[:2000])
    elif result.status == "failed":
        console.print(f"[red]Failed:[/red] {result.error}")
    elif result.status == "timeout":
        console.print(f"[red]Timeout[/red] after {plan.estimated_minutes * 2}m")


# ---------------------------------------------------------------------------
# Dispatch History and Monitoring
# ---------------------------------------------------------------------------


@dispatches_app.callback(invoke_without_command=True)
def dispatches_list(ctx: typer.Context) -> None:
    """List recent dispatches (default when no subcommand given)."""
    if ctx.invoked_subcommand is not None:
        return

    from datetime import datetime
    from pathlib import Path

    dispatches_dir = Path("/opt/swarm/artifacts/dispatches")
    if not dispatches_dir.exists():
        console.print("[dim]No dispatches yet.[/dim]")
        return

    # Collect dispatch info from .plan.yaml files
    dispatches = []
    now = datetime.now(UTC)

    for plan_file in sorted(dispatches_dir.glob("*.plan.yaml"), reverse=True):
        dispatch_id = plan_file.stem
        try:
            plan_data = lib._locked_read_yaml(plan_file)
            started_at = plan_data.get("started_at", "")
            started_dt = None
            if started_at:
                try:
                    started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass

            # Check if still running
            pid_file = dispatches_dir / f"{dispatch_id}.pid"
            dispatches_dir / f"{dispatch_id}.output"
            status = "completed"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    # Check if process is still running
                    import os

                    os.kill(pid, 0)  # Signal 0 doesn't kill, just checks
                    status = "running"
                except (ValueError, OSError):
                    status = "completed"

            duration = ""
            if started_dt:
                age_seconds = (now - started_dt).total_seconds()
                if status == "running":
                    if age_seconds < 60:
                        duration = f"{int(age_seconds)}s"
                    elif age_seconds < 3600:
                        duration = f"{int(age_seconds // 60)}m"
                    else:
                        duration = f"{age_seconds / 3600:.1f}h"
                else:
                    if age_seconds < 60:
                        duration = f"{int(age_seconds)}s ago"
                    elif age_seconds < 3600:
                        duration = f"{int(age_seconds // 60)}m ago"
                    else:
                        duration = f"{age_seconds / 3600:.1f}h ago"

            dispatches.append(
                {
                    "id": dispatch_id,
                    "host": plan_data.get("host", "?"),
                    "strategy": plan_data.get("strategy", "?"),
                    "model": plan_data.get("model", "?"),
                    "status": status,
                    "started_at": started_at,
                    "duration": duration,
                }
            )
        except Exception as exc:
            console.print(f"[yellow]Warning:[/yellow] Could not parse {plan_file}: {exc}")

    if not dispatches:
        console.print("[dim]No dispatches found.[/dim]")
        return

    table = Table(title=f"Recent Dispatches ({len(dispatches)})")
    table.add_column("ID", style="bold")
    table.add_column("Host")
    table.add_column("Strategy")
    table.add_column("Model")
    table.add_column("Status")
    table.add_column("Duration")

    for d in dispatches:
        status_color = (
            "green"
            if d["status"] == "completed"
            else ("cyan" if d["status"] == "running" else "red")
        )
        table.add_row(
            d["id"],
            d["host"],
            d["strategy"],
            d["model"],
            f"[{status_color}]{d['status']}[/{status_color}]",
            d["duration"],
        )

    console.print(table)


@dispatches_app.command("show")
def dispatches_show(
    dispatch_id: str = typer.Argument(..., help="Dispatch ID to show"),
) -> None:
    """Show full details of a dispatch + last 50 lines of output."""
    from pathlib import Path

    dispatches_dir = Path("/opt/swarm/artifacts/dispatches")

    # Read plan
    plan_file = dispatches_dir / f"{dispatch_id}.plan.yaml"
    if not plan_file.exists():
        console.print(f"[red]Error:[/red] Dispatch {dispatch_id} not found")
        raise typer.Exit(1)

    plan_data = lib._locked_read_yaml(plan_file)

    # Display plan details
    console.print(f"\n[bold]Dispatch: {dispatch_id}[/bold]")
    console.print(f"  Host: {plan_data.get('host', '?')}")
    console.print(f"  Strategy: {plan_data.get('strategy', '?')}")
    console.print(f"  Model: {plan_data.get('model', '?')}")
    console.print(f"  Complexity: {plan_data.get('complexity', '?')}")
    console.print(f"  Est. Duration: {plan_data.get('estimated_minutes', '?')}m")
    console.print(f"\n  Reasoning:\n    {plan_data.get('reasoning', '?')}")
    console.print(f"\n  Prompt (truncated):\n    {plan_data.get('prompt', '?')}")

    # Show last 50 lines of output
    output_file = dispatches_dir / f"{dispatch_id}.output"
    if output_file.exists():
        console.print("\n[bold]Last 50 lines of output:[/bold]")
        with open(output_file) as f:
            lines = f.readlines()
            for line in lines[-50:]:
                console.print(line.rstrip())


@dispatches_app.command("tail")
def dispatches_tail(
    dispatch_id: str = typer.Argument(..., help="Dispatch ID to monitor"),
) -> None:
    """Tail the output of a running dispatch (live monitoring)."""
    import subprocess
    from pathlib import Path

    dispatches_dir = Path("/opt/swarm/artifacts/dispatches")
    output_file = dispatches_dir / f"{dispatch_id}.output"

    if not output_file.exists():
        console.print(f"[red]Error:[/red] Output file not found for {dispatch_id}")
        raise typer.Exit(1)

    console.print(f"[dim]Tailing {output_file}... (Ctrl+C to exit)[/dim]")

    try:
        subprocess.run(["tail", "-f", str(output_file)])
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped tailing.[/dim]")


# ---------------------------------------------------------------------------
# Collaborative mode
# ---------------------------------------------------------------------------

collab_app = typer.Typer(help="Collaborative sessions (orchestrator/worker pattern)")
app.add_typer(collab_app, name="collab")


@collab_app.command("start")
def collab_start(
    task: str = typer.Argument(..., help="Task description for the worker"),
    worker: str = typer.Option(..., "--worker", "-w", help="Worker host (e.g. node_gpu)"),
    project: str = typer.Option("", "--project", "-p", help="Project directory"),
    model: str = typer.Option("sonnet", "--model", "-m", help="Model tier"),
) -> None:
    """Start a collaborative session with a worker host."""
    from collaborative import start_collaborative

    session = start_collaborative(
        task=task,
        worker_host=worker,
        project_dir=project,
        model=model,
    )
    console.print(f"[green]Collaborative session started:[/green] {session.session_id}")
    console.print(f"  Worker: {worker}")
    console.print(f"  Project: {project or '(none)'}")
    console.print(f"  Context dir: {session.context_dir}")


@collab_app.command("status")
def collab_status(
    session_id: str = typer.Argument("", help="Session ID (omit for all sessions)"),
) -> None:
    """Show status of collaborative session(s)."""
    from collaborative import get_session_status, list_sessions

    if session_id:
        status = get_session_status(session_id)
        if status:
            console.print(f"Session {session_id}: [bold]{status}[/bold]")
        else:
            console.print(f"[red]Session not found:[/red] {session_id}")
        return

    sessions = list_sessions()
    if not sessions:
        console.print("[dim]No collaborative sessions.[/dim]")
        return

    table = Table(title="Collaborative Sessions")
    table.add_column("Session ID", style="bold")
    table.add_column("Status")
    table.add_column("Worker")
    table.add_column("Task")
    table.add_column("Started")

    for s in sessions:
        table.add_row(
            s.get("session_id", "?"),
            s.get("status", "?"),
            s.get("worker_host", "?"),
            (s.get("task", "?") or "?")[:50],
            _relative_time(s.get("started_at", "")),
        )
    console.print(table)


@collab_app.command("resolve")
def collab_resolve(
    session_id: str = typer.Argument(..., help="Session ID"),
    blocker_id: str = typer.Option(..., "--blocker-id", "-b", help="Blocker ID to resolve"),
    resolution: str = typer.Option(..., "--resolution", "-r", help="Resolution text"),
) -> None:
    """Resolve a blocker in a collaborative session."""
    from collaborative import resolve_blocker

    resolve_blocker(session_id, blocker_id, {"resolution": resolution})
    console.print(f"[green]Resolved blocker {blocker_id} in session {session_id}[/green]")


@collab_app.command("blockers")
def collab_blockers(
    session_id: str = typer.Argument(..., help="Session ID"),
) -> None:
    """List blockers for a collaborative session."""
    from collaborative import read_blockers

    blockers = read_blockers(session_id)
    if not blockers:
        console.print("[dim]No blockers.[/dim]")
        return

    for b in blockers:
        resolved = "[green]RESOLVED[/green]" if b.get("resolved") else "[red]OPEN[/red]"
        console.print(f"  {b.get('blocker_id', '?')}: {resolved} — {b.get('description', '?')}")


# ---------------------------------------------------------------------------
# Performance rating subcommands
# ---------------------------------------------------------------------------

ratings_app = typer.Typer(help="Host performance ratings and benchmarks")
app.add_typer(ratings_app, name="ratings")


@ratings_app.command("show")
def ratings_show() -> None:
    """Show performance ratings for all fleet hosts."""
    from performance_rating import get_all_ratings

    ratings = get_all_ratings()
    if not ratings:
        console.print("[dim]No ratings yet. Dispatch some tasks first.[/dim]")
        return

    console.print("[bold cyan]Host Performance Ratings[/bold cyan]")
    console.print("─" * 70)
    console.print(
        f"{'Host':15s} {'Score':>7s} {'Tasks':>6s} {'Complete':>9s} {'ErrRate':>8s} {'Tput/hr':>8s}"
    )
    console.print("─" * 70)
    for r in sorted(ratings, key=lambda x: x.composite_score, reverse=True):
        score_color = (
            "green" if r.composite_score >= 600 else "yellow" if r.composite_score >= 400 else "red"
        )
        console.print(
            f"{r.hostname:15s} [{score_color}]{r.composite_score:>7.0f}[/{score_color}] "
            f"{r.task_count:>6d} {r.completion_rate:>8.1%} {r.error_rate:>8.1%} {r.throughput_per_hour:>7.1f}"
        )


@ratings_app.command("benchmark")
def ratings_benchmark(
    host: str = typer.Argument(help="Host to benchmark"),
) -> None:
    """Run an on-demand benchmark probe against a host."""
    from performance_rating import benchmark_host

    fleet = fleet_from_config() or {}
    # CS2 fix: case-insensitive hostname resolution
    from util import resolve_host_key
    canonical = resolve_host_key(host, fleet)
    if canonical is None:
        console.print(f"[red]Unknown host: {host}. Known: {list(fleet.keys())}[/red]")
        raise typer.Exit(1)
    host = canonical

    config = fleet[host]
    ip = config.get("ip", "")
    user = config.get("user", "josh")

    console.print(f"[cyan]Benchmarking {host} ({ip})...[/cyan]")
    result = benchmark_host(host, ip, user)

    console.print(f"  Reachable:  {'✓' if result.reachable else '✗'}")
    if result.reachable:
        console.print(f"  SSH latency: {result.ssh_latency_ms:.0f}ms")
        console.print(f"  Disk latency: {result.disk_latency_ms:.0f}ms")
        console.print(
            f"  GPU: {'✓' if result.gpu_available else '✗'}"
            + (f" ({result.gpu_vram_free_mb}MB free)" if result.gpu_available else "")
        )
        console.print(f"  Ollama: {'✓' if result.ollama_healthy else '✗'}")
        console.print(f"  Claude: {'✓' if result.claude_available else '✗'}")


@ratings_app.command("metrics")
def ratings_metrics(
    host: str = typer.Argument(help="Host to show metrics for"),
    limit: int = typer.Option(20, help="Number of recent metrics"),
) -> None:
    """Show recent performance metrics for a host."""
    from performance_rating import get_metrics_for_host

    metrics = get_metrics_for_host(host, limit)
    if not metrics:
        console.print(f"[dim]No metrics for {host}[/dim]")
        return

    console.print(f"[bold cyan]Recent metrics for {host}[/bold cyan]")
    console.print("─" * 80)
    for m in metrics:
        status = "[green]✓[/green]" if m["success"] else "[red]✗[/red]"
        dur = f"{m['duration_seconds']:.0f}s" if m["duration_seconds"] else "..."
        console.print(
            f"  {status} {m['task_id'][:30]:30s} {dur:>6s} {m.get('model_used', ''):>8s} "
            f"{m.get('started_at', '')[:16]}"
        )


# Convenience alias at top level
@app.command()
def benchmark(
    host: str = typer.Argument(help="Host to benchmark"),
) -> None:
    """Run an on-demand benchmark probe against a host."""
    ratings_benchmark(host)


@app.command()
def ratings() -> None:
    """Show performance ratings for all fleet hosts."""
    ratings_show()


# ---------------------------------------------------------------------------
# Hook subcommands — single-process handlers for bash hooks
# ---------------------------------------------------------------------------

hooks_app = typer.Typer(help="Hook handlers (called by bash hooks)")
app.add_typer(hooks_app, name="hooks")


@hooks_app.command("session-start")
def hook_session_start() -> None:
    """SessionStart hook: register, report peers, load summaries."""
    import os
    import socket

    hostname = socket.gethostname()
    session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")
    model = os.environ.get("CLAUDE_MODEL", "unknown")

    # Register as active
    lib.update_status(state="active", session_id=session_id, model=model)

    # Mark stale nodes
    lib.mark_stale_nodes()

    # Build status summary
    nodes = lib.get_all_status()
    parts = []
    for n in nodes:
        h = n.get("hostname", "?")
        if h == hostname:
            continue
        state = n.get("state", "unknown")
        task = n.get("current_task", "") or "no task"
        m = n.get("model", "")
        model_str = f" on {m}" if m else ""
        parts.append(f"{h} is {state} ({task}{model_str})")

    pending = lib.list_tasks("pending")
    matching = lib.get_matching_tasks()
    msgs = lib.read_inbox()

    summary = f"Swarm: {hostname} registered as active."
    if parts:
        summary += " " + ". ".join(parts) + "."
    if pending:
        summary += f" {len(pending)} pending task(s)."
    if matching:
        summary += f" {len(matching)} task(s) match your capabilities."
    if msgs:
        summary += f" {len(msgs)} unread message(s) in inbox."

    # Load context from last session
    current = lib.get_status(hostname)
    project = current.get("project", "") if current else ""
    if project:
        ctx = lib.get_latest_summary_context(project)
        if ctx:
            summary += f" {ctx}"

    # IPC auto-registration
    try:
        from ipc.agent import list_agents as ipc_list
        from ipc.agent import register as ipc_register

        ipc_id = ipc_register(project=project, model=model, auto_heartbeat=False)
        ipc_peers = ipc_list()
        peer_count = len([a for a in ipc_peers if a.get("agent_id") != ipc_id])
        summary += f" IPC agent_id: {ipc_id}."
        if peer_count > 0:
            summary += f" {peer_count} IPC peer(s) online."
    except Exception:
        pass  # IPC unavailable — don't break session start

    print(json.dumps({"systemMessage": summary}))


@hooks_app.command("session-end")
def hook_session_end() -> None:
    """Stop hook: idle status, session summary, warn about uncompleted tasks."""
    import os
    import socket
    import sys

    hostname = socket.gethostname()
    session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")

    current = lib.get_status(hostname)
    project = current.get("project", "") if current else ""
    task_id = current.get("current_task", "") if current else ""

    if project:
        summary = lib.generate_session_summary(
            project=project,
            session_id=session_id,
            task_id=task_id if task_id else None,
            context_for_next="Session ended normally. Check git log for recent changes.",
        )
        lib.share_session_summary(summary)

    lib.update_status(state="idle", current_task="", project="")

    # IPC deregister
    try:
        from ipc.agent import deregister as ipc_deregister

        ipc_deregister()
    except Exception:
        pass

    # Warn about uncompleted claimed tasks
    claimed = lib.list_tasks("claimed")
    my_claimed = [t for t in claimed if t.get("claimed_by") == hostname]
    if my_claimed:
        ids = ", ".join(t.get("id", "?") for t in my_claimed)
        print(
            f"WARNING: {len(my_claimed)} claimed task(s) not completed: {ids}",
            file=sys.stderr,
        )


@hooks_app.command("heartbeat")
def hook_heartbeat() -> None:
    """Heartbeat hook: update timestamp, mark stale, check inbox."""
    import socket

    hostname = socket.gethostname()
    current = lib.get_status(hostname)
    if current:
        lib.update_status(
            state=current.get("state", "active"),
            current_task=current.get("current_task", ""),
            project=current.get("project", ""),
            session_id=current.get("session_id", ""),
            model=current.get("model", ""),
        )

    stale = lib.mark_stale_nodes()
    msgs = lib.read_inbox()

    parts = []
    if stale:
        parts.append(f"Marked stale: {', '.join(stale)}")
    if msgs:
        parts.append(f"{len(msgs)} unread message(s)")

    # IPC heartbeat + inbox check
    try:
        from ipc.agent import get_current_agent_id, refresh_heartbeat
        from ipc.direct import inbox_depth
        from ipc.dlq import sweep_pending

        refresh_heartbeat()
        sweep_pending()
        aid = get_current_agent_id()
        if aid:
            depth = inbox_depth(aid)
            if depth > 0:
                parts.append(f"{depth} IPC message(s)")
    except Exception:
        pass

    if parts:
        print(json.dumps({"systemMessage": "Swarm heartbeat: " + ". ".join(parts)}))


@hooks_app.command("task-check")
def hook_task_check() -> None:
    """Task check hook: report available matching tasks."""

    matching = lib.get_matching_tasks()
    if matching:
        titles = [t.get("title", t.get("id", "?"))[:60] for t in matching[:3]]
        msg = f"Swarm: {len(matching)} task(s) available: " + "; ".join(titles)
        print(json.dumps({"systemMessage": msg}))


@hooks_app.command("test")
def hook_test() -> None:
    """Self-test: run each hook handler in dry-run mode and report timing."""
    import time

    handlers = [
        ("session-start", hook_session_start),
        ("heartbeat", hook_heartbeat),
        ("task-check", hook_task_check),
    ]

    table = Table(title="Hook Self-Test")
    table.add_column("Hook", style="bold")
    table.add_column("Time (ms)")
    table.add_column("Status")

    for name, handler in handlers:
        start = time.time()
        try:
            # Suppress output during test
            import contextlib
            import io

            f = io.StringIO()
            with contextlib.redirect_stdout(f):
                handler()
            elapsed = (time.time() - start) * 1000
            status = "[green]OK[/green]" if elapsed < 500 else "[yellow]SLOW[/yellow]"
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            status = f"[red]FAIL: {e}[/red]"

        table.add_row(name, f"{elapsed:.0f}", status)

    console.print(table)


if __name__ == "__main__":
    app()
