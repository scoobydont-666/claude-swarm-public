"""Rate-limit detection for Claude Code agent output.

Parses Claude CLI stdout for rate-limit markers and classifies them as
session or weekly limits. Tracks cooldown windows per profile so the
dispatcher can route tasks to non-limited profiles.

Adapted from Aperant's rate-limit-detector pattern (AGPL-3.0 reference,
reimplemented in Python for claude-swarm).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Detection patterns — match Claude Code CLI stdout
# ---------------------------------------------------------------------------

# "Limit reached · resets in 3 hours" or "Limit reached · resets tomorrow at 9:00 AM"
RE_LIMIT_REACHED = re.compile(
    r"Limit reached\s*[·•]\s*resets?\s+(.+?)(?:\s*$|\n)", re.IGNORECASE | re.MULTILINE
)

# Session overloaded / capacity messages
RE_OVERLOADED = re.compile(
    r"overloaded|capacity|try again later|rate.limit|too many requests",
    re.IGNORECASE,
)

# Auth failures (401)
RE_AUTH_FAILURE = re.compile(
    r"unauthorized|401|authentication.failed|invalid.api.key|expired.token",
    re.IGNORECASE,
)

# Billing failures (402 / credit exhaustion)
RE_BILLING_FAILURE = re.compile(
    r"402|payment.required|credit|billing|insufficient.funds",
    re.IGNORECASE,
)


@dataclass
class RateLimitEvent:
    """A detected rate-limit event."""

    profile: str
    limit_type: str  # "session" | "weekly" | "overloaded" | "auth" | "billing"
    reset_hint: str  # raw reset time string from CLI output
    detected_at: float = field(default_factory=time.time)
    cooldown_until: float = 0.0  # unix timestamp when this profile is available again

    def to_dict(self) -> dict[str, Any]:
        """Convert rate limit to a dictionary with ISO timestamps."""
        return {
            "profile": self.profile,
            "limit_type": self.limit_type,
            "reset_hint": self.reset_hint,
            "detected_at": datetime.fromtimestamp(
                self.detected_at, tz=timezone.utc
            ).isoformat(),
            "cooldown_until": datetime.fromtimestamp(
                self.cooldown_until, tz=timezone.utc
            ).isoformat()
            if self.cooldown_until
            else "",
        }


def _classify_limit(reset_hint: str) -> tuple[str, float]:
    """Classify a rate-limit reset hint as session or weekly.

    Session limits: "resets in 3 hours", "resets in 45 minutes"
    Weekly limits: "resets Monday at 9:00 AM", "resets March 28"

    Returns (limit_type, estimated_cooldown_seconds).
    """
    hint_lower = reset_hint.lower().strip()

    # Weekly: contains day names or dates
    weekly_markers = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]
    if any(marker in hint_lower for marker in weekly_markers):
        return "weekly", 86400.0  # conservative: 24h cooldown

    # Parse "in N hours/minutes"
    hours_match = re.search(r"(\d+)\s*hours?", hint_lower)
    minutes_match = re.search(r"(\d+)\s*minutes?", hint_lower)

    cooldown = 0.0
    if hours_match:
        cooldown += int(hours_match.group(1)) * 3600
    if minutes_match:
        cooldown += int(minutes_match.group(1)) * 60

    if cooldown > 0:
        return "session", cooldown

    # Default: session with 1-hour cooldown
    return "session", 3600.0


def detect_rate_limit(
    output_line: str, profile: str = "default"
) -> RateLimitEvent | None:
    """Parse a line of Claude Code output for rate-limit signals.

    Args:
        output_line: A line from Claude Code's stdout/stderr.
        profile: The API profile (key/account) the agent is using.

    Returns:
        RateLimitEvent if a limit was detected, None otherwise.
    """
    # Check for explicit "Limit reached" marker (highest confidence)
    match = RE_LIMIT_REACHED.search(output_line)
    if match:
        reset_hint = match.group(1).strip()
        limit_type, cooldown = _classify_limit(reset_hint)
        return RateLimitEvent(
            profile=profile,
            limit_type=limit_type,
            reset_hint=reset_hint,
            cooldown_until=time.time() + cooldown,
        )

    # Auth failure
    if RE_AUTH_FAILURE.search(output_line):
        return RateLimitEvent(
            profile=profile,
            limit_type="auth",
            reset_hint="authentication failure — check API key",
            cooldown_until=0.0,  # permanent until fixed
        )

    # Billing failure
    if RE_BILLING_FAILURE.search(output_line):
        return RateLimitEvent(
            profile=profile,
            limit_type="billing",
            reset_hint="billing/credit issue — check account",
            cooldown_until=0.0,  # permanent until fixed
        )

    # Generic overload
    if RE_OVERLOADED.search(output_line):
        return RateLimitEvent(
            profile=profile,
            limit_type="overloaded",
            reset_hint="server overloaded — retry in 5 minutes",
            cooldown_until=time.time() + 300,
        )

    return None


class RateLimitTracker:
    """Track rate-limit state across multiple API profiles.

    Used by auto-dispatch to avoid sending tasks to rate-limited profiles.
    """

    def __init__(self) -> None:
        self._limits: dict[str, RateLimitEvent] = {}  # profile → last event

    def record(self, event: RateLimitEvent) -> None:
        """Record a rate-limit event for a profile."""
        self._limits[event.profile] = event

    def is_available(self, profile: str) -> bool:
        """Check if a profile is currently available (not rate-limited)."""
        event = self._limits.get(profile)
        if event is None:
            return True
        # Permanent failures (auth/billing) never clear
        if event.cooldown_until == 0.0:
            return False
        return time.time() >= event.cooldown_until

    def get_available_profiles(self, profiles: list[str]) -> list[str]:
        """Return profiles not currently rate-limited."""
        return [p for p in profiles if self.is_available(p)]

    def get_best_profile(self, profiles: list[str]) -> str | None:
        """Return the best available profile, or None if all limited.

        Prefers profiles with no recorded limits, then earliest cooldown expiry.
        """
        available = self.get_available_profiles(profiles)
        if available:
            # Prefer profiles never rate-limited
            never_limited = [p for p in available if p not in self._limits]
            if never_limited:
                return never_limited[0]
            return available[0]
        return None

    def status(self) -> dict[str, Any]:
        """Return current rate-limit status for all tracked profiles."""
        now = time.time()
        return {
            profile: {
                "available": self.is_available(profile),
                "limit_type": event.limit_type,
                "reset_hint": event.reset_hint,
                "seconds_remaining": max(0, event.cooldown_until - now)
                if event.cooldown_until
                else "permanent",
            }
            for profile, event in self._limits.items()
        }
