"""Tests for model-size routing — NAI Swarm backport."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hydra_dispatch import (
    _load_routing_config,
    classify_model_size,
    get_model_gpu_requirements,
)


class TestLoadRoutingConfig:
    def test_config_loads(self):
        """Routing config loads from disk."""
        config = _load_routing_config()
        assert "models" in config
        assert "rules" in config
        assert "fleet_gpus" in config

    def test_models_have_required_fields(self):
        config = _load_routing_config()
        for size, info in config["models"].items():
            assert "gpu_count" in info, f"Model {size} missing gpu_count"
            assert "min_vram_gb" in info, f"Model {size} missing min_vram_gb"
            assert "hosts" in info, f"Model {size} missing hosts"

    def test_rules_have_required_fields(self):
        config = _load_routing_config()
        for rule in config["rules"]:
            assert "name" in rule
            assert "pattern" in rule


class TestClassifyModelSize:
    def test_small_model(self):
        result = classify_model_size("deploy qwen2.5-coder-7b on ollama")
        assert result is not None
        assert result["model_size"] == "7b"
        assert result["gpu_count"] == 1
        assert result["gpu_required"] is True

    def test_medium_model(self):
        result = classify_model_size("load the 13b codellama model")
        assert result is not None
        assert result["model_size"] == "13b"

    def test_large_model(self):
        result = classify_model_size("benchmark deepseek-r1-32b performance")
        assert result is not None
        assert result["model_size"] == "32b"
        assert result["gpu_count"] == 1
        assert result["min_vram_gb"] >= 24

    def test_xlarge_model(self):
        result = classify_model_size("run llama3.3-70b inference")
        assert result is not None
        assert result["model_size"] == "70b"
        assert "GIGA" in result["hosts"]

    def test_code_analysis_no_gpu(self):
        result = classify_model_size("fix the login bug in auth.py")
        assert result is not None
        assert result["gpu_required"] is False
        assert result["model_size"] is None

    def test_no_match_returns_none(self):
        result = classify_model_size("write documentation for the API")
        assert result is None

    def test_refactor_matches_code_analysis(self):
        result = classify_model_size("refactor the database layer")
        assert result is not None
        assert result["rule_name"] == "code-analysis"


class TestGetModelGPURequirements:
    def test_known_model_by_example(self):
        result = get_model_gpu_requirements("qwen2.5-coder-7b")
        assert result is not None
        assert result["gpu_count"] == 1
        assert result["min_vram_gb"] == 8

    def test_known_model_by_size(self):
        result = get_model_gpu_requirements("some-custom-70b-model")
        assert result is not None
        assert result["model_size"] == "70b"
        assert result["min_vram_gb"] >= 48

    def test_unknown_model(self):
        result = get_model_gpu_requirements("completely-unknown-model")
        assert result is None

    def test_large_model_hosts(self):
        result = get_model_gpu_requirements("llama3.3-70b")
        assert result is not None
        assert "GIGA" in result["hosts"]

    def test_405b_needs_tensor_parallel(self):
        result = get_model_gpu_requirements("llama3.1-405b")
        assert result is not None
        assert result["gpu_count"] == 4
        assert result.get("tensor_parallel") is True
