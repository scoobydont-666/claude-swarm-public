"""Tests for remote session orchestration — strategy decisions and execution plans."""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remote_session import (
    ExecutionStrategy,
    TaskComplexity,
    plan_execution,
    _classify_complexity,
    _needs_remote_resources,
    _needs_interactive,
    _select_model,
)


class TestClassifyComplexity:
    def test_trivial(self):
        assert _classify_complexity("check docker status") == TaskComplexity.TRIVIAL

    def test_simple(self):
        assert _classify_complexity("install kin binary") == TaskComplexity.SIMPLE

    def test_moderate(self):
        assert (
            _classify_complexity("implement JWT auth layer") == TaskComplexity.MODERATE
        )

    def test_complex(self):
        assert (
            _classify_complexity("debug complex race condition in pipeline")
            == TaskComplexity.COMPLEX
        )

    def test_exploratory(self):
        assert (
            _classify_complexity("explore why the RAG returns stale data")
            == TaskComplexity.EXPLORATORY
        )

    def test_default_moderate(self):
        assert (
            _classify_complexity("do something with the config")
            == TaskComplexity.MODERATE
        )


class TestNeedsRemoteResources:
    def test_gpu_task(self):
        needs, host = _needs_remote_resources("run inference on GPU with Ollama")
        assert needs is True
        assert host == "GIGA"

    def test_docker_task(self):
        needs, host = _needs_remote_resources("deploy docker swarm stack")
        assert needs is True
        assert host == "GIGA"

    def test_local_task(self):
        needs, host = _needs_remote_resources("edit the README file")
        assert needs is False
        assert host == ""

    def test_christi_needs_giga(self):
        needs, host = _needs_remote_resources("update Christi RAG embeddings")
        assert needs is True
        assert host == "GIGA"


class TestNeedsInteractive:
    def test_debug_is_interactive(self):
        assert (
            _needs_interactive("debug why tests fail", TaskComplexity.MODERATE) is True
        )

    def test_investigate_is_interactive(self):
        assert (
            _needs_interactive("investigate memory leak", TaskComplexity.MODERATE)
            is True
        )

    def test_complex_is_interactive(self):
        assert (
            _needs_interactive("refactor the auth module", TaskComplexity.COMPLEX)
            is True
        )

    def test_simple_not_interactive(self):
        assert _needs_interactive("copy file to server", TaskComplexity.SIMPLE) is False

    def test_trivial_not_interactive(self):
        assert _needs_interactive("check status", TaskComplexity.TRIVIAL) is False


class TestSelectModel:
    def test_trivial_gets_haiku(self):
        assert _select_model(TaskComplexity.TRIVIAL, False) == "haiku"

    def test_simple_gets_sonnet(self):
        assert _select_model(TaskComplexity.SIMPLE, False) == "sonnet"

    def test_complex_interactive_gets_opus(self):
        assert _select_model(TaskComplexity.COMPLEX, True) == "opus"

    def test_complex_non_interactive_gets_sonnet(self):
        assert _select_model(TaskComplexity.COMPLEX, False) == "sonnet"


class TestPlanExecution:
    def test_local_task_on_same_host(self):
        plan = plan_execution("edit config file", current_host="miniboss")
        assert plan.strategy == ExecutionStrategy.LOCAL
        assert plan.host == "miniboss"

    def test_gpu_task_routes_to_giga(self):
        plan = plan_execution("run Ollama inference", current_host="miniboss")
        assert plan.host == "GIGA"
        assert plan.needs_ollama is True

    def test_simple_remote_uses_dispatch(self):
        plan = plan_execution(
            "install package on GIGA",
            current_host="miniboss",
            force_host="GIGA",
        )
        assert plan.strategy == ExecutionStrategy.REMOTE_DISPATCH

    def test_complex_remote_uses_session(self):
        plan = plan_execution(
            "debug complex race condition in Docker Swarm service",
            current_host="miniboss",
        )
        assert plan.host == "GIGA"  # Docker → GIGA
        assert plan.strategy == ExecutionStrategy.REMOTE_SESSION
        assert plan.model == "opus"

    def test_force_host_overrides(self):
        plan = plan_execution("edit file", current_host="miniboss", force_host="GIGA")
        assert plan.host == "GIGA"

    def test_project_affinity_christi(self):
        plan = plan_execution(
            "add new endpoint",
            current_host="miniboss",
            project_dir="/opt/christi-project",
        )
        assert plan.host == "GIGA"  # Christi is a GPU project

    def test_project_affinity_local(self):
        plan = plan_execution(
            "update tests",
            current_host="miniboss",
            project_dir="/opt/monero-farm",
        )
        assert plan.host == "miniboss"

    def test_reasoning_populated(self):
        plan = plan_execution("check Ollama model list", current_host="miniboss")
        assert plan.reasoning  # Not empty
        assert (
            "GIGA" in plan.reasoning
            or "GPU" in plan.reasoning.upper()
            or "resource" in plan.reasoning
        )

    def test_estimated_minutes(self):
        trivial = plan_execution("check status", current_host="miniboss")
        complex_ = plan_execution("architect new microservice", current_host="miniboss")
        assert trivial.estimated_minutes < complex_.estimated_minutes

    def test_max_turns_trivial(self):
        plan = plan_execution(
            "check version", current_host="miniboss", force_host="GIGA"
        )
        assert plan.max_turns == 3

    def test_max_turns_interactive(self):
        plan = plan_execution(
            "debug why Christi embeddings are stale",
            current_host="miniboss",
        )
        assert plan.max_turns == 0  # Unlimited for interactive

    def test_exploratory_gets_session(self):
        plan = plan_execution(
            "explore why the Docker Swarm pipeline is slow",
            current_host="miniboss",
        )
        assert plan.host == "GIGA"  # Docker → GIGA
        assert plan.strategy == ExecutionStrategy.REMOTE_SESSION
        assert plan.complexity == TaskComplexity.EXPLORATORY
