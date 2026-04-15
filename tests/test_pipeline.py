"""Tests for pipeline.py — validation, cycle detection, topological ordering."""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from pipeline import Pipeline, PipelineStage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_stage(name: str, depends_on=None, model="sonnet", host=None, requires=None):
    return PipelineStage(
        name=name,
        role=f"Role for {name}",
        model=model,
        host=host,
        requires=requires or [],
        depends_on=depends_on or [],
        prompt_template=f"Prompt for {name}: {{input}}",
        timeout_minutes=10,
    )


def make_pipeline(*stages):
    return Pipeline(name="test", description="test pipeline", stages=list(stages))


# ---------------------------------------------------------------------------
# Validation: dependency references
# ---------------------------------------------------------------------------


class TestPipelineValidation:
    def test_valid_pipeline_no_errors(self):
        p = make_pipeline(
            make_stage("a"),
            make_stage("b", depends_on=["a"]),
            make_stage("c", depends_on=["b"]),
        )
        assert p.validate() == []

    def test_missing_dep_reference(self):
        p = make_pipeline(
            make_stage("a"),
            make_stage("b", depends_on=["nonexistent"]),
        )
        errors = p.validate()
        assert any("nonexistent" in e for e in errors)

    def test_self_dependency_is_a_cycle(self):
        # Self-dep — cycle detector should catch it
        p = make_pipeline(make_stage("a", depends_on=["a"]))
        errors = p.validate()
        assert any("cycle" in e.lower() for e in errors)

    def test_two_stage_cycle(self):
        p = make_pipeline(
            make_stage("a", depends_on=["b"]),
            make_stage("b", depends_on=["a"]),
        )
        errors = p.validate()
        assert any("cycle" in e.lower() for e in errors)

    def test_three_stage_cycle(self):
        p = make_pipeline(
            make_stage("a", depends_on=["c"]),
            make_stage("b", depends_on=["a"]),
            make_stage("c", depends_on=["b"]),
        )
        errors = p.validate()
        assert any("cycle" in e.lower() for e in errors)

    def test_invalid_model(self):
        p = make_pipeline(make_stage("a", model="gpt-4"))
        errors = p.validate()
        assert any("gpt-4" in e for e in errors)

    def test_valid_models_accepted(self):
        for model in ("opus", "sonnet", "haiku"):
            p = make_pipeline(make_stage("a", model=model))
            errors = p.validate()
            assert not any("invalid model" in e.lower() for e in errors)

    def test_multiple_errors_reported(self):
        p = make_pipeline(
            make_stage("a", depends_on=["missing1"], model="bad-model"),
        )
        errors = p.validate()
        assert len(errors) >= 2

    def test_empty_pipeline_valid(self):
        p = Pipeline(name="empty", description="empty", stages=[])
        assert p.validate() == []

    def test_parallel_stages_valid(self):
        """Two stages with no deps between them — both valid."""
        p = make_pipeline(
            make_stage("a"),
            make_stage("b"),
            make_stage("c", depends_on=["a", "b"]),
        )
        assert p.validate() == []


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------


class TestTopologicalOrder:
    def test_linear_chain_order(self):
        p = make_pipeline(
            make_stage("a"),
            make_stage("b", depends_on=["a"]),
            make_stage("c", depends_on=["b"]),
        )
        batches = p.topological_order()
        # Should be 3 sequential batches
        names = [[s.name for s in batch] for batch in batches]
        assert names == [["a"], ["b"], ["c"]]

    def test_parallel_root_stages(self):
        """Two independent roots should land in the same first batch."""
        p = make_pipeline(
            make_stage("scan_a"),
            make_stage("scan_b"),
            make_stage("analyze", depends_on=["scan_a", "scan_b"]),
        )
        batches = p.topological_order()
        assert len(batches) == 2
        first_batch_names = {s.name for s in batches[0]}
        assert first_batch_names == {"scan_a", "scan_b"}
        assert batches[1][0].name == "analyze"

    def test_single_stage_single_batch(self):
        p = make_pipeline(make_stage("only"))
        batches = p.topological_order()
        assert len(batches) == 1
        assert batches[0][0].name == "only"

    def test_feature_build_order(self):
        """architect → implement → (test, review both depend on implement)."""
        p = make_pipeline(
            make_stage("architect"),
            make_stage("implement", depends_on=["architect"]),
            make_stage("test", depends_on=["implement"]),
            make_stage("review", depends_on=["implement", "test"]),
        )
        batches = p.topological_order()
        batch_names = [[s.name for s in b] for b in batches]
        assert batch_names[0] == ["architect"]
        assert batch_names[1] == ["implement"]
        # test before review
        assert "test" in batch_names[2]
        assert "review" in batch_names[3]

    def test_all_stages_covered(self):
        p = make_pipeline(
            make_stage("a"),
            make_stage("b", depends_on=["a"]),
            make_stage("c", depends_on=["a"]),
            make_stage("d", depends_on=["b", "c"]),
        )
        batches = p.topological_order()
        all_names = {s.name for batch in batches for s in batch}
        assert all_names == {"a", "b", "c", "d"}


# ---------------------------------------------------------------------------
# Timeout calculation
# ---------------------------------------------------------------------------


class TestPipelineTimeout:
    def test_timeout_is_sum_plus_10_percent(self):
        p = make_pipeline(
            make_stage("a"),  # 10m
            make_stage("b"),  # 10m
            make_stage("c"),  # 10m
        )
        assert p.timeout_minutes == int(30 * 1.1)

    def test_single_stage_timeout(self):
        s = PipelineStage(
            name="x",
            role="x",
            model="sonnet",
            prompt_template="x",
            timeout_minutes=45,
        )
        p = Pipeline(name="t", description="t", stages=[s])
        assert p.timeout_minutes == int(45 * 1.1)
