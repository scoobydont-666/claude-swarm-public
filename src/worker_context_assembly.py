#!/usr/bin/env python3
"""Worker-side context assembly for routing-protocol-v1 §5.

Extends coordinator-side context_assembly.py with delta-mode retrieval:
Workers receive CB-assembled context instead of full file reads, enabling
token savings and narrower scope (workers don't need coordinator view).

Tier budgets are smaller than coordinator tier_ladder.
"""

import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Worker Tier Budgets (smaller than coordinator tiers per routing-protocol-v1)
# ---------------------------------------------------------------------------

@dataclass
class WorkerTierBudget:
    tier: str           # "worker-sm", "worker-md", "worker-lg"
    ctx_window: int     # tokens available to worker
    cb_exemplars: int   # tokens for CB delta retrieval
    task_prompt: int    # tokens for the task
    repo_files: int     # tokens for target files


WORKER_TIER_BUDGETS: dict[str, WorkerTierBudget] = {
    "worker-sm":  WorkerTierBudget("worker-sm",   8_000,  1_000,    500,  2_000),
    "worker-md":  WorkerTierBudget("worker-md",  16_000,  3_000,  1_000,  4_000),
    "worker-lg":  WorkerTierBudget("worker-lg",  32_000,  8_000,  2_000,  8_000),
}

# Default worker tier if not specified
DEFAULT_WORKER_TIER = "worker-md"

# CB MCP endpoint (context-bridge port 8518 per port registry)
_CB_MCP_URL = "http://127.0.0.1:8518/mcp"
_CB_CACHE_BASE = Path("/opt/swarm/artifacts/cb-cache-worker")
_MAX_CACHE_EXEMPLARS = 30  # Smaller cache for workers


# ---------------------------------------------------------------------------
# Worker system prompt template (delta variant)
# ---------------------------------------------------------------------------

_WORKER_SYSTEM_TEMPLATE = """\
You are a specialized code generation worker in Project Hydra's routing protocol. This is a focused worker task.

LANGUAGE: {language}
REPO: {repo_name}
TIER: {tier}
DELTA_MODE: {delta_mode}

Quality bar: match the code style shown in the exemplars. Output ONLY code — no prose, no markdown fences unless the language requires them.

Target files (delta context):
{target_files_block}

Relevant exemplars from this codebase (retrieved by CB):
{cb_exemplars_block}\
"""


# ---------------------------------------------------------------------------
# Token estimation (shared with coordinator)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough 4 chars / token. Good enough for budgeting."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# CB exemplar retrieval (delta mode)
# ---------------------------------------------------------------------------

def retrieve_worker_cb_exemplars(
    query: str,
    repo_name: str,
    budget_tokens: int,
    target_files: Optional[list[str]] = None,
) -> list[dict]:
    """Fetch exemplars from context-bridge in delta mode (narrower scope).

    Delta mode: if target_files are specified, CB search is scoped to those files.

    Returns list of {"source": str, "snippet": str, "tokens": int}.
    """
    if budget_tokens <= 0:
        return []

    # In delta mode, add file context to the query
    delta_context = ""
    if target_files and len(target_files) <= 3:
        delta_context = f" (focus: {', '.join(target_files)})"

    exemplars = _fetch_from_cb_delta(query + delta_context, repo_name)
    if exemplars is None:
        exemplars = _load_from_cache(repo_name)

    if exemplars is None:
        logger.warning("WARN: no worker CB exemplars available, reduced context")
        return []

    return _truncate_to_budget(exemplars, budget_tokens)


def _fetch_from_cb_delta(query: str, repo_name: str) -> Optional[list[dict]]:
    """POST to context-bridge MCP endpoint with delta scope. Returns raw exemplar list or None on failure."""
    payload = json.dumps({
        "method": "cb_search",
        "params": {"query": query, "limit": 5, "ns": repo_name},  # Limit smaller for workers
    }).encode()

    req = urllib.request.Request(
        _CB_MCP_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
            raw = body.get("result") or body.get("results") or []
            exemplars = []
            for item in raw:
                snippet = item.get("snippet") or item.get("content") or ""
                exemplars.append({
                    "source": item.get("source", ""),
                    "snippet": snippet,
                    "tokens": estimate_tokens(snippet),
                })
            return exemplars
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Worker CB HTTP failed (%s), trying cache", exc)
        return None


def _load_from_cache(repo_name: str) -> Optional[list[dict]]:
    cache_file = _CB_CACHE_BASE / repo_name / "exemplars.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Worker cache load failed for %s: %s", repo_name, exc)
        return None


def _truncate_to_budget(exemplars: list[dict], budget_tokens: int) -> list[dict]:
    """Return exemplars (in order) that fit within budget_tokens total."""
    kept: list[dict] = []
    used = 0
    for ex in exemplars:
        tok = ex.get("tokens") or estimate_tokens(ex.get("snippet", ""))
        if used + tok > budget_tokens:
            break
        kept.append(ex)
        used += tok
    return kept


# ---------------------------------------------------------------------------
# Cache write
# ---------------------------------------------------------------------------

def cache_worker_exemplars(repo_name: str, exemplars: list[dict]) -> None:
    """Write top-30 exemplars to /opt/swarm/artifacts/cb-cache-worker/<repo>/exemplars.json."""
    cache_dir = _CB_CACHE_BASE / repo_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "exemplars.json"
    top30 = exemplars[:_MAX_CACHE_EXEMPLARS]
    cache_file.write_text(json.dumps(top30, indent=2))
    logger.debug("Cached %d worker exemplars to %s", len(top30), cache_file)


# ---------------------------------------------------------------------------
# File loading helpers (delta: load only specified files)
# ---------------------------------------------------------------------------

def _load_file_verbatim(path: str, token_budget: int) -> tuple[str, int]:
    """Read file content; truncate to token_budget. Returns (text, tokens_used)."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError as exc:
        text = f"[ERROR reading {path}: {exc}]"
    tok = estimate_tokens(text)
    if tok > token_budget:
        char_limit = token_budget * 4
        text = text[:char_limit] + f"\n[... truncated at {token_budget} tokens ...]"
        tok = token_budget
    return text, tok


# ---------------------------------------------------------------------------
# Main worker context assembly function
# ---------------------------------------------------------------------------

def build_worker_dispatch_prompt(
    task_description: str,
    target_files: Optional[list[str]] = None,
    repo_name: Optional[str] = None,
    language: str = "python",
    worker_tier: Optional[str] = None,
    context_mode: str = "delta",  # "delta" or "full" (opt-out)
) -> dict:
    """Build a worker dispatch prompt with CB context assembly.

    Args:
        task_description: The task to complete
        target_files: List of file paths to include (delta mode)
        repo_name: Repo name for CB scoping
        language: Programming language
        worker_tier: Worker tier (defaults to DEFAULT_WORKER_TIER)
        context_mode: "delta" (default, CB-assembled) or "full" (legacy, full files)

    Returns {
        "system": str,
        "user": str,
        "metadata": {
            "cb_exemplars_used": int,
            "repo_files_attached": int,
            "estimated_tokens": int,
            "budget_exceeded": bool,
            "context_mode": str,
            "worker_tier": str,
            "assembled_context_bytes": int,
            "estimated_full_context_bytes": int,
        }
    }
    """
    if worker_tier is None:
        worker_tier = DEFAULT_WORKER_TIER

    budget = WORKER_TIER_BUDGETS.get(worker_tier)
    if budget is None:
        raise ValueError(
            f"Unknown worker tier {worker_tier!r}. Valid: {list(WORKER_TIER_BUDGETS)}"
        )

    target_files = target_files or []
    repo_name = repo_name or "unknown"

    # Legacy opt-out: full-file mode (no CB assembly)
    if context_mode == "full":
        logger.info("Worker context_mode=full: legacy full-file dispatch (no CB assembly)")
        return _build_legacy_worker_prompt(
            task_description, target_files, repo_name, language, worker_tier, budget
        )

    # Delta mode: CB-assembled context
    logger.debug("Worker context_mode=delta for %s", repo_name)

    # --- CB exemplars (narrower scope in delta mode) ---
    exemplars = retrieve_worker_cb_exemplars(
        task_description, repo_name, budget.cb_exemplars, target_files
    )
    if exemplars:
        cache_worker_exemplars(repo_name, exemplars)

    cb_block_parts: list[str] = []
    cb_tokens_used = 0
    for ex in exemplars:
        cb_block_parts.append(f"# {ex['source']}\n{ex['snippet']}")
        cb_tokens_used += ex.get("tokens", estimate_tokens(ex["snippet"]))
    cb_exemplars_block = "\n\n".join(cb_block_parts) if cb_block_parts else "(none)"

    # --- Target files (delta: only specified files) ---
    files_block_parts: list[str] = []
    repo_files_attached = 0
    remaining_file_budget = budget.repo_files
    assembled_context_bytes = 0
    estimated_full_context_bytes = 0

    for path in target_files:
        if remaining_file_budget <= 0:
            break
        content, used = _load_file_verbatim(path, remaining_file_budget)
        assembled_context_bytes += len(content.encode())
        files_block_parts.append(f"### {path}\n```{language}\n{content}\n```")
        remaining_file_budget -= used
        repo_files_attached += 1

    # Estimate full context for metrics
    for path in target_files:
        try:
            full_text = Path(path).read_text(errors="replace")
            estimated_full_context_bytes += len(full_text.encode())
        except OSError:
            pass

    target_files_block = "\n\n".join(files_block_parts) if files_block_parts else "(none)"

    # --- Render system prompt ---
    system_prompt = _WORKER_SYSTEM_TEMPLATE.format(
        language=language,
        repo_name=repo_name,
        tier=worker_tier,
        delta_mode=len(target_files) > 0,
        target_files_block=target_files_block,
        cb_exemplars_block=cb_exemplars_block,
    )

    # --- Estimate totals ---
    total_tokens = (
        estimate_tokens(system_prompt)
        + estimate_tokens(task_description)
    )
    budget_exceeded = total_tokens > budget.ctx_window

    # --- Metrics ---
    savings_pct = 0
    if estimated_full_context_bytes > 0:
        savings_pct = int(
            (1.0 - assembled_context_bytes / estimated_full_context_bytes) * 100
        )

    return {
        "system": system_prompt,
        "user": task_description,
        "metadata": {
            "cb_exemplars_used": cb_tokens_used,
            "repo_files_attached": repo_files_attached,
            "estimated_tokens": total_tokens,
            "budget_exceeded": budget_exceeded,
            "context_mode": context_mode,
            "worker_tier": worker_tier,
            "assembled_context_bytes": assembled_context_bytes,
            "estimated_full_context_bytes": estimated_full_context_bytes,
            "context_savings_pct": savings_pct,
        },
    }


def _build_legacy_worker_prompt(
    task_description: str,
    target_files: list[str],
    repo_name: str,
    language: str,
    worker_tier: str,
    budget: WorkerTierBudget,
) -> dict:
    """Legacy full-file dispatch (context_mode=full)."""
    files_block_parts: list[str] = []
    repo_files_attached = 0
    remaining_file_budget = budget.repo_files
    assembled_context_bytes = 0

    for path in target_files:
        if remaining_file_budget <= 0:
            break
        content, used = _load_file_verbatim(path, remaining_file_budget)
        assembled_context_bytes += len(content.encode())
        files_block_parts.append(f"### {path}\n```{language}\n{content}\n```")
        remaining_file_budget -= used
        repo_files_attached += 1

    target_files_block = "\n\n".join(files_block_parts) if files_block_parts else "(none)"

    system_prompt = _WORKER_SYSTEM_TEMPLATE.format(
        language=language,
        repo_name=repo_name,
        tier=worker_tier,
        delta_mode=False,
        target_files_block=target_files_block,
        cb_exemplars_block="(legacy mode — CB disabled)",
    )

    total_tokens = (
        estimate_tokens(system_prompt)
        + estimate_tokens(task_description)
    )
    budget_exceeded = total_tokens > budget.ctx_window

    return {
        "system": system_prompt,
        "user": task_description,
        "metadata": {
            "cb_exemplars_used": 0,
            "repo_files_attached": repo_files_attached,
            "estimated_tokens": total_tokens,
            "budget_exceeded": budget_exceeded,
            "context_mode": "full",
            "worker_tier": worker_tier,
            "assembled_context_bytes": assembled_context_bytes,
            "estimated_full_context_bytes": assembled_context_bytes,
            "context_savings_pct": 0,
        },
    }
