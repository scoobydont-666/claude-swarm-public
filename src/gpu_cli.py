#!/usr/bin/env python3
# plan-approved: claude-swarm-scripts
"""GPU fleet CLI — flip hosts between Ollama and vLLM inference modes.

Exposes three subcommands:

    swarm gpu status                 — show current inference mode per fleet host
    swarm gpu flip <host> <mode>     — flip single host to ollama|vllm
    swarm gpu flip-all <mode>        — flip every eligible host in parallel

Replaces the shell wrapper <hydra-project-path>/scripts/fleet/flip-inference-mode.sh
with a typed, idempotent Python implementation that:
  * reads the fleet host list from config/swarm.yaml (no hardcoded hosts)
  * pre-flights node_gpu for active training before any flip
  * uses kubectl via SSH to giga (KUBECONFIG lives there) to scale deployments
  * waits for readiness and verifies Ollama /api/tags after flip-to-ollama
  * returns non-zero if any host failed so CI/pipeline can detect partial flips

2026-04-23 lesson (feedback_dogfood_claude_swarm_from_start): the fleet
capability index run needed an inference-mode flip. The shell script worked
one-host-at-a-time; swarm gpu flip-all parallelises this while keeping the
same safety gates.
"""

from __future__ import annotations

import concurrent.futures
import subprocess

import typer
from rich.console import Console
from rich.table import Table

from util import fleet_from_config, resolve_host_key

console = Console()
gpu_app = typer.Typer(help="GPU fleet management — Ollama/vLLM mode flips")


# Deployment-to-host mapping. Fleet-specific; kept in the CLI module so a new
# host added to swarm.yaml doesn't silently get skipped by the shell script.
# Empty list means "no vLLM deployment on this host" (Ollama-only).
_VLLM_DEPLOYMENTS: dict[str, list[str]] = {
    "mecha": ["vllm-mecha-gpu0"],
    "mega": ["vllm-mega-tandem"],
    "node_primary": ["vllm-node_primary-gpu0"],
    "giga": [],
    "mongo": [],
}

_OLLAMA_DEPLOYMENTS: dict[str, list[str]] = {
    "mecha": ["ollama-mecha-gpu0"],
    "mega": ["ollama-mega-gpu0", "ollama-mega-gpu1"],
    "node_primary": ["ollama-node_primary-gpu0"],
    "giga": ["ollama-giga-gpu0"],
    "mongo": ["ollama-mongo-gpu0"],
}

_NAMESPACE = "ai-cluster"
_KUBECTL_BASTION = "giga"  # KUBECONFIG lives on node_gpu
_SCALE_DOWN_TIMEOUT = 120
_SCALE_UP_TIMEOUT = 180


def _kubectl(args: list[str]) -> subprocess.CompletedProcess:
    """Run kubectl via SSH to the bastion host that holds KUBECONFIG."""
    cmd = ["ssh", "-o", "ConnectTimeout=10", _KUBECTL_BASTION, "kubectl", "-n", _NAMESPACE] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def _preflight_no_training() -> tuple[bool, str]:
    """Return (ok, reason). Aborts flip if node_gpu has an active training run.

    Per feedback_no_interrupt_training — never interrupt node_gpu during training.
    """
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "giga", "pgrep", "-af", "unsloth|train\\.py|finetune"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode == 0 and result.stdout.strip():
        return False, f"node_gpu has active training process(es):\n{result.stdout}"
    return True, ""


def _scale(deployment: str, replicas: int, wait_timeout: int) -> tuple[bool, str]:
    """Scale a deployment and wait for pod ready / delete."""
    r = _kubectl(["scale", "deployment", deployment, f"--replicas={replicas}"])
    if r.returncode != 0:
        return False, f"scale {deployment}={replicas} failed: {r.stderr.strip()}"

    if replicas == 0:
        # Wait for pod deletion — tolerate timeout (pods may already be gone)
        _kubectl(
            [
                "wait",
                "--for=delete",
                "pod",
                "-l",
                f"app={deployment.rsplit('-gpu', 1)[0]}",
                f"--timeout={wait_timeout}s",
            ],
        )
        return True, f"scaled down {deployment}"

    # replicas >= 1 — wait for ready
    r = _kubectl(
        [
            "wait",
            "--for=condition=ready",
            "pod",
            "-l",
            f"app={deployment.rsplit('-gpu', 1)[0]}",
            f"--timeout={wait_timeout}s",
        ],
    )
    if r.returncode != 0:
        return False, f"wait ready {deployment}: {r.stderr.strip()}"
    return True, f"scaled up {deployment}"


def _verify_ollama(host: str) -> tuple[bool, int | None]:
    """Confirm Ollama /api/tags responds and count models."""
    cmd = ["ssh", "-o", "ConnectTimeout=5", host, "curl", "-sf", "http://127.0.0.1:11434/api/tags"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return False, None
    try:
        import json as _json

        data = _json.loads(r.stdout)
        return True, len(data.get("models", []))
    except (ValueError, KeyError):
        return True, None  # responding but parse failed — still counts as up


def _flip_one(host: str, mode: str) -> tuple[str, bool, list[str]]:
    """Flip a single host. Returns (host, success, log_lines)."""
    canonical = resolve_host_key(host, fleet_from_config()) or host
    log: list[str] = [f"[{canonical}] flip → {mode}"]

    # Normalize host key for deployment lookup (keys are lowercase)
    host_key = canonical.lower()
    vllm = _VLLM_DEPLOYMENTS.get(host_key, [])
    ollama = _OLLAMA_DEPLOYMENTS.get(host_key, [])

    if not vllm and not ollama:
        log.append(f"[{canonical}] no known deployments — skipping")
        return canonical, True, log

    if mode == "ollama":
        down_targets, up_targets = vllm, ollama
    elif mode == "vllm":
        if not vllm:
            log.append(f"[{canonical}] no vLLM deployment configured — cannot flip to vllm")
            return canonical, False, log
        down_targets, up_targets = ollama, vllm
    else:
        log.append(f"[{canonical}] unknown mode '{mode}'")
        return canonical, False, log

    # Scale down first to free VRAM, then scale up
    for d in down_targets:
        ok, msg = _scale(d, 0, _SCALE_DOWN_TIMEOUT)
        log.append(f"[{canonical}] {msg}")
        if not ok:
            return canonical, False, log

    for d in up_targets:
        ok, msg = _scale(d, 1, _SCALE_UP_TIMEOUT)
        log.append(f"[{canonical}] {msg}")
        if not ok:
            return canonical, False, log

    if mode == "ollama":
        ok, count = _verify_ollama(host_key)
        if ok:
            log.append(f"[{canonical}] verified Ollama :11434 ({count} models)")
        else:
            log.append(f"[{canonical}] WARN: Ollama not responding on :11434")
            return canonical, False, log

    return canonical, True, log


def _eligible_hosts(fleet: dict) -> list[str]:
    """Fleet hosts that have any deployment (Ollama or vLLM) mapping."""
    return [h for h in fleet if h.lower() in _OLLAMA_DEPLOYMENTS or h.lower() in _VLLM_DEPLOYMENTS]


@gpu_app.command("status")
def gpu_status() -> None:
    """Show current inference mode per fleet host (from kubectl replicas)."""
    fleet = fleet_from_config()
    hosts = _eligible_hosts(fleet)
    if not hosts:
        console.print("[red]No eligible hosts in swarm.yaml[/red]")
        raise typer.Exit(1)

    table = Table(title="GPU Fleet — Inference Mode")
    table.add_column("Host", style="bold")
    table.add_column("Ollama")
    table.add_column("vLLM")
    table.add_column("Mode")

    for host in hosts:
        key = host.lower()
        ollama_deploys = _OLLAMA_DEPLOYMENTS.get(key, [])
        vllm_deploys = _VLLM_DEPLOYMENTS.get(key, [])

        ollama_up = sum(_deployment_replicas(d) for d in ollama_deploys)
        vllm_up = sum(_deployment_replicas(d) for d in vllm_deploys)
        total_ollama = len(ollama_deploys)
        total_vllm = len(vllm_deploys)

        mode = "?"
        if ollama_up > 0 and vllm_up == 0:
            mode = "[green]ollama[/green]"
        elif vllm_up > 0 and ollama_up == 0:
            mode = "[cyan]vllm[/cyan]"
        elif ollama_up == 0 and vllm_up == 0:
            mode = "[red]down[/red]"
        else:
            mode = "[yellow]mixed[/yellow]"

        table.add_row(
            host,
            f"{ollama_up}/{total_ollama}" if total_ollama else "-",
            f"{vllm_up}/{total_vllm}" if total_vllm else "-",
            mode,
        )

    console.print(table)


def _deployment_replicas(deployment: str) -> int:
    """Return current ready-replicas for a deployment (0 if not found/not ready)."""
    r = _kubectl(["get", "deployment", deployment, "-o", "jsonpath={.status.readyReplicas}"])
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.strip() or "0")
    except ValueError:
        return 0


@gpu_app.command("flip")
def gpu_flip(
    host: str = typer.Argument(..., help="Fleet host (case-insensitive)"),
    mode: str = typer.Argument(..., help="Target mode: ollama | vllm"),
    skip_preflight: bool = typer.Option(
        False, "--skip-preflight", help="Skip node_gpu training check (dangerous)"
    ),
) -> None:
    """Flip a single fleet host between Ollama and vLLM."""
    if mode not in ("ollama", "vllm"):
        console.print(f"[red]Unknown mode '{mode}' (expected ollama|vllm)[/red]")
        raise typer.Exit(2)

    fleet = fleet_from_config()
    canonical = resolve_host_key(host, fleet)
    if not canonical:
        console.print(f"[red]Unknown host '{host}' — not in swarm.yaml[/red]")
        raise typer.Exit(2)

    if not skip_preflight:
        ok, reason = _preflight_no_training()
        if not ok:
            console.print(f"[red]Preflight failed: {reason}[/red]")
            console.print("[yellow]Use --skip-preflight to override (not recommended)[/yellow]")
            raise typer.Exit(3)

    console.print(f"[bold]Flipping {canonical} → {mode}[/bold]")
    _, ok, log = _flip_one(canonical, mode)
    for line in log:
        console.print(line)

    if not ok:
        raise typer.Exit(4)


@gpu_app.command("flip-all")
def gpu_flip_all(
    mode: str = typer.Argument(..., help="Target mode: ollama | vllm"),
    skip_preflight: bool = typer.Option(
        False, "--skip-preflight", help="Skip node_gpu training check (dangerous)"
    ),
    exclude: list[str] = typer.Option(
        [], "--exclude", "-e", help="Host(s) to exclude (repeatable)"
    ),
    max_parallel: int = typer.Option(5, "--max-parallel", "-p", help="Max concurrent flips"),
) -> None:
    """Flip every eligible fleet host to the target mode in parallel."""
    if mode not in ("ollama", "vllm"):
        console.print(f"[red]Unknown mode '{mode}' (expected ollama|vllm)[/red]")
        raise typer.Exit(2)

    if not skip_preflight:
        ok, reason = _preflight_no_training()
        if not ok:
            console.print(f"[red]Preflight failed: {reason}[/red]")
            console.print("[yellow]Use --skip-preflight to override (not recommended)[/yellow]")
            raise typer.Exit(3)

    fleet = fleet_from_config()
    excluded_lower = {e.lower() for e in exclude}
    targets = [h for h in _eligible_hosts(fleet) if h.lower() not in excluded_lower]

    if not targets:
        console.print("[yellow]No eligible targets after exclusions[/yellow]")
        raise typer.Exit(0)

    # For mode=vllm, drop hosts with no vLLM deployment (they'd fail the flip)
    if mode == "vllm":
        targets = [t for t in targets if _VLLM_DEPLOYMENTS.get(t.lower())]

    console.print(f"[bold]Flipping {len(targets)} hosts → {mode}[/bold]: {', '.join(targets)}")

    results: dict[str, tuple[bool, list[str]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as ex:
        futures = {ex.submit(_flip_one, t, mode): t for t in targets}
        for fut in concurrent.futures.as_completed(futures):
            host_name, ok, log = fut.result()
            results[host_name] = (ok, log)

    # Summary
    console.print()
    table = Table(title=f"Flip results → {mode}")
    table.add_column("Host")
    table.add_column("Status")
    failed = []
    for host in targets:
        ok, _ = results.get(host, (False, []))
        status = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
        if not ok:
            failed.append(host)
        table.add_row(host, status)
    console.print(table)

    # Detail log for failed hosts
    if failed:
        console.print("\n[red]Failed hosts:[/red]")
        for host in failed:
            _, log = results[host]
            for line in log:
                console.print(line)
        raise typer.Exit(4)


if __name__ == "__main__":
    gpu_app()
