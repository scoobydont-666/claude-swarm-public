"""Tests for collaborative.py — verify locking prevents lost updates."""

import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from collaborative import Blocker, read_blockers, resolve_blocker, write_blocker


class TestBlockerLocking:
    """Verify write_blocker and resolve_blocker use file locking."""

    def test_write_blocker_creates_file(self, tmp_path):
        with patch("collaborative.COLLAB_ROOT", tmp_path):
            b = Blocker(blocker_id="b1", description="stuck", reported_at="2026-04-02T00:00:00Z")
            write_blocker("sess-1", b)

            blockers = read_blockers("sess-1")
            assert len(blockers) == 1
            assert blockers[0]["blocker_id"] == "b1"

    def test_multiple_blockers_append(self, tmp_path):
        with patch("collaborative.COLLAB_ROOT", tmp_path):
            write_blocker(
                "sess-2",
                Blocker(
                    blocker_id="b1",
                    description="first",
                    reported_at="2026-04-02T00:00:00Z",
                ),
            )
            write_blocker(
                "sess-2",
                Blocker(
                    blocker_id="b2",
                    description="second",
                    reported_at="2026-04-02T00:00:00Z",
                ),
            )

            blockers = read_blockers("sess-2")
            assert len(blockers) == 2

    def test_resolve_blocker_marks_resolved(self, tmp_path):
        with patch("collaborative.COLLAB_ROOT", tmp_path):
            write_blocker(
                "sess-3",
                Blocker(
                    blocker_id="b1",
                    description="issue",
                    reported_at="2026-04-02T00:00:00Z",
                ),
            )
            resolve_blocker("sess-3", "b1", {"fix": "done"})

            blockers = read_blockers("sess-3")
            assert blockers[0]["resolved"] is True
            assert blockers[0]["resolution"]["fix"] == "done"

    def test_concurrent_writes_no_lost_updates(self, tmp_path):
        """Two threads writing blockers simultaneously — both should persist."""
        with patch("collaborative.COLLAB_ROOT", tmp_path):
            errors = []

            def _write(bid):
                try:
                    write_blocker(
                        "sess-race",
                        Blocker(
                            blocker_id=bid,
                            description=f"blocker-{bid}",
                            reported_at="2026-04-02T00:00:00Z",
                        ),
                    )
                except Exception as e:
                    errors.append(e)

            t1 = threading.Thread(target=_write, args=("race-1",))
            t2 = threading.Thread(target=_write, args=("race-2",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert not errors
            blockers = read_blockers("sess-race")
            assert len(blockers) == 2
            ids = {b["blocker_id"] for b in blockers}
            assert ids == {"race-1", "race-2"}

    def test_read_empty_session(self, tmp_path):
        with patch("collaborative.COLLAB_ROOT", tmp_path):
            assert read_blockers("nonexistent") == []
