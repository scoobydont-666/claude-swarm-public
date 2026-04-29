"""Tests for PipelineExecutor — mock dispatches, context passing, state persistence."""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from pipeline import (
    Pipeline,
    PipelineExecutor,
    PipelineStage,
    get_pipeline_run,
    get_stage_output,
    list_pipeline_runs,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeDispatchResult:
    dispatch_id: str
    host: str
    task: str
    model: str
    status: str
    started_at: str = ""
    completed_at: str = ""
    exit_code: int = 0
    output_file: str = ""
    error: str = ""


def make_stage(name: str, depends_on=None, model="sonnet", host=None, requires=None):
    return PipelineStage(
        name=name,
        role=f"Role: {name}",
        model=model,
        host=host or "node_gpu",
        requires=requires or [],
        depends_on=depends_on or [],
        prompt_template=f"Do {name}: {{input}}",
        timeout_minutes=5,
    )


def make_pipeline(*stages):
    return Pipeline(name="test-pipeline", description="test", stages=list(stages))


@pytest.fixture
def tmp_pipelines(tmp_path, monkeypatch):
    """Redirect all pipeline state to a temp directory."""
    monkeypatch.setattr("pipeline._pipelines_root", lambda: tmp_path / "pipelines")
    (tmp_path / "pipelines").mkdir(parents=True, exist_ok=True)
    return tmp_path / "pipelines"


def make_executor(outputs: dict[str, str]) -> PipelineExecutor:
    """Create a PipelineExecutor with mocked dispatch + recall.

    outputs: stage_name → output text to return from recall()
    """
    executor = PipelineExecutor.__new__(PipelineExecutor)

    call_counter = {"n": 0}

    def fake_dispatch(host, task, model, project_dir=None, background=False, timeout_minutes=30):
        call_counter["n"] += 1
        dispatch_id = f"dispatch-{call_counter['n']}"
        return FakeDispatchResult(
            dispatch_id=dispatch_id,
            host=host,
            task=task,
            model=model,
            status="completed",
        )

    def fake_recall(dispatch_id: str) -> str:
        # Extract call number, look up by order
        n = int(dispatch_id.split("-")[1])
        # Return outputs in order they were dispatched
        keys = list(outputs.keys())
        if n <= len(keys):
            return outputs[keys[n - 1]]
        return f"output-{n}"

    def fake_find_best_host(requires):
        return "node_gpu"

    executor._dispatch = fake_dispatch
    executor._recall = fake_recall
    executor._find_best_host = fake_find_best_host
    return executor


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------


class TestPipelineExecutorBasic:
    def test_single_stage_completes(self, tmp_pipelines):
        p = make_pipeline(make_stage("only"))
        executor = make_executor({"only": "stage output"})

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "do something"})

        assert result.status == "completed"
        assert "only" in result.stage_results

    def test_stage_result_has_output(self, tmp_pipelines):
        p = make_pipeline(make_stage("stage1"))
        executor = make_executor({"stage1": "hello world"})

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "x"})

        sr = result.stage_results["stage1"]
        assert sr["output"] == "hello world"
        assert sr["status"] == "completed"

    def test_three_stage_linear_chain(self, tmp_pipelines):
        p = make_pipeline(
            make_stage("a"),
            make_stage("b", depends_on=["a"]),
            make_stage("c", depends_on=["b"]),
        )
        executor = make_executor({"a": "out-a", "b": "out-b", "c": "out-c"})

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "chain"})

        assert result.status == "completed"
        assert result.stage_results["a"]["output"] == "out-a"
        assert result.stage_results["b"]["output"] == "out-b"
        assert result.stage_results["c"]["output"] == "out-c"


# ---------------------------------------------------------------------------
# Context passing
# ---------------------------------------------------------------------------


class TestContextPassing:
    def test_stage_receives_previous_output_in_prompt(self, tmp_pipelines):
        """The implement stage's rendered prompt should contain architect's output."""
        captured_prompts = {}

        def fake_dispatch(
            host, task, model, project_dir=None, background=False, timeout_minutes=30
        ):
            # task IS the rendered prompt
            stage_order = len(captured_prompts)
            stage_names = ["architect", "implement"]
            if stage_order < len(stage_names):
                captured_prompts[stage_names[stage_order]] = task
            return FakeDispatchResult(
                dispatch_id=f"dispatch-{stage_order + 1}",
                host=host,
                task=task,
                model=model,
                status="completed",
            )

        def fake_recall(dispatch_id):
            n = int(dispatch_id.split("-")[1])
            return [
                "architect output: use layered design",
                "implement output: code written",
            ][n - 1]

        executor = PipelineExecutor.__new__(PipelineExecutor)
        executor._dispatch = fake_dispatch
        executor._recall = fake_recall
        executor._find_best_host = lambda r: "node_gpu"

        stages = [
            PipelineStage(
                name="architect",
                role="arch",
                model="opus",
                host="node_gpu",
                requires=[],
                depends_on=[],
                prompt_template="Design: {input}",
                timeout_minutes=5,
            ),
            PipelineStage(
                name="implement",
                role="impl",
                model="sonnet",
                host="node_gpu",
                requires=[],
                depends_on=["architect"],
                prompt_template="Arch was: {previous_output.architect}\nTask: {input}",
                timeout_minutes=5,
            ),
        ]
        p = Pipeline(name="test", description="test", stages=stages)

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "build feature"})

        assert result.status == "completed"
        # The implement prompt should contain architect's output
        assert "architect output: use layered design" in captured_prompts.get("implement", "")


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_failed_stage_stops_pipeline(self, tmp_pipelines):
        def fake_dispatch(
            host, task, model, project_dir=None, background=False, timeout_minutes=30
        ):
            return FakeDispatchResult(
                dispatch_id="dispatch-1",
                host=host,
                task=task,
                model=model,
                status="failed",
                error="SSH timeout",
            )

        executor = PipelineExecutor.__new__(PipelineExecutor)
        executor._dispatch = fake_dispatch
        executor._recall = lambda d: ""
        executor._find_best_host = lambda r: "node_gpu"

        p = make_pipeline(
            make_stage("a"),
            make_stage("b", depends_on=["a"]),
        )

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "x"})

        assert result.status == "failed"
        assert "a" in result.error or "failed" in result.error.lower()
        # Stage b should not be in results since a failed
        assert "b" not in result.stage_results

    def test_no_host_available_fails_stage(self, tmp_pipelines):
        executor = PipelineExecutor.__new__(PipelineExecutor)
        executor._dispatch = lambda **kw: None
        executor._recall = lambda d: ""
        executor._find_best_host = lambda r: None  # No host available

        stage = PipelineStage(
            name="needs_gpu",
            role="gpu work",
            model="sonnet",
            host=None,
            requires=["gpu"],
            depends_on=[],
            prompt_template="use gpu: {input}",
            timeout_minutes=5,
        )
        p = Pipeline(name="test", description="test", stages=[stage])

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "gpu task"})

        assert result.status == "failed"
        sr = result.stage_results.get("needs_gpu", {})
        assert sr.get("status") == "failed"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_pipeline_meta_saved(self, tmp_pipelines):
        p = make_pipeline(make_stage("x"))
        executor = make_executor({"x": "output"})

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "test"})
            pid = result.pipeline_id
            d = tmp_pipelines / f"pipeline-{pid}"
            assert (d / "pipeline.yaml").exists()

    def test_stage_yaml_saved(self, tmp_pipelines):
        p = make_pipeline(make_stage("mstage"))
        executor = make_executor({"mstage": "my output"})

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "t"})
            pid = result.pipeline_id
            stage_file = tmp_pipelines / f"pipeline-{pid}" / "stage-mstage.yaml"
            assert stage_file.exists()
            data = yaml.safe_load(stage_file.read_text())
            assert data["output"] == "my output"
            assert data["status"] == "completed"

    def test_result_yaml_saved(self, tmp_pipelines):
        p = make_pipeline(make_stage("s"))
        executor = make_executor({"s": "done"})

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "t"})
            pid = result.pipeline_id
            result_file = tmp_pipelines / f"pipeline-{pid}" / "result.yaml"
            assert result_file.exists()
            data = yaml.safe_load(result_file.read_text())
            assert data["status"] == "completed"
            assert data["pipeline_id"] == pid

    def test_list_pipeline_runs(self, tmp_pipelines):
        p = make_pipeline(make_stage("s"))
        executor = make_executor({"s": "done"})

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            executor.execute(p, {"task": "first"})
            executor.execute(p, {"task": "second"})
            runs = list_pipeline_runs()

        assert len(runs) >= 2

    def test_get_stage_output(self, tmp_pipelines):
        p = make_pipeline(make_stage("tgt"))
        executor = make_executor({"tgt": "target output text"})

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "x"})
            out = get_stage_output(result.pipeline_id, "tgt")

        assert out == "target output text"

    def test_get_pipeline_run(self, tmp_pipelines):
        p = make_pipeline(make_stage("s"))
        executor = make_executor({"s": "x"})

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "x"})
            fetched = get_pipeline_run(result.pipeline_id)

        assert fetched is not None
        assert fetched["pipeline_id"] == result.pipeline_id
        assert fetched["status"] == "completed"


# ---------------------------------------------------------------------------
# Parallel stage execution
# ---------------------------------------------------------------------------


class TestParallelExecution:
    def test_parallel_stages_both_complete(self, tmp_pipelines):
        """Two independent root stages should both complete."""
        p = make_pipeline(
            make_stage("scan_a"),
            make_stage("scan_b"),
            make_stage("analyze", depends_on=["scan_a", "scan_b"]),
        )
        executor = make_executor(
            {
                "scan_a": "findings from A",
                "scan_b": "findings from B",
                "analyze": "analysis complete",
            }
        )

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "audit"})

        assert result.status == "completed"
        assert "scan_a" in result.stage_results
        assert "scan_b" in result.stage_results
        assert "analyze" in result.stage_results

    def test_analyze_context_has_both_scan_outputs(self, tmp_pipelines):
        """The analyze stage's prompt should contain outputs from both scan stages."""
        captured = {}

        call_n = {"n": 0}

        def fake_dispatch(
            host, task, model, project_dir=None, background=False, timeout_minutes=30
        ):
            call_n["n"] += 1
            captured[call_n["n"]] = task
            return FakeDispatchResult(
                dispatch_id=f"dispatch-{call_n['n']}",
                host=host,
                task=task,
                model=model,
                status="completed",
            )

        outputs_by_call = ["findings from A", "findings from B", "combined analysis"]

        def fake_recall(dispatch_id):
            n = int(dispatch_id.split("-")[1])
            return outputs_by_call[n - 1] if n <= len(outputs_by_call) else ""

        executor = PipelineExecutor.__new__(PipelineExecutor)
        executor._dispatch = fake_dispatch
        executor._recall = fake_recall
        executor._find_best_host = lambda r: "node_gpu"

        stages = [
            PipelineStage(
                name="scan_a",
                role="scan",
                model="sonnet",
                host="node_primary",
                requires=[],
                depends_on=[],
                prompt_template="Scan A: {input}",
                timeout_minutes=5,
            ),
            PipelineStage(
                name="scan_b",
                role="scan",
                model="sonnet",
                host="node_gpu",
                requires=[],
                depends_on=[],
                prompt_template="Scan B: {input}",
                timeout_minutes=5,
            ),
            PipelineStage(
                name="analyze",
                role="analyze",
                model="opus",
                host=None,
                requires=[],
                depends_on=["scan_a", "scan_b"],
                prompt_template="A: {previous_output.scan_a}\nB: {previous_output.scan_b}",
                timeout_minutes=5,
            ),
        ]
        p = Pipeline(name="test", description="test", stages=stages)

        with patch("pipeline._pipelines_root", return_value=tmp_pipelines):
            result = executor.execute(p, {"task": "security"})

        assert result.status == "completed"
        # The analyze prompt (3rd call) should reference both scan outputs
        analyze_prompt = captured.get(3, "")
        assert "findings from A" in analyze_prompt
        assert "findings from B" in analyze_prompt
