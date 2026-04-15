"""Validate all pre-built pipelines: structure, dependencies, model choices."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from pipeline_registry import PIPELINES, get_pipeline, list_pipelines
from pipelines.feature_build import FEATURE_BUILD
from pipelines.bug_fix import BUG_FIX
from pipelines.security_audit import SECURITY_AUDIT
from pipelines.question_generation import QUESTION_GEN


class TestPipelineRegistry:
    def test_all_pipelines_present(self):
        assert "feature-build" in PIPELINES
        assert "bug-fix" in PIPELINES
        assert "security-audit" in PIPELINES
        assert "question-gen" in PIPELINES

    def test_get_pipeline_returns_correct_object(self):
        p = get_pipeline("feature-build")
        assert p.name == "feature-build"

    def test_get_pipeline_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown pipeline"):
            get_pipeline("does-not-exist")

    def test_list_pipelines_structure(self):
        pipelines = list_pipelines()
        assert len(pipelines) >= 4
        for entry in pipelines:
            assert "name" in entry
            assert "description" in entry
            assert "stages" in entry
            assert "timeout_minutes" in entry
            assert isinstance(entry["stages"], list)

    def test_all_registered_pipelines_pass_validation(self):
        for name, pipeline in PIPELINES.items():
            errors = pipeline.validate()
            assert errors == [], f"Pipeline '{name}' has validation errors: {errors}"


class TestFeatureBuildPipeline:
    def test_has_four_stages(self):
        assert len(FEATURE_BUILD.stages) == 4

    def test_stage_names(self):
        names = [s.name for s in FEATURE_BUILD.stages]
        assert "architect" in names
        assert "implement" in names
        assert "test" in names
        assert "review" in names

    def test_architect_uses_opus(self):
        s = next(s for s in FEATURE_BUILD.stages if s.name == "architect")
        assert s.model == "opus"

    def test_implement_uses_sonnet(self):
        s = next(s for s in FEATURE_BUILD.stages if s.name == "implement")
        assert s.model == "sonnet"

    def test_test_uses_haiku(self):
        s = next(s for s in FEATURE_BUILD.stages if s.name == "test")
        assert s.model == "haiku"

    def test_review_uses_opus(self):
        s = next(s for s in FEATURE_BUILD.stages if s.name == "review")
        assert s.model == "opus"

    def test_implement_depends_on_architect(self):
        s = next(s for s in FEATURE_BUILD.stages if s.name == "implement")
        assert "architect" in s.depends_on

    def test_test_depends_on_implement(self):
        s = next(s for s in FEATURE_BUILD.stages if s.name == "test")
        assert "implement" in s.depends_on

    def test_review_depends_on_implement_and_test(self):
        s = next(s for s in FEATURE_BUILD.stages if s.name == "review")
        assert "implement" in s.depends_on
        assert "test" in s.depends_on

    def test_all_prompt_templates_non_empty(self):
        for s in FEATURE_BUILD.stages:
            assert s.prompt_template.strip(), f"Stage '{s.name}' has empty prompt"

    def test_topological_order_correct(self):
        batches = FEATURE_BUILD.topological_order()
        names = [[s.name for s in b] for b in batches]
        assert names[0] == ["architect"]
        assert names[1] == ["implement"]
        assert "test" in names[2]

    def test_timeout_includes_buffer(self):
        raw = sum(s.timeout_minutes for s in FEATURE_BUILD.stages)
        assert FEATURE_BUILD.timeout_minutes == int(raw * 1.1)


class TestBugFixPipeline:
    def test_has_three_stages(self):
        assert len(BUG_FIX.stages) == 3

    def test_stage_names(self):
        names = [s.name for s in BUG_FIX.stages]
        assert names == ["investigate", "fix", "verify"]

    def test_verify_uses_haiku(self):
        s = next(s for s in BUG_FIX.stages if s.name == "verify")
        assert s.model == "haiku"

    def test_fix_depends_on_investigate(self):
        s = next(s for s in BUG_FIX.stages if s.name == "fix")
        assert "investigate" in s.depends_on

    def test_verify_depends_on_fix(self):
        s = next(s for s in BUG_FIX.stages if s.name == "verify")
        assert "fix" in s.depends_on

    def test_passes_validation(self):
        assert BUG_FIX.validate() == []


class TestSecurityAuditPipeline:
    def test_has_three_stages(self):
        assert len(SECURITY_AUDIT.stages) == 3

    def test_scan_stages_pinned_to_hosts(self):
        scan_mb = next(s for s in SECURITY_AUDIT.stages if s.name == "scan_orchestration")
        scan_giga = next(s for s in SECURITY_AUDIT.stages if s.name == "scan_giga")
        assert scan_mb.host == "orchestration-node"
        assert scan_giga.host == "gpu-server-1"

    def test_analyze_uses_opus(self):
        s = next(s for s in SECURITY_AUDIT.stages if s.name == "analyze")
        assert s.model == "opus"

    def test_analyze_depends_on_both_scans(self):
        s = next(s for s in SECURITY_AUDIT.stages if s.name == "analyze")
        assert "scan_orchestration" in s.depends_on
        assert "scan_giga" in s.depends_on

    def test_scans_run_in_parallel(self):
        """Both scan stages have no deps — should be in the same batch."""
        batches = SECURITY_AUDIT.topological_order()
        first_batch_names = {s.name for s in batches[0]}
        assert "scan_orchestration" in first_batch_names
        assert "scan_giga" in first_batch_names

    def test_passes_validation(self):
        assert SECURITY_AUDIT.validate() == []


class TestQuestionGenPipeline:
    def test_has_two_stages(self):
        assert len(QUESTION_GEN.stages) == 2

    def test_both_stages_pinned_to_orchestration-node(self):
        for s in QUESTION_GEN.stages:
            assert s.host == "orchestration-node"

    def test_validate_depends_on_generate(self):
        s = next(s for s in QUESTION_GEN.stages if s.name == "validate")
        assert "generate" in s.depends_on

    def test_prompt_uses_input_fields(self):
        gen = next(s for s in QUESTION_GEN.stages if s.name == "generate")
        assert "{input.cert}" in gen.prompt_template
        assert "{input.count}" in gen.prompt_template
        assert "{input.domain}" in gen.prompt_template

    def test_passes_validation(self):
        assert QUESTION_GEN.validate() == []
