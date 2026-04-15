"""Corpus build pipeline: Extract → Sanitize → Dedup → Validate."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

CORPUS_BUILD = Pipeline(
    name="corpus-build",
    description="Build training corpus from Hydra repos: extract code, sanitize, dedup, validate",
    stages=[
        PipelineStage(
            name="extract",
            role="Extract code from Hydra repos using AST parser",
            model="haiku",
            host="mecha",  # Preprocessing node
            requires=["cpu_heavy"],
            depends_on=[],
            prompt_template="""Extract code from Hydra repos for training corpus.
Run: cd /opt/ntnx-codeforge && python3 data/extract_code.py --output /var/lib/swarm/training/datasets/raw-{input.run_id}.jsonl
Report: record count, file size, source repos included.""",
            timeout_minutes=20,
        ),
        PipelineStage(
            name="sanitize",
            role="Sanitize extracted code — remove IPs, credentials, PII",
            model="haiku",
            host="mecha",
            requires=[],
            depends_on=["extract"],
            prompt_template="""Sanitize the extracted corpus.
Run: cd /opt/ntnx-codeforge && python3 -c "
from data.nutanix.sanitizer import sanitize_batch
import json
records = [json.loads(l) for l in open('/var/lib/swarm/training/datasets/raw-{input.run_id}.jsonl')]
clean, audit = sanitize_batch(records)
with open('/var/lib/swarm/training/datasets/sanitized-{input.run_id}.jsonl', 'w') as f:
    for r in clean: f.write(json.dumps(r) + '\\n')
print(f'Sanitized: {{len(clean)}} records, {{audit}}')"
Report: records processed, redactions made.""",
            timeout_minutes=15,
        ),
        PipelineStage(
            name="dedup",
            role="Deduplicate corpus — exact SHA256 + near-dedup",
            model="haiku",
            host="mecha",
            requires=[],
            depends_on=["sanitize"],
            prompt_template="""Deduplicate the sanitized corpus.
Run: cd /opt/ntnx-codeforge && python3 -c "
from data.dedup import dedup_exact, dedup_near
import json
records = [json.loads(l) for l in open('/var/lib/swarm/training/datasets/sanitized-{input.run_id}.jsonl')]
unique, exact_removed = dedup_exact(records)
final, near_removed = dedup_near(unique, threshold=0.8)
with open('/var/lib/swarm/training/datasets/deduped-{input.run_id}.jsonl', 'w') as f:
    for r in final: f.write(json.dumps(r) + '\\n')
print(f'Dedup: {{len(final)}} final, {{exact_removed}} exact dupes, {{near_removed}} near dupes')"
Report: final count, exact removed, near-dupes removed.""",
            timeout_minutes=15,
        ),
        PipelineStage(
            name="validate",
            role="Validate dataset and generate manifest",
            model="haiku",
            host="mecha",
            requires=[],
            depends_on=["dedup"],
            prompt_template="""Validate the deduplicated corpus and create dataset manifest.
Run: cd /opt/ntnx-codeforge && python3 -c "
from ntnx_codeforge.manifest import DatasetManifest, compute_file_sha256
from datetime import datetime, UTC
import json
path = '/var/lib/swarm/training/datasets/deduped-{input.run_id}.jsonl'
records = [json.loads(l) for l in open(path)]
sha = compute_file_sha256(path)
m = DatasetManifest(sha256=sha, name='hydra-corpus-{input.run_id}', path=path,
    record_count=len(records), sanitized=True, created_at=datetime.now(UTC).isoformat())
m.to_yaml(f'/var/lib/swarm/training/datasets/{{sha}}/manifest.yaml')
print(f'Dataset manifest: {{sha}} — {{len(records)}} records')"
Report: SHA256, record count, manifest path.""",
            timeout_minutes=10,
        ),
    ],
)
