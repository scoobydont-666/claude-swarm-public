"""Tests for pipeline definitions — structural integrity checks."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipeline import Pipeline
from pipelines.feature_build import FEATURE_BUILD
from pipelines.bug_fix import BUG_FIX
from pipelines.question_generation import QUESTION_GEN
from pipelines.security_audit import SECURITY_AUDIT

ALL_PIPELINES = [FEATURE_BUILD, BUG_FIX, QUESTION_GEN, SECURITY_AUDIT]
PIPELINE_IDS = ["feature_build", "bug_fix", "question_generation", "security_audit"]

VALID_MODELS = {"opus", "sonnet", "haiku"}


class TestPipelineStructure:
    @pytest.mark.parametrize("pipeline", ALL_PIPELINES, ids=PIPELINE_IDS)
    def test_has_name(self, pipeline: Pipeline):
        """Each pipeline must have a non-empty name."""
        assert hasattr(pipeline, "name")
        assert isinstance(pipeline.name, str)
        assert pipeline.name.strip() != ""

    @pytest.mark.parametrize("pipeline", ALL_PIPELINES, ids=PIPELINE_IDS)
    def test_has_description(self, pipeline: Pipeline):
        """Each pipeline must have a description."""
        assert hasattr(pipeline, "description")
        assert isinstance(pipeline.description, str)
        assert pipeline.description.strip() != ""

    @pytest.mark.parametrize("pipeline", ALL_PIPELINES, ids=PIPELINE_IDS)
    def test_has_stages(self, pipeline: Pipeline):
        """Each pipeline must have at least one stage."""
        assert hasattr(pipeline, "stages")
        assert isinstance(pipeline.stages, list)
        assert len(pipeline.stages) > 0

    @pytest.mark.parametrize("pipeline", ALL_PIPELINES, ids=PIPELINE_IDS)
    def test_stage_names_unique(self, pipeline: Pipeline):
        """Stage names within a pipeline must be unique."""
        names = [stage.name for stage in pipeline.stages]
        assert len(names) == len(set(names)), (
            f"Pipeline '{pipeline.name}' has duplicate stage names: {names}"
        )


class TestStageStructure:
    @pytest.mark.parametrize("pipeline", ALL_PIPELINES, ids=PIPELINE_IDS)
    def test_stages_have_names(self, pipeline: Pipeline):
        """Every stage must have a non-empty name."""
        for stage in pipeline.stages:
            assert hasattr(stage, "name")
            assert isinstance(stage.name, str)
            assert stage.name.strip() != "", (
                f"Pipeline '{pipeline.name}' has a stage with empty name"
            )

    @pytest.mark.parametrize("pipeline", ALL_PIPELINES, ids=PIPELINE_IDS)
    def test_stages_have_valid_model(self, pipeline: Pipeline):
        """Every stage must use a known model."""
        for stage in pipeline.stages:
            assert stage.model in VALID_MODELS, (
                f"Pipeline '{pipeline.name}', stage '{stage.name}': "
                f"unknown model '{stage.model}'"
            )

    @pytest.mark.parametrize("pipeline", ALL_PIPELINES, ids=PIPELINE_IDS)
    def test_stages_have_prompt_template(self, pipeline: Pipeline):
        """Every stage must have a non-empty prompt_template."""
        for stage in pipeline.stages:
            assert hasattr(stage, "prompt_template")
            assert isinstance(stage.prompt_template, str)
            assert stage.prompt_template.strip() != "", (
                f"Pipeline '{pipeline.name}', stage '{stage.name}': empty prompt_template"
            )

    @pytest.mark.parametrize("pipeline", ALL_PIPELINES, ids=PIPELINE_IDS)
    def test_stages_have_timeout(self, pipeline: Pipeline):
        """Every stage must have a positive timeout_minutes."""
        for stage in pipeline.stages:
            assert hasattr(stage, "timeout_minutes")
            assert isinstance(stage.timeout_minutes, int)
            assert stage.timeout_minutes > 0, (
                f"Pipeline '{pipeline.name}', stage '{stage.name}': "
                f"timeout_minutes must be > 0"
            )

    @pytest.mark.parametrize("pipeline", ALL_PIPELINES, ids=PIPELINE_IDS)
    def test_depends_on_references_valid_stages(self, pipeline: Pipeline):
        """depends_on must only reference stage names that exist in the pipeline."""
        stage_names = {stage.name for stage in pipeline.stages}
        for stage in pipeline.stages:
            for dep in stage.depends_on:
                assert dep in stage_names, (
                    f"Pipeline '{pipeline.name}', stage '{stage.name}': "
                    f"depends_on '{dep}' does not exist"
                )


class TestSpecificPipelines:
    def test_feature_build_has_four_stages(self):
        """feature-build pipeline should have: architect, implement, test, review."""
        stage_names = {s.name for s in FEATURE_BUILD.stages}
        assert stage_names == {"architect", "implement", "test", "review"}

    def test_bug_fix_has_three_stages(self):
        """bug-fix pipeline should have: investigate, fix, verify."""
        stage_names = {s.name for s in BUG_FIX.stages}
        assert stage_names == {"investigate", "fix", "verify"}

    def test_question_gen_has_two_stages(self):
        """question-generation pipeline should have: generate, validate."""
        stage_names = {s.name for s in QUESTION_GEN.stages}
        assert stage_names == {"generate", "validate"}

    def test_security_audit_parallel_scans(self):
        """security-audit should have parallel scan stages with no dependencies."""
        scan_stages = [s for s in SECURITY_AUDIT.stages if s.name.startswith("scan_")]
        assert len(scan_stages) >= 2
        for stage in scan_stages:
            assert stage.depends_on == [], (
                f"Scan stage '{stage.name}' should have no dependencies (runs in parallel)"
            )

    def test_security_audit_analyze_depends_on_scans(self):
        """security-audit analyze stage must depend on all scan stages."""
        scan_names = {
            s.name for s in SECURITY_AUDIT.stages if s.name.startswith("scan_")
        }
        analyze = next(s for s in SECURITY_AUDIT.stages if s.name == "analyze")
        for scan_name in scan_names:
            assert scan_name in analyze.depends_on, (
                f"analyze stage must depend on {scan_name}"
            )
