"""Question generation pipeline: Generate → Validate CPA exam questions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

QUESTION_GEN = Pipeline(
    name="question-generation",
    description="Generate → Validate → Report CPA exam questions",
    stages=[
        PipelineStage(
            name="generate",
            role="Generate CPA exam questions via Ollama",
            model="sonnet",
            host="orchestration-node",  # orchestrates, calls gpu-server-1 Ollama
            requires=[],
            depends_on=[],
            prompt_template="""Generate {input.count} CPA exam questions for section {input.cert}.
Use the examforge CLI:
  cd /opt/examforge/backend && .venv/bin/python -m examforge.cli generate --cert {input.cert} --domain {input.domain} --count {input.count}
Report how many were generated.""",
            timeout_minutes=30,
        ),
        PipelineStage(
            name="validate",
            role="Run QA validation pipeline",
            model="sonnet",
            host="orchestration-node",
            requires=[],
            depends_on=["generate"],
            prompt_template="""Questions were generated: {previous_output.generate}

Run QA validation:
  cd /opt/examforge/backend && .venv/bin/python -m examforge.cli validate --cert {input.cert}
Report: how many APPROVE, REVIEW, REJECT.""",
            timeout_minutes=20,
        ),
    ],
)
