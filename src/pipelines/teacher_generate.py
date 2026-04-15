"""Teacher generation pipeline: Shard → vLLM Batch → Collect → Filter."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

TEACHER_GENERATE = Pipeline(
    name="teacher-generate",
    description="Generate training corpus from teacher models via vLLM batch inference",
    stages=[
        PipelineStage(
            name="shard_prompts",
            role="Split prompt corpus into shards for parallel generation",
            model="haiku",
            host="mecha",  # Preprocessing node
            requires=[],
            depends_on=[],
            prompt_template="""Shard the prompt corpus for parallel teacher generation.
Run: cd /opt/ntnx-codeforge && python3 -c "
import json, os
prompts = [json.loads(l) for l in open('{input.prompt_file}')]
total = len(prompts)
shard_size = (total + 1) // 2  # 2 shards: gpu-server-1 + gpu-server-3
for i in range(2):
    shard = prompts[i*shard_size:(i+1)*shard_size]
    path = '/var/lib/swarm/training/datasets/shard-{input.run_id}-' + str(i) + '.jsonl'
    with open(path, 'w') as f:
        for p in shard: f.write(json.dumps(p) + '\\n')
    print(f'Shard {{i}}: {{len(shard)}} prompts → {{path}}')"
Report: shard count, prompts per shard.""",
            timeout_minutes=5,
        ),
        PipelineStage(
            name="generate_giga",
            role="Generate responses on gpu-server-1 vLLM (Qwen3-32B, complex prompts)",
            model="haiku",
            host="giga",
            requires=["gpu"],
            depends_on=["shard_prompts"],
            prompt_template="""Generate teacher responses using gpu-server-1 vLLM (Qwen3-32B-AWQ).
Run: cd /opt/ntnx-codeforge && python3 scripts/vllm_batch_generate.py \
  --endpoint http://127.0.0.1:8000/v1 \
  --input /var/lib/swarm/training/datasets/shard-{input.run_id}-0.jsonl \
  --output /var/lib/swarm/training/datasets/generated-{input.run_id}-giga.jsonl \
  --concurrency 4 --model qwen3:32b
Report: records generated, average latency, errors.""",
            timeout_minutes=60,
        ),
        PipelineStage(
            name="generate_mega",
            role="Generate responses on gpu-server-3 vLLM TP=2 (Qwen3-14B, simpler prompts)",
            model="haiku",
            host="mega",
            requires=["gpu"],
            depends_on=["shard_prompts"],
            prompt_template="""Generate teacher responses using gpu-server-3 vLLM TP=2 (Qwen3-14B-AWQ).
Run: cd /opt/ntnx-codeforge && python3 scripts/vllm_batch_generate.py \
  --endpoint http://127.0.0.1:8000/v1 \
  --input /var/lib/swarm/training/datasets/shard-{input.run_id}-1.jsonl \
  --output /var/lib/swarm/training/datasets/generated-{input.run_id}-mega.jsonl \
  --concurrency 8 --model qwen3:14b
Report: records generated, average latency, errors.""",
            timeout_minutes=60,
        ),
        PipelineStage(
            name="collect_filter",
            role="Merge shards, deduplicate, quality filter",
            model="haiku",
            host="mecha",
            requires=[],
            depends_on=["generate_giga", "generate_mega"],
            prompt_template="""Merge and filter generated teacher responses.
Run: cd /opt/ntnx-codeforge && python3 -c "
import json
from data.dedup import dedup_exact
giga = [json.loads(l) for l in open('/var/lib/swarm/training/datasets/generated-{input.run_id}-giga.jsonl')]
mega = [json.loads(l) for l in open('/var/lib/swarm/training/datasets/generated-{input.run_id}-mega.jsonl')]
combined = giga + mega
unique, removed = dedup_exact(combined)
# Quality filter: remove empty/short responses
filtered = [r for r in unique if len(r.get('output','')) > 100]
path = '/var/lib/swarm/training/datasets/teacher-corpus-{input.run_id}.jsonl'
with open(path, 'w') as f:
    for r in filtered: f.write(json.dumps(r) + '\\n')
print(f'Merged: {{len(combined)}} → {{len(unique)}} (dedup) → {{len(filtered)}} (filtered)')"
Report: total generated, after dedup, after filter.""",
            timeout_minutes=10,
        ),
    ],
)
