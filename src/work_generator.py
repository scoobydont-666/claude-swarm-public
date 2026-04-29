"""Work Generator — Stage 2 of claude-swarm evolution.

Scans project state, Prometheus alerts, git changes, ExamForge pipeline,
and scheduled maintenance windows to generate swarm tasks automatically.

Work generation is ALWAYS safe: it only creates task files, never modifies
project state or triggers execution.
"""

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen
import json
import sys

import yaml

sys.path.insert(0, str(Path(__file__).parent))
try:
    from backend import lib as swarm
except ImportError:
    pass
from util import atomic_write_yaml as _atomic_write_yaml


# ---------------------------------------------------------------------------
# Model inference helpers
# ---------------------------------------------------------------------------

# Keywords → model tier
_HUMAN_SKIP_RE = re.compile(
    r"\b(josh|manual review|human review|physical|approve|sign off)\b", re.IGNORECASE
)
_HAIKU_RE = re.compile(
    r"\b(test|run|check|verify|count|list|search|grep|find|scan|validate|monitor)\b",
    re.IGNORECASE,
)
_OPUS_RE = re.compile(
    r"\b(design|architect|architecture|plan|research|audit|analyze|analysis)\b",
    re.IGNORECASE,
)
# Everything else → sonnet
_SONNET_RE = re.compile(
    r"\b(build|implement|create|write|refactor|fix|update|migrate|generate|add)\b",
    re.IGNORECASE,
)

# Capability keywords
_GPU_RE = re.compile(
    r"\b(gpu|cuda|tensor|train|comfyui|ollama|llm|embed)\b", re.IGNORECASE
)
_DOCKER_RE = re.compile(r"\b(docker|container|swarm|deploy|ansible)\b", re.IGNORECASE)


def infer_model(text: str) -> str:
    """Infer appropriate model tier from task text."""
    if _OPUS_RE.search(text):
        return "opus"
    if _HAIKU_RE.search(text):
        return "haiku"
    return "sonnet"


def infer_requires(text: str) -> list[str]:
    """Infer required capabilities from task text."""
    caps: list[str] = []
    if _GPU_RE.search(text):
        caps.append("gpu")
    if _DOCKER_RE.search(text):
        caps.append("docker")
    return caps


def is_human_task(text: str) -> bool:
    """Return True if the item requires human action and should be skipped."""
    return bool(_HUMAN_SKIP_RE.search(text))


# ---------------------------------------------------------------------------
# Scan state tracker
# ---------------------------------------------------------------------------


def _scan_state_path(swarm_root: Path) -> Path:
    path = swarm_root / "config" / "git-scan-state.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_scan_state(swarm_root: Path) -> dict:
    """Load git scan state from disk.

    Args:
        swarm_root: Root directory of swarm installation

    Returns:
        Dictionary of scan state, empty dict if file doesn't exist
    """
    path = _scan_state_path(swarm_root)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return {}


def save_scan_state(swarm_root: Path, state: dict) -> None:
    """Save git scan state to disk.

    Args:
        swarm_root: Root directory of swarm installation
        state: Scan state dictionary to persist
    """
    path = _scan_state_path(swarm_root)
    _atomic_write_yaml(path, state)


# ---------------------------------------------------------------------------
# WorkGenerator
# ---------------------------------------------------------------------------


class WorkGenerator:
    """Scans projects, Prometheus alerts, and git state to generate work tasks."""

    def __init__(self, config: dict) -> None:
        self.config = config  # Store config for access in generate_work
        self.swarm_root = Path(config.get("swarm_root", "/opt/swarm"))
        wg_cfg = config.get("work_generator", {})
        self.projects: dict[str, dict] = wg_cfg.get("projects", {})
        self.prometheus_url: str = wg_cfg.get("prometheus_url", "http://127.0.0.1:9090")
        sched_cfg = config.get("scheduled_maintenance", {})
        self.daily_hour: int = sched_cfg.get("daily_hour", 6)
        self.weekly_day: int = sched_cfg.get("weekly_day", 0)  # 0 = Monday

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def generate_work(self) -> list[dict]:
        """Scan all sources and return proposed (not-yet-created) tasks.

        Respects backpressure: if pending tasks >= max_pending_tasks, returns empty list.
        """
        # Check backpressure
        max_pending = self.config.get("work_generator", {}).get("max_pending_tasks", 10)
        from pathlib import Path

        pending_dir = Path(self.swarm_root) / "tasks" / "pending"
        pending_count = 0
        if pending_dir.is_dir():
            pending_count = len(list(pending_dir.glob("task-*.yaml")))

        if pending_count >= max_pending:
            import logging

            log = logging.getLogger("swarm.work_generator")
            log.debug(
                "backpressure: %d tasks pending (max %d), skipping generation",
                pending_count,
                max_pending,
            )
            return []

        tasks: list[dict] = []
        tasks.extend(self.scan_project_plans())
        tasks.extend(self.scan_prometheus_alerts())
        tasks.extend(self.scan_git_changes())
        tasks.extend(self.scan_examforge_pipeline())
        tasks.extend(self.scan_scheduled_maintenance())
        return self.deduplicate(tasks)

    # -----------------------------------------------------------------------
    # 1. Project plan scanner
    # -----------------------------------------------------------------------

    def scan_project_plans(self) -> list[dict]:
        """Find uncompleted items in project plans and create tasks for them."""
        tasks: list[dict] = []
        for project_name, project_cfg in self.projects.items():
            project_path = Path(project_cfg.get("path", f"/opt/{project_name}"))
            host = project_cfg.get("host", "node_primary")
            plans_dir = project_path / "plans"
            if not plans_dir.is_dir():
                continue

            # Accept <project>-plan.md or plan.md or any *.md in plans/
            candidates = list(plans_dir.glob(f"{project_name}-plan.md"))
            if not candidates:
                candidates = list(plans_dir.glob("plan.md"))
            if not candidates:
                candidates = list(plans_dir.glob("*.md"))

            for plan_file in candidates:
                item = self._first_actionable_item(plan_file)
                if item is None:
                    continue
                text = item["text"]
                requires = infer_requires(text)
                # Override requires based on project host capabilities
                if host == "node_gpu":
                    if "gpu" not in requires:
                        requires.append("gpu")

                tasks.append(
                    self._make_task(
                        title=f"[{project_name}] {text}",
                        description=(
                            f"From project plan: {plan_file.name}, "
                            f"phase {item.get('phase', '?')}"
                        ),
                        project=str(project_path),
                        priority="medium",
                        requires=requires,
                        source="project_plan",
                    )
                )
        return tasks

    def _first_actionable_item(self, plan_file: Path) -> Optional[dict]:
        """Parse a markdown plan file and return the first actionable incomplete item.

        Skips human tasks (Josh review, manual, physical) and continues to find
        the first machine-actionable incomplete checklist item.
        """
        try:
            content = plan_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        current_phase = "unknown"
        incomplete_re = re.compile(r"^\s*-\s+\[ \]\s+(.+)$")
        phase_re = re.compile(r"^#+\s+(Phase\s+\S+.*|Stage\s+\S+.*)", re.IGNORECASE)

        for line in content.splitlines():
            phase_match = phase_re.match(line)
            if phase_match:
                current_phase = phase_match.group(1).strip()
                continue
            item_match = incomplete_re.match(line)
            if item_match:
                text = item_match.group(1).strip()
                if is_human_task(text):
                    continue  # skip human tasks and keep looking
                return {"text": text, "phase": current_phase}
        return None

    # -----------------------------------------------------------------------
    # 2. Prometheus alert scanner
    # -----------------------------------------------------------------------

    def scan_prometheus_alerts(self) -> list[dict]:
        """Check Prometheus for firing alerts and create investigation tasks."""
        tasks: list[dict] = []
        try:
            url = f"{self.prometheus_url}/api/v1/alerts"
            with urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode())
        except (URLError, OSError, json.JSONDecodeError):
            return tasks

        alerts = data.get("data", {}).get("alerts", [])
        for alert in alerts:
            if alert.get("state") != "firing":
                continue
            labels = alert.get("labels", {})
            alertname = labels.get("alertname", "UnknownAlert")
            instance = labels.get("instance", labels.get("job", "unknown"))
            severity = labels.get("severity", "warning")
            priority = "high" if severity == "critical" else "medium"

            tasks.append(
                self._make_task(
                    title=f"Investigate alert: {alertname} on {instance}",
                    description=(
                        f"Prometheus alert firing — severity={severity}, "
                        f"labels={labels}"
                    ),
                    project="",
                    priority=priority,
                    requires=[],
                    source="prometheus",
                )
            )
        return tasks

    # -----------------------------------------------------------------------
    # 3. Git change scanner
    # -----------------------------------------------------------------------

    def scan_git_changes(self) -> list[dict]:
        """Detect code changes and create corresponding tasks."""
        tasks: list[dict] = []
        scan_state = load_scan_state(self.swarm_root)
        updated_state = dict(scan_state)

        for project_name, project_cfg in self.projects.items():
            project_path = Path(project_cfg.get("path", f"/opt/{project_name}"))
            if not (project_path / ".git").is_dir():
                continue

            last_commit = scan_state.get(project_name, {}).get("last_commit", "")
            current_commit = self._get_head_commit(project_path)
            if current_commit is None:
                continue

            if current_commit == last_commit:
                # No new commits — check for uncommitted changes
                if self._has_uncommitted_changes(project_path):
                    tasks.append(
                        self._make_task(
                            title=f"Run Kin commit on {project_name} (uncommitted changes)",
                            description=f"Uncommitted changes detected in {project_path}",
                            project=str(project_path),
                            priority="low",
                            requires=[],
                            source="git_scan",
                        )
                    )
                continue

            # New commits since last scan
            changed_files = self._get_changed_files(project_path, last_commit)

            # If tests exist, create a test task
            if self._has_tests(project_path):
                tasks.append(
                    self._make_task(
                        title=f"Run tests on {project_name}",
                        description=(
                            f"New commits in {project_name} since {last_commit[:8] if last_commit else 'initial'}"
                        ),
                        project=str(project_path),
                        priority="medium",
                        requires=[],
                        source="git_scan",
                    )
                )

            # If CLAUDE.md changed, flag for review
            if any("CLAUDE.md" in f for f in changed_files):
                tasks.append(
                    self._make_task(
                        title=f"Review CLAUDE.md changes in {project_name}",
                        description="CLAUDE.md was modified in a recent commit",
                        project=str(project_path),
                        priority="low",
                        requires=[],
                        source="git_scan",
                    )
                )

            # Update scan state
            updated_state.setdefault(project_name, {})["last_commit"] = current_commit

        save_scan_state(self.swarm_root, updated_state)
        return tasks

    def _get_head_commit(self, project_path: Path) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(project_path),
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except OSError:
            pass
        return None

    def _has_uncommitted_changes(self, project_path: Path) -> bool:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=str(project_path),
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except OSError:
            return False

    def _get_changed_files(self, project_path: Path, since_commit: str) -> list[str]:
        if not since_commit:
            return []
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", since_commit, "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(project_path),
            )
            if result.returncode == 0:
                return [l.strip() for l in result.stdout.splitlines() if l.strip()]
        except OSError:
            pass
        return []

    def _has_tests(self, project_path: Path) -> bool:
        """Check whether the project has a tests directory or test files."""
        for candidate in ["tests", "test"]:
            if (project_path / candidate).is_dir():
                return True
        return bool(
            list(project_path.glob("test_*.py")) + list(project_path.glob("*_test.py"))
        )

    # -----------------------------------------------------------------------
    # 4. ExamForge pipeline scanner
    # -----------------------------------------------------------------------

    def scan_examforge_pipeline(self) -> list[dict]:
        """Check ExamForge question pipeline and create tasks."""
        tasks: list[dict] = []
        examforge_cfg = self.projects.get("examforge", {})
        if not examforge_cfg:
            return tasks

        base = Path(examforge_cfg.get("path", "/opt/examforge"))
        if not base.is_dir():
            return tasks

        # Count unvalidated draft questions
        seed_dir = base / "seed"
        draft_count = 0
        if seed_dir.is_dir():
            for drafts_dir in seed_dir.glob("*-drafts"):
                if drafts_dir.is_dir():
                    draft_count += len(
                        list(drafts_dir.glob("*.json"))
                        + list(drafts_dir.glob("*.yaml"))
                    )

        if draft_count > 0:
            tasks.append(
                self._make_task(
                    title=f"Run QA validation on {draft_count} draft questions",
                    description=f"ExamForge: {draft_count} draft questions need QA validation",
                    project=str(base),
                    priority="medium",
                    requires=[],
                    source="examforge",
                )
            )

        # Check for approved questions not yet imported to DB
        approved_dir = base / "seed" / "approved"
        if approved_dir.is_dir():
            approved_files = list(approved_dir.glob("*.json")) + list(
                approved_dir.glob("*.yaml")
            )
            if approved_files:
                tasks.append(
                    self._make_task(
                        title=f"Import {len(approved_files)} approved questions to DB",
                        description="ExamForge: approved questions awaiting DB import",
                        project=str(base),
                        priority="medium",
                        requires=[],
                        source="examforge",
                    )
                )

        # Daily: generate more questions for the weakest section
        weakest = self._find_weakest_examforge_section(base)
        if weakest:
            tasks.append(
                self._make_task(
                    title=f"Generate 10 more questions for {weakest}",
                    description=f"ExamForge daily generation: section {weakest} has fewest questions",
                    project=str(base),
                    priority="low",
                    requires=[],
                    source="examforge_daily",
                )
            )

        return tasks

    def _find_weakest_examforge_section(self, base: Path) -> Optional[str]:
        """Find the CPA section with the fewest approved questions."""
        sections = ["FAR", "AUD", "REG", "BAR"]
        approved_dir = base / "seed" / "approved"
        if not approved_dir.is_dir():
            return sections[0]  # Default to first section

        counts: dict[str, int] = {}
        for section in sections:
            section_dir = approved_dir / section
            if section_dir.is_dir():
                counts[section] = len(
                    list(section_dir.glob("*.json")) + list(section_dir.glob("*.yaml"))
                )
            else:
                counts[section] = 0

        return min(counts, key=lambda s: counts[s])

    # -----------------------------------------------------------------------
    # 5. Scheduled maintenance scanner
    # -----------------------------------------------------------------------

    def scan_scheduled_maintenance(self) -> list[dict]:
        """Create periodic maintenance tasks based on schedule."""
        tasks: list[dict] = []
        scan_state = load_scan_state(self.swarm_root)
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        this_week_str = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"

        daily_run = scan_state.get("maintenance", {}).get("last_daily", "")
        weekly_run = scan_state.get("maintenance", {}).get("last_weekly", "")

        # Daily tasks — run once per day at or after daily_hour
        if now.hour >= self.daily_hour and daily_run != today_str:
            tasks.extend(
                [
                    self._make_task(
                        title="Run security scan on all repos",
                        description="Daily: supply-chain-risk-auditor on all Hydra repos",
                        project="",
                        priority="medium",
                        requires=[],
                        source="scheduled_daily",
                    ),
                    self._make_task(
                        title="Check for package updates on node_primary and node_gpu",
                        description="Daily: apt/pip update check across fleet",
                        project="",
                        priority="low",
                        requires=[],
                        source="scheduled_daily",
                    ),
                    self._make_task(
                        title="Sync claude-config between hosts",
                        description="Daily: ensure claude-config repo is current on all hosts",
                        project="/opt/claude-configs/claude-config",
                        priority="low",
                        requires=[],
                        source="scheduled_daily",
                    ),
                    self._make_task(
                        title="Run Kin commit on repos with uncommitted semantic changes",
                        description="Daily: kin commit sweep across all indexed repos",
                        project="",
                        priority="low",
                        requires=[],
                        source="scheduled_daily",
                    ),
                ]
            )
            # Record that daily tasks were generated today
            scan_state.setdefault("maintenance", {})["last_daily"] = today_str
            save_scan_state(self.swarm_root, scan_state)

        # Weekly tasks — run once per week on weekly_day
        if now.weekday() == self.weekly_day and weekly_run != this_week_str:
            tasks.extend(
                [
                    self._make_task(
                        title="Full security audit of node_gpu",
                        description="Weekly: comprehensive security audit of primary GPU host",
                        project="",
                        priority="high",
                        requires=["gpu"],
                        source="scheduled_weekly",
                    ),
                    self._make_task(
                        title="Review and clean up completed swarm tasks",
                        description="Weekly: archive completed tasks older than 7 days",
                        project="",
                        priority="low",
                        requires=[],
                        source="scheduled_weekly",
                    ),
                    self._make_task(
                        title="Update skill freshness (token-miser pricing, versions)",
                        description="Weekly: verify token-miser model pricing and skill versions",
                        project="/opt/claude-configs/claude-config",
                        priority="medium",
                        requires=[],
                        source="scheduled_weekly",
                    ),
                ]
            )
            scan_state.setdefault("maintenance", {})["last_weekly"] = this_week_str
            save_scan_state(self.swarm_root, scan_state)

        return tasks

    # -----------------------------------------------------------------------
    # 6. Deduplication
    # -----------------------------------------------------------------------

    def deduplicate(self, proposed: list[dict]) -> list[dict]:
        """Remove tasks whose titles already exist in pending or claimed."""
        existing_titles: set[str] = set()
        for stage in ("pending", "claimed"):
            stage_dir = self.swarm_root / "tasks" / stage
            if not stage_dir.is_dir():
                continue
            for f in stage_dir.glob("*.yaml"):
                try:
                    with open(f) as fh:
                        data = yaml.safe_load(fh) or {}
                    existing_titles.add(data.get("title", ""))
                except (yaml.YAMLError, OSError):
                    continue

        seen_titles: set[str] = set()
        unique: list[dict] = []
        for task in proposed:
            title = task.get("title", "")
            if title in existing_titles or title in seen_titles:
                continue
            seen_titles.add(title)
            unique.append(task)
        return unique

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _make_task(
        self,
        title: str,
        description: str,
        project: str,
        priority: str,
        requires: list[str],
        source: str,
    ) -> dict:
        return {
            "title": title,
            "description": description,
            "project": project,
            "priority": priority,
            "requires": requires,
            "source": source,
            "suggested_model": infer_model(title + " " + description),
        }
