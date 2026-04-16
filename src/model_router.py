"""
Model Router — Unified task-to-model routing.

Classifies tasks by content and routes to the appropriate model tier:
  - LOCAL: Ollama models on fleet GPUs ($0)
  - HAIKU: Simple tasks (classify, search, format)
  - SONNET: Standard tasks (code gen, review, debug)
  - OPUS: Complex tasks (architecture, deep reasoning)

Replaces scattered routing logic in:
  - hydra_dispatch._model_for_task()
  - work_generator.infer_model()

Backported from NAI Swarm, stripped of Nutanix dependencies.
"""

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class RoutingRule:
    """A single task classification rule."""
    name: str
    pattern: str
    tier: str
    model: str
    fallback: str
    _compiled: re.Pattern | None = field(default=None, repr=False)

    def __post_init__(self):
        self._compiled = re.compile(self.pattern, re.IGNORECASE)

    def matches(self, text: str) -> bool:
        return bool(self._compiled and self._compiled.search(text))


@dataclass
class RouteDecision:
    """Result of routing a task."""
    rule_name: str
    tier: str
    model: str
    fallback_model: str
    local_model: str = ""
    reason: str = ""
    # Opus 4.7 extras — populated when the resolved model is 4.7-family.
    # Downstream API callers should forward these verbatim to messages.create.
    # beta_headers: list of beta header values (e.g. "task-budgets-2026-03-13")
    # task_budget: dict to splat into output_config (e.g. {"task_budget":
    #   {"type":"tokens","total":128000}}). Empty when not applicable.
    beta_headers: list[str] = field(default_factory=list)
    task_budget: dict = field(default_factory=dict)


# ── Default Rules ─────────────────────────────────────────────────────────────
# Applied when no routing.yaml is found or for quick in-code routing.

DEFAULT_RULES = [
    # OPUS tier — complex reasoning
    RoutingRule("architecture", r"architect|design.*system|security.*audit|threat.*model", "opus", "claude-opus-4-7", "claude-sonnet-4-6"),
    RoutingRule("deep_reasoning", r"complex.*debug|root.*cause|deep.*dive|explain.*why|reason.*about", "opus", "claude-opus-4-7", "claude-sonnet-4-6"),
    RoutingRule("planning", r"plan|strategy|roadmap|evaluate.*tradeoff", "opus", "claude-opus-4-7", "claude-sonnet-4-6"),

    # SONNET tier — standard code work
    RoutingRule("code_gen", r"implement|build|create|add.*feature|write.*code|refactor", "sonnet", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"),
    RoutingRule("code_review", r"review|audit.*code|check.*quality|lint", "sonnet", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"),
    RoutingRule("debug", r"fix.*bug|debug|troubleshoot|investigate.*error", "sonnet", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"),
    RoutingRule("test", r"write.*test|add.*test|test.*coverage|tdd", "sonnet", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"),

    # HAIKU tier — simple tasks
    RoutingRule("search", r"search|find|grep|locate|list.*files", "haiku", "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    RoutingRule("status", r"status|check|verify|health|monitor", "haiku", "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    RoutingRule("format", r"format|lint|style|rename|typo|spelling", "haiku", "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    RoutingRule("docs", r"document|readme|comment|docstring|changelog", "haiku", "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),

    # LOCAL tier — Ollama models (requires GPU)
    RoutingRule("local_inference", r"ollama|local.*model|qwen|devstral|deepseek", "local", "devstral:latest", "claude-sonnet-4-6"),
    RoutingRule("embedding", r"embed|vector|index.*code|semantic.*search", "local", "nomic-embed-text", "nomic-embed-text"),
    RoutingRule("tax_domain", r"tax|irs|cpa|deduction|1040|schedule.*c", "local", "christi-14b", "claude-sonnet-4-6"),
]

# Local model mapping (tier → Ollama model)
LOCAL_MODELS = {
    "haiku": "qwen3:8b",
    "sonnet": "devstral:latest",
    "opus": "deepseek-r1:32b",
}


# ── Router ────────────────────────────────────────────────────────────────────

class ModelRouter:
    """Unified model router with configurable rules."""

    # Opus 4.7 task_budget defaults — conservative ceiling for long-horizon
    # agent runs so the model self-regulates reasoning + tool-call spend.
    # Min is 20k per Anthropic spec; override via config["task_budget_total"].
    TASK_BUDGET_BETA_HEADER = "task-budgets-2026-03-13"
    DEFAULT_TASK_BUDGET_TOTAL = 128_000
    _OPUS_4_7_RE = re.compile(r"^claude-(?:opus|sonnet|haiku)-4-7\b")

    def __init__(
        self,
        config_path: str | Path | None = None,
        prefer_local: bool = False,
        task_budget_total: int | None = None,
    ):
        self.rules: list[RoutingRule] = []
        self.prefer_local = prefer_local
        self.task_budget_total = task_budget_total or self.DEFAULT_TASK_BUDGET_TOTAL

        if config_path and Path(config_path).exists():
            self._load_yaml(Path(config_path))
        else:
            self.rules = list(DEFAULT_RULES)
            logger.debug(f"Using {len(self.rules)} default routing rules")

    def _load_yaml(self, path: Path):
        """Load routing rules from YAML config."""
        try:
            with path.open() as f:
                data = yaml.safe_load(f) or {}

            routing = data.get("routing", data)
            for r in routing.get("rules", []):
                self.rules.append(RoutingRule(
                    name=r["name"],
                    pattern=r["pattern"],
                    tier=r.get("tier", "sonnet"),
                    model=r.get("model", "claude-sonnet-4-6"),
                    fallback=r.get("fallback", "claude-sonnet-4-6"),
                ))
            logger.info(f"Loaded {len(self.rules)} routing rules from {path}")
        except Exception as e:
            logger.warning(f"Failed to load routing config: {e}, using defaults")
            self.rules = list(DEFAULT_RULES)

    def route(self, task_description: str, context_tokens: int = 0) -> RouteDecision:
        """Classify a task and return the routing decision.

        Args:
            task_description: Natural language task description
            context_tokens: Estimated context size (for tier escalation)

        Returns:
            RouteDecision with model, tier, and fallback
        """
        # Try each rule in order
        for rule in self.rules:
            if rule.matches(task_description):
                decision = RouteDecision(
                    rule_name=rule.name,
                    tier=rule.tier,
                    model=rule.model,
                    fallback_model=rule.fallback,
                    local_model=LOCAL_MODELS.get(rule.tier, ""),
                    reason=f"Matched rule '{rule.name}' (pattern: {rule.pattern})",
                )

                # Context-size escalation
                if context_tokens > 100_000 and decision.tier == "haiku":
                    decision.tier = "sonnet"
                    decision.model = "claude-sonnet-4-6"
                    decision.reason += " [escalated: context > 100K tokens]"
                elif context_tokens > 200_000 and decision.tier == "sonnet":
                    decision.tier = "opus"
                    decision.model = "claude-opus-4-7"
                    decision.reason += " [escalated: context > 200K tokens]"

                # Local preference
                if self.prefer_local and decision.tier in LOCAL_MODELS:
                    decision.model = LOCAL_MODELS[decision.tier]
                    decision.reason += " [prefer_local: using Ollama]"

                # Opus 4.7 task_budget — populate beta header + payload so
                # downstream API callers can self-regulate long-horizon runs.
                if self._OPUS_4_7_RE.match(decision.model):
                    decision.beta_headers = [self.TASK_BUDGET_BETA_HEADER]
                    decision.task_budget = {
                        "type": "tokens",
                        "total": self.task_budget_total,
                    }

                return decision

        # Default: sonnet
        return RouteDecision(
            rule_name="default",
            tier="sonnet",
            model="claude-sonnet-4-6",
            fallback_model="claude-haiku-4-5-20251001",
            reason="No rule matched, defaulting to sonnet",
        )

    def classify_tier(self, task_description: str) -> str:
        """Quick tier classification (opus/sonnet/haiku/local)."""
        return self.route(task_description).tier

    def get_model(self, task_description: str) -> str:
        """Quick model selection."""
        return self.route(task_description).model


# ── Module-level convenience ──────────────────────────────────────────────────

_ROUTER: ModelRouter | None = None


def get_router() -> ModelRouter:
    """Get or create the global router instance."""
    global _ROUTER
    if _ROUTER is None:
        config_path = Path("/opt/claude-swarm/config/routing.yaml")
        _ROUTER = ModelRouter(config_path=config_path if config_path.exists() else None)
    return _ROUTER


def route_task(task_description: str, context_tokens: int = 0) -> RouteDecision:
    """Route a task using the global router."""
    return get_router().route(task_description, context_tokens)


def get_model_for_task(task_description: str) -> str:
    """Get the recommended model for a task."""
    return get_router().get_model(task_description)
