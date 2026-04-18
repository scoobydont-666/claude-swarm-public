"""Tests for GPU slot management — claiming, releasing, and status."""

import os
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gpu_slots import (
    _load_queue,
    _parse_slot_info,
    _queue_key,
    _save_queue,
    claim_slot,
    claim_slot_with_deadline,
    get_queue_position,
    get_slot_status,
    heartbeat_slot,
    is_slot_available,
    is_slot_expired,
    is_slot_stale,
    release_slot,
    release_stale_slots,
    setup_ollama_slot,
    wait_for_slot,
)


@pytest.fixture
def gpu_tmpdir(tmp_path):
    """Create a temporary GPU slot directory."""
    gpu_dir = tmp_path / "gpu"
    gpu_dir.mkdir(parents=True, exist_ok=True)

    with patch("gpu_slots._gpu_dir", return_value=gpu_dir):
        yield gpu_dir


class TestGPUSlotClaiming:
    def test_claim_slot_success(self, gpu_tmpdir):
        """Test claiming an available slot."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert claim_slot(0)
            lock_file = gpu_tmpdir / "slot-0.lock"
            assert lock_file.exists()
            content = lock_file.read_text().strip()
            assert ":" in content  # hostname:pid:timestamp format
            assert str(os.getpid()) in content

    def test_claim_slot_twice_fails(self, gpu_tmpdir):
        """Test that claiming the same slot twice fails."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert claim_slot(0)
            assert not claim_slot(0)  # Second claim should fail

    def test_claim_multiple_slots(self, gpu_tmpdir):
        """Test claiming multiple different slots."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert claim_slot(0)
            assert claim_slot(1)
            assert claim_slot(2)
            assert (gpu_tmpdir / "slot-0.lock").exists()
            assert (gpu_tmpdir / "slot-1.lock").exists()
            assert (gpu_tmpdir / "slot-2.lock").exists()

    def test_release_slot(self, gpu_tmpdir):
        """Test releasing a claimed slot."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert claim_slot(0)
            assert release_slot(0)
            # After release, should be available again
            assert claim_slot(0)

    def test_release_unclaimed_slot_fails(self, gpu_tmpdir):
        """Test releasing a slot that was never claimed."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert not release_slot(0)


class TestGPUSlotAvailability:
    def test_slot_available_when_unclaimed(self, gpu_tmpdir):
        """Test that unclaimed slots are available."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert is_slot_available(0)
            assert is_slot_available(1)

    def test_slot_unavailable_when_claimed(self, gpu_tmpdir):
        """Test that claimed slots are unavailable."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            claim_slot(0)
            assert not is_slot_available(0)

    def test_slot_available_after_release(self, gpu_tmpdir):
        """Test that released slots become available again."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            claim_slot(0)
            release_slot(0)
            assert is_slot_available(0)


class TestGPUSlotStatus:
    def test_get_status_empty(self, gpu_tmpdir):
        """Test getting status with no claimed slots."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            status = get_slot_status()
            assert len(status) >= 2  # At least GPU 0 and 1
            for slot in status:
                assert not slot["claimed"]
                assert slot["holder"] == ""

    def test_get_status_with_claimed_slot(self, gpu_tmpdir):
        """Test getting status with a claimed slot."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            claim_slot(0)
            status = get_slot_status()
            slot_0 = next((s for s in status if s["gpu_id"] == 0), None)
            assert slot_0 is not None
            assert slot_0["claimed"]
            assert str(os.getpid()) in slot_0["holder"]

    def test_status_sorted_by_gpu_id(self, gpu_tmpdir):
        """Test that status list is sorted by GPU ID."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            claim_slot(2)
            claim_slot(0)
            claim_slot(1)
            status = get_slot_status()
            gpu_ids = [s["gpu_id"] for s in status]
            assert gpu_ids == sorted(gpu_ids)


class TestOllamaPermanentSlot:
    def test_setup_ollama_slot(self, gpu_tmpdir):
        """Test setting up permanent Ollama slot on GPU 0."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert setup_ollama_slot()
            assert not is_slot_available(0)
            status = get_slot_status()
            slot_0 = next((s for s in status if s["gpu_id"] == 0), None)
            assert slot_0["claimed"]


class TestSlotOperations:
    def test_claim_release_sequence(self, gpu_tmpdir):
        """Test a sequence of claim/release operations."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            # Claim slots 0 and 1
            assert claim_slot(0)
            assert claim_slot(1)
            assert not is_slot_available(0)
            assert not is_slot_available(1)

            # Release slot 0
            assert release_slot(0)
            assert is_slot_available(0)
            assert not is_slot_available(1)

            # Reclaim slot 0
            assert claim_slot(0)
            assert not is_slot_available(0)

            # Release both
            assert release_slot(0)
            assert release_slot(1)
            assert is_slot_available(0)
            assert is_slot_available(1)

    def test_concurrent_access_simulation(self, gpu_tmpdir):
        """Test simulated concurrent access to slots."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            # Simulate two processes trying to claim the same slot
            assert claim_slot(0)
            # Second "process" (simulated by another claim) should fail
            assert not claim_slot(0)
            # Release and let second "process" claim
            assert release_slot(0)
            assert claim_slot(0)


class TestWaitForSlot:
    def test_wait_for_slot_available_immediately(self, gpu_tmpdir):
        """wait_for_slot claims immediately when slot is free."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            result = wait_for_slot(gpu_id=0, timeout_seconds=5, poll_interval=1)
            assert result is True
            assert not is_slot_available(0)
            # Clean up
            release_slot(0)

    def test_wait_for_slot_timeout(self, gpu_tmpdir):
        """wait_for_slot returns False when slot never becomes free within timeout."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            # Claim the slot as a "foreign" host so it won't be reclaimed by PID check
            lock_path = gpu_tmpdir / "slot-0.lock"
            lock_path.write_text("otherhost:99999:2026-01-01T00:00:00Z\n")

            result = wait_for_slot(gpu_id=0, timeout_seconds=2, poll_interval=1)
            assert result is False

            # Queue should be cleaned up after timeout
            queue = _load_queue(0)
            pid = os.getpid()
            assert not any(e["pid"] == pid for e in queue)

    def test_wait_for_slot_removes_self_from_queue_on_success(self, gpu_tmpdir):
        """Queue entry is removed after successful claim."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            result = wait_for_slot(gpu_id=0, timeout_seconds=5, poll_interval=1)
            assert result is True
            queue = _load_queue(0)
            pid = os.getpid()
            assert not any(e["pid"] == pid for e in queue)
            release_slot(0)


class TestQueuePriority:
    def test_queue_sort_key_priority_order(self):
        """Lower priority number sorts first (higher urgency)."""
        entries = [
            {
                "hostname": "h1",
                "pid": 1,
                "priority": 9,
                "requested_at": "2026-01-01T00:00:00Z",
            },
            {
                "hostname": "h2",
                "pid": 2,
                "priority": 1,
                "requested_at": "2026-01-01T00:00:01Z",
            },
            {
                "hostname": "h3",
                "pid": 3,
                "priority": 5,
                "requested_at": "2026-01-01T00:00:02Z",
            },
        ]
        entries.sort(key=_queue_key)
        assert entries[0]["priority"] == 1
        assert entries[1]["priority"] == 5
        assert entries[2]["priority"] == 9

    def test_queue_sort_key_fifo_within_same_priority(self):
        """Within same priority, earlier requested_at comes first."""
        entries = [
            {
                "hostname": "h1",
                "pid": 1,
                "priority": 5,
                "requested_at": "2026-01-01T00:00:05Z",
            },
            {
                "hostname": "h2",
                "pid": 2,
                "priority": 5,
                "requested_at": "2026-01-01T00:00:01Z",
            },
            {
                "hostname": "h3",
                "pid": 3,
                "priority": 5,
                "requested_at": "2026-01-01T00:00:03Z",
            },
        ]
        entries.sort(key=_queue_key)
        assert entries[0]["requested_at"] == "2026-01-01T00:00:01Z"
        assert entries[1]["requested_at"] == "2026-01-01T00:00:03Z"
        assert entries[2]["requested_at"] == "2026-01-01T00:00:05Z"

    def test_get_queue_position_not_in_queue(self, gpu_tmpdir):
        """Returns 0 when this process is not in queue."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            pos = get_queue_position(0)
            assert pos == 0

    def test_get_queue_position_head_of_queue(self, gpu_tmpdir):
        """Returns 1 when this process is at head of queue."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            hostname = socket.gethostname()
            pid = os.getpid()
            queue = [
                {
                    "hostname": hostname,
                    "pid": pid,
                    "priority": 1,
                    "requested_at": "2026-01-01T00:00:00Z",
                },
                {
                    "hostname": "other",
                    "pid": 99998,
                    "priority": 1,
                    "requested_at": "2026-01-01T00:00:01Z",
                },
            ]
            _save_queue(0, queue)
            pos = get_queue_position(0)
            assert pos == 1

    def test_get_queue_position_second_in_queue(self, gpu_tmpdir):
        """Returns 2 when another entry has higher priority."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            hostname = socket.gethostname()
            pid = os.getpid()
            # other host's entry has lower priority number = higher precedence
            queue = [
                {
                    "hostname": "other",
                    "pid": 99998,
                    "priority": 1,
                    "requested_at": "2026-01-01T00:00:00Z",
                },
                {
                    "hostname": hostname,
                    "pid": pid,
                    "priority": 5,
                    "requested_at": "2026-01-01T00:00:01Z",
                },
            ]
            _save_queue(0, queue)
            pos = get_queue_position(0)
            assert pos == 2

    def test_stale_pids_cleaned_from_queue(self, gpu_tmpdir):
        """Dead PIDs are removed from queue during clean_queue."""
        import gpu_slots

        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            hostname = socket.gethostname()
            # PID 1 is always alive; use a definitely-dead PID (very high number)
            dead_pid = 2147483647  # max int32, almost certainly not a real PID
            queue = [
                {
                    "hostname": hostname,
                    "pid": dead_pid,
                    "priority": 1,
                    "requested_at": "2026-01-01T00:00:00Z",
                },
                {
                    "hostname": hostname,
                    "pid": os.getpid(),
                    "priority": 5,
                    "requested_at": "2026-01-01T00:00:01Z",
                },
            ]
            _save_queue(0, queue)
            cleaned = gpu_slots._clean_queue(0)
            pids = [e["pid"] for e in cleaned]
            assert dead_pid not in pids
            assert os.getpid() in pids


class TestParseSlotInfo:
    def test_parse_full_format(self):
        """Parse new pipe-delimited lockfile content."""
        info = _parse_slot_info("myhost:1234|2026-01-01T00:00:00Z|2026-01-01T00:05:00Z|1735689600")
        assert info["hostname"] == "myhost"
        assert info["pid"] == 1234
        assert info["claimed_at"] == "2026-01-01T00:00:00Z"
        assert info["heartbeat"] == "2026-01-01T00:05:00Z"
        assert info["deadline_ts"] == 1735689600

    def test_parse_legacy_3_field_format(self):
        """Parse old colon-delimited lockfile content (backward compat)."""
        info = _parse_slot_info("myhost:1234:2026-01-01T00:00:00Z")
        assert info["hostname"] == "myhost"
        assert info["pid"] == 1234
        assert info["claimed_at"] == "2026-01-01T00:00:00Z"
        assert info["heartbeat"] == ""
        assert info["deadline_ts"] == 0

    def test_parse_empty(self):
        """Empty content returns empty dict."""
        assert _parse_slot_info("") == {}
        assert _parse_slot_info("   ") == {}

    def test_parse_single_field(self):
        """Single field returns empty dict (need at least hostname:pid)."""
        assert _parse_slot_info("myhost") == {}


class TestHeartbeat:
    def test_heartbeat_updates_timestamp(self, gpu_tmpdir):
        """Heartbeat updates the heartbeat field in lockfile."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert claim_slot(0)
            import time as _time

            _time.sleep(0.05)  # Ensure time advances
            assert heartbeat_slot(0)
            content = (gpu_tmpdir / "slot-0.lock").read_text().strip()
            info = _parse_slot_info(content)
            assert info["heartbeat"] != ""
            # Heartbeat should be >= claimed_at
            assert info["heartbeat"] >= info["claimed_at"]

    def test_heartbeat_fails_on_unclaimed_slot(self, gpu_tmpdir):
        """Heartbeat on unclaimed slot returns False."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert not heartbeat_slot(0)

    def test_heartbeat_fails_for_different_host(self, gpu_tmpdir):
        """Heartbeat fails if slot is held by a different host."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            lock_path = gpu_tmpdir / "slot-0.lock"
            lock_path.write_text(
                "otherhost:1234|2026-01-01T00:00:00Z|2026-01-01T00:00:00Z|9999999999\n"
            )
            assert not heartbeat_slot(0)


class TestSlotStaleness:
    def test_fresh_slot_not_stale(self, gpu_tmpdir):
        """Freshly claimed slot is not stale."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert claim_slot(0)
            assert not is_slot_stale(0)

    def test_unclaimed_slot_not_stale(self, gpu_tmpdir):
        """Unclaimed slot returns False for staleness check."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert not is_slot_stale(0)

    def test_old_heartbeat_is_stale(self, gpu_tmpdir):
        """Slot with heartbeat older than threshold is stale."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            lock_path = gpu_tmpdir / "slot-0.lock"
            # Write a heartbeat from 10 minutes ago
            lock_path.write_text(
                "otherhost:1234|2026-01-01T00:00:00Z|2020-01-01T00:00:00Z|9999999999\n"
            )
            assert is_slot_stale(0, stale_threshold=60)

    def test_dead_pid_is_stale(self, gpu_tmpdir):
        """Slot held by dead PID on same host is stale."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            hostname = socket.gethostname()
            dead_pid = 2147483647
            lock_path = gpu_tmpdir / "slot-0.lock"
            lock_path.write_text(
                f"{hostname}:{dead_pid}|2026-01-01T00:00:00Z|2026-01-01T00:00:00Z|9999999999\n"
            )
            assert is_slot_stale(0)


class TestSlotExpiry:
    def test_fresh_slot_not_expired(self, gpu_tmpdir):
        """Freshly claimed slot is not expired."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert claim_slot(0)
            assert not is_slot_expired(0)

    def test_past_deadline_is_expired(self, gpu_tmpdir):
        """Slot with deadline in the past is expired."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            lock_path = gpu_tmpdir / "slot-0.lock"
            lock_path.write_text(
                "otherhost:1234|2026-01-01T00:00:00Z|2026-01-01T00:00:00Z|1000000000\n"
            )
            assert is_slot_expired(0)

    def test_future_deadline_not_expired(self, gpu_tmpdir):
        """Slot with deadline in the future is not expired."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            lock_path = gpu_tmpdir / "slot-0.lock"
            import time as _time

            future_ts = str(int(_time.time()) + 86400)
            lock_path.write_text(
                f"otherhost:1234|2026-01-01T00:00:00Z|2026-01-01T00:00:00Z|{future_ts}\n"
            )
            assert not is_slot_expired(0)

    def test_unclaimed_slot_not_expired(self, gpu_tmpdir):
        """Unclaimed slot returns False for expiry check."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert not is_slot_expired(0)


class TestReleaseStaleSlots:
    def test_releases_stale_slot(self, gpu_tmpdir):
        """Stale slots get released."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            lock_path = gpu_tmpdir / "slot-0.lock"
            lock_path.write_text(
                "otherhost:1234|2026-01-01T00:00:00Z|2020-01-01T00:00:00Z|9999999999\n"
            )
            released = release_stale_slots(stale_threshold=60)
            assert 0 in released
            assert is_slot_available(0)

    def test_releases_expired_slot(self, gpu_tmpdir):
        """Expired slots get released."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            lock_path = gpu_tmpdir / "slot-0.lock"
            lock_path.write_text(
                "otherhost:1234|2026-01-01T00:00:00Z|2026-04-11T00:00:00Z|1000000000\n"
            )
            released = release_stale_slots()
            assert 0 in released

    def test_skips_healthy_slot(self, gpu_tmpdir):
        """Healthy slots are not released."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert claim_slot(0)
            released = release_stale_slots()
            assert 0 not in released
            assert not is_slot_available(0)

    def test_empty_dir_returns_empty(self, gpu_tmpdir):
        """No slots to release returns empty list."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert release_stale_slots() == []


class TestClaimSlotWithDeadline:
    def test_claim_with_custom_deadline(self, gpu_tmpdir):
        """Claim with custom deadline writes correct deadline_ts."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            import time as _time

            before = int(_time.time())
            assert claim_slot_with_deadline(0, deadline_seconds=120)
            content = (gpu_tmpdir / "slot-0.lock").read_text().strip()
            info = _parse_slot_info(content)
            assert info["deadline_ts"] >= before + 120
            assert info["deadline_ts"] <= before + 125  # small tolerance

    def test_claim_with_deadline_prevents_double_claim(self, gpu_tmpdir):
        """Second claim with deadline fails."""
        with patch("gpu_slots._gpu_dir", return_value=gpu_tmpdir):
            assert claim_slot_with_deadline(0)
            assert not claim_slot_with_deadline(0)
