"""Tests for crash_handler.py — signal handling and task requeue."""

import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crash_handler import (
    _crash_callbacks,
    _session_info,
    install_crash_handlers,
    register_crash_callback,
    set_session_info,
)


class TestInstallCrashHandlers:
    """Verify signal handler registration."""

    def test_installs_signal_handlers(self):
        with patch("signal.signal") as mock_signal:
            with patch("atexit.register"):
                install_crash_handlers()

            calls = [c[0][0] for c in mock_signal.call_args_list]
            assert signal.SIGTERM in calls
            assert signal.SIGINT in calls
            assert signal.SIGHUP in calls

    def test_registers_atexit(self):
        with patch("signal.signal"):
            with patch("atexit.register") as mock_atexit:
                install_crash_handlers()
            assert mock_atexit.called


class TestCrashCallbacks:
    def setup_method(self):
        _crash_callbacks.clear()

    def test_register_callback(self):
        cb = MagicMock()
        register_crash_callback(cb)
        assert cb in _crash_callbacks

    def test_callbacks_stored_in_order(self):
        cb1, cb2 = MagicMock(), MagicMock()
        register_crash_callback(cb1)
        register_crash_callback(cb2)
        assert _crash_callbacks == [cb1, cb2]


class TestSessionInfo:
    def test_set_session_info(self):
        set_session_info({"hostname": "test", "pid": 123})
        assert _session_info["hostname"] == "test"
        assert _session_info["pid"] == 123
