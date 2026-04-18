#!/usr/bin/env python3
"""Broker client helper — thin wrapper for workers to call the credential broker."""

import json
import socket
import uuid

SOCKET_PATH = "/run/hydra/broker.sock"


class BrokerError(Exception):
    """Raised when the broker returns ok=false."""


def call(method: str, params: dict, timeout: int = 30) -> dict:
    """Send a request to the credential broker and return the result dict.

    Raises BrokerError if the broker returns ok=false.
    Raises OSError if the socket is unreachable.
    """
    request = {
        "method": method,
        "params": params,
        "request_id": str(uuid.uuid4()),
    }
    payload = (json.dumps(request) + "\n").encode()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(SOCKET_PATH)
        sock.sendall(payload)

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()

    response = json.loads(buf.split(b"\n")[0])
    if not response.get("ok"):
        raise BrokerError(response.get("error", "unknown broker error"))
    return response.get("result", {})
