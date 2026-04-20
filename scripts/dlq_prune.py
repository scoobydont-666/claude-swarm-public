#!/usr/bin/env python3
"""S3-11 DLQ prune CLI.

Invokes `swarm.ipc.dlq.prune_old_messages(hours)` to drop DLQ entries older than
the configured retention window. Designed for periodic invocation via CronJob
or host crontab.

Exit codes: 0 on success (even when 0 entries pruned), 1 on Redis / transport
failure. stdout emits a single-line JSON record for log aggregation.

Usage:
    python3 scripts/dlq_prune.py                   # default 72h retention
    python3 scripts/dlq_prune.py --hours 48        # 48h retention
    python3 scripts/dlq_prune.py --dry-run         # print would-be count, don't delete

Environment:
    REDIS_URL  redis://[password@]host:port/db (default from transport config)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=72, help="retention window (default 72h)")
    parser.add_argument("--dry-run", action="store_true", help="report count, do not delete")
    args = parser.parse_args()

    started = time.time()
    try:
        if args.dry_run:
            from ipc import dlq

            depth = dlq.dlq_depth()
            record = {
                "event": "dlq_prune_dry_run",
                "hours": args.hours,
                "dlq_depth": depth,
                "elapsed_ms": int((time.time() - started) * 1000),
            }
            print(json.dumps(record))
            return 0

        from ipc.dlq import prune_old_messages

        removed = prune_old_messages(hours=args.hours)
        record = {
            "event": "dlq_prune",
            "hours": args.hours,
            "removed": removed,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        print(json.dumps(record))
        return 0
    except Exception as exc:
        record = {
            "event": "dlq_prune_error",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        print(json.dumps(record), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
