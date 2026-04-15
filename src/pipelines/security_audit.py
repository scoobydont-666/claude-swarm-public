"""Security audit pipeline: parallel host scans → combined analysis."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import Pipeline, PipelineStage

SECURITY_AUDIT = Pipeline(
    name="security-audit",
    description="Scan → Analyze → Report across the fleet",
    stages=[
        PipelineStage(
            name="scan_orchestration",
            role="Security scan of orchestration-node",
            model="sonnet",
            host="orchestration-node",
            requires=[],
            depends_on=[],
            prompt_template="""Run a security audit of this host:
- Check listening ports (ss -tlnp)
- Check UFW rules
- Check for outdated packages
- Check service permissions
- Check for secrets in environment
Report all findings with severity.""",
            timeout_minutes=20,
        ),
        PipelineStage(
            name="scan_giga",
            role="Security scan of gpu-server-1",
            model="sonnet",
            host="gpu-server-1",
            requires=[],
            depends_on=[],
            prompt_template="""Run a security audit of this host:
- Check listening ports (ss -tlnp)
- Check UFW rules
- Check Docker container security
- Check for outdated packages
- Check NFS exports
Report all findings with severity.""",
            timeout_minutes=20,
        ),
        PipelineStage(
            name="analyze",
            role="Analyze combined findings and prioritize",
            model="opus",
            host=None,
            requires=[],
            depends_on=["scan_orchestration", "scan_giga"],
            prompt_template="""Security scan results:

Orchestration: {previous_output.scan_orchestration}
gpu-server-1: {previous_output.scan_giga}

Analyze the combined findings:
1. Prioritize by severity and blast radius
2. Identify cross-host issues
3. Create a remediation plan ordered by priority
4. Flag anything that needs immediate human attention""",
            timeout_minutes=20,
        ),
    ],
)
