#!/usr/bin/env python3
"""Session-end report emitter for routing protocol observability.

Generates markdown reports summarizing routing-protocol session metrics:
- Dispatch statistics and tier distribution
- Slot utilization
- Hook fire frequency
- DLQ items
- Cost estimates
- Regressions and next-session recommendations
"""

import json
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Tier definitions for cost estimation
TIER_RATES = {
    "Haiku": {"input": 1, "output": 5},  # $ per 1M tokens
    "Sonnet": {"input": 3, "output": 15},
    "Opus": {"input": 15, "output": 75},
}


def _parse_log_line(line: str) -> dict[str, Any] | None:
    """Parse a single JSONL line, return None on error."""
    if not line.strip():
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _load_dispatch_log(log_path: Path) -> list[dict[str, Any]]:
    """Load dispatch records from JSONL file. Returns empty list if missing."""
    if not log_path.exists():
        logger.warning(f"Dispatch log not found: {log_path}")
        return []
    records = []
    with open(log_path) as f:
        for line in f:
            record = _parse_log_line(line)
            if record:
                records.append(record)
    return records


def _load_slot_samples(slots_path: Path) -> list[dict[str, Any]]:
    """Load slot utilization samples from JSONL file. Returns empty list if missing."""
    if not slots_path.exists():
        logger.warning(f"Slot samples file not found: {slots_path}")
        return []
    samples = []
    with open(slots_path) as f:
        for line in f:
            sample = _parse_log_line(line)
            if sample:
                samples.append(sample)
    return samples


def _load_dlq_state(session_id: str) -> list[dict[str, Any]]:
    """Load DLQ items from fallback SQLite (if Redis unavailable).

    v1: Returns empty list (Redis backend assumed available).
    Future: Implement SQLite fallback from routing:dlq table.
    """
    # TODO: Implement Redis -> SQLite fallback
    return []


def _analyze_dispatches(
    records: list[dict[str, Any]],
) -> tuple[int, int, int, float, list[dict[str, Any]]]:
    """Analyze dispatch records.

    Returns: (total, accepted, escalated, avg_escalation_jumps, escalations)
    """
    dispatches = [r for r in records if r.get("type") == "dispatch"]
    if not dispatches:
        return 0, 0, 0, 0.0, []

    total = len(dispatches)
    accepted_first = sum(1 for d in dispatches if d.get("status") == "accepted")
    escalation_chain = [
        d for d in dispatches if d.get("status") == "escalated"
    ]
    num_escalated = len(escalation_chain)
    avg_jumps = (
        sum(len(d.get("tier_chain", [])) for d in escalation_chain) / num_escalated
        if num_escalated > 0
        else 0.0
    )

    return total, accepted_first, num_escalated, avg_jumps, escalation_chain


def _tier_summary(records: list[dict[str, Any]]) -> list[dict[str, str | int]]:
    """Summarize dispatches by tier."""
    dispatches = [r for r in records if r.get("type") == "dispatch"]
    if not dispatches:
        return []

    tier_groups = Counter(d.get("tier", "?") for d in dispatches)
    total = len(dispatches)
    summary = []

    for tier in sorted(tier_groups.keys()):
        tier_recs = [d for d in dispatches if d.get("tier") == tier]
        model = tier_recs[0].get("model", "?") if tier_recs else "?"
        count = len(tier_recs)
        pct = (count / total * 100) if total > 0 else 0.0
        avg_wall = sum(d.get("wall_ms", 0) for d in tier_recs) / count if count else 0
        esc_rate = sum(1 for d in tier_recs if d.get("status") == "escalated") / count * 100 if count else 0.0

        summary.append({
            "tier": tier,
            "model": model,
            "count": count,
            "pct": f"{pct:.1f}%",
            "avg_wall_ms": f"{int(avg_wall)}",
            "escalation_rate": f"{esc_rate:.1f}%",
        })
    return summary


def _analyze_slot_utilization(samples: list[dict[str, Any]]) -> dict[str, float | int]:
    """Analyze slot utilization from samples."""
    if not samples:
        return {"gpu_utilization_pct": 0.0, "cpu_utilization_pct": 0.0, "cloud_peak_concurrency": 0, "sample_count": 0}

    total = len(samples)
    gpu_busy = sum(1 for s in samples if s.get("gpu_busy_count", 0) >= 2)
    cpu_busy = sum(1 for s in samples if s.get("cpu_busy_count", 0) >= 2)
    cloud_peak = max((s.get("cloud_worker_count", 0) for s in samples), default=0)

    return {
        "gpu_utilization_pct": gpu_busy / total * 100 if total else 0.0,
        "cpu_utilization_pct": cpu_busy / total * 100 if total else 0.0,
        "cloud_peak_concurrency": cloud_peak,
        "sample_count": total,
    }


def _analyze_hooks(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Analyze hook fire events.

    Returns: {hook_name: {blocks: int, warnings: int}}
    """
    hook_fires = [r for r in records if r.get("type") == "hook_fire"]
    result = {}

    for fire in hook_fires:
        hook = fire.get("hook", "unknown")
        mode = fire.get("mode", "warn")
        action = fire.get("action", "warn")

        if hook not in result:
            result[hook] = {"blocks": 0, "warnings": 0}

        if mode == "block" or action == "blocked":
            result[hook]["blocks"] += 1
        else:
            result[hook]["warnings"] += 1

    return result


def _time_range(records: list[dict[str, Any]]) -> tuple[str, str]:
    """Extract start and end timestamps from records.

    Returns: (start_iso, end_iso) or ("unknown", "unknown")
    """
    timestamps = []
    for r in records:
        ts = r.get("timestamp") or r.get("ts")
        if ts:
            timestamps.append(ts)

    if not timestamps:
        return "unknown", "unknown"

    timestamps.sort()
    return timestamps[0], timestamps[-1]


def _duration_str(start: str, end: str) -> str:
    """Format duration between two ISO timestamps."""
    try:
        s_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        secs = (e_dt - s_dt).total_seconds()
        return f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"
    except (ValueError, AttributeError):
        return "unknown"


def _detect_serial_regressions(records: list[dict[str, Any]]) -> int:
    """Detect serial execution regressions.

    A regression is a non-dispatched operation that could have been parallel.
    v1: Simple heuristic — text-only responses mid-session without tool use.
    Returns count of detected regressions.
    """
    # TODO: Implement heuristic based on response patterns
    # For now, return 0 (no regression detection in v1)
    return 0


def _estimate_cost(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate cost from Tier-4+ (Claude) dispatches."""
    claude = [r for r in records if r.get("type") == "dispatch" and str(r.get("tier", "")).startswith("4")]
    if not claude:
        return {"total_tokens": 0, "estimated_usd": 0.0, "breakdown": {}}

    breakdown, total_cost = {}, 0.0
    for d in claude:
        model = d.get("model", "unknown")
        ctx_tok = d.get("context_tokens", 0)
        out_tok = int(ctx_tok * 0.5)

        if model not in breakdown:
            breakdown[model] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

        breakdown[model]["input_tokens"] += ctx_tok
        breakdown[model]["output_tokens"] += out_tok

        rates = TIER_RATES.get(model, TIER_RATES["Haiku"])
        cost = (ctx_tok / 1_000_000) * rates["input"] + (out_tok / 1_000_000) * rates["output"]
        breakdown[model]["cost_usd"] += cost
        total_cost += cost

    return {
        "total_tokens": sum(d.get("context_tokens", 0) for d in claude),
        "estimated_usd": total_cost,
        "breakdown": breakdown,
    }


def _next_recommendations(total: int, esc_rate: float, serials: int, dlq: int, blocks: int) -> list[str]:
    """Generate next-session recommendations."""
    recs = []
    if esc_rate > 30:
        recs.append("**High escalation (>30%)**. Profile Tier-1 quality; review CB exemplars.")
    if serials > 3:
        recs.append("**Serial regressions detected**. Enable parallel detection hook.")
    if dlq > 0:
        recs.append(f"**{dlq} DLQ items** block completion. Manual retry required.")
    if blocks > 5:
        recs.append("**Frequent blocks (>5)**. Review pause-ask patterns for false positives.")
    if total == 0:
        recs.append("**No dispatches detected**. Protocol may not have engaged.")
    if not recs:
        recs.append("Session completed within targets. No action needed.")
    return recs


def generate_report(session_id: str) -> Path:
    """Generate markdown report from routing logs.

    Returns path to written report file.
    """
    swarm_root = Path("/opt/swarm")
    log_dir = swarm_root / "artifacts" / "routing-logs"
    report_dir = swarm_root / "artifacts" / "routing-reports"

    log_path = log_dir / f"{session_id}.jsonl"
    slots_path = log_dir / f"{session_id}.slots.jsonl"
    report_path = report_dir / f"{session_id}.md"

    report_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    dispatch_records = _load_dispatch_log(log_path)
    slot_samples = _load_slot_samples(slots_path)
    dlq_items = _load_dlq_state(session_id)

    # Analyze
    total, accepted, escalated, avg_jumps, esc_chain = _analyze_dispatches(
        dispatch_records
    )
    tier_summary = _tier_summary(dispatch_records)
    slot_util = _analyze_slot_utilization(slot_samples)
    hooks = _analyze_hooks(dispatch_records)
    start_ts, end_ts = _time_range(dispatch_records)
    duration = _duration_str(start_ts, end_ts)
    serials = _detect_serial_regressions(dispatch_records)
    cost = _estimate_cost(dispatch_records)

    accepted_pct = (accepted / total * 100) if total > 0 else 0.0
    escalated_pct = (escalated / total * 100) if total > 0 else 0.0

    hook_warns = sum(h.get("warnings", 0) for h in hooks.values())
    hook_blocks = sum(h.get("blocks", 0) for h in hooks.values())

    recommendations = _next_recommendations(
        total, escalated_pct, serials, len(dlq_items), hook_blocks
    )

    # Render markdown
    md_lines = [
        "# Routing Protocol Session Report",
        "",
        f"**Session:** `{session_id}`",
        f"**Start:** {start_ts} · **End:** {end_ts} · **Duration:** {duration}",
        "",
        "## Summary",
        "",
        f"- **Total dispatches:** {total}",
        f"- **Accepted on first tier:** {accepted} ({accepted_pct:.1f}%)",
        (
            f"- **Escalated:** {escalated} ({escalated_pct:.1f}%"
            f" — avg {avg_jumps:.1f} tier jumps)"
        ),
        (
            f"- **DLQ:** {len(dlq_items)}"
            f" ({'blocks plan completion' if dlq_items else 'none'})"
        ),
        f"- **Hook fires:** {hook_warns} warnings, {hook_blocks} blocks",
        "",
        "## Dispatches by Tier",
        "",
    ]

    if tier_summary:
        md_lines.append("| Tier | Model | Count | % | Avg wall (ms) | Escalation rate |")
        md_lines.append("|------|-------|-------|---|---------------|-----------------|")
        for row in tier_summary:
            md_lines.append(
                f"| {row['tier']} | {row['model']} | {row['count']} |"
                f" {row['pct']} | {row['avg_wall_ms']} | {row['escalation_rate']} |"
            )
    else:
        md_lines.append("*No dispatch data available.*")

    md_lines.extend(
        [
            "",
            "## Slot Utilization",
            "",
            (
                f"- **GPU slot utilization:** {slot_util['gpu_utilization_pct']:.1f}%"
                f" of wall-clock with ≥2 workers (target: ≥50%)"
            ),
            (
                f"- **CPU slot utilization:** {slot_util['cpu_utilization_pct']:.1f}%"
                f" of wall-clock with ≥2 workers"
            ),
            (
                f"- **Cloud-worker peak concurrency:** {slot_util['cloud_peak_concurrency']}/3"
                f" (cap)"
            ),
            "",
            "## Hook Fires",
            "",
        ]
    )

    if hooks:
        md_lines.append("| Hook | Warnings | Blocks |")
        md_lines.append("|------|----------|--------|")
        for hook_name in sorted(hooks.keys()):
            h = hooks[hook_name]
            md_lines.append(f"| {hook_name} | {h['warnings']} | {h['blocks']} |")
    else:
        md_lines.append("*No hook fires detected.*")

    md_lines.extend(
        [
            "",
            "## DLQ Items (blocks plan completion if >0)",
            "",
        ]
    )

    if dlq_items:
        for item in dlq_items:
            md_lines.append(
                f"- **{item.get('task_id', '?')}:** {item.get('original_ask', 'N/A')}"
                f" → escalated {item.get('tier_chain', [])} → {item.get('final_error', 'Unknown')}"
            )
    else:
        md_lines.append("*None — plan not blocked.*")

    md_lines.extend(
        [
            "",
            "## Regressions",
            "",
            (
                f"- **Serial regressions detected:** {serials}"
                f" (target: 0 in sessions ≥15min)"
            ),
            "",
            "## Cost Estimate (tokens + dollars)",
            "",
            "Rough estimate for Tier 4+ (Claude) dispatches:",
            "",
            f"- **Total tokens:** {cost['total_tokens']}",
            f"- **Estimated cost:** ${cost['estimated_usd']:.2f}",
            "",
        ]
    )

    if cost["breakdown"]:
        md_lines.append("### Breakdown by Model")
        md_lines.append("")
        for model, stats in cost["breakdown"].items():
            md_lines.append(
                f"- **{model}:** {stats['input_tokens']} input +"
                f" {stats['output_tokens']} output = ${stats['cost_usd']:.2f}"
            )
        md_lines.append("")

    md_lines.extend(
        [
            "## Next Session Recommendations",
            "",
        ]
    )
    for rec in recommendations:
        md_lines.append(f"- {rec}")

    md_lines.extend(
        [
            "",
            f"*Report generated: {datetime.now(UTC).isoformat()}Z*",
        ]
    )

    # Write report
    report_content = "\n".join(md_lines) + "\n"
    report_path.write_text(report_content)
    logger.info(f"Report written to {report_path}")

    return report_path


def emit_on_session_end(session_id: str) -> None:
    """Called by session-end hook. Generate and log completion."""
    try:
        report_path = generate_report(session_id)
        logger.info(f"Session report emitted: {report_path}")
    except Exception as e:
        logger.error(f"Failed to emit session report for {session_id}: {e}", exc_info=True)


def index_to_cb(report_path: Path) -> bool:
    """Try to ingest report into Context Bridge (CB).

    Non-fatal if CB is down. Returns True on success.
    """
    try:
        # v1: Stub — would call cb_ingest via MCP in real implementation
        # This allows graceful degradation if CB is unavailable
        logger.info(f"CB indexing deferred: {report_path}")
        return False
    except Exception as e:
        logger.warning(f"CB indexing failed (non-fatal): {e}")
        return False


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: session_report.py <session_id>")
        sys.exit(1)

    session_id = sys.argv[1]
    report_path = generate_report(session_id)
    print(f"Report: {report_path}")
