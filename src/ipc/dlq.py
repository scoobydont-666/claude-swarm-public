"""Dead-letter queue management for undeliverable IPC messages."""

from __future__ import annotations

import logging
import time

from . import transport
from .agent import _K_INBOX, _K_INBOX_GROUP, get_current_agent_id
from .envelope import Envelope

_DLQ_KEY = "ipc:dlq"
_DLQ_MAXLEN = 5000

# Messages pending longer than this get moved to DLQ
_PENDING_TIMEOUT_MS = 60_000  # 60 seconds

log = logging.getLogger(__name__)


def _warn_if_no_persistence() -> None:
    """REL-01: Warn loudly if Redis has no AOF or RDB persistence configured.

    DLQ entries are stored in Redis streams. Without appendonly or save
    configured, a Redis restart silently drops all DLQ entries — undeliverable
    messages are permanently lost with no record for triage.

    This is ops-side config; the app does not modify Redis config. The warning
    is intentionally loud (WARNING level) so it surfaces in service logs on
    every startup when persistence is off.
    """
    try:
        r = transport.get_client()
        appendonly = r.config_get("appendonly").get("appendonly", "no")
        save = r.config_get("save").get("save", "")
        if appendonly != "yes" and not save:
            log.warning(
                "REL-01: Redis has NO persistence (appendonly=no, save='')."
                " DLQ entries in ipc:dlq will be LOST on Redis restart."
                " To enable: CONFIG SET appendonly yes; CONFIG REWRITE"
                " — see /opt/claude-swarm/docs/RUNBOOK.md#rel-01-dlq-persistence"
            )
    except Exception as exc:  # noqa: BLE001
        # If we cannot reach Redis at all, transport will surface its own errors.
        # Don't let a persistence check crash the DLQ module.
        log.debug("REL-01 persistence check skipped: %s", exc)


_warn_if_no_persistence()


def list_dlq(limit: int = 50) -> list[dict]:
    """List dead-letter queue entries.

    Returns list of dicts with: stream_id, envelope, reason, timestamp.
    """
    r = transport.get_client()
    raw = r.xrange(_DLQ_KEY, "-", "+", count=limit)
    entries = []
    for stream_id, fields in raw:
        entry = {"stream_id": stream_id, "reason": fields.get("reason", "unknown")}
        raw_env = fields.get("envelope", "")
        if raw_env:
            try:
                entry["envelope"] = Envelope.from_json(raw_env)
            except Exception:
                entry["envelope_raw"] = raw_env
        entries.append(entry)
    return entries


def requeue(message_id: str, new_recipient: str | None = None) -> bool:
    """Move a message from DLQ back to a target inbox.

    Args:
        message_id: Stream ID in the DLQ
        new_recipient: Override recipient (defaults to original)

    Returns True if requeued successfully.
    """
    r = transport.get_client()
    raw = r.xrange(_DLQ_KEY, message_id, message_id, count=1)
    if not raw:
        return False

    _, fields = raw[0]
    raw_env = fields.get("envelope", "")
    if not raw_env:
        return False

    try:
        env = Envelope.from_json(raw_env)
    except Exception:
        return False

    recipient = new_recipient or env.recipient
    inbox_key = f"{_K_INBOX}{recipient}"

    # Ensure target inbox exists
    transport.ensure_consumer_group(inbox_key, _K_INBOX_GROUP)

    # Add to inbox
    transport.stream_add(inbox_key, {"envelope": raw_env}, maxlen=5000)

    # Remove from DLQ
    r.xdel(_DLQ_KEY, message_id)

    return True


def purge(older_than_seconds: int = 3600) -> int:
    """Remove DLQ entries older than the given threshold.

    Returns count of purged entries.
    """
    r = transport.get_client()
    cutoff = time.time() - older_than_seconds

    raw = r.xrange(_DLQ_KEY, "-", "+")
    to_delete = []
    for stream_id, fields in raw:
        # Stream IDs are {timestamp_ms}-{seq}
        try:
            ts_ms = int(stream_id.split("-")[0])
            if ts_ms / 1000 < cutoff:
                to_delete.append(stream_id)
        except (ValueError, IndexError):
            continue

    if to_delete:
        r.xdel(_DLQ_KEY, *to_delete)

    return len(to_delete)


def dlq_depth() -> int:
    """Get the number of entries in the DLQ."""
    return transport.stream_len(_DLQ_KEY)


def prune_old_messages(hours: int = 72) -> int:
    """E5: Remove DLQ entries older than `hours`. Returns count pruned.

    XADD caps at _DLQ_MAXLEN=5000 entries, so capacity is already bounded;
    this age-based prune is for dashboard cleanliness (old entries no longer
    useful for triage). 72h default covers a Friday-to-Monday work cycle
    plus the 48h D3-style staging observation windows with a buffer day —
    Josh directive 2026-04-18 (work spans multiple days, don't lose
    triage-able entries over a weekend). Increase freely if investigation
    windows grow; decrease only if the DLQ table gets noisy.

    Args:
        hours: Messages with stream_id older than (now - hours) are removed.
            Default 72h. Minimum sensible value is 48h for a multi-day
            work cadence.

    Returns:
        Number of entries removed.
    """
    r = transport.get_client()
    # Redis stream IDs are millis + sequence. Compute cutoff millis.
    cutoff_ms = int((time.time() - hours * 3600) * 1000)
    cutoff_id = f"{cutoff_ms}-0"

    # XTRIM with MINID: O(N) in removed entries, cheaper than range+delete.
    # approximate=True lets Redis batch the trim for lower tail latency.
    try:
        removed = r.xtrim(_DLQ_KEY, minid=cutoff_id, approximate=True)
        return int(removed or 0)
    except Exception:
        # Best-effort: fall back to explicit XRANGE + XDEL if XTRIM minid
        # isn't supported (very old Redis).
        entries = r.xrange(_DLQ_KEY, "-", f"({cutoff_id}")
        if not entries:
            return 0
        ids = [stream_id for stream_id, _ in entries]
        for i in range(0, len(ids), 100):
            r.xdel(_DLQ_KEY, *ids[i : i + 100])
        return len(ids)


def sweep_pending(agent_id: str | None = None) -> int:
    """Check for stuck pending messages and move them to DLQ.

    Runs on the current agent's inbox. Messages pending > 60s
    without XACK are moved to the dead-letter queue.

    Returns count of messages moved to DLQ.
    """
    agent_id = agent_id or get_current_agent_id()
    if not agent_id:
        return 0

    inbox_key = f"{_K_INBOX}{agent_id}"
    pending = transport.stream_pending(
        inbox_key, _K_INBOX_GROUP, count=50, min_idle_ms=_PENDING_TIMEOUT_MS
    )

    if not pending:
        return 0

    r = transport.get_client()
    moved = 0
    for entry in pending:
        mid = entry["message_id"]
        # Read the actual message
        raw = r.xrange(inbox_key, mid, mid, count=1)
        if raw:
            _, fields = raw[0]
            fields["reason"] = f"pending_timeout_{entry['idle_ms']}ms"
            r.xadd(_DLQ_KEY, fields, maxlen=_DLQ_MAXLEN, approximate=True)
            # Ack to remove from pending
            transport.stream_ack(inbox_key, _K_INBOX_GROUP, mid)
            moved += 1

    return moved
