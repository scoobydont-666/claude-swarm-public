# plan-approved: claude-swarm-scripts
"""Fleet Capability Index pipeline — 45 models × N task classes across node_gpu/node_mongo/node_reserve2/node_reserve1/MINIBOSS."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

FLEET_CAPABILITY_INDEX = Pipeline(
    name="fleet-capability-index",
    description="Run bakeoff across full fleet; emit capability index JSON + MD.",
    stages=[
        PipelineStage(
            name="preflight",
            role="Verify node_gpu no training, all 5 Ollama hosts up on :11434, GpuScheduler inventory fresh",
            model="haiku",
            host="node_primary",
            requires=["coordinator"],
            depends_on=[],
            prompt_template="""Pre-flight checks:
1. ssh giga 'pgrep -f "unsloth|train.py|finetune"' — abort if any hit
2. for h in giga mongo mecha mega node_primary; do curl -sf http://$h:11434/api/tags | jq '.models | length'; done — all must return same count
3. swarm gpu inventory refresh
Report: OK / ABORT with reason.""",
            timeout_minutes=5,
        ),
        PipelineStage(
            name="spawn_tier1",
            role="Create swarm tasks for Tier 1 screening — 45 models × 3 fast tasks (classification, json_mode, code_gen)",
            model="haiku",
            host="node_primary",
            requires=["coordinator"],
            depends_on=["preflight"],
            prompt_template="""For each model in /opt/ai-shared/ollama/manifests (45 unique), for each class in [classification, json_mode, code_gen]:
  swarm tasks create "bench-{{model_safe}}-{{class}}" \\
    --desc "Bakeoff cell: {{model}} × {{class}}" \\
    --project fleet-capability-index \\
    --priority 3 \\
    --requires gpu,{{model_size_tag}} \\
    --minutes 5 \\
    --payload '{{"model":"{{model}}","class":"{{class}}","tier":"screen","out":"/opt/swarm/artifacts/bakeoff-cells/tier1/{{model_safe}}-{{class}}.json"}}'
Expected: ~135 tasks queued.""",
            timeout_minutes=10,
        ),
        PipelineStage(
            name="execute_tier1",
            role="Workers claim Tier 1 cells and execute via bakeoff.py --cell",
            model="sonnet",
            host="any",
            requires=["gpu"],
            depends_on=["spawn_tier1"],
            prompt_template="""Per-cell worker logic:
1. claim = swarm tasks claim --requires gpu
2. sched = GpuScheduler.schedule(claim.id, payload.model, required_vram_mb=model_size_gb*1300, prefer_host=None)
3. Run: python3 <hydra-project-path>/scripts/bakeoff/bakeoff.py --cell --model payload.model --class payload.class --host sched.host --out payload.out
4. GpuScheduler.release(sched.host, sched.gpu_indices)
5. swarm tasks complete claim.id --artifact payload.out
Expected: ~2h to drain Tier 1 queue.""",
            timeout_minutes=180,
        ),
        PipelineStage(
            name="tier1_gate",
            role="Screen Tier 1 results; select top-15 + pinned seeds for finals",
            model="haiku",
            host="node_primary",
            requires=["coordinator"],
            depends_on=["execute_tier1"],
            prompt_template="""Read all /opt/swarm/artifacts/bakeoff-cells/tier1/*.json.
Compute avg(pct) per model. Sort. Select top-15 + pinned seeds: project-a-14b-obbba, project-a-tax:v5, hydracoder:v5, hydracoder:32bv2, hermes3:8b, qwen3.6:35b-a3b, deepseek-r1:32b.
Write selection to /opt/swarm/artifacts/bakeoff-cells/tier1-selection.json.""",
            timeout_minutes=5,
        ),
        PipelineStage(
            name="spawn_tier2",
            role="Create swarm tasks for Tier 2 finals — top-15 × 10 task classes",
            model="haiku",
            host="node_primary",
            requires=["coordinator"],
            depends_on=["tier1_gate"],
            prompt_template="""Read tier1-selection.json. For each model × all 10 classes including creative + tax_rag (tax_rag gated by model tag):
  swarm tasks create "bench-fin-{{model_safe}}-{{class}}" ...
Expected: ~150-180 tasks queued.""",
            timeout_minutes=10,
        ),
        PipelineStage(
            name="execute_tier2",
            role="Workers claim Tier 2 cells",
            model="sonnet",
            host="any",
            requires=["gpu"],
            depends_on=["spawn_tier2"],
            prompt_template="Same as execute_tier1 but output to tier2/. Expected: ~5h to drain.",
            timeout_minutes=360,
        ),
        PipelineStage(
            name="aggregate",
            role="Build capability index JSON + MD from Tier 1 + Tier 2 cells",
            model="haiku",
            host="node_primary",
            requires=["coordinator"],
            depends_on=["execute_tier2"],
            prompt_template="""Run: python3 <hydra-project-path>/scripts/bakeoff/build_capability_index.py \\
  --tier1 /opt/swarm/artifacts/bakeoff-cells/tier1/ \\
  --tier2 /opt/swarm/artifacts/bakeoff-cells/tier2/ \\
  --out /opt/swarm/artifacts/fleet-capability-index-2026-04-23.json \\
  --md <hydra-project-path>/docs/model-capability-index.md \\
  --verify
Anchor spot-check: hydracoder:v5 wins coding_hot, project-a-14b-obbba wins tax_rag, hermes3:8b wins refusal_resistance.""",
            timeout_minutes=10,
        ),
    ],
)
