"""S3-11 CLI wrapper tests for scripts/dlq_prune.py.

Covers: (1) happy path — prune function called, JSON record emitted, exit 0.
        (2) dry-run — depth read, no prune.
        (3) transport error — stderr JSON, exit 1.
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))  # so `import scripts.dlq_prune` works


@pytest.fixture
def cli_module():
    import importlib

    import scripts.dlq_prune as mod

    importlib.reload(mod)
    return mod


def _run(cli, argv: list[str]) -> tuple[int, str, str]:
    saved = sys.argv[:]
    sys.argv = ["dlq_prune.py", *argv]
    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main()
    finally:
        sys.argv = saved
    return rc, out.getvalue(), err.getvalue()


def test_cli_dry_run_emits_depth(cli_module):
    with patch("ipc.dlq.dlq_depth", return_value=42):
        rc, out, err = _run(cli_module, ["--dry-run"])
    assert rc == 0, f"stderr={err!r}"
    record = json.loads(out.strip().splitlines()[-1])
    assert record["event"] == "dlq_prune_dry_run"
    assert record["dlq_depth"] == 42
    assert record["hours"] == 72


def test_cli_happy_path_emits_removed_count(cli_module):
    with patch("ipc.dlq.prune_old_messages", return_value=17):
        rc, out, err = _run(cli_module, ["--hours", "48"])
    assert rc == 0, f"stderr={err!r}"
    record = json.loads(out.strip().splitlines()[-1])
    assert record["event"] == "dlq_prune"
    assert record["removed"] == 17
    assert record["hours"] == 48


def test_cli_transport_error_nonzero_exit(cli_module):
    def boom(*_a, **_kw):
        raise ConnectionError("redis down")

    with patch("ipc.dlq.prune_old_messages", side_effect=boom):
        rc, out, err = _run(cli_module, [])
    assert rc == 1
    record = json.loads(err.strip().splitlines()[-1])
    assert record["event"] == "dlq_prune_error"
    assert "ConnectionError" in record["error"]
