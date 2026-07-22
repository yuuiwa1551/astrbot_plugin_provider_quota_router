from __future__ import annotations

from dataclasses import dataclass
from typing import Any


LOCAL_QUOTA_NONE = "none"
LOCAL_QUOTA_DAILY = "daily"
QUOTA_EXHAUSTION_NONE = "none"
QUOTA_EXHAUSTION_ROLLING = "rolling_24h"
QUOTA_EXHAUSTION_UNKNOWN_RESET = "unknown_reset_probe"


@dataclass(frozen=True)
class ProviderPolicy:
    provider_id: str
    provider_source_id: str
    local_quota_mode: str
    quota_exhaustion_mode: str
    health_cooldown_seconds: int
    unknown_error_cooldown_seconds: int
    first_response_timeout_seconds: int

    @property
    def manages_local_quota(self) -> bool:
        return self.local_quota_mode == LOCAL_QUOTA_DAILY

    @property
    def uses_unknown_reset_quota(self) -> bool:
        return self.quota_exhaustion_mode == QUOTA_EXHAUSTION_UNKNOWN_RESET


def provider_identity(provider: Any) -> tuple[str, str, str]:
    provider_config = getattr(provider, "provider_config", {}) or {}
    provider_id = str(provider_config.get("id") or "")
    source_id = str(provider_config.get("provider_source_id") or "")
    try:
        provider_model = str(provider.get_model() or "")
    except Exception:  # noqa: BLE001
        provider_model = ""
    if not provider_model:
        provider_model = str(provider_config.get("model") or "")
    return provider_id, source_id, provider_model


def build_provider_policy(*, provider: Any, settings: Any) -> ProviderPolicy:
    provider_id, source_id, _ = provider_identity(provider)
    local_quota = bool(settings.is_volcengine_source(source_id))
    unknown_reset = bool(settings.is_upstream_quota_provider(provider_id))
    return ProviderPolicy(
        provider_id=provider_id,
        provider_source_id=source_id,
        local_quota_mode=(LOCAL_QUOTA_DAILY if local_quota else LOCAL_QUOTA_NONE),
        quota_exhaustion_mode=(
            QUOTA_EXHAUSTION_ROLLING
            if local_quota
            else (
                QUOTA_EXHAUSTION_UNKNOWN_RESET
                if unknown_reset
                else QUOTA_EXHAUSTION_NONE
            )
        ),
        health_cooldown_seconds=max(
            0, int(settings.provider_error_cooldown_seconds)
        ),
        unknown_error_cooldown_seconds=max(
            0, int(settings.unknown_provider_error_cooldown_seconds)
        ),
        first_response_timeout_seconds=max(
            0, int(settings.provider_error_attempt_timeout_seconds)
        ),
    )
