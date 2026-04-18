"""IPC message envelope — structured, time-ordered, serializable."""

from __future__ import annotations

import json
import os
import struct
import time
import uuid
from dataclasses import asdict, dataclass, field


def _uuid7() -> str:
    """Generate a UUIDv7 (time-ordered, sortable).

    Layout: 48-bit unix_ms | 4-bit version(7) | 12-bit rand | 2-bit variant | 62-bit rand
    """
    unix_ms = int(time.time() * 1000)
    rand_bytes = os.urandom(10)
    # Pack: 48-bit timestamp in top 6 bytes
    ts_bytes = struct.pack(">Q", unix_ms)[2:]  # 6 bytes
    # Byte 6-7: version nibble (0x7) + 12 random bits
    rand_a = int.from_bytes(rand_bytes[:2], "big")
    byte_67 = (0x7000 | (rand_a & 0x0FFF)).to_bytes(2, "big")
    # Byte 8-9: variant bits (0b10) + 14 random bits
    rand_b = int.from_bytes(rand_bytes[2:4], "big")
    byte_89 = (0x8000 | (rand_b & 0x3FFF)).to_bytes(2, "big")
    # Bytes 10-15: 48 random bits
    tail = rand_bytes[4:]
    raw = ts_bytes + byte_67 + byte_89 + tail
    return str(uuid.UUID(bytes=raw))


# Valid message types
MESSAGE_TYPES = frozenset(
    {
        "direct",
        "channel",
        "rpc_request",
        "rpc_response",
        "rpc_stream",
        "broadcast",
        "presence",
        "ping",
        "pong",
    }
)


@dataclass
class Envelope:
    """Structured message envelope for all IPC communication."""

    sender: str
    recipient: str
    message_type: str
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=_uuid7)
    timestamp: float = field(default_factory=time.time)
    correlation_id: str = ""
    reply_to: str = ""
    ttl: int = 0
    priority: int = 3
    sequence: int = 0
    final: bool = True

    def __post_init__(self) -> None:
        if self.message_type not in MESSAGE_TYPES:
            raise ValueError(
                f"Invalid message_type '{self.message_type}', "
                f"must be one of {sorted(MESSAGE_TYPES)}"
            )
        if not 0 <= self.priority <= 5:
            raise ValueError(f"Priority must be 0-5, got {self.priority}")

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> Envelope:
        """Deserialize from JSON string."""
        data = json.loads(raw)
        return cls(**data)

    def is_expired(self) -> bool:
        """Check if this message has exceeded its TTL."""
        if self.ttl <= 0:
            return False
        return time.time() > self.timestamp + self.ttl

    def make_reply(self, payload: dict, message_type: str = "direct") -> Envelope:
        """Create a reply envelope addressed to the sender."""
        return Envelope(
            sender=self.recipient,
            recipient=self.sender,
            message_type=message_type,
            payload=payload,
            correlation_id=self.correlation_id or self.id,
            reply_to="",
        )
