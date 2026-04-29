"""Tests for training/distillation pipeline definitions."""

import sys
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipeline import Pipeline, PipelineStage
from pipelines.corpus_build import CORPUS_BUILD
from pipelines.teacher_generate import TEACHER_GENERATE
from pipelines.student_train import STUDENT_TRAIN
from pipelines.benchmark_run import BENCHMARK_RUN
from pipelines.export_promote import EXPORT_PROMOTE


class TestCorpusBuild:
    """Tests for the corpus build pipeline."""

    def test_pipeline_validates(self):
        errors = CORPUS_BUILD.validate()
        assert errors == [], f"Validation errors: {errors}"

    def test_has_correct_stages(self):
        names = [s.name for s in CORPUS_BUILD.stages]
        assert names == ["extract", "sanitize", "dedup", "validate"]

    def test_dependency_chain(self):
        stages = {s.name: s for s in CORPUS_BUILD.stages}
        assert stages["extract"].depends_on == []
        assert stages["sanitize"].depends_on == ["extract"]
        assert stages["dedup"].depends_on == ["sanitize"]
        assert stages["validate"].depends_on == ["dedup"]

    def test_all_stages_on_mecha(self):
        for stage in CORPUS_BUILD.stages:
            assert stage.host == "mecha", f"Stage {stage.name} not on mecha"

    def test_topological_order_is_sequential(self):
        batches = CORPUS_BUILD.topological_order()
        assert len(batches) == 4  # All sequential
        assert batches[0][0].name == "extract"
        assert batches[3][0].name == "validate"


class TestTeacherGenerate:
    """Tests for the teacher generation pipeline."""

    def test_pipeline_validates(self):
        errors = TEACHER_GENERATE.validate()
        assert errors == [], f"Validation errors: {errors}"

    def test_has_correct_stages(self):
        names = [s.name for s in TEACHER_GENERATE.stages]
        assert "shard_prompts" in names
        assert "generate_giga" in names
        assert "generate_mega" in names
        assert "collect_filter" in names

    def test_parallel_generation(self):
        """node_gpu and node_reserve1 generation should run in parallel."""
        batches = TEACHER_GENERATE.topological_order()
        # Batch 0: shard_prompts
        # Batch 1: generate_giga + generate_mega (parallel)
        # Batch 2: collect_filter
        assert len(batches) == 3
        parallel_batch = batches[1]
        parallel_names = {s.name for s in parallel_batch}
        assert "generate_giga" in parallel_names
        assert "generate_mega" in parallel_names

    def test_giga_on_giga_mega_on_mega(self):
        stages = {s.name: s for s in TEACHER_GENERATE.stages}
        assert stages["generate_giga"].host == "giga"
        assert stages["generate_mega"].host == "mega"

    def test_collect_depends_on_both_generators(self):
        stages = {s.name: s for s in TEACHER_GENERATE.stages}
        deps = set(stages["collect_filter"].depends_on)
        assert "generate_giga" in deps
        assert "generate_mega" in deps


class TestStudentTrain:
    """Tests for the student training pipeline."""

    def test_pipeline_validates(self):
        errors = STUDENT_TRAIN.validate()
        assert errors == [], f"Validation errors: {errors}"

    def test_has_correct_stages(self):
        names = [s.name for s in STUDENT_TRAIN.stages]
        assert names == ["claim_gpu", "train", "smoke_test", "release_gpu"]

    def test_all_stages_on_giga(self):
        for stage in STUDENT_TRAIN.stages:
            assert stage.host == "giga", f"Stage {stage.name} not on giga"

    def test_training_timeout(self):
        stages = {s.name: s for s in STUDENT_TRAIN.stages}
        assert stages["train"].timeout_minutes == 120  # 2 hours for training

    def test_gpu_lifecycle(self):
        """claim_gpu → train → smoke_test → release_gpu."""
        stages = {s.name: s for s in STUDENT_TRAIN.stages}
        assert stages["claim_gpu"].depends_on == []
        assert stages["train"].depends_on == ["claim_gpu"]
        assert stages["smoke_test"].depends_on == ["train"]
        assert stages["release_gpu"].depends_on == ["smoke_test"]


class TestBenchmarkRun:
    """Tests for the benchmark pipeline."""

    def test_pipeline_validates(self):
        errors = BENCHMARK_RUN.validate()
        assert errors == [], f"Validation errors: {errors}"

    def test_has_correct_stages(self):
        names = [s.name for s in BENCHMARK_RUN.stages]
        assert "deploy_model" in names
        assert "opencode_bench" in names
        assert "convention_bench" in names
        assert "degen_check" in names
        assert "record_results" in names

    def test_parallel_benchmarks(self):
        """Three benchmark suites should run in parallel after model deploy."""
        batches = BENCHMARK_RUN.topological_order()
        # Batch 0: deploy_model
        # Batch 1: opencode + convention + degen (parallel)
        # Batch 2: record_results
        assert len(batches) == 3
        parallel_names = {s.name for s in batches[1]}
        assert "opencode_bench" in parallel_names
        assert "convention_bench" in parallel_names
        assert "degen_check" in parallel_names

    def test_evaluation_on_mongo(self):
        stages = {s.name: s for s in BENCHMARK_RUN.stages}
        assert stages["deploy_model"].host == "mongo"
        assert stages["opencode_bench"].host == "mongo"

    def test_record_results_depends_on_all_benchmarks(self):
        stages = {s.name: s for s in BENCHMARK_RUN.stages}
        deps = set(stages["record_results"].depends_on)
        assert "opencode_bench" in deps
        assert "convention_bench" in deps
        assert "degen_check" in deps


class TestExportPromote:
    """Tests for the export and promotion pipeline."""

    def test_pipeline_validates(self):
        errors = EXPORT_PROMOTE.validate()
        assert errors == [], f"Validation errors: {errors}"

    def test_has_correct_stages(self):
        names = [s.name for s in EXPORT_PROMOTE.stages]
        assert names == ["gguf_export", "gate_check", "deploy", "tag_promote"]

    def test_gate_check_before_deploy(self):
        stages = {s.name: s for s in EXPORT_PROMOTE.stages}
        assert "gate_check" in stages["deploy"].depends_on

    def test_gguf_export_on_giga(self):
        stages = {s.name: s for s in EXPORT_PROMOTE.stages}
        assert stages["gguf_export"].host == "giga"

    def test_gate_check_fast(self):
        stages = {s.name: s for s in EXPORT_PROMOTE.stages}
        assert stages["gate_check"].timeout_minutes <= 5


class TestAllPipelines:
    """Cross-pipeline validation tests."""

    @pytest.mark.parametrize("pipeline", [
        CORPUS_BUILD, TEACHER_GENERATE, STUDENT_TRAIN, BENCHMARK_RUN, EXPORT_PROMOTE
    ])
    def test_all_validate(self, pipeline):
        errors = pipeline.validate()
        assert errors == [], f"{pipeline.name}: {errors}"

    @pytest.mark.parametrize("pipeline", [
        CORPUS_BUILD, TEACHER_GENERATE, STUDENT_TRAIN, BENCHMARK_RUN, EXPORT_PROMOTE
    ])
    def test_all_have_description(self, pipeline):
        assert pipeline.description
        assert len(pipeline.description) > 10

    @pytest.mark.parametrize("pipeline", [
        CORPUS_BUILD, TEACHER_GENERATE, STUDENT_TRAIN, BENCHMARK_RUN, EXPORT_PROMOTE
    ])
    def test_all_stages_have_prompt_templates(self, pipeline):
        for stage in pipeline.stages:
            assert stage.prompt_template, f"{pipeline.name}.{stage.name} missing prompt"

    @pytest.mark.parametrize("pipeline", [
        CORPUS_BUILD, TEACHER_GENERATE, STUDENT_TRAIN, BENCHMARK_RUN, EXPORT_PROMOTE
    ])
    def test_all_use_valid_models(self, pipeline):
        valid = {"opus", "sonnet", "haiku"}
        for stage in pipeline.stages:
            # Some stages use template hosts like {input.target_host}
            assert stage.model in valid, f"{pipeline.name}.{stage.name}: {stage.model}"

    def test_pipeline_names_unique(self):
        names = [p.name for p in [CORPUS_BUILD, TEACHER_GENERATE, STUDENT_TRAIN, BENCHMARK_RUN, EXPORT_PROMOTE]]
        assert len(names) == len(set(names))
