from __future__ import annotations

from dataclasses import dataclass


EXPLICIT_SELECTION_ORIGINS = frozenset({"request", "umo"})


@dataclass(frozen=True)
class ProviderSelection:
    """The effective Provider and why AstrBot selected it for this request."""

    provider_id: str
    origin: str = "default"

    @property
    def is_explicit(self) -> bool:
        return self.origin in EXPLICIT_SELECTION_ORIGINS
