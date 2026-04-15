"""Student training pipeline: Claim GPU → Stop vLLM → Train QLoRA → Smoke Test → Release."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

STUDENT_TRAIN = Pipeline(
    name="student-train",
    description="Train a student model via QLoRA on GIGA, with GPU lifecycle management",
    stages=[
        PipelineStage(
            name="claim_gpu",
            role="Stop vLLM on GIGA to free GPU for training",
            model="haiku",
            host="giga",
            requires=["gpu"],
            depends_on=[],
            prompt_template="""Prepare GIGA GPU for training by stopping vLLM.
Run:
  docker stop vllm-giga-primary 2>/dev/null || true
  sleep 5
  nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
Verify: GPU VRAM is mostly free (< 2GB used).
Report: VRAM status before and after.""",
            timeout_minutes=5,
        ),
        PipelineStage(
            name="train",
            role="Run QLoRA fine-tuning with Unsloth",
            model="haiku",
            host="giga",
            requires=["gpu"],
            depends_on=["claim_gpu"],
            prompt_template="""Train student model on GIGA.
Run: cd /opt/ntnx-codeforge && python3 finetune.py \
  --model-size {input.model_size} \
  --dataset {input.dataset_path} \
  --run-id {input.run_id} \
  --output-dir /opt/swarm/training/checkpoints/{input.run_id}
Monitor: GPU utilization, loss curve, ETA.
Report: final loss, training duration, checkpoint path.""",
            timeout_minutes=120,
        ),
        PipelineStage(
            name="smoke_test",
            role="Run smoke test on trained checkpoint",
            model="haiku",
            host="giga",
            requires=["gpu"],
            depends_on=["train"],
            prompt_template="""Smoke test the trained model.
Run: cd /opt/ntnx-codeforge && python3 eval/smoke_test.py \
  --checkpoint /opt/swarm/training/checkpoints/{input.run_id} \
  --model-size {input.model_size}
Report: pass/fail for each test, overall verdict.""",
            timeout_minutes=15,
        ),
        PipelineStage(
            name="release_gpu",
            role="Restart vLLM on GIGA after training",
            model="haiku",
            host="giga",
            requires=[],
            depends_on=["smoke_test"],
            prompt_template="""Restore vLLM on GIGA after training completes.
Run:
  docker start vllm-giga-primary
  sleep 30
  curl -sf http://127.0.0.1:8000/health && echo "vLLM restored" || echo "vLLM failed to restart"
Report: vLLM health status.""",
            timeout_minutes=5,
        ),
    ],
)
