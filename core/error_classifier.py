from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .policies import ProviderPolicy
from .provider_errors import is_upstream_free_quota_exhausted_text


ERROR_QUOTA = "quota"
ERROR_LOCAL_ATTEMPT_TIMEOUT = "local_attempt_timeout"
ERROR_PROVIDER_TRANSIENT = "provider_transient"
ERROR_PROVIDER_ACCOUNT = "provider_account"
ERROR_REQUEST = "request"
ERROR_INTERNAL = "internal"

SCOPE_MODEL = "model"
SCOPE_SOURCE = "source"
SCOPE_NONE = "none"


@dataclass(frozen=True)
class ErrorDisposition:
    kind: str
    scope: str
    should_fallback: bool
    cooldown_seconds: int | None
    reason: str

    @property
    def should_cooldown_model(self) -> bool:
        return self.scope == SCOPE_MODEL and bool(self.cooldown_seconds)

    @property
    def should_open_source(self) -> bool:
        return self.scope == SCOPE_SOURCE


_REQUEST_ERROR_MARKERS = (
    "maximum context length",
    "context length",
    "context_length_exceeded",
    "the model is not a vlm",
    "function calling is not enabled",
    "tool is not supported",
    "tools are not supported",
    "invalid attachment",
    "content policy",
    "content moderation",
    "content_filter",
    "error code: 400",
    "status code: 400",
    "status_code=400",
    "error code: 422",
    "status code: 422",
    "status_code=422",
    "badrequesterror",
    "invalidrequesterror",
    "modality_not_supported",
)

_TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "connection error",
    "connectionerror",
    "connection reset",
    "ratelimiterror",
    "rate limit exceeded",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "error code: 408",
    "status code: 408",
    "status_code=408",
    "error code: 429",
    "status code: 429",
    "status_code=429",
    "error code: 500",
    "status code: 500",
    "status_code=500",
    "error code: 502",
    "status code: 502",
    "status_code=502",
    "error code: 503",
    "status code: 503",
    "status_code=503",
    "error code: 504",
    "status code: 504",
    "status_code=504",
)

_ACCOUNT_ERROR_MARKERS = (
    "accountoverdueerror",
    "account overdue",
    "authenticationerror",
    "invalid api key",
    "incorrect api key",
    "error code: 401",
    "status code: 401",
    "status_code=401",
    "unauthorized",
)

_GUARD_STATE_ERROR_NAMES = {
    "ProviderModelCooldownError",
}

_LOCAL_ATTEMPT_TIMEOUT_MARKERS = (
    "providerattempttimeouterror",
    "first response timed out after",
)


def classify_provider_error(
    *,
    error: Exception | str,
    policy: ProviderPolicy,
) -> ErrorDisposition:
    text = _error_text(error)
    normalized = text.casefold()
    error_name = type(error).__name__ if isinstance(error, Exception) else ""

    if error_name in _GUARD_STATE_ERROR_NAMES:
        return ErrorDisposition(
            kind=ERROR_INTERNAL,
            scope=SCOPE_NONE,
            should_fallback=True,
            cooldown_seconds=None,
            reason="existing_cooldown_guard",
        )

    if error_name == "ProviderAttemptTimeoutError" or any(
        marker in normalized for marker in _LOCAL_ATTEMPT_TIMEOUT_MARKERS
    ):
        return ErrorDisposition(
            kind=ERROR_LOCAL_ATTEMPT_TIMEOUT,
            scope=SCOPE_NONE,
            should_fallback=True,
            cooldown_seconds=None,
            reason="local_attempt_timeout",
        )

    if policy.uses_unknown_reset_quota and is_upstream_free_quota_exhausted_text(
        text
    ):
        return ErrorDisposition(
            kind=ERROR_QUOTA,
            scope=SCOPE_MODEL,
            should_fallback=True,
            cooldown_seconds=None,
            reason="upstream_free_quota_exhausted",
        )

    if policy.manages_local_quota and any(
        marker in normalized for marker in _ACCOUNT_ERROR_MARKERS
    ):
        return ErrorDisposition(
            kind=ERROR_PROVIDER_ACCOUNT,
            scope=SCOPE_SOURCE,
            should_fallback=True,
            cooldown_seconds=policy.health_cooldown_seconds,
            reason="provider_account_unavailable",
        )

    if any(marker in normalized for marker in _REQUEST_ERROR_MARKERS):
        return ErrorDisposition(
            kind=ERROR_REQUEST,
            scope=SCOPE_NONE,
            should_fallback=True,
            cooldown_seconds=None,
            reason="request_not_supported",
        )

    if error_name == "TimeoutError" or any(
        marker in normalized for marker in _TRANSIENT_ERROR_MARKERS
    ):
        return ErrorDisposition(
            kind=ERROR_PROVIDER_TRANSIENT,
            scope=SCOPE_MODEL,
            should_fallback=True,
            cooldown_seconds=policy.health_cooldown_seconds,
            reason="provider_transient_error",
        )

    return ErrorDisposition(
        kind=ERROR_PROVIDER_TRANSIENT,
        scope=SCOPE_MODEL,
        should_fallback=True,
        cooldown_seconds=policy.unknown_error_cooldown_seconds,
        reason="provider_unknown_error",
    )


def _error_text(error: Any) -> str:
    if isinstance(error, Exception):
        return f"{type(error).__name__}: {error}"
    return str(error or "")
