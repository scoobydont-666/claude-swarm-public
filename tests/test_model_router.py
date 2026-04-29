"""Tests for unified model router."""

import pytest
from src.model_router import ModelRouter, RouteDecision, route_task, DEFAULT_RULES


@pytest.fixture
def router():
    return ModelRouter()  # uses DEFAULT_RULES


class TestRouting:
    def test_architecture_routes_to_opus(self, router):
        decision = router.route("Design the system architecture for the new API")
        assert decision.tier == "opus"
        assert "opus" in decision.model

    def test_code_gen_routes_to_sonnet(self, router):
        decision = router.route("Implement a new REST endpoint for user management")
        assert decision.tier == "sonnet"
        assert "sonnet" in decision.model

    def test_search_routes_to_haiku(self, router):
        decision = router.route("Search for all Python files containing 'async def'")
        assert decision.tier == "haiku"
        assert "haiku" in decision.model

    def test_debug_routes_to_sonnet(self, router):
        decision = router.route("Fix the bug in the authentication middleware")
        assert decision.tier == "sonnet"

    def test_docs_routes_to_haiku(self, router):
        decision = router.route("Update the README with installation instructions")
        assert decision.tier == "haiku"

    def test_local_inference(self, router):
        decision = router.route("Run qwen3 model on local GPU")
        assert decision.tier == "local"

    def test_tax_domain(self, router):
        decision = router.route("Calculate Schedule C deductions for 2025")
        assert decision.tier == "local"
        assert "project-a" in decision.model

    def test_default_fallback(self, router):
        decision = router.route("xyzzy random unknown task type")
        assert decision.tier == "sonnet"
        assert decision.rule_name == "default"


class TestContextEscalation:
    def test_haiku_escalated_on_large_context(self, router):
        decision = router.route("Search for patterns in codebase", context_tokens=150_000)
        assert decision.tier == "sonnet"
        assert "escalated" in decision.reason

    def test_sonnet_escalated_on_huge_context(self, router):
        decision = router.route("Implement feature in large codebase", context_tokens=250_000)
        assert decision.tier == "opus"
        assert "escalated" in decision.reason

    def test_no_escalation_on_small_context(self, router):
        decision = router.route("Search for files", context_tokens=10_000)
        assert decision.tier == "haiku"


class TestLocalPreference:
    def test_prefer_local_uses_ollama(self):
        router = ModelRouter(prefer_local=True)
        decision = router.route("Implement a new feature")
        assert "devstral" in decision.model or "qwen" in decision.model or "deepseek" in decision.model


class TestConvenience:
    def test_route_task_function(self):
        decision = route_task("Fix the deployment script")
        assert isinstance(decision, RouteDecision)
        assert decision.model is not None

    def test_classify_tier(self):
        router = ModelRouter()
        assert router.classify_tier("Design system architecture") == "opus"
        assert router.classify_tier("Search for files") == "haiku"
        assert router.classify_tier("Write a new function") == "sonnet"

    def test_get_model(self):
        router = ModelRouter()
        model = router.get_model("Write tests for the API")
        assert "sonnet" in model or "haiku" in model
