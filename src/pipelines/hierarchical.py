"""Hierarchical Planning Pipeline — Planner → Manager → Worker.

Three-tier decomposition:
1. Planner (Opus): Reads PRD/spec, decomposes into sub-tasks
2. Manager (Sonnet): Sequences sub-tasks, assigns to hosts
3. Worker (Sonnet/Haiku): Executes each sub-task

Pattern from Paperclip AI's hierarchical agent model.
"""

from pipeline import Pipeline, PipelineStage


def create(project_dir: str = "<hydra-project-path>") -> Pipeline:
    """Create a hierarchical planning pipeline."""
    return Pipeline(
        name="hierarchical",
        description="Planner decomposes → Manager sequences → Worker executes",
        stages=[
            PipelineStage(
                name="plan",
                role="Decompose the high-level goal into concrete sub-tasks",
                model="opus",
                prompt_template=(
                    "You are a software architect planning work in {project_dir}.\n\n"
                    "Goal: {input}\n\n"
                    "Decompose this into 3-7 concrete, independently executable sub-tasks.\n"
                    "For each sub-task, specify:\n"
                    "- Title (one line)\n"
                    "- Description (what to do)\n"
                    "- Files likely affected\n"
                    "- Estimated complexity (haiku/sonnet/opus)\n"
                    "- Dependencies (which sub-tasks must complete first)\n\n"
                    "Output as YAML list."
                ),
                timeout_minutes=10,
            ),
            PipelineStage(
                name="sequence",
                role="Sequence sub-tasks and assign to fleet members",
                model="sonnet",
                prompt_template=(
                    "You are a project manager sequencing work across the fleet.\n\n"
                    "Sub-tasks from planner:\n{previous_output.plan}\n\n"
                    "Fleet: GIGA (96GB GPU), MEGA (32GB GPU x2), MECHA (16GB GPU), "
                    "MONGO (16GB GPU), miniboss (CPU only)\n\n"
                    "Sequence the sub-tasks in execution order. Group independent tasks "
                    "for parallel execution. Assign each to the best fleet member.\n\n"
                    "Output as ordered YAML with host assignments."
                ),
                depends_on=["plan"],
                timeout_minutes=5,
            ),
            PipelineStage(
                name="execute",
                role="Execute the sequenced sub-tasks",
                model="sonnet",
                prompt_template=(
                    "Execute the following sequenced work plan in {project_dir}:\n\n"
                    "{previous_output.sequence}\n\n"
                    "Original goal: {input}\n\n"
                    "Execute each sub-task in order. Commit after each. "
                    "Report progress and any blockers."
                ),
                depends_on=["sequence"],
                timeout_minutes=30,
            ),
        ],
    )
