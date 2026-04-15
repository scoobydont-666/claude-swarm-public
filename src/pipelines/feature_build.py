"""Feature build pipeline: Architect → Implement → Test → Review."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

FEATURE_BUILD = Pipeline(
    name="feature-build",
    description="Architect → Implement → Test → Review pipeline",
    stages=[
        PipelineStage(
            name="architect",
            role="Design the solution architecture and create implementation plan",
            model="opus",
            host=None,
            requires=[],
            depends_on=[],
            prompt_template="""You are a senior software architect.

Task: {input}

Design the solution:
1. Architecture decisions
2. Files to create/modify
3. Key interfaces and data models
4. Test strategy

Be specific — the implementer needs exact file paths and function signatures.""",
            timeout_minutes=20,
        ),
        PipelineStage(
            name="implement",
            role="Write the code based on the architecture",
            model="sonnet",
            host=None,
            requires=[],
            depends_on=["architect"],
            prompt_template="""You are implementing a feature based on this architecture:

{previous_output.architect}

Original task: {input}

Write ALL the code. Create all files. Follow the architecture exactly.
Use the project's existing patterns and conventions.""",
            timeout_minutes=40,
        ),
        PipelineStage(
            name="test",
            role="Run tests and verify the implementation",
            model="haiku",
            host=None,
            requires=[],
            depends_on=["implement"],
            prompt_template="""The following code was just implemented:

{previous_output.implement}

Run the test suite. Report:
1. Tests passing/failing
2. Any errors or warnings
3. Test coverage gaps""",
            timeout_minutes=15,
        ),
        PipelineStage(
            name="review",
            role="Code review the implementation for quality and security",
            model="opus",
            host=None,
            requires=[],
            depends_on=["implement", "test"],
            prompt_template="""Review this implementation:

Architecture: {previous_output.architect}
Implementation: {previous_output.implement}
Test results: {previous_output.test}

Check for:
1. Architecture compliance
2. Security issues
3. Code quality
4. Missing edge cases
5. Whether tests are sufficient

If the implementation is good, say APPROVED.
If issues found, list them with severity.""",
            timeout_minutes=20,
        ),
    ],
)
