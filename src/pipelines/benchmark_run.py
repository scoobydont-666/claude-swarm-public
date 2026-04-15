"""Benchmark pipeline: Load Model → OpenCode → Convention → Score → Record."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

BENCHMARK_RUN = Pipeline(
    name="benchmark-run",
    description="Run multi-metric benchmark suite on a trained model and record results",
    stages=[
        PipelineStage(
            name="deploy_model",
            role="Deploy model to Ollama for benchmark evaluation",
            model="haiku",
            host="mongo",  # Evaluation node
            requires=["gpu"],
            depends_on=[],
            prompt_template="""Deploy the candidate model for benchmarking on gpu-server-4.
Run:
  # Create Ollama model from GGUF
  ollama create {input.model_name} -f /var/lib/swarm/training/exports/{input.run_id}/Modelfile
  # Verify model loads
  curl -s http://127.0.0.1:11434/api/generate -d '{{"model": "{input.model_name}", "prompt": "hello", "stream": false}}' | head -1
Report: model loaded, VRAM used.""",
            timeout_minutes=10,
        ),
        PipelineStage(
            name="opencode_bench",
            role="Run OpenCode benchmark suite (5 coding tasks)",
            model="haiku",
            host="mongo",
            requires=["gpu"],
            depends_on=["deploy_model"],
            prompt_template="""Run the OpenCode benchmark suite.
Run: cd /opt/ntnx-codeforge && python3 scripts/benchmark_suite.py \
  --model {input.model_name} \
  --suite opencode \
  --output /var/lib/swarm/training/runs/{input.run_id}/benchmark-opencode.json \
  --endpoint http://127.0.0.1:11434
Report: per-task scores, average, pass/fail.""",
            timeout_minutes=30,
        ),
        PipelineStage(
            name="convention_bench",
            role="Run convention compliance benchmark",
            model="haiku",
            host="mongo",
            requires=["gpu"],
            depends_on=["deploy_model"],
            prompt_template="""Run convention compliance tests.
Run: cd /opt/ntnx-codeforge && python3 scripts/benchmark_suite.py \
  --model {input.model_name} \
  --suite convention \
  --output /var/lib/swarm/training/runs/{input.run_id}/benchmark-convention.json \
  --endpoint http://127.0.0.1:11434
Report: convention pass rate, failures.""",
            timeout_minutes=20,
        ),
        PipelineStage(
            name="degen_check",
            role="Check for model degeneration (repetition, coherence)",
            model="haiku",
            host="mongo",
            requires=["gpu"],
            depends_on=["deploy_model"],
            prompt_template="""Run degeneration detection tests.
Run: cd /opt/ntnx-codeforge && python3 scripts/benchmark_suite.py \
  --model {input.model_name} \
  --suite degeneration \
  --output /var/lib/swarm/training/runs/{input.run_id}/benchmark-degen.json \
  --endpoint http://127.0.0.1:11434
Report: repetition ratio, coherence score, pass/fail.""",
            timeout_minutes=15,
        ),
        PipelineStage(
            name="record_results",
            role="Aggregate benchmark results and update RunManifest",
            model="haiku",
            host="mecha",
            requires=[],
            depends_on=["opencode_bench", "convention_bench", "degen_check"],
            prompt_template="""Aggregate all benchmark results and update the run manifest.
Run: cd /opt/ntnx-codeforge && python3 -c "
import json, yaml
from ntnx_codeforge.manifest import RunManifest
manifest = RunManifest.from_yaml('/var/lib/swarm/training/runs/{input.run_id}/manifest.yaml')
opencode = json.load(open('/var/lib/swarm/training/runs/{input.run_id}/benchmark-opencode.json'))
convention = json.load(open('/var/lib/swarm/training/runs/{input.run_id}/benchmark-convention.json'))
degen = json.load(open('/var/lib/swarm/training/runs/{input.run_id}/benchmark-degen.json'))
manifest.benchmark_scores = dict(
    opencode=opencode.get('average_score', 0),
    convention=convention.get('pass_rate', 0),
    degeneration=degen.get('repeat_ratio', 1.0))
# Gate check
gates_passed = (manifest.benchmark_scores['opencode'] >= 80
    and manifest.benchmark_scores['convention'] >= 95
    and manifest.benchmark_scores['degeneration'] < 0.05)
manifest.benchmark_passed = gates_passed
manifest.to_yaml('/var/lib/swarm/training/runs/{input.run_id}/manifest.yaml')
print(f'Benchmark: opencode={{manifest.benchmark_scores[\"opencode\"]}}% convention={{manifest.benchmark_scores[\"convention\"]}}% degen={{manifest.benchmark_scores[\"degeneration\"]}}'  )
print(f'Gates: {{\"PASSED\" if gates_passed else \"FAILED\"}}')"
Report: all scores, gate verdict.""",
            timeout_minutes=5,
        ),
    ],
)
