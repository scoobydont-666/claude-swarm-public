"""Export and promote pipeline: GGUF → Modelfile → Gate → Deploy → Tag."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

EXPORT_PROMOTE = Pipeline(
    name="export-promote",
    description="Export trained model to GGUF, verify benchmarks pass gates, deploy to Ollama fleet",
    stages=[
        PipelineStage(
            name="gguf_export",
            role="Export checkpoint to GGUF quantized format",
            model="haiku",
            host="giga",  # Export on training anchor
            requires=["gpu"],
            depends_on=[],
            prompt_template="""Export the trained model to GGUF.
Run: cd /opt/ntnx-codeforge && python3 scripts/export.py \
  --checkpoint /opt/swarm/training/checkpoints/{input.run_id} \
  --output-dir /opt/swarm/training/exports/{input.run_id} \
  --quant {input.quant_method} \
  --model-size {input.model_size}
Report: GGUF file size, quantization method, output path.""",
            timeout_minutes=30,
        ),
        PipelineStage(
            name="gate_check",
            role="Verify benchmark gates pass before promotion",
            model="haiku",
            host="mecha",
            requires=[],
            depends_on=["gguf_export"],
            prompt_template="""Verify benchmark gates for promotion eligibility.
Run: cd /opt/ntnx-codeforge && python3 -c "
from ntnx_codeforge.manifest import RunManifest
m = RunManifest.from_yaml('/opt/swarm/training/runs/{input.run_id}/manifest.yaml')
if not m.benchmark_passed:
    print(f'GATE FAILED: scores={{m.benchmark_scores}}')
    exit(1)
print(f'GATE PASSED: scores={{m.benchmark_scores}}')"
Report: pass/fail with scores.""",
            timeout_minutes=2,
        ),
        PipelineStage(
            name="deploy",
            role="Deploy model to target Ollama instances",
            model="haiku",
            host="{input.target_host}",
            requires=["gpu"],
            depends_on=["gate_check"],
            prompt_template="""Deploy the exported model to Ollama on {input.target_host}.
Run:
  cd /opt/swarm/training/exports/{input.run_id}
  ollama create {input.model_name}:{input.version} -f Modelfile
  # Verify model loads and generates
  curl -sf http://127.0.0.1:11434/api/generate \
    -d '{{"model": "{input.model_name}:{input.version}", "prompt": "Write a Python function", "stream": false}}' | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'Response: {{len(d.get(\"response\",\"\"))}} chars')
if len(d.get('response','')) < 10: exit(1)"
Report: deployment status, model tag, response test.""",
            timeout_minutes=10,
        ),
        PipelineStage(
            name="tag_promote",
            role="Tag as latest and record promotion",
            model="haiku",
            host="mecha",
            requires=[],
            depends_on=["deploy"],
            prompt_template="""Record promotion in manifest and tag model.
Run: cd /opt/ntnx-codeforge && python3 -c "
from ntnx_codeforge.manifest import RunManifest
from datetime import datetime, UTC
m = RunManifest.from_yaml('/opt/swarm/training/runs/{input.run_id}/manifest.yaml')
m.promoted = True
m.promoted_at = datetime.now(UTC).isoformat()
m.promotion_targets = ['{input.target_host}']
m.to_yaml('/opt/swarm/training/runs/{input.run_id}/manifest.yaml')
# Append to promotion log
import yaml
log_entry = dict(run_id='{input.run_id}', model=m.model_name, version='{input.version}',
    target='{input.target_host}', promoted_at=m.promoted_at, scores=m.benchmark_scores)
with open('/opt/swarm/training/promotions/log.yaml', 'a') as f:
    f.write('---\\n')
    yaml.dump(log_entry, f, default_flow_style=False)
print(f'Promoted: {input.model_name}:{input.version} → {input.target_host}')"
Report: promotion recorded, model tag, target host.""",
            timeout_minutes=5,
        ),
    ],
)
