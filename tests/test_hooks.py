"""Tests for swarm hooks — output format, stale detection, capability matching."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import swarm_lib as lib


class TestSessionStartOutput:
    """Verify session start produces valid systemMessage JSON."""

    def test_status_registered_on_start(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.update_status(state="active", session_id="test-123", model="opus")
            status = lib.get_status("testhost")
            assert status["state"] == "active"
            assert status["session_id"] == "test-123"

    def test_other_nodes_visible(self, swarm_tmpdir):
        """When other nodes exist, session start should report them."""
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"),
        ):
            # Write another node's status
            other_status = {
                "hostname": "node_gpu",
                "ip": "<primary-node-ip>",
                "state": "active",
                "current_task": "ProjectA dev",
                "model": "opus",
                "updated_at": lib._now_iso(),
            }
            path = swarm_tmpdir / "status" / "node_gpu.json"
            with open(path, "w") as f:
                json.dump(other_status, f)

            all_status = lib.get_all_status()
            other_nodes = [s for s in all_status if s["hostname"] != "testhost"]
            assert len(other_nodes) >= 1
            assert other_nodes[0]["hostname"] == "node_gpu"
            assert other_nodes[0]["current_task"] == "ProjectA dev"


class TestHeartbeatStaleDetection:
    def test_fresh_node_not_stale(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.update_status(state="active")
            stale = lib.mark_stale_nodes(threshold_seconds=300)
            assert "testhost" not in stale

    def test_old_node_marked_stale(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            old_status = {
                "hostname": "oldhost",
                "ip": "10.0.0.1",
                "state": "active",
                "updated_at": "2020-01-01T00:00:00Z",
            }
            path = swarm_tmpdir / "status" / "oldhost.json"
            with open(path, "w") as f:
                json.dump(old_status, f)

            stale = lib.mark_stale_nodes(threshold_seconds=300)
            assert "oldhost" in stale

    def test_offline_node_not_re_marked(self, swarm_tmpdir):
        with patch.object(lib, "_swarm_root", return_value=swarm_tmpdir):
            offline_status = {
                "hostname": "downhost",
                "ip": "10.0.0.2",
                "state": "offline",
                "updated_at": "2020-01-01T00:00:00Z",
            }
            path = swarm_tmpdir / "status" / "downhost.json"
            with open(path, "w") as f:
                json.dump(offline_status, f)

            stale = lib.mark_stale_nodes(threshold_seconds=300)
            assert "downhost" not in stale


class TestTaskCheckCapabilities:
    def test_matching_capabilities(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.update_status(state="active")

            # Task requiring docker+gpu (testhost has both)
            lib.create_task(title="Docker+GPU", requires=["docker", "gpu"])
            # Task requiring ollama (testhost doesn't have)
            lib.create_task(title="Needs ollama", requires=["ollama"])

            matching = lib.get_matching_tasks()
            titles = [t["title"] for t in matching]
            assert "Docker+GPU" in titles
            assert "Needs ollama" not in titles

    def test_no_requirements_always_matches(self, swarm_tmpdir):
        with (
            patch.object(lib, "_swarm_root", return_value=swarm_tmpdir),
            patch.object(lib, "_config_path", return_value=swarm_tmpdir / "config" / "swarm.yaml"),
            patch.object(lib, "_hostname", return_value="testhost"),
        ):
            lib.update_status(state="active")
            lib.create_task(title="Open task")

            matching = lib.get_matching_tasks()
            assert any(t["title"] == "Open task" for t in matching)
