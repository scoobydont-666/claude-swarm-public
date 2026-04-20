#!/usr/bin/env python3
"""
Pluggable Claude backend dispatcher (CLAUDE_BACKEND=sdk|cli|auto).

Supports three modes:
  - sdk: Anthropic SDK (API key required, ANTHROPIC_API_KEY env var)
  - cli: Claude Code CLI (OAuth-authenticated, `claude` binary required)
  - auto: Try CLI first, fall back to SDK if CLI not found or fails
"""

import os
import subprocess
import sys
from typing import Optional

_BACKEND = os.getenv("CLAUDE_BACKEND", "auto").lower()


def call_claude(
    prompt: str,
    model: str = "sonnet",
    system: str = "",
    max_tokens: int = 1024,
    **kwargs
) -> str:
    """
    Dispatch a Claude API call via configured backend.

    Args:
        prompt: User message
        model: Model ID (e.g., "sonnet", "opus", "haiku")
        system: System prompt
        max_tokens: Max response tokens
        **kwargs: Additional arguments (ignored for compatibility)

    Returns:
        Response text

    Raises:
        RuntimeError: If all backends fail or backend is misconfigured
    """
    if _BACKEND == "cli":
        return _call_claude_cli(prompt, model, system, max_tokens)
    elif _BACKEND == "sdk":
        return _call_claude_sdk(prompt, model, system, max_tokens)
    elif _BACKEND == "auto":
        try:
            return _call_claude_cli(prompt, model, system, max_tokens)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            sys.stderr.write(f"CLI backend failed ({e}), falling back to SDK\n")
            return _call_claude_sdk(prompt, model, system, max_tokens)
    else:
        raise ValueError(f"Unknown CLAUDE_BACKEND: {_BACKEND}")


def _call_claude_cli(
    prompt: str,
    model: str = "sonnet",
    system: str = "",
    max_tokens: int = 1024,
) -> str:
    """
    Call Claude via `claude` CLI (OAuth-authenticated).

    Raises:
        FileNotFoundError: if claude CLI not on PATH
        subprocess.CalledProcessError: if CLI returns non-zero
    """
    args = [
        "claude",
        "--print",
        "--model",
        model,
        "--dangerously-skip-permissions",
    ]

    if system:
        args.extend(["--append-system-prompt", system])

    timeout_s = int(os.getenv("CLAUDE_CLI_TIMEOUT_SEC", "180"))

    try:
        result = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Claude CLI timed out after {timeout_s}s")

    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            args[0],
            output=result.stdout,
            stderr=result.stderr,
        )

    return result.stdout.strip()


def _call_claude_sdk(
    prompt: str,
    model: str = "sonnet",
    system: str = "",
    max_tokens: int = 1024,
) -> str:
    """
    Call Claude via Anthropic SDK (API key required).

    Raises:
        RuntimeError: if ANTHROPIC_API_KEY not set or import fails
        anthropic.*Error: if API call fails
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK not installed")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    system_blocks = []
    if system:
        system_blocks.append({"type": "text", "text": system})

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks if system_blocks else None,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        raise RuntimeError(f"SDK API call failed: {e}") from e


__all__ = ["call_claude"]
