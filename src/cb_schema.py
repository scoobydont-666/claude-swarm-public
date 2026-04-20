"""CB↔Swarm response schema — B1 contract versioning.

Closes the silent-drift risk identified in the 2026-04-18 audit: claude-swarm
was reading ``item.get("source", "")`` which CB never sends, silently
degrading to empty strings in assembled context. Plus the ``|| content``
fallback masked schema changes.

This module defines the shape claude-swarm expects from CB's ``cb_search``
tool and validates every item. Field names match CB's actual SearchResult
interface in /opt/context-bridge-mcp-server/src/types.ts.

Plan: <hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase B1
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Expected CB contract version. Increment when the SearchResult shape
# changes in a backward-incompatible way. claude-swarm logs a WARN when
# it sees an unknown version; callers can choose to reject in strict mode.
EXPECTED_CB_CONTRACT_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class CBSearchResultItem:
    """Single hit returned from CB ``cb_search`` under the v1.0 contract.

    Mirrors the TypeScript ``SearchResult.results[]`` shape from
    /opt/context-bridge-mcp-server/src/types.ts. Fields documented there
    are the source of truth; this is the consumer-side declaration.
    """

    alias: str
    chunk_index: int
    chunk_label: str
    snippet: str
    rank: int
    bm25_score: float

    @property
    def source(self) -> str:
        """Human-readable source reference used in assembled context blocks.

        Format: "<alias>:<chunk_label>" (stable across CB restarts; safe for
        dedup). Replaces the legacy ``item.get("source", "")`` lookup that
        always returned empty because CB never sent that field.
        """
        return f"{self.alias}:{self.chunk_label}"


def parse_cb_search_response(
    body: dict[str, Any], *, strict: bool = False
) -> list[CBSearchResultItem]:
    """Validate + parse a CB cb_search JSON response body.

    Args:
        body: Decoded JSON from CB. Expected keys: ``results`` (list) and
            optionally ``schema_version``. Also tolerates the legacy
            ``result`` key (singular) for backward compat with pre-1.0 CB.
        strict: If True, raise ValueError on any malformed item or missing
            required field. If False (default), skip malformed items and
            log WARN.

    Returns:
        List of validated CBSearchResultItem instances. Empty list on
        missing/empty response or if every item failed validation.
    """
    if not isinstance(body, dict):
        msg = f"CB response is not a JSON object: {type(body).__name__}"
        if strict:
            raise ValueError(msg)
        logger.warning(msg)
        return []

    # Version gate — WARN on drift so operators notice silent breakage early.
    version = body.get("schema_version")
    if version is not None and version != EXPECTED_CB_CONTRACT_VERSION:
        logger.warning(
            "CB schema_version drift: expected %s, got %s — results may not parse",
            EXPECTED_CB_CONTRACT_VERSION,
            version,
        )

    # Accept both 'results' (current) and 'result' (legacy singular) for
    # rolling-upgrade compat. Remove legacy key once all CB instances updated.
    raw_list = body.get("results")
    if raw_list is None:
        raw_list = body.get("result")
    if raw_list is None:
        return []
    if not isinstance(raw_list, list):
        msg = f"CB results is not a list: {type(raw_list).__name__}"
        if strict:
            raise ValueError(msg)
        logger.warning(msg)
        return []

    out: list[CBSearchResultItem] = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            logger.warning("CB result[%d] is not an object: %r", idx, type(item).__name__)
            continue
        try:
            out.append(
                CBSearchResultItem(
                    alias=str(item["alias"]),
                    chunk_index=int(item["chunk_index"]),
                    chunk_label=str(item["chunk_label"]),
                    snippet=str(item["snippet"]),
                    rank=int(item.get("rank", idx + 1)),
                    bm25_score=float(item.get("bm25_score", 0.0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            msg = (
                f"CB result[{idx}] missing/invalid field: {exc}; "
                f"keys present={sorted(item.keys())}"
            )
            if strict:
                raise ValueError(msg) from exc
            logger.warning(msg)
            continue

    return out
