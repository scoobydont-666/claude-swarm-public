#!/usr/bin/env python3
"""Coordinator-side context assembly helper for routing-protocol-v1 §5.

Packages dispatch prompts for tiered local LLMs with CB exemplar retrieval,
repo-convention snippets, and target-file verbatim attachment.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier budgets (§5 of routing-protocol-v1.md)
# ---------------------------------------------------------------------------


@dataclass
class TierBudget:
    tier: str  # "1-3b", "1-7b", "2-14b", "3-32b", "4+"
    ctx_window: int  # tokens
    cb_exemplars: int  # tokens for CB retrieval
    task_prompt: int  # tokens for the ask
    repo_files: int  # tokens for repo-file context


TIER_BUDGETS: dict[str, TierBudget] = {
    "1-3b": TierBudget("1-3b", 8_000, 2_000, 1_000, 3_000),
    "1-7b": TierBudget("1-7b", 32_000, 8_000, 2_000, 16_000),
    "2-14b": TierBudget("2-14b", 32_000, 16_000, 2_000, 32_000),
    "3-32b": TierBudget("3-32b", 128_000, 32_000, 4_000, 64_000),
    "4+": TierBudget("4+", 200_000, 0, 0, 0),
}

# CB MCP endpoint (context-bridge port 8518 per port registry)
_CB_MCP_URL = "http://127.0.0.1:8518/mcp"
_CB_CACHE_BASE = Path("/opt/swarm/artifacts/cb-cache")
_MAX_CACHE_EXEMPLARS = 50

# ---------------------------------------------------------------------------
# Universal system prompt template
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You are a specialized code generation worker in Project Hydra's routing protocol. You are running on local GPU infrastructure.

LANGUAGE: {language}
REPO: {repo_name}
TIER: {tier}

Quality bar: match the code style and conventions shown in the exemplars below. Output ONLY code — no prose explanation, no markdown fences unless the language requires them.

Repository conventions:
{repo_conventions_block}

Relevant exemplars from this codebase (retrieved by CB):
{cb_exemplars_block}

Target files (verbatim):
{target_files_block}\
"""

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough 4 chars / token. Good enough for budgeting."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# CB exemplar retrieval
# ---------------------------------------------------------------------------


def retrieve_cb_exemplars(query: str, repo_name: str, budget_tokens: int) -> list[dict]:
    """Fetch exemplars from context-bridge, falling back to local cache.

    Returns list of {"source": str, "snippet": str, "tokens": int}.
    """
    if budget_tokens <= 0:
        return []

    exemplars = _fetch_from_cb(query, repo_name)
    if exemplars is None:
        exemplars = _load_from_cache(repo_name)

    if exemplars is None:
        logger.warning("WARN: no CB exemplars available, reduced context")
        return []

    return _truncate_to_budget(exemplars, budget_tokens)


def _fetch_from_cb(query: str, repo_name: str) -> list[dict] | None:
    """POST to context-bridge MCP endpoint. Returns raw exemplar list or None on failure."""
    payload = json.dumps(
        {
            "method": "cb_search",
            "params": {"query": query, "limit": 10, "ns": repo_name},
        }
    ).encode()

    req = urllib.request.Request(
        _CB_MCP_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
            # B1: validate against CB v1.0 contract (cb_schema). Replaces the
            # legacy `source`/`content` fallbacks that silently hid schema drift.
            from .cb_schema import parse_cb_search_response

            items = parse_cb_search_response(body)
            exemplars = [
                {
                    "source": item.source,
                    "snippet": item.snippet,
                    "tokens": estimate_tokens(item.snippet),
                }
                for item in items
            ]
            return exemplars
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.debug("CB HTTP failed (%s), trying cache", exc)
        return None


def _load_from_cache(repo_name: str) -> list[dict] | None:
    cache_file = _CB_CACHE_BASE / repo_name / "exemplars.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Cache load failed for %s: %s", repo_name, exc)
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


def cache_exemplars(repo_name: str, exemplars: list[dict]) -> None:
    """Write top-50 exemplars to /opt/swarm/artifacts/cb-cache/<repo>/exemplars.json."""
    cache_dir = _CB_CACHE_BASE / repo_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "exemplars.json"
    top50 = exemplars[:_MAX_CACHE_EXEMPLARS]
    cache_file.write_text(json.dumps(top50, indent=2))
    logger.debug("Cached %d exemplars to %s", len(top50), cache_file)


# ---------------------------------------------------------------------------
# File loading helpers
# ---------------------------------------------------------------------------


def _load_file_verbatim(path: str, token_budget: int) -> tuple[str, int]:
    """Read file content; truncate to token_budget. Returns (text, tokens_used)."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError as exc:
        text = f"[ERROR reading {path}: {exc}]"
    tok = estimate_tokens(text)
    if tok > token_budget:
        # Truncate to approximate char count
        char_limit = token_budget * 4
        text = text[:char_limit] + f"\n[... truncated at {token_budget} tokens ...]"
        tok = token_budget
    return text, tok


# ---------------------------------------------------------------------------
# Main assembly function
# ---------------------------------------------------------------------------


def build_dispatch_prompt(
    task_description: str,
    tier: str,
    language: str,
    target_files: list[str] | None = None,
    repo_conventions: list[str] | None = None,
    repo_name: str | None = None,
) -> dict:
    """Build a coordinator dispatch prompt for a local LLM worker.

    Returns {
        "system": str,
        "user": str,
        "metadata": {
            "cb_exemplars_used": int,
            "repo_files_attached": int,
            "estimated_tokens": int,
            "budget_exceeded": bool,
        }
    }
    """
    budget = TIER_BUDGETS.get(tier)
    if budget is None:
        raise ValueError(f"Unknown tier {tier!r}. Valid: {list(TIER_BUDGETS)}")

    target_files = target_files or []
    repo_conventions = repo_conventions or []
    repo_name = repo_name or "unknown"

    # --- CB exemplars ---
    exemplars = retrieve_cb_exemplars(task_description, repo_name, budget.cb_exemplars)
    if exemplars:
        cache_exemplars(repo_name, exemplars)

    cb_block_parts: list[str] = []
    cb_tokens_used = 0
    for ex in exemplars:
        cb_block_parts.append(f"# {ex['source']}\n{ex['snippet']}")
        cb_tokens_used += ex.get("tokens", estimate_tokens(ex["snippet"]))
    cb_exemplars_block = "\n\n".join(cb_block_parts) if cb_block_parts else "(none)"

    # --- Repo conventions ---
    conventions_block = (
        "\n".join(f"- {c}" for c in repo_conventions) if repo_conventions else "(none)"
    )

    # --- Target files ---
    files_block_parts: list[str] = []
    repo_files_attached = 0
    remaining_file_budget = budget.repo_files
    for path in target_files:
        if remaining_file_budget <= 0:
            break
        content, used = _load_file_verbatim(path, remaining_file_budget)
        files_block_parts.append(f"### {path}\n```{language}\n{content}\n```")
        remaining_file_budget -= used
        repo_files_attached += 1
    target_files_block = "\n\n".join(files_block_parts) if files_block_parts else "(none)"

    # --- Render system prompt ---
    system_prompt = _SYSTEM_TEMPLATE.format(
        language=language,
        repo_name=repo_name,
        tier=tier,
        repo_conventions_block=conventions_block,
        cb_exemplars_block=cb_exemplars_block,
        target_files_block=target_files_block,
    )

    # --- Estimate totals ---
    total_tokens = estimate_tokens(system_prompt) + estimate_tokens(task_description)
    budget_exceeded = total_tokens > budget.ctx_window

    return {
        "system": system_prompt,
        "user": task_description,
        "metadata": {
            "cb_exemplars_used": cb_tokens_used,
            "repo_files_attached": repo_files_attached,
            "estimated_tokens": total_tokens,
            "budget_exceeded": budget_exceeded,
        },
    }
