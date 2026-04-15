"""Self-Correcting Test Loop Pipeline — Run tests, fix failures, repeat.

Specialized generator-verifier for test-driven fixing:
1. Run pytest on the project
2. If failures, dispatch Claude to fix the code
3. Re-run tests
4. Max 3 iterations

Pattern from Cursor Background Agents test loop.
"""

from pipeline import Pipeline, PipelineStage


def create(project_dir: str = "<hydra-project-path>", test_cmd: str = "pytest") -> Pipeline:
    """Create a self-correcting test-fix loop pipeline."""
    return Pipeline(
        name="test_fix_loop",
        description="Run tests, fix failures, repeat until green (max 3 iterations)",
        stages=[
            PipelineStage(
                name="run_tests",
                role="Execute the project test suite and report results",
                model="haiku",
                prompt_template=(
                    f"Run the test suite in {{project_dir}}:\n\n"
                    f"  cd {{project_dir}} && {test_cmd}\n\n"
                    "Report the full output. If all tests pass, say 'ALL TESTS PASSED'.\n"
                    "If any tests fail, report the exact failure output."
                ),
                timeout_minutes=10,
            ),
            PipelineStage(
                name="fix_failures",
                role="Analyze test failures and fix the code",
                model="sonnet",
                prompt_template=(
                    "Tests failed in {project_dir}. Fix the code.\n\n"
                    "Test output:\n{previous_output.run_tests}\n\n"
                    "Original task: {input}\n\n"
                    "{_loop_error}\n\n"
                    "Fix the failing tests. Be minimal — only change what's needed."
                ),
                depends_on=["run_tests"],
                timeout_minutes=15,
                loop_to="run_tests",  # Re-run tests after fix
                max_iterations=3,
                success_pattern=r"(?i)(all tests? passed|0 failed|passed.*0 errors)",
            ),
        ],
    )
