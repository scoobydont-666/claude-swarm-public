"""Smoke tests for credential broker daemon and client helper."""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def broker_socket(tmp_path):
    """Start the broker in a subprocess with a tmp socket path, yield the path, then stop."""
    sock_path = str(tmp_path / "broker.sock")
    log_dir = str(tmp_path / "broker-logs")
    os.makedirs(log_dir, exist_ok=True)

    env = {
        **os.environ,
        "_BROKER_SOCKET_OVERRIDE": sock_path,
        "_BROKER_LOG_DIR_OVERRIDE": log_dir,
        "SWARM_REDIS_SKIP_CHECK": "1",
    }

    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "src" / "credential_broker.py"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for socket to appear
    deadline = time.time() + 5
    while not Path(sock_path).exists():
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("broker socket never appeared")
        time.sleep(0.1)

    yield sock_path

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _raw_call(sock_path: str, method: str, params: dict, timeout: int = 10) -> dict:
    """Send a raw JSON-line request to the broker socket and return parsed response."""
    import socket as _socket

    request_id = str(uuid.uuid4())
    payload = (
        json.dumps({"method": method, "params": params, "request_id": request_id}) + "\n"
    ).encode()

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sock_path)
        sock.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()

    return json.loads(buf.split(b"\n")[0])


# ── broker module override support ────────────────────────────────────────────
# The broker reads env vars at module level for override in tests.
# We patch SOCKET_PATH and LOG_DIR via env before import — but since
# the broker runs in a subprocess, we just pass env vars and rely on
# the override block at the bottom of credential_broker.py.
# For unit-level tests we import the handlers directly.


# ── tests: unknown method ─────────────────────────────────────────────────────


class TestBrokerProtocol:
    def test_unknown_method(self, broker_socket):
        resp = _raw_call(broker_socket, "bogus_method", {})
        assert resp["ok"] is False
        assert "unknown method" in resp["error"]
        assert "request_id" in resp

    def test_invalid_json(self, broker_socket):
        import socket as _socket

        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(broker_socket)
            sock.sendall(b"not json at all\n")
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()

        resp = json.loads(buf.split(b"\n")[0])
        assert resp["ok"] is False
        assert "invalid json" in resp["error"]


# ── tests: ssh_exec ───────────────────────────────────────────────────────────


class TestSshExec:
    def test_echo_localhost(self, broker_socket):
        """ssh_exec to localhost with echo hello — requires SSH access to self."""
        resp = _raw_call(
            broker_socket,
            "ssh_exec",
            {"host": "127.0.0.1", "cmd": "echo hello", "timeout_s": 10},
        )
        # ssh_exec always returns ok=True (protocol success); check exit_code
        assert resp["ok"] is True
        if resp["result"]["exit_code"] != 0:
            pytest.skip(f"SSH to localhost not configured (exit {resp['result']['exit_code']})")
        assert "hello" in resp["result"]["stdout"]

    def test_timeout_or_auth_fail(self, broker_socket):
        """ssh_exec with very short timeout times out, or auth fails fast."""
        resp = _raw_call(
            broker_socket,
            "ssh_exec",
            {"host": "127.0.0.1", "cmd": "sleep 60", "timeout_s": 1},
        )
        # Either timed out (ok=False, error contains timeout) or auth failed (ok=True, exit_code!=0)
        if resp["ok"]:
            assert resp["result"]["exit_code"] != 0
        else:
            assert "timed out" in resp["error"]


# ── tests: git_push ───────────────────────────────────────────────────────────


class TestGitPush:
    def _init_repo_with_remote(self, tmp_path):
        """Create a bare remote and a clone with one commit. Returns (worktree, branch)."""
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=str(worktree),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(worktree),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(worktree),
            check=True,
            capture_output=True,
        )
        (worktree / "README.md").write_text("init\n")
        subprocess.run(["git", "add", "-A"], cwd=str(worktree), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(worktree),
            check=True,
            capture_output=True,
        )
        # Detect the actual default branch name (master vs main)
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(worktree),
            check=True,
            capture_output=True,
        )
        branch = result.stdout.decode().strip()
        subprocess.run(
            ["git", "push", "origin", f"HEAD:{branch}"],
            cwd=str(worktree),
            check=True,
            capture_output=True,
        )
        return worktree, branch

    def test_no_changes_fails(self, broker_socket, tmp_path):
        worktree, branch = self._init_repo_with_remote(tmp_path)
        resp = _raw_call(
            broker_socket,
            "git_push",
            {
                "worktree_path": str(worktree),
                "branch": branch,
                "commit_message": "should fail",
                "author_name": "Bot",
                "author_email": "bot@hydra",
            },
        )
        assert resp["ok"] is False
        assert "no changes" in resp["error"]

    def test_commit_and_push(self, broker_socket, tmp_path):
        worktree, branch = self._init_repo_with_remote(tmp_path)
        (worktree / "new_file.txt").write_text("content\n")

        resp = _raw_call(
            broker_socket,
            "git_push",
            {
                "worktree_path": str(worktree),
                "branch": branch,
                "commit_message": "add new_file",
                "author_name": "Hydra Bot",
                "author_email": "hydra@node_primary",
            },
        )
        assert resp["ok"] is True, resp.get("error")
        assert resp["result"]["pushed"] is True
        assert len(resp["result"]["commit_sha"]) == 40


# ── tests: anthropic_proxy ────────────────────────────────────────────────────


class TestAnthropicProxy:
    def test_no_api_key(self, broker_socket, monkeypatch):
        """Without ANTHROPIC_API_KEY the broker should fail gracefully."""
        # The broker subprocess already has env from fixture; we can't easily
        # unset it post-hoc. We test the handler directly instead.
        import unittest.mock as mock

        import credential_broker as cb

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY not set"):
                asyncio.run(cb._anthropic_proxy({"model": "x", "messages": [], "max_tokens": 1}))

    def test_live_call_skipped_without_key(self, broker_socket):
        """Skip live Anthropic test if no key configured."""
        if not os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("ANTHROPIC_API_KEY not set")

        resp = _raw_call(
            broker_socket,
            "anthropic_proxy",
            {
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "say pong"}],
                "max_tokens": 10,
            },
        )
        assert resp["ok"] is True


# ── tests: broker_client helper ───────────────────────────────────────────────


class TestBrokerClient:
    def test_broker_error_raised(self, broker_socket, monkeypatch):
        import broker_client as bc

        monkeypatch.setattr(bc, "SOCKET_PATH", broker_socket)
        with pytest.raises(bc.BrokerError, match="unknown method"):
            bc.call("not_a_method", {})

    def test_successful_call_returns_result(self, broker_socket, tmp_path, monkeypatch):
        import broker_client as bc

        monkeypatch.setattr(bc, "SOCKET_PATH", broker_socket)

        remote = tmp_path / "r.git"
        remote.mkdir()
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        wt = tmp_path / "wt"
        wt.mkdir()
        for cmd in [
            ["git", "init", str(wt)],
            ["git", "-C", str(wt), "remote", "add", "origin", str(remote)],
            ["git", "-C", str(wt), "config", "user.email", "t@t.com"],
            ["git", "-C", str(wt), "config", "user.name", "T"],
        ]:
            subprocess.run(cmd, check=True, capture_output=True)

        (wt / "f.txt").write_text("hello\n")
        subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "init"], check=True, capture_output=True
        )

        branch_result = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
        )
        branch = branch_result.stdout.decode().strip()
        subprocess.run(
            ["git", "-C", str(wt), "push", "origin", f"HEAD:{branch}"],
            check=True,
            capture_output=True,
        )

        (wt / "extra.txt").write_text("new\n")
        result = bc.call(
            "git_push",
            {
                "worktree_path": str(wt),
                "branch": branch,
                "commit_message": "via client helper",
                "author_name": "Test",
                "author_email": "test@hydra",
            },
        )
        assert result["pushed"] is True
