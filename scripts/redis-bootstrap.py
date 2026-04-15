#!/usr/bin/env python3
"""Bootstrap Redis from NFS state.

Reads all tasks, status files, and events from /var/lib/swarm/ (NFS)
and populates Redis so both backends have identical state.

Safe to re-run — uses idempotent writes (HSET, ZADD).

Usage:
    python3 /opt/claude-swarm/scripts/redis-bootstrap.py
    python3 /opt/claude-swarm/scripts/redis-bootstrap.py --dry-run
"""

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

# Add src to path for redis_client
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import redis_client as rc

SWARM_ROOT = Path("/var/lib/swarm")

PRIORITY_MAP = {
    "critical": 0,
    "high": 2,
    "medium": 5,
    "low": 7,
    "lowest": 9,
}


def iso_to_epoch(iso_str: str) -> float:
    """Convert ISO timestamp to epoch seconds."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return time.time()


def load_yaml(path: Path) -> dict:
    """Load a YAML file."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path) -> dict:
    """Load a JSON file."""
    with open(path) as f:
        return json.load(f)


def bootstrap_tasks(dry_run: bool = False) -> dict:
    """Load all tasks from NFS into Redis."""
    stats = {"pending": 0, "claimed": 0, "completed": 0, "skipped": 0}

    for stage in ("pending", "claimed", "completed"):
        task_dir = SWARM_ROOT / "tasks" / stage
        if not task_dir.exists():
            continue

        for f in sorted(task_dir.glob("*.yaml")):
            try:
                task = load_yaml(f)
            except Exception as e:
                print(f"  SKIP {f.name}: {e}")
                stats["skipped"] += 1
                continue

            task_id = task.get("id", f.stem)
            priority_str = task.get("priority", "medium")
            priority = PRIORITY_MAP.get(priority_str, 5)
            created_at = iso_to_epoch(task.get("created_at", ""))
            score = priority * 1000 + int(created_at)

            # Build hash data
            hash_data = {
                "id": task_id,
                "state": stage,
                "priority": str(priority),
                "created_at": str(created_at),
                "data": json.dumps({
                    k: v for k, v in task.items()
                    if k not in ("id", "priority", "created_at")
                }),
            }

            if stage == "claimed":
                hash_data["claimed_at"] = str(iso_to_epoch(task.get("claimed_at", "")))
                hash_data["claimed_by"] = task.get("claimed_by", "unknown")
            elif stage == "completed":
                hash_data["completed_at"] = str(iso_to_epoch(task.get("completed_at", "")))
                if task.get("completed_by"):
                    hash_data["claimed_by"] = task["completed_by"]

            if dry_run:
                print(f"  [DRY] {stage}: {task_id} (priority={priority}, score={score})")
            else:
                r = rc.get_client()
                r.hset(f"task:{task_id}", mapping=hash_data)
                r.zadd(f"tasks:{stage}", {task_id: score})

            stats[stage] += 1

    return stats


def bootstrap_status(dry_run: bool = False) -> int:
    """Load all node status files into Redis."""
    status_dir = SWARM_ROOT / "status"
    if not status_dir.exists():
        return 0

    count = 0
    for f in sorted(status_dir.glob("*.json")):
        try:
            status = load_json(f)
        except Exception as e:
            print(f"  SKIP {f.name}: {e}")
            continue

        hostname = status.get("hostname", f.stem)

        if dry_run:
            state = status.get("state", "unknown")
            print(f"  [DRY] status:{hostname} state={state}")
        else:
            r = rc.get_client()
            flat = {}
            for k, v in status.items():
                if isinstance(v, (dict, list)):
                    flat[k] = json.dumps(v)
                else:
                    flat[k] = str(v)
            r.hset(f"status:{hostname}", mapping=flat)
            # Don't set TTL on bootstrap — let heartbeats manage it
        count += 1

    return count


def bootstrap_events(dry_run: bool = False) -> int:
    """Load recent events into Redis stream."""
    events_dir = SWARM_ROOT / "events"
    if not events_dir.exists():
        return 0

    count = 0
    for f in sorted(events_dir.glob("*.jsonl")):
        try:
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    if dry_run:
                        etype = event.get("type", "unknown")
                        print(f"  [DRY] event: {etype}")
                    else:
                        fields = {
                            "type": event.get("type", "unknown"),
                            "timestamp": str(event.get("timestamp", time.time())),
                            "data": json.dumps(event.get("data", event)),
                        }
                        rc.get_client().xadd("events", fields)
                    count += 1
        except Exception as e:
            print(f"  SKIP {f.name}: {e}")

    # Also check for single events.jsonl
    single = SWARM_ROOT / "events.jsonl"
    if single.exists():
        try:
            with open(single) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    if not dry_run:
                        fields = {
                            "type": event.get("type", "unknown"),
                            "timestamp": str(event.get("timestamp", time.time())),
                            "data": json.dumps(event.get("data", event)),
                        }
                        rc.get_client().xadd("events", fields)
                    count += 1
        except Exception:
            pass

    return count


def verify(expected_tasks: dict) -> bool:
    """Verify Redis state matches expectations."""
    r = rc.get_client()
    ok = True

    for stage in ("pending", "claimed", "completed"):
        redis_count = r.zcard(f"tasks:{stage}")
        nfs_count = expected_tasks.get(stage, 0)
        match = "OK" if redis_count == nfs_count else "MISMATCH"
        if match == "MISMATCH":
            ok = False
        print(f"  tasks:{stage} — Redis: {redis_count}, NFS: {nfs_count} [{match}]")

    status_keys = r.keys("status:*")
    nfs_status = len(list((SWARM_ROOT / "status").glob("*.json")))
    match = "OK" if len(status_keys) == nfs_status else "MISMATCH"
    if match == "MISMATCH":
        ok = False
    print(f"  status — Redis: {len(status_keys)}, NFS: {nfs_status} [{match}]")

    events_len = r.xlen("events")
    print(f"  events — Redis stream length: {events_len}")

    return ok


def main():
    parser = argparse.ArgumentParser(description="Bootstrap Redis from NFS swarm state")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--flush", action="store_true", help="Flush Redis before bootstrap")
    args = parser.parse_args()

    if not SWARM_ROOT.exists():
        print(f"ERROR: {SWARM_ROOT} not found. Mount NFS first.")
        sys.exit(1)

    # Verify Redis connectivity
    if not args.dry_run:
        if not rc.health_check():
            print("ERROR: Cannot connect to Redis.")
            sys.exit(1)
        print(f"Connected to Redis at {rc.REDIS_HOST}:{rc.REDIS_PORT}")

        if args.flush:
            rc.get_client().flushdb()
            print("Flushed Redis DB.")

    print("\n=== Bootstrapping tasks ===")
    task_stats = bootstrap_tasks(args.dry_run)
    for stage, count in task_stats.items():
        print(f"  {stage}: {count}")

    print("\n=== Bootstrapping node status ===")
    status_count = bootstrap_status(args.dry_run)
    print(f"  Loaded {status_count} node statuses")

    print("\n=== Bootstrapping events ===")
    event_count = bootstrap_events(args.dry_run)
    print(f"  Loaded {event_count} events")

    if not args.dry_run:
        print("\n=== Verification ===")
        ok = verify(task_stats)
        if ok:
            print("\nBootstrap complete — Redis matches NFS state.")
        else:
            print("\nWARNING: Mismatches detected. Check above.")
            sys.exit(1)
    else:
        print("\n[DRY RUN] No changes made.")


if __name__ == "__main__":
    main()
