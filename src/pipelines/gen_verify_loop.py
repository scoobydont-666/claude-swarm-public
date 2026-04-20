"""Generator-Verifier Loop Pipeline — Write code, test it, fix if broken.

This pipeline implements the generator-verifier pattern:
1. Generate: Write/modify code based on the task
2. Verify: Run tests or checks
3. If verify fails, loop back to generate with error context
4. Max 3 iterations before giving up

Pattern from Anthropic Multi-Agent Coordination and Cursor Background Agents.
"""

from pipeline import Pipeline, PipelineStage


def create(project_dir: str = "<hydra-project-path>") -> Pipeline:
    """Create a generator-verifier loop pipeline."""
    return Pipeline(
        name="gen_verify_loop",
        description="Generate code, verify with tests, loop on failure (max 3 iterations)",
        stages=[
            PipelineStage(
                name="generate",
                role="Write or modify code to implement the requested change",
                model="sonnet",
                prompt_template=(
                    "You are implementing a code change in {project_dir}.\n\n"
                    "Task: {input}\n\n"
                    "{_loop_error}\n\n"
                    "Write the code changes needed. Be precise and test-aware."
                ),
                timeout_minutes=15,
            ),
            PipelineStage(
                name="verify",
                role="Run tests and verify the generated code works correctly",
                model="haiku",
                prompt_template=(
                    "Verify the code changes just made in {project_dir}.\n\n"
                    "Original task: {input}\n\n"
                    "Run the project's test suite. If tests pass, report SUCCESS.\n"
                    "If tests fail, report the exact failures so the generator can fix them.\n\n"
                    "Previous generate output: {previous_output.generate}"
                ),
                depends_on=["generate"],
                timeout_minutes=10,
                loop_to="generate",  # Loop back to generate on failure
                max_iterations=3,
                success_pattern=r"(?i)(all.*pass|success|0 failed|tests? passed)",
            ),
        ],
    )
