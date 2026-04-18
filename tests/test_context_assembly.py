#!/usr/bin/env python3
"""Unit tests for src/context_assembly.py."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

# Allow running from repo root or tests/ dir
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import context_assembly as ca


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(ca.estimate_tokens(""), 1)  # max(1, 0)

    def test_four_chars_one_token(self):
        self.assertEqual(ca.estimate_tokens("abcd"), 1)

    def test_hundred_chars(self):
        result = ca.estimate_tokens("x" * 100)
        self.assertEqual(result, 25)

    def test_ballpark_accuracy(self):
        # 1000 chars → ~250 tokens; allow ±20%
        text = "a" * 1000
        tokens = ca.estimate_tokens(text)
        self.assertGreater(tokens, 200)
        self.assertLess(tokens, 300)


# ---------------------------------------------------------------------------
# build_dispatch_prompt — budget compliance
# ---------------------------------------------------------------------------

class TestBuildDispatchPromptBudget(unittest.TestCase):
    """build_dispatch_prompt must not exceed ctx_window for any tier."""

    def _no_cb(self, *args, **kwargs):
        """Stub: no CB exemplars so we have deterministic sizes."""
        return []

    def test_budget_respected_1_3b(self):
        with patch.object(ca, "retrieve_cb_exemplars", self._no_cb):
            result = ca.build_dispatch_prompt(
                task_description="Write a hello-world function.",
                tier="1-3b",
                language="python",
            )
        meta = result["metadata"]
        self.assertFalse(meta["budget_exceeded"],
                         f"1-3b budget exceeded: {meta['estimated_tokens']} > 8000")

    def test_budget_respected_2_14b(self):
        with patch.object(ca, "retrieve_cb_exemplars", self._no_cb):
            result = ca.build_dispatch_prompt(
                task_description="Refactor the GPU slot manager.",
                tier="2-14b",
                language="python",
                repo_name="claude-swarm",
            )
        meta = result["metadata"]
        self.assertFalse(meta["budget_exceeded"],
                         f"2-14b budget exceeded: {meta['estimated_tokens']} > 32000")

    def test_budget_respected_3_32b(self):
        with patch.object(ca, "retrieve_cb_exemplars", self._no_cb):
            result = ca.build_dispatch_prompt(
                task_description="Add distributed tracing to the pipeline.",
                tier="3-32b",
                language="go",
            )
        meta = result["metadata"]
        self.assertFalse(meta["budget_exceeded"],
                         f"3-32b budget exceeded: {meta['estimated_tokens']} > 128000")

    def test_invalid_tier_raises(self):
        with self.assertRaises(ValueError):
            ca.build_dispatch_prompt("task", tier="bogus", language="python")


# ---------------------------------------------------------------------------
# build_dispatch_prompt — template rendering with empty inputs
# ---------------------------------------------------------------------------

class TestBuildDispatchPromptRendering(unittest.TestCase):
    def _no_cb(self, *args, **kwargs):
        return []

    def test_empty_lists_render_none_placeholders(self):
        with patch.object(ca, "retrieve_cb_exemplars", self._no_cb):
            result = ca.build_dispatch_prompt(
                task_description="Do something.",
                tier="1-7b",
                language="bash",
                target_files=None,
                repo_conventions=None,
                repo_name=None,
            )
        system = result["system"]
        self.assertIn("(none)", system)
        self.assertIn("LANGUAGE: bash", system)
        self.assertIn("TIER: 1-7b", system)

    def test_user_is_task_description(self):
        with patch.object(ca, "retrieve_cb_exemplars", self._no_cb):
            result = ca.build_dispatch_prompt(
                task_description="Add retry logic.",
                tier="1-7b",
                language="rust",
            )
        self.assertEqual(result["user"], "Add retry logic.")

    def test_repo_conventions_appear_in_system(self):
        with patch.object(ca, "retrieve_cb_exemplars", self._no_cb):
            result = ca.build_dispatch_prompt(
                task_description="task",
                tier="1-7b",
                language="python",
                repo_conventions=["Use type hints", "Prefer pathlib"],
            )
        self.assertIn("Use type hints", result["system"])
        self.assertIn("Prefer pathlib", result["system"])

    def test_metadata_keys_present(self):
        with patch.object(ca, "retrieve_cb_exemplars", self._no_cb):
            result = ca.build_dispatch_prompt("t", tier="1-7b", language="yaml")
        meta = result["metadata"]
        for key in ("cb_exemplars_used", "repo_files_attached", "estimated_tokens", "budget_exceeded"):
            self.assertIn(key, meta, f"Missing metadata key: {key}")

    def test_target_files_attached_count(self):
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("def foo(): pass\n")
            tmp_path = f.name
        with patch.object(ca, "retrieve_cb_exemplars", self._no_cb):
            result = ca.build_dispatch_prompt(
                task_description="Fix foo.",
                tier="2-14b",
                language="python",
                target_files=[tmp_path],
            )
        self.assertEqual(result["metadata"]["repo_files_attached"], 1)
        self.assertIn("def foo(): pass", result["system"])


# ---------------------------------------------------------------------------
# CB fallback to cache when HTTP fails
# ---------------------------------------------------------------------------

class TestCBFallback(unittest.TestCase):
    def test_fallback_to_cache_on_http_error(self):
        """When CB HTTP fails, exemplars should load from local cache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Override cache base
            original_base = ca._CB_CACHE_BASE
            ca._CB_CACHE_BASE = Path(tmpdir)

            repo = "test-repo"
            cache_dir = Path(tmpdir) / repo
            cache_dir.mkdir(parents=True)
            sample = [{"source": "test-repo:foo.py", "snippet": "def bar(): pass", "tokens": 5}]
            (cache_dir / "exemplars.json").write_text(json.dumps(sample))

            try:
                # Patch urlopen to raise URLError
                with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
                    result = ca.retrieve_cb_exemplars("some query", repo, budget_tokens=200)

                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["source"], "test-repo:foo.py")
            finally:
                ca._CB_CACHE_BASE = original_base

    def test_returns_empty_when_both_fail(self):
        """No CB + no cache → empty list, no exception."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base = ca._CB_CACHE_BASE
            ca._CB_CACHE_BASE = Path(tmpdir)  # empty — no cache files
            try:
                with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
                    result = ca.retrieve_cb_exemplars("query", "nonexistent-repo", budget_tokens=100)
                self.assertEqual(result, [])
            finally:
                ca._CB_CACHE_BASE = original_base

    def test_zero_budget_returns_empty(self):
        result = ca.retrieve_cb_exemplars("query", "repo", budget_tokens=0)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# cache_exemplars
# ---------------------------------------------------------------------------

class TestCacheExemplars(unittest.TestCase):
    def test_write_and_read_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base = ca._CB_CACHE_BASE
            ca._CB_CACHE_BASE = Path(tmpdir)
            try:
                exemplars = [{"source": "r:a.py", "snippet": "x = 1", "tokens": 2}]
                ca.cache_exemplars("my-repo", exemplars)
                loaded = ca._load_from_cache("my-repo")
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded[0]["source"], "r:a.py")
            finally:
                ca._CB_CACHE_BASE = original_base

    def test_capped_at_50(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base = ca._CB_CACHE_BASE
            ca._CB_CACHE_BASE = Path(tmpdir)
            try:
                big_list = [{"source": f"r:{i}.py", "snippet": "x", "tokens": 1} for i in range(100)]
                ca.cache_exemplars("r", big_list)
                loaded = ca._load_from_cache("r")
                self.assertEqual(len(loaded), 50)
            finally:
                ca._CB_CACHE_BASE = original_base


if __name__ == "__main__":
    unittest.main(verbosity=2)
