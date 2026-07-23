from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AttemptTimeoutObservation:
    count: int
    threshold: int
    should_cooldown: bool


class AttemptTimeoutTracker:
    """Track consecutive local first-response timeouts per Provider."""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}

    def record_timeout(
        self,
        *,
        provider_id: str,
        threshold: int,
        window_seconds: int,
        now: float | None = None,
    ) -> AttemptTimeoutObservation:
        normalized_id = str(provider_id or "")
        normalized_threshold = max(1, int(threshold))
        current = time.monotonic() if now is None else float(now)
        cutoff = current - max(1, int(window_seconds))
        failures = [
            timestamp
            for timestamp in self._failures.get(normalized_id, [])
            if timestamp >= cutoff
        ]
        failures.append(current)
        should_cooldown = len(failures) >= normalized_threshold
        if should_cooldown:
            self._failures.pop(normalized_id, None)
        else:
            self._failures[normalized_id] = failures
        return AttemptTimeoutObservation(
            count=len(failures),
            threshold=normalized_threshold,
            should_cooldown=should_cooldown,
        )

    def record_success(self, *, provider_id: str) -> None:
        self._failures.pop(str(provider_id or ""), None)

    def pending_count(self, *, provider_id: str) -> int:
        return len(self._failures.get(str(provider_id or ""), []))
