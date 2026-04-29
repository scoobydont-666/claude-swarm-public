"""Tests for DispatchSpec typed dispatch specifications."""

from dataclasses import asdict
from unittest.mock import patch

import pytest

from hydra_dispatch import DispatchSpec, DispatchResult, dispatch_from_spec


class TestDispatchSpec:
    def test_defaults(self):
        spec = DispatchSpec(task="run tests")
        assert spec.task == "run tests"
        assert spec.host is None
        assert spec.model is None
        assert spec.background is True
        assert spec.track is True
        assert spec.requires == []
        assert spec.timeout_minutes == 30

    def test_full_spec(self):
        spec = DispatchSpec(
            task="generate questions",
            host="node_gpu",
            model="opus",
            project_dir="/opt/examforge",
            timeout_minutes=60,
            requires=["gpu", "ollama"],
            task_id="task-001",
        )
        assert spec.host == "node_gpu"
        assert spec.model == "opus"
        assert spec.requires == ["gpu", "ollama"]
        assert spec.task_id == "task-001"

    def test_serializable(self):
        spec = DispatchSpec(task="test", host="node_gpu")
        d = asdict(spec)
        assert d["task"] == "test"
        assert d["host"] == "node_gpu"
        assert isinstance(d["requires"], list)


class TestDispatchFromSpec:
    @patch("hydra_dispatch.dispatch")
    @patch("hydra_dispatch._find_best_host")
    def test_auto_routes_when_no_host(self, mock_find, mock_dispatch):
        mock_find.return_value = "node_gpu"
        mock_dispatch.return_value = DispatchResult(
            dispatch_id="d-1",
            host="node_gpu",
            task="test",
            model="sonnet",
            status="running",
        )
        spec = DispatchSpec(task="test", requires=["gpu"])
        result = dispatch_from_spec(spec)
        mock_find.assert_called_once_with(["gpu"], "test")
        assert result.host == "node_gpu"

    @patch("hydra_dispatch.dispatch")
    def test_uses_explicit_host(self, mock_dispatch):
        mock_dispatch.return_value = DispatchResult(
            dispatch_id="d-1",
            host="node_reserve2",
            task="test",
            model="sonnet",
            status="running",
        )
        spec = DispatchSpec(task="test", host="node_reserve2")
        result = dispatch_from_spec(spec)
        mock_dispatch.assert_called_once()
        assert (
            mock_dispatch.call_args[1]["host"] == "node_reserve2"
            or mock_dispatch.call_args[0][0] == "node_reserve2"
        )

    @patch("hydra_dispatch.dispatch")
    @patch("hydra_dispatch._find_best_host")
    def test_raises_when_no_host_found(self, mock_find, mock_dispatch):
        mock_find.return_value = None
        spec = DispatchSpec(task="test", requires=["quantum_gpu"])
        with pytest.raises(RuntimeError, match="No host matches"):
            dispatch_from_spec(spec)

    @patch("hydra_dispatch.dispatch")
    def test_passes_all_params(self, mock_dispatch):
        mock_dispatch.return_value = DispatchResult(
            dispatch_id="d-1",
            host="node_gpu",
            task="test",
            model="opus",
            status="running",
        )
        spec = DispatchSpec(
            task="generate",
            host="node_gpu",
            model="opus",
            project_dir="/opt/examforge",
            timeout_minutes=60,
            background=False,
        )
        dispatch_from_spec(spec)
        call_kwargs = mock_dispatch.call_args
        assert call_kwargs[1].get("model") == "opus" or call_kwargs[0][2] == "opus"
