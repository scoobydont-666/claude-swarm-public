"""Tests for pluggable Claude backend (CLAUDE_BACKEND=sdk|cli|auto)."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src import claude_backend


class TestCliBackend:
    """Test claude --print CLI backend."""

    def test_call_claude_cli_success(self):
        """CLI backend successfully returns response."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="CLI response",
                stderr="",
            )
            result = claude_backend._call_claude_cli("test prompt", model="sonnet")
            assert result == "CLI response"
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[0] == "claude"
            assert "--print" in args
            assert "--model" in args
            assert "sonnet" in args

    def test_call_claude_cli_with_system(self):
        """CLI backend includes system prompt via --append-system-prompt."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="response",
                stderr="",
            )
            result = claude_backend._call_claude_cli(
                "prompt",
                system="You are helpful",
            )
            assert result == "response"
            args = mock_run.call_args[0][0]
            assert "--append-system-prompt" in args
            assert "You are helpful" in args

    def test_call_claude_cli_not_found(self):
        """CLI backend raises FileNotFoundError if claude not on PATH."""
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(FileNotFoundError):
                claude_backend._call_claude_cli("prompt")

    def test_call_claude_cli_non_zero_exit(self):
        """CLI backend raises CalledProcessError on non-zero exit."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="error",
            )
            with pytest.raises(subprocess.CalledProcessError):
                claude_backend._call_claude_cli("prompt")

    def test_call_claude_cli_timeout(self):
        """CLI backend raises RuntimeError on timeout."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 180)):
            with pytest.raises(RuntimeError, match="timed out"):
                claude_backend._call_claude_cli("prompt")


class TestSdkBackend:
    """Test Anthropic SDK backend."""

    def test_call_claude_sdk_success(self):
        """SDK backend successfully returns response."""
        with patch("anthropic.Anthropic") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text="SDK response")]
            mock_client.messages.create.return_value = mock_response

            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                result = claude_backend._call_claude_sdk("prompt", model="sonnet")
                assert result == "SDK response"
                mock_client.messages.create.assert_called_once()

    def test_call_claude_sdk_with_system(self):
        """SDK backend includes system prompt."""
        with patch("anthropic.Anthropic") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text="response")]
            mock_client.messages.create.return_value = mock_response

            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                result = claude_backend._call_claude_sdk(
                    "prompt",
                    system="Be helpful",
                )
                assert result == "response"
                call_kwargs = mock_client.messages.create.call_args[1]
                assert call_kwargs["system"] is not None

    def test_call_claude_sdk_no_api_key(self):
        """SDK backend raises RuntimeError if ANTHROPIC_API_KEY not set."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
                claude_backend._call_claude_sdk("prompt")

    def test_call_claude_sdk_import_error(self):
        """SDK backend raises RuntimeError if anthropic not installed."""
        with patch("builtins.__import__", side_effect=ImportError()):
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}):
                with pytest.raises(RuntimeError, match="anthropic SDK not installed"):
                    claude_backend._call_claude_sdk("prompt")


class TestAutoBackend:
    """Test auto backend (CLI first, fallback to SDK)."""

    def test_auto_backend_uses_cli_when_available(self):
        """Auto backend uses CLI if available."""
        with patch.dict(os.environ, {"CLAUDE_BACKEND": "auto"}):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout="cli-response",
                    stderr="",
                )
                result = claude_backend.call_claude("prompt")
                assert result == "cli-response"

    def test_auto_backend_falls_back_to_sdk_on_cli_fail(self):
        """Auto backend falls back to SDK if CLI fails."""
        with patch.dict(os.environ, {"CLAUDE_BACKEND": "auto", "ANTHROPIC_API_KEY": "key"}):
            with patch("subprocess.run", side_effect=FileNotFoundError()):
                with patch("anthropic.Anthropic") as mock_client_class:
                    mock_client = MagicMock()
                    mock_client_class.return_value = mock_client
                    mock_response = MagicMock()
                    mock_response.content = [MagicMock(text="sdk-response")]
                    mock_client.messages.create.return_value = mock_response

                    result = claude_backend.call_claude("prompt")
                    assert result == "sdk-response"


class TestDispatch:
    """Test call_claude dispatcher."""

    def test_backend_sdk_explicit(self):
        """Explicit CLAUDE_BACKEND=sdk uses SDK."""
        with patch.dict(os.environ, {"CLAUDE_BACKEND": "sdk", "ANTHROPIC_API_KEY": "key"}):
            # Reload to pick up env var
            import importlib

            importlib.reload(claude_backend)
            with patch("anthropic.Anthropic") as mock_client_class:
                mock_client = MagicMock()
                mock_client_class.return_value = mock_client
                mock_response = MagicMock()
                mock_response.content = [MagicMock(text="response")]
                mock_client.messages.create.return_value = mock_response

                result = claude_backend.call_claude("prompt")
                assert result == "response"

    def test_backend_invalid(self):
        """Invalid CLAUDE_BACKEND raises ValueError."""
        with patch.dict(os.environ, {"CLAUDE_BACKEND": "invalid"}):
            import importlib

            importlib.reload(claude_backend)
            with pytest.raises(ValueError, match="Unknown CLAUDE_BACKEND"):
                claude_backend.call_claude("prompt")

    def test_default_backend_is_auto(self):
        """Default backend is auto."""
        with patch.dict(os.environ, {}, clear=True):
            import importlib

            importlib.reload(claude_backend)
            assert claude_backend._BACKEND == "auto"
