"""
claude-swarm pipeline engine — Stage 3: Multi-Head Reasoning.

Multi-stage pipelines where each stage dispatches to a fleet member via
hydra_dispatch and passes context forward via NFS-shared state files.
"""

import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _pipelines_root() -> Path:
    """Return NFS-shared pipeline state root."""
    p = Path("/var/lib/swarm/pipelines")
    p.mkdir(parents=True, exist_ok=True)
    return p


from util import now_iso as _now_iso


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PipelineStage:
    """A single stage in a multi-stage pipeline.

    Attributes:
        name: Stage identifier (e.g., "architect", "implement", "test", "review")
        role: Human-readable description of what this stage does
        model: Claude model to use (opus, sonnet, or haiku)
        prompt_template: Jinja2 template with {input}, {previous_output.*} tokens
        host: Target fleet member hostname, or None for auto-select
        requires: List of required capabilities (e.g., ["gpu", "docker"])
        depends_on: List of stage names that must complete before this one
        timeout_minutes: Maximum runtime for this stage
    """

    name: str  # e.g., "architect", "implement", "test", "review"
    role: str  # human-readable description of what this stage does
    model: str  # opus, sonnet, or haiku
    prompt_template: str  # Jinja2 template with {input}, {previous_output.*}
    host: Optional[str] = None  # target host, or None for auto-select
    requires: list = field(default_factory=list)  # capabilities needed
    depends_on: list = field(
        default_factory=list
    )  # stage names that must complete first
    timeout_minutes: int = 30
    # v3: Generator-verifier loop support
    loop_to: Optional[str] = None  # stage to loop back to on failure
    max_iterations: int = 3  # max loop iterations before giving up
    success_pattern: Optional[str] = None  # regex: if output matches, stage succeeded


@dataclass
class Pipeline:
    """A multi-stage pipeline configuration.

    Attributes:
        name: Pipeline identifier
        description: Human-readable description
        stages: List of PipelineStage objects
    """

    name: str
    description: str
    stages: list  # list[PipelineStage]

    def validate(self) -> list[str]:
        """Check for dependency cycles, undefined stage references, and missing hosts.

        Returns list of error strings. Empty list = valid.
        """
        errors: list[str] = []
        stage_names = {s.name for s in self.stages}

        # Check depends_on references exist
        for stage in self.stages:
            for dep in stage.depends_on:
                if dep not in stage_names:
                    errors.append(
                        f"Stage '{stage.name}' depends on unknown stage '{dep}'"
                    )

        # Cycle detection via DFS
        def has_cycle(name: str, visiting: set, visited: set) -> bool:
            if name in visiting:
                return True
            if name in visited:
                return False
            visiting.add(name)
            deps_map = {s.name: s.depends_on for s in self.stages}
            for dep in deps_map.get(name, []):
                if has_cycle(dep, visiting, visited):
                    return True
            visiting.discard(name)
            visited.add(name)
            return False

        visiting: set = set()
        visited: set = set()
        for stage in self.stages:
            if has_cycle(stage.name, visiting, visited):
                errors.append(
                    f"Dependency cycle detected involving stage '{stage.name}'"
                )
                break

        # Validate model names
        valid_models = {"opus", "sonnet", "haiku"}
        for stage in self.stages:
            if stage.model not in valid_models:
                errors.append(
                    f"Stage '{stage.name}' has invalid model '{stage.model}'. "
                    f"Must be one of: {sorted(valid_models)}"
                )

        return errors

    def topological_order(self) -> list[list]:
        """Return stages in dependency order as batches of parallelizable stages.

        Each inner list is a batch that can run concurrently (all their deps complete).
        Returns list[list[PipelineStage]].
        """
        deps_map = {s.name: set(s.depends_on) for s in self.stages}
        name_to_stage = {s.name: s for s in self.stages}
        completed: set = set()
        batches = []

        while len(completed) < len(self.stages):
            # Find all stages whose deps are satisfied and haven't run yet
            ready = [
                name_to_stage[name]
                for name, deps in deps_map.items()
                if name not in completed and deps.issubset(completed)
            ]
            if not ready:
                # Remaining stages — should not happen if validate() passed
                break
            batches.append(ready)
            for s in ready:
                completed.add(s.name)

        return batches

    @property
    def timeout_minutes(self) -> int:
        """Total pipeline timeout = sum of stage timeouts + 10% buffer."""
        total = sum(s.timeout_minutes for s in self.stages)
        return int(total * 1.1)


@dataclass
class StageResult:
    """Result of executing a single stage in a pipeline.

    Attributes:
        stage_name: Name of the executed stage
        status: Execution status (pending, running, completed, failed, skipped)
        host: Fleet member that executed the stage
        model: Model that was used
        output: Stage output text
        error: Error message if stage failed
        dispatch_id: Dispatch ID from hydra_dispatch
        started_at: ISO timestamp when stage started
        completed_at: ISO timestamp when stage completed
        duration_seconds: Total execution time in seconds
    """

    stage_name: str
    status: str  # pending, running, completed, failed, skipped
    host: str = ""
    model: str = ""
    output: str = ""
    error: str = ""
    dispatch_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: int = 0


@dataclass
class PipelineResult:
    """Result of executing a complete multi-stage pipeline.

    Attributes:
        pipeline_id: Unique pipeline execution identifier
        pipeline_name: Name of the pipeline definition
        status: Overall status (running, completed, failed)
        started_at: ISO timestamp when pipeline started
        completed_at: ISO timestamp when pipeline finished
        stage_results: Dict mapping stage names to StageResult objects
        error: Overall error message if pipeline failed
    """

    pipeline_id: str
    pipeline_name: str
    status: str  # running, completed, failed
    started_at: str = ""
    completed_at: str = ""
    stage_results: dict = field(default_factory=dict)  # stage_name → StageResult
    error: str = ""


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class PipelineContext:
    """Accumulates outputs from completed stages for use in prompt rendering."""

    def __init__(self, input_data: dict):
        self.input_data = input_data
        self.outputs: dict[str, str] = {}  # stage_name → output text

    def add_output(self, stage_name: str, output: str) -> None:
        """Record the output from a completed stage.

        Args:
            stage_name: Name of the stage that produced the output
            output: Output text from the stage
        """
        self.outputs[stage_name] = output

    def render_prompt(self, template: str) -> str:
        """Render a prompt template with context substitutions.

        Supported tokens:
          {input}                      — original task string (or str(input_data))
          {input.field}                — specific field from input_data dict
          {previous_output.stage_name} — output text from a named stage
          {context}                    — all previous outputs concatenated
        """
        import re

        result = template

        # {context} → all previous outputs concatenated
        if "{context}" in result:
            ctx_text = "\n\n".join(
                f"=== {name} ===\n{out}" for name, out in self.outputs.items()
            )
            result = result.replace("{context}", ctx_text)

        # {input.field} — specific field access
        for m in re.findall(r"\{input\.(\w+)\}", result):
            val = str(self.input_data.get(m, f"<input.{m} not found>"))
            result = result.replace(f"{{input.{m}}}", val)

        # {input} — raw task string or string repr of input_data
        if "{input}" in result:
            val = self.input_data.get("task", str(self.input_data))
            result = result.replace("{input}", str(val))

        # {previous_output.stage_name}
        for m in re.findall(r"\{previous_output\.(\w+)\}", result):
            val = self.outputs.get(m, f"<output of '{m}' not available>")
            result = result.replace(f"{{previous_output.{m}}}", val)

        return result


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _pipeline_dir(pipeline_id: str) -> Path:
    d = _pipelines_root() / f"pipeline-{pipeline_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_stage_result(pipeline_id: str, result: StageResult) -> None:
    d = _pipeline_dir(pipeline_id)
    path = d / f"stage-{result.stage_name}.yaml"
    data = asdict(result)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    tmp.rename(path)


def _load_stage_result(pipeline_id: str, stage_name: str) -> Optional[StageResult]:
    path = _pipeline_dir(pipeline_id) / f"stage-{stage_name}.yaml"
    if not path.exists():
        return None
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return StageResult(**data)


def _save_pipeline_result(pipeline_id: str, result: PipelineResult) -> None:
    d = _pipeline_dir(pipeline_id)
    path = d / "result.yaml"
    # Serialize stage_results dict (values are StageResult dataclasses)
    data = asdict(result)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    tmp.rename(path)


def _save_pipeline_meta(pipeline_id: str, pipeline: Pipeline, input_data: dict) -> None:
    """Save pipeline definition + input to pipeline.yaml."""
    d = _pipeline_dir(pipeline_id)
    path = d / "pipeline.yaml"
    data = {
        "pipeline_id": pipeline_id,
        "name": pipeline.name,
        "description": pipeline.description,
        "input": input_data,
        "stages": [
            {
                "name": s.name,
                "role": s.role,
                "model": s.model,
                "host": s.host,
                "requires": s.requires,
                "depends_on": s.depends_on,
                "timeout_minutes": s.timeout_minutes,
            }
            for s in pipeline.stages
        ],
        "created_at": _now_iso(),
    }
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class PipelineExecutor:
    """Executes a multi-stage pipeline across the fleet.

    Stages without dependencies run in parallel (separate threads).
    Each stage dispatches via hydra_dispatch and waits for completion.
    All state is persisted to /var/lib/swarm/pipelines/pipeline-{id}/.
    """

    def __init__(self):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from hydra_dispatch import dispatch, _find_best_host, recall

        self._dispatch = dispatch
        self._find_best_host = _find_best_host
        self._recall = recall

    def execute(self, pipeline: Pipeline, input_data: dict) -> PipelineResult:
        """Run all stages in dependency order, passing context forward.

        Stages in the same batch (no unmet deps between them) run concurrently
        via threads. Each stage polls its output file until complete.

        Returns PipelineResult with all stage outputs.
        """
        import concurrent.futures

        errors = pipeline.validate()
        if errors:
            raise ValueError(f"Pipeline validation failed: {errors}")

        pipeline_id = str(uuid.uuid4())[:8]
        _save_pipeline_meta(pipeline_id, pipeline, input_data)

        # v3: Create living spec in Context Bridge for cross-stage context
        try:
            from living_spec import create_spec
            spec_content = f"# Pipeline: {pipeline.name}\n\n## Goal\n{input_data.get('input', '')}\n\n## Stages\n"
            for s in pipeline.stages:
                spec_content += f"- **{s.name}** ({s.model}): {s.role}\n"
            create_spec(pipeline_id, spec_content, title=pipeline.name)
        except Exception:
            pass  # CB not available — continue without spec

        pr = PipelineResult(
            pipeline_id=pipeline_id,
            pipeline_name=pipeline.name,
            status="running",
            started_at=_now_iso(),
        )
        _save_pipeline_result(pipeline_id, pr)

        context = PipelineContext(input_data)
        batches = pipeline.topological_order()

        try:
            for batch in batches:
                if len(batch) == 1:
                    stage = batch[0]
                    sr = self.execute_stage(stage, context, input_data, pipeline_id)
                    context.add_output(sr.stage_name, sr.output)
                    pr.stage_results[sr.stage_name] = asdict(sr)
                    _save_pipeline_result(pipeline_id, pr)

                    # v3: Generator-verifier loop — retry on failure
                    if sr.status == "failed" and stage.loop_to:
                        loop_target = next(
                            (s for s in pipeline.stages if s.name == stage.loop_to), None
                        )
                        if loop_target:
                            iteration = 1
                            while sr.status == "failed" and iteration < stage.max_iterations:
                                import logging
                                logging.getLogger(__name__).info(
                                    f"Loop iteration {iteration}/{stage.max_iterations}: "
                                    f"re-running '{loop_target.name}' after '{stage.name}' failed"
                                )
                                # Re-run the target stage with error context appended
                                error_context = f"\n\nPrevious attempt failed:\n{sr.error or sr.output}"
                                augmented_input = dict(input_data)
                                augmented_input["_loop_error"] = error_context
                                augmented_input["_loop_iteration"] = iteration

                                # Re-run generate stage
                                gen_sr = self.execute_stage(loop_target, context, augmented_input, pipeline_id)
                                context.add_output(gen_sr.stage_name, gen_sr.output)
                                pr.stage_results[f"{gen_sr.stage_name}_iter{iteration}"] = asdict(gen_sr)

                                # Re-run verify stage
                                sr = self.execute_stage(stage, context, augmented_input, pipeline_id)
                                context.add_output(sr.stage_name, sr.output)
                                pr.stage_results[f"{sr.stage_name}_iter{iteration}"] = asdict(sr)
                                _save_pipeline_result(pipeline_id, pr)
                                iteration += 1

                    if sr.status == "failed":
                        pr.status = "failed"
                        pr.error = f"Stage '{sr.stage_name}' failed: {sr.error}"
                        pr.completed_at = _now_iso()
                        _save_pipeline_result(pipeline_id, pr)
                        return pr
                else:
                    # Parallel batch
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=len(batch)
                    ) as ex:
                        futures = {
                            ex.submit(
                                self.execute_stage,
                                stage,
                                context,
                                input_data,
                                pipeline_id,
                            ): stage.name
                            for stage in batch
                        }
                        failed = False
                        for fut in concurrent.futures.as_completed(futures):
                            sr = fut.result()
                            context.add_output(sr.stage_name, sr.output)
                            pr.stage_results[sr.stage_name] = asdict(sr)
                            _save_pipeline_result(pipeline_id, pr)
                            if sr.status == "failed":
                                failed = True
                                pr.status = "failed"
                                pr.error = f"Stage '{sr.stage_name}' failed: {sr.error}"

                    if failed:
                        pr.completed_at = _now_iso()
                        _save_pipeline_result(pipeline_id, pr)
                        return pr

            pr.status = "completed"
            pr.completed_at = _now_iso()
            _save_pipeline_result(pipeline_id, pr)

            # v3: Update living spec with completion status
            try:
                from living_spec import update_spec, read_spec
                existing = read_spec(pipeline_id) or ""
                completion = f"\n\n## Result: COMPLETED\n"
                for name, sr_dict in pr.stage_results.items():
                    completion += f"- **{name}**: {sr_dict.get('status', 'unknown')}\n"
                update_spec(pipeline_id, existing + completion)
            except Exception:
                pass

            # v3: Emit pipeline completion via IPC
            try:
                from ipc_bridge import publish, TASK_EVENTS
                publish(TASK_EVENTS, "pipeline.completed", {
                    "pipeline_id": pipeline_id,
                    "pipeline_name": pipeline.name,
                    "stages_completed": len(pr.stage_results),
                    "status": pr.status,
                })
            except Exception:
                pass

            return pr

        except Exception as exc:
            pr.status = "failed"
            pr.error = str(exc)
            pr.completed_at = _now_iso()
            _save_pipeline_result(pipeline_id, pr)
            raise

    def execute_stage(
        self,
        stage: PipelineStage,
        context: PipelineContext,
        input_data: dict,
        pipeline_id: str,
    ) -> StageResult:
        """Dispatch a single stage and wait for its output.

        Returns StageResult with output populated on success.
        """
        sr = StageResult(
            stage_name=stage.name,
            status="running",
            model=stage.model,
            host=stage.host or "",
            started_at=_now_iso(),
        )
        _save_stage_result(pipeline_id, sr)

        # Resolve host
        host = stage.host
        if not host:
            host = self._find_best_host(stage.requires)
        if not host:
            sr.status = "failed"
            sr.error = f"No host satisfies requirements: {stage.requires}"
            sr.completed_at = _now_iso()
            _save_stage_result(pipeline_id, sr)
            return sr

        sr.host = host
        prompt = context.render_prompt(stage.prompt_template)

        t_start = time.time()
        try:
            result = self._dispatch(
                host=host,
                task=prompt,
                model=stage.model,
                project_dir=input_data.get("project"),
                background=False,
                timeout_minutes=stage.timeout_minutes,
            )
            sr.dispatch_id = result.dispatch_id
            if result.status in ("completed",):
                sr.status = "completed"
                sr.output = self._recall(result.dispatch_id)
            else:
                sr.status = "failed"
                sr.error = result.error or f"Dispatch status: {result.status}"
        except Exception as exc:
            sr.status = "failed"
            sr.error = str(exc)

        t_end = time.time()
        sr.duration_seconds = int(t_end - t_start)
        sr.completed_at = _now_iso()
        _save_stage_result(pipeline_id, sr)
        return sr


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def list_pipeline_runs() -> list[dict]:
    """List all pipeline runs from /var/lib/swarm/pipelines/."""
    root = _pipelines_root()
    results = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not d.name.startswith("pipeline-"):
            continue
        result_file = d / "result.yaml"
        if result_file.exists():
            try:
                with open(result_file) as f:
                    data = yaml.safe_load(f) or {}
                data["_dir"] = str(d)
                results.append(data)
            except (yaml.YAMLError, OSError):
                continue
    return sorted(results, key=lambda x: x.get("started_at", ""), reverse=True)


def get_pipeline_run(pipeline_id: str) -> Optional[dict]:
    """Return full pipeline result for a given pipeline_id."""
    # pipeline_id may be the full dir name or just the short ID
    root = _pipelines_root()
    for d in root.iterdir():
        if d.name == f"pipeline-{pipeline_id}" or d.name.endswith(pipeline_id):
            result_file = d / "result.yaml"
            if result_file.exists():
                with open(result_file) as f:
                    return yaml.safe_load(f) or {}
    return None


def get_stage_output(pipeline_id: str, stage_name: str) -> Optional[str]:
    """Return the raw output string for a specific stage of a pipeline run."""
    sr = _load_stage_result(pipeline_id, stage_name)
    return sr.output if sr else None
