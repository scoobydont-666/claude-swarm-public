"""Tests for PipelineContext — prompt rendering with previous outputs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from pipeline import PipelineContext


class TestPromptRendering:
    def test_input_token(self):
        ctx = PipelineContext({"task": "Build a CSV exporter"})
        result = ctx.render_prompt("Task: {input}")
        assert result == "Task: Build a CSV exporter"

    def test_input_field_access(self):
        ctx = PipelineContext({"cert": "FAR", "count": "10", "domain": "Revenue"})
        result = ctx.render_prompt("Cert={input.cert} Count={input.count} Domain={input.domain}")
        assert result == "Cert=FAR Count=10 Domain=Revenue"

    def test_missing_input_field_placeholder(self):
        ctx = PipelineContext({"cert": "FAR"})
        result = ctx.render_prompt("{input.missing}")
        assert "<input.missing not found>" in result

    def test_previous_output_substitution(self):
        ctx = PipelineContext({"task": "fix bug"})
        ctx.add_output("investigate", "Root cause: off-by-one in line 42")
        result = ctx.render_prompt("Investigation: {previous_output.investigate}")
        assert "off-by-one in line 42" in result

    def test_multiple_previous_outputs(self):
        ctx = PipelineContext({"task": "review"})
        ctx.add_output("architect", "Design: use layered arch")
        ctx.add_output("implement", "Code: 200 lines written")
        ctx.add_output("test", "Tests: 15/15 pass")
        result = ctx.render_prompt(
            "Arch={previous_output.architect}\n"
            "Impl={previous_output.implement}\n"
            "Test={previous_output.test}"
        )
        assert "layered arch" in result
        assert "200 lines" in result
        assert "15/15" in result

    def test_missing_previous_output_placeholder(self):
        ctx = PipelineContext({"task": "x"})
        result = ctx.render_prompt("{previous_output.nonexistent}")
        assert "<output of 'nonexistent' not available>" in result

    def test_context_token_concatenates_all_outputs(self):
        ctx = PipelineContext({"task": "audit"})
        ctx.add_output("scan_a", "findings from A")
        ctx.add_output("scan_b", "findings from B")
        result = ctx.render_prompt("All context:\n{context}")
        assert "findings from A" in result
        assert "findings from B" in result
        assert "scan_a" in result
        assert "scan_b" in result

    def test_empty_context_token(self):
        ctx = PipelineContext({"task": "start"})
        result = ctx.render_prompt("Context: {context}")
        assert result == "Context: "

    def test_input_fallback_to_str_when_no_task_key(self):
        ctx = PipelineContext({"cert": "FAR", "count": "5"})
        result = ctx.render_prompt("{input}")
        # No 'task' key — should fall back to str(input_data)
        assert "FAR" in result or "cert" in result

    def test_template_with_no_tokens(self):
        ctx = PipelineContext({"task": "x"})
        result = ctx.render_prompt("Static prompt with no tokens.")
        assert result == "Static prompt with no tokens."

    def test_add_output_overwrites_previous(self):
        ctx = PipelineContext({"task": "x"})
        ctx.add_output("stage1", "first output")
        ctx.add_output("stage1", "second output")
        result = ctx.render_prompt("{previous_output.stage1}")
        assert "second output" in result
        assert "first output" not in result

    def test_input_integer_value(self):
        ctx = PipelineContext({"count": 42})
        result = ctx.render_prompt("{input.count}")
        assert "42" in result
