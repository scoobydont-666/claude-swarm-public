#!/usr/bin/env python3
"""Unit tests for src/worker_context_assembly.py."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

# Allow running from repo root or tests/ dir
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import worker_context_assembly as wca


# ---------------------------------------------------------------------------
# estimate_tokens (shared with coordinator)
# ---------------------------------------------------------------------------

class TestEstimateTokens(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(wca.estimate_tokens(""), 1)

    def test_four_chars_one_token(self):
        self.assertEqual(wca.estimate_tokens("abcd"), 1)

    def test_hundred_chars(self):
        result = wca.estimate_tokens("x" * 100)
        self.assertEqual(result, 25)


# ---------------------------------------------------------------------------
# Worker Tier Budgets
# ---------------------------------------------------------------------------

class TestWorkerTierBudgets(unittest.TestCase):
    def test_all_tiers_present(self):
        """Verify default worker tiers exist."""
        self.assertIn("worker-sm", wca.WORKER_TIER_BUDGETS)
        self.assertIn("worker-md", wca.WORKER_TIER_BUDGETS)
        self.assertIn("worker-lg", wca.WORKER_TIER_BUDGETS)

    def test_tier_sizes_increasing(self):
        """Worker tiers should increase in context window size."""
        sm = wca.WORKER_TIER_BUDGETS["worker-sm"]
        md = wca.WORKER_TIER_BUDGETS["worker-md"]
        lg = wca.WORKER_TIER_BUDGETS["worker-lg"]

        self.assertLess(sm.ctx_window, md.ctx_window)
        self.assertLess(md.ctx_window, lg.ctx_window)

    def test_default_tier_valid(self):
        """DEFAULT_WORKER_TIER must be in WORKER_TIER_BUDGETS."""
        self.assertIn(wca.DEFAULT_WORKER_TIER, wca.WORKER_TIER_BUDGETS)


# ---------------------------------------------------------------------------
# build_worker_dispatch_prompt — delta mode
# ---------------------------------------------------------------------------

class TestBuildWorkerDispatchPromptDelta(unittest.TestCase):
    def _no_cb(self, *args, **kwargs):
        """Stub: no CB exemplars."""
        return []

    def test_delta_mode_budget_sm(self):
        """worker-sm should respect 8k token budget."""
        with patch.object(wca, "retrieve_worker_cb_exemplars", self._no_cb):
            result = wca.build_worker_dispatch_prompt(
                task_description="Write a hello-world.",
                worker_tier="worker-sm",
                context_mode="delta",
            )
        meta = result["metadata"]
        self.assertFalse(
            meta["budget_exceeded"],
            f"worker-sm budget exceeded: {meta['estimated_tokens']} > 8000"
        )
        self.assertEqual(meta["context_mode"], "delta")
        self.assertEqual(meta["worker_tier"], "worker-sm")

    def test_delta_mode_budget_md(self):
        """worker-md should respect 16k token budget."""
        with patch.object(wca, "retrieve_worker_cb_exemplars", self._no_cb):
            result = wca.build_worker_dispatch_prompt(
                task_description="Refactor the GPU slot manager.",
                worker_tier="worker-md",
                context_mode="delta",
            )
        meta = result["metadata"]
        self.assertFalse(
            meta["budget_exceeded"],
            f"worker-md budget exceeded: {meta['estimated_tokens']} > 16000"
        )

    def test_delta_mode_budget_lg(self):
        """worker-lg should respect 32k token budget."""
        with patch.object(wca, "retrieve_worker_cb_exemplars", self._no_cb):
            result = wca.build_worker_dispatch_prompt(
                task_description="Add distributed tracing.",
                worker_tier="worker-lg",
                context_mode="delta",
            )
        meta = result["metadata"]
        self.assertFalse(
            meta["budget_exceeded"],
            f"worker-lg budget exceeded: {meta['estimated_tokens']} > 32000"
        )

    def test_delta_mode_target_files(self):
        """Delta mode should include target files."""
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("def foo(): pass\n")
            tmp_path = f.name

        try:
            with patch.object(wca, "retrieve_worker_cb_exemplars", self._no_cb):
                result = wca.build_worker_dispatch_prompt(
                    task_description="Fix foo.",
                    target_files=[tmp_path],
                    worker_tier="worker-md",
                    context_mode="delta",
                )
            self.assertEqual(result["metadata"]["repo_files_attached"], 1)
            self.assertIn("def foo(): pass", result["system"])
        finally:
            Path(tmp_path).unlink()

    def test_delta_narrow_exemplar_limit(self):
        """Delta mode should use smaller exemplar limit than coordinator."""
        # In _fetch_from_cb_delta, we use limit=5 for workers vs 10 for coordinator
        # This test verifies the behavior indirectly
        with patch.object(wca, "retrieve_worker_cb_exemplars", return_value=[]):
            result = wca.build_worker_dispatch_prompt(
                task_description="task",
                worker_tier="worker-md",
                context_mode="delta",
            )
        self.assertEqual(result["metadata"]["cb_exemplars_used"], 0)


# ---------------------------------------------------------------------------
# build_worker_dispatch_prompt — full mode (opt-out)
# ---------------------------------------------------------------------------

class TestBuildWorkerDispatchPromptFull(unittest.TestCase):
    def test_full_mode_disables_cb(self):
        """context_mode=full should disable CB assembly."""
        result = wca.build_worker_dispatch_prompt(
            task_description="task",
            worker_tier="worker-md",
            context_mode="full",
        )
        meta = result["metadata"]
        self.assertEqual(meta["context_mode"], "full")
        self.assertEqual(meta["cb_exemplars_used"], 0)
        self.assertIn("legacy mode", result["system"].lower())

    def test_full_mode_still_respects_budget(self):
        """context_mode=full should still enforce worker budget."""
        result = wca.build_worker_dispatch_prompt(
            task_description="task",
            worker_tier="worker-sm",
            context_mode="full",
        )
        meta = result["metadata"]
        self.assertFalse(meta["budget_exceeded"])


# ---------------------------------------------------------------------------
# Invalid tier raises
# ---------------------------------------------------------------------------

class TestInvalidTier(unittest.TestCase):
    def test_invalid_tier_raises_value_error(self):
        """Unknown tier should raise ValueError."""
        with self.assertRaises(ValueError) as ctx:
            wca.build_worker_dispatch_prompt(
                task_description="task",
                worker_tier="bogus-tier",
            )
        self.assertIn("bogus-tier", str(ctx.exception))


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

class TestWorkerTemplateRendering(unittest.TestCase):
    def _no_cb(self, *args, **kwargs):
        return []

    def test_system_prompt_includes_tier(self):
        """System prompt should include worker tier."""
        with patch.object(wca, "retrieve_worker_cb_exemplars", self._no_cb):
            result = wca.build_worker_dispatch_prompt(
                task_description="Do something.",
                worker_tier="worker-md",
                context_mode="delta",
            )
        self.assertIn("TIER: worker-md", result["system"])

    def test_system_prompt_includes_delta_flag(self):
        """System prompt should indicate DELTA_MODE when files are present."""
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("x = 1\n")
            tmp_path = f.name

        try:
            with patch.object(wca, "retrieve_worker_cb_exemplars", self._no_cb):
                result = wca.build_worker_dispatch_prompt(
                    task_description="Refactor.",
                    target_files=[tmp_path],
                    worker_tier="worker-md",
                    context_mode="delta",
                )
            self.assertIn("DELTA_MODE: True", result["system"])
        finally:
            Path(tmp_path).unlink()

    def test_user_is_task_description(self):
        """User message should be the task description."""
        with patch.object(wca, "retrieve_worker_cb_exemplars", self._no_cb):
            result = wca.build_worker_dispatch_prompt(
                task_description="Add retry logic.",
                worker_tier="worker-md",
                context_mode="delta",
            )
        self.assertEqual(result["user"], "Add retry logic.")

    def test_metadata_keys_present(self):
        """All required metadata keys should be present."""
        result = wca.build_worker_dispatch_prompt(
            task_description="task",
            worker_tier="worker-md",
        )
        meta = result["metadata"]
        for key in (
            "cb_exemplars_used",
            "repo_files_attached",
            "estimated_tokens",
            "budget_exceeded",
            "context_mode",
            "worker_tier",
            "assembled_context_bytes",
            "estimated_full_context_bytes",
            "context_savings_pct",
        ):
            self.assertIn(key, meta, f"Missing metadata key: {key}")


# ---------------------------------------------------------------------------
# CB fallback to cache
# ---------------------------------------------------------------------------

class TestCBFallback(unittest.TestCase):
    def test_fallback_to_cache_on_http_error(self):
        """When CB HTTP fails, should load from local cache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base = wca._CB_CACHE_BASE
            wca._CB_CACHE_BASE = Path(tmpdir)

            repo = "test-repo"
            cache_dir = Path(tmpdir) / repo
            cache_dir.mkdir(parents=True)
            sample = [{"source": "test-repo:foo.py", "snippet": "def bar(): pass", "tokens": 5}]
            (cache_dir / "exemplars.json").write_text(json.dumps(sample))

            try:
                with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
                    result = wca.retrieve_worker_cb_exemplars("query", repo, budget_tokens=200)

                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["source"], "test-repo:foo.py")
            finally:
                wca._CB_CACHE_BASE = original_base

    def test_returns_empty_when_both_fail(self):
        """No CB + no cache → empty list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base = wca._CB_CACHE_BASE
            wca._CB_CACHE_BASE = Path(tmpdir)
            try:
                with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
                    result = wca.retrieve_worker_cb_exemplars("query", "nonexistent", budget_tokens=100)
                self.assertEqual(result, [])
            finally:
                wca._CB_CACHE_BASE = original_base

    def test_zero_budget_returns_empty(self):
        result = wca.retrieve_worker_cb_exemplars("query", "repo", budget_tokens=0)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Cache write
# ---------------------------------------------------------------------------

class TestCacheWorkerExemplars(unittest.TestCase):
    def test_write_and_read_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base = wca._CB_CACHE_BASE
            wca._CB_CACHE_BASE = Path(tmpdir)
            try:
                exemplars = [{"source": "r:a.py", "snippet": "x = 1", "tokens": 2}]
                wca.cache_worker_exemplars("my-repo", exemplars)
                loaded = wca._load_from_cache("my-repo")
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded[0]["source"], "r:a.py")
            finally:
                wca._CB_CACHE_BASE = original_base

    def test_capped_at_30(self):
        """Worker cache should cap at 30 exemplars (vs 50 for coordinator)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base = wca._CB_CACHE_BASE
            wca._CB_CACHE_BASE = Path(tmpdir)
            try:
                big_list = [{"source": f"r:{i}.py", "snippet": "x", "tokens": 1} for i in range(60)]
                wca.cache_worker_exemplars("r", big_list)
                loaded = wca._load_from_cache("r")
                self.assertEqual(len(loaded), 30)
            finally:
                wca._CB_CACHE_BASE = original_base


# ---------------------------------------------------------------------------
# Context savings metrics
# ---------------------------------------------------------------------------

class TestContextSavingsMetrics(unittest.TestCase):
    def _no_cb(self, *args, **kwargs):
        return []

    def test_savings_pct_calculated(self):
        """context_savings_pct should reflect delta vs full context."""
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("x" * 4000)  # ~1000 tokens
            tmp_path = f.name

        try:
            with patch.object(wca, "retrieve_worker_cb_exemplars", self._no_cb):
                result = wca.build_worker_dispatch_prompt(
                    task_description="task",
                    target_files=[tmp_path],
                    worker_tier="worker-md",
                    context_mode="delta",
                )
            meta = result["metadata"]
            # Both should be same size for single file, savings = 0
            self.assertEqual(
                meta["assembled_context_bytes"],
                meta["estimated_full_context_bytes"]
            )
            self.assertEqual(meta["context_savings_pct"], 0)
        finally:
            Path(tmp_path).unlink()

    def test_no_savings_in_full_mode(self):
        """Full mode should show 0% savings."""
        result = wca.build_worker_dispatch_prompt(
            task_description="task",
            worker_tier="worker-md",
            context_mode="full",
        )
        self.assertEqual(result["metadata"]["context_savings_pct"], 0)


# ---------------------------------------------------------------------------
# Default tier selection
# ---------------------------------------------------------------------------

class TestDefaultTierSelection(unittest.TestCase):
    def test_none_tier_uses_default(self):
        """When worker_tier=None, should use DEFAULT_WORKER_TIER."""
        result = wca.build_worker_dispatch_prompt(
            task_description="task",
            worker_tier=None,  # explicitly None
        )
        self.assertEqual(
            result["metadata"]["worker_tier"],
            wca.DEFAULT_WORKER_TIER
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
