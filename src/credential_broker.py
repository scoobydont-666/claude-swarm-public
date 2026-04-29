#!/usr/bin/env python3
"""Credential broker daemon — unix-socket proxy for git, SSH, and Anthropic API calls."""

import asyncio
import json
import logging
import os
import signal
import uuid
from datetime import UTC, datetime
from pathlib import Path

SOCKET_PATH = os.environ.get("_BROKER_SOCKET_OVERRIDE", "/run/hydra/broker.sock")
LOG_DIR = Path(os.environ.get("_BROKER_LOG_DIR_OVERRIDE", "/opt/swarm/artifacts/broker-logs"))

_in_flight: set[asyncio.Task] = set()
_shutdown = False


# ── logging ──────────────────────────────────────────────────────────────────


def _log_path() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    return LOG_DIR / f"{date}.log"


def _log(record: dict) -> None:
    record.setdefault("ts", datetime.now(UTC).isoformat())
    line = json.dumps(record)
    try:
        with open(_log_path(), "a") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        logging.warning("broker log write failed: %s", exc)


# ── method handlers ───────────────────────────────────────────────────────────


async def _git_push(params: dict) -> dict:
    """Commit all changes in worktree and push to origin."""
    worktree = params["worktree_path"]
    branch = params["branch"]
    message = params["commit_message"]
    author_name = params["author_name"]
    author_email = params["author_email"]

    env = {**os.environ}
    author_str = f"{author_name} <{author_email}>"

    async def run(*args, cwd=worktree):
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode(), stderr.decode()

    # Check for changes
    rc, out, _ = await run("git", "status", "--porcelain")
    if rc != 0:
        raise RuntimeError("git status failed")
    if not out.strip():
        raise RuntimeError("no changes to commit in worktree")

    rc, _, err = await run("git", "add", "-A")
    if rc != 0:
        raise RuntimeError(f"git add failed: {err.strip()}")

    rc, _, err = await run("git", "commit", "-m", message, f"--author={author_str}")
    if rc != 0:
        raise RuntimeError(f"git commit failed: {err.strip()}")

    rc, sha_out, _ = await run("git", "rev-parse", "HEAD")
    commit_sha = sha_out.strip()

    rc, _, err = await run("git", "push", "origin", branch)
    if rc != 0:
        raise RuntimeError(f"git push failed: {err.strip()}")

    return {"commit_sha": commit_sha, "pushed": True}


async def _ssh_exec(params: dict) -> dict:
    """Run a command on a remote host via SSH."""
    host = params["host"]
    cmd = params["cmd"]
    timeout_s = int(params.get("timeout_s", 30))

    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        host,
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"ssh_exec timed out after {timeout_s}s")

    return {
        "stdout": stdout.decode(),
        "stderr": stderr.decode(),
        "exit_code": proc.returncode,
    }


async def _anthropic_proxy(params: dict) -> dict:
    """Proxy a request to the Anthropic messages API."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in broker environment")

    model = params["model"]
    messages = params["messages"]
    max_tokens = int(params.get("max_tokens", 1024))

    try:
        import anthropic as _anthropic  # type: ignore

        client = _anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
        )
        return response.model_dump()
    except ImportError:
        pass

    # Fallback: raw requests
    try:
        import urllib.request as _req

        payload = json.dumps(
            {"model": model, "messages": messages, "max_tokens": max_tokens}
        ).encode()
        request = _req.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with _req.urlopen(request, timeout=120) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"anthropic_proxy http error: {exc}") from exc


# ── dispatch ──────────────────────────────────────────────────────────────────

_METHODS = {
    "git_push": _git_push,
    "ssh_exec": _ssh_exec,
    "anthropic_proxy": _anthropic_proxy,
}


async def _handle_request(raw: str) -> dict:
    """Parse and dispatch a single JSON-line request."""
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"request_id": None, "ok": False, "result": {}, "error": f"invalid json: {exc}"}

    request_id = req.get("request_id", str(uuid.uuid4()))
    method = req.get("method", "")
    params = req.get("params", {})

    log_params = {k: v for k, v in params.items() if k != "api_key"}
    if method == "anthropic_proxy":
        log_params.pop("messages", None)
        log_params["messages"] = "[redacted]"

    _log({"request_id": request_id, "method": method, "params": log_params})

    handler = _METHODS.get(method)
    if handler is None:
        err = f"unknown method: {method}"
        _log({"request_id": request_id, "ok": False, "error": err})
        return {"request_id": request_id, "ok": False, "result": {}, "error": err}

    try:
        result = await handler(params)
        _log({"request_id": request_id, "ok": True})
        return {"request_id": request_id, "ok": True, "result": result, "error": ""}
    except Exception as exc:
        err = str(exc)
        _log({"request_id": request_id, "ok": False, "error": err})
        return {"request_id": request_id, "ok": False, "result": {}, "error": err}


# ── connection handler ────────────────────────────────────────────────────────


async def _client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handle one connected client — read lines, respond, close."""
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            response = await _handle_request(line.decode().strip())
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ── lifecycle ─────────────────────────────────────────────────────────────────


def _setup_socket_dir() -> None:
    sock_dir = Path(SOCKET_PATH).parent
    sock_dir.mkdir(parents=True, exist_ok=True)


async def _serve() -> None:
    _setup_socket_dir()

    sock_path = Path(SOCKET_PATH)
    if sock_path.exists():
        sock_path.unlink()

    server = await asyncio.start_unix_server(_client_connected, path=SOCKET_PATH)

    # 0600 owned by aisvc — set mode; ownership set by systemd RuntimeDirectory
    os.chmod(SOCKET_PATH, 0o600)

    logging.info("credential broker listening on %s", SOCKET_PATH)
    _log({"event": "broker_start", "socket": SOCKET_PATH})

    loop = asyncio.get_running_loop()

    def _sigterm(_sig, _frame):
        global _shutdown
        _shutdown = True
        logging.info("SIGTERM received — draining in-flight requests")
        server.close()

    loop.add_signal_handler(signal.SIGTERM, lambda: _sigterm(None, None))
    loop.add_signal_handler(signal.SIGINT, lambda: _sigterm(None, None))

    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    finally:
        _log({"event": "broker_stop"})
        logging.info("broker exited")


if __name__ == "__main__":
    main()
