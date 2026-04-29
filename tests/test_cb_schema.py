"""B1: CB contract schema tests.

Covers the silent-drift fix from
<hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase B1.

Before: claude-swarm read ``item.get("source", "")`` — CB never sent that
field, so source was always empty. The ``|| content`` fallback also masked
schema drift.

Now: parse_cb_search_response validates every item against the v1.0
contract (alias + chunk_label + snippet required). Missing fields get
logged and skipped rather than silently producing empty strings.
"""

from __future__ import annotations

import logging

import pytest

from src.cb_schema import (
    EXPECTED_CB_CONTRACT_VERSION,
    CBSearchResultItem,
    parse_cb_search_response,
)


def _valid_body(n: int = 2) -> dict:
    return {
        "query": "how to configure X",
        "total_matches": n,
        "schema_version": EXPECTED_CB_CONTRACT_VERSION,
        "results": [
            {
                "alias": f"doc-{i}",
                "chunk_index": i,
                "chunk_label": f"chunk-label-{i}",
                "rank": i + 1,
                "bm25_score": 0.5 * (i + 1),
                "snippet": f"excerpt {i}",
            }
            for i in range(n)
        ],
    }


class TestParseCBSearchResponse:
    def test_valid_body_parses_all_items(self):
        items = parse_cb_search_response(_valid_body(3))
        assert len(items) == 3
        assert all(isinstance(i, CBSearchResultItem) for i in items)
        assert items[0].chunk_label == "chunk-label-0"
        assert items[0].snippet == "excerpt 0"

    def test_source_property_combines_alias_and_label(self):
        items = parse_cb_search_response(_valid_body(1))
        assert items[0].source == "doc-0:chunk-label-0"

    def test_empty_body_returns_empty_list(self):
        assert parse_cb_search_response({}) == []
        assert parse_cb_search_response({"results": []}) == []

    def test_legacy_singular_result_key_still_parses(self):
        """Rolling-upgrade: older CB returning 'result' (singular) must still parse."""
        body = _valid_body(1)
        body["result"] = body.pop("results")
        items = parse_cb_search_response(body)
        assert len(items) == 1

    def test_malformed_item_is_skipped_by_default(self, caplog):
        body = _valid_body(2)
        # Drop required field from one item
        body["results"][0] = {"alias": "doc-0"}  # missing chunk_label, snippet, etc
        with caplog.at_level(logging.WARNING):
            items = parse_cb_search_response(body)
        assert len(items) == 1  # second item parsed
        assert items[0].chunk_label == "chunk-label-1"
        assert any("missing/invalid field" in r.message for r in caplog.records)

    def test_strict_mode_raises_on_malformed_item(self):
        body = _valid_body(1)
        body["results"][0] = {"alias": "doc-0"}  # missing required fields
        with pytest.raises(ValueError, match="missing/invalid field"):
            parse_cb_search_response(body, strict=True)

    def test_version_mismatch_logs_warning(self, caplog):
        body = _valid_body(1)
        body["schema_version"] = "2.0.0"
        with caplog.at_level(logging.WARNING):
            parse_cb_search_response(body)
        assert any("schema_version drift" in r.message for r in caplog.records), (
            f"expected schema_version drift warning; got: {[r.message for r in caplog.records]}"
        )

    def test_non_dict_body_logged_and_empty(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = parse_cb_search_response("not a dict")  # type: ignore[arg-type]
        assert result == []
        assert any("not a JSON object" in r.message for r in caplog.records)

    def test_results_not_a_list_logged_and_empty(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = parse_cb_search_response({"results": "not a list"})
        assert result == []
        assert any("not a list" in r.message for r in caplog.records)

    def test_missing_rank_and_bm25_defaults(self):
        """rank and bm25_score are optional — defaults used if absent."""
        body = _valid_body(1)
        del body["results"][0]["rank"]
        del body["results"][0]["bm25_score"]
        items = parse_cb_search_response(body)
        assert len(items) == 1
        assert items[0].rank == 1  # default: idx + 1
        assert items[0].bm25_score == 0.0
