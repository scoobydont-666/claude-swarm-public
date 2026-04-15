"""Bug fix pipeline: Investigate → Root cause → Fix → Test → Verify."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

BUG_FIX = Pipeline(
    name="bug-fix",
    description="Investigate → Root cause → Fix → Test → Verify",
    stages=[
        PipelineStage(
            name="investigate",
            role="Explore the codebase to understand the bug",
            model="sonnet",
            host=None,
            requires=[],
            depends_on=[],
            prompt_template="""Bug report: {input}

Investigate:
1. Find the relevant code
2. Identify potential root causes
3. Check related tests
4. Report your findings""",
            timeout_minutes=20,
        ),
        PipelineStage(
            name="fix",
            role="Implement the fix",
            model="sonnet",
            host=None,
            requires=[],
            depends_on=["investigate"],
            prompt_template="""Investigation results: {previous_output.investigate}

Fix the bug. Make minimal, targeted changes. Write a test that would have caught this.""",
            timeout_minutes=30,
        ),
        PipelineStage(
            name="verify",
            role="Run tests to verify the fix",
            model="haiku",
            host=None,
            requires=[],
            depends_on=["fix"],
            prompt_template="""A fix was applied: {previous_output.fix}

Run ALL tests. Verify:
1. The new test passes
2. No existing tests broke
3. The original bug is fixed""",
            timeout_minutes=15,
        ),
    ],
)
