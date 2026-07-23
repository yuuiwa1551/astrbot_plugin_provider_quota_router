from __future__ import annotations

import unittest

from core.error_classifier import (
    ERROR_LOCAL_ATTEMPT_TIMEOUT,
    ERROR_PROVIDER_ACCOUNT,
    ERROR_PROVIDER_TRANSIENT,
    ERROR_QUOTA,
    ERROR_REQUEST,
    SCOPE_MODEL,
    SCOPE_NONE,
    SCOPE_SOURCE,
    classify_provider_error,
)
from core.opencode_quota_guard import ProviderAttemptTimeoutError
from core.policies import ProviderPolicy


def policy(
    *,
    local_quota_mode: str = "none",
    quota_exhaustion_mode: str = "none",
) -> ProviderPolicy:
    return ProviderPolicy(
        provider_id="provider/model",
        provider_source_id="relay",
        local_quota_mode=local_quota_mode,
        quota_exhaustion_mode=quota_exhaustion_mode,
        health_cooldown_seconds=1800,
        unknown_error_cooldown_seconds=300,
        first_response_timeout_seconds=20,
    )


class ErrorClassifierTests(unittest.TestCase):
    def test_request_error_does_not_poison_provider_health(self) -> None:
        disposition = classify_provider_error(
            error="BadRequestError: Error code: 400 maximum context length",
            policy=policy(),
        )

        self.assertEqual(disposition.kind, ERROR_REQUEST)
        self.assertEqual(disposition.scope, SCOPE_NONE)
        self.assertIsNone(disposition.cooldown_seconds)

    def test_known_transient_error_uses_normal_model_cooldown(self) -> None:
        disposition = classify_provider_error(
            error=TimeoutError("timed out"),
            policy=policy(),
        )

        self.assertEqual(disposition.kind, ERROR_PROVIDER_TRANSIENT)
        self.assertEqual(disposition.scope, SCOPE_MODEL)
        self.assertEqual(disposition.cooldown_seconds, 1800)

    def test_local_attempt_timeout_does_not_immediately_poison_health(self) -> None:
        disposition = classify_provider_error(
            error=ProviderAttemptTimeoutError(
                "provider/model first response timed out after 20 seconds"
            ),
            policy=policy(),
        )

        self.assertEqual(disposition.kind, ERROR_LOCAL_ATTEMPT_TIMEOUT)
        self.assertEqual(disposition.scope, SCOPE_NONE)
        self.assertTrue(disposition.should_fallback)
        self.assertIsNone(disposition.cooldown_seconds)

    def test_serialized_local_attempt_timeout_is_still_distinguished(self) -> None:
        disposition = classify_provider_error(
            error=(
                "All chat models failed: ProviderAttemptTimeoutError: "
                "provider/model first response timed out after 20 seconds"
            ),
            policy=policy(),
        )

        self.assertEqual(disposition.kind, ERROR_LOCAL_ATTEMPT_TIMEOUT)
        self.assertEqual(disposition.scope, SCOPE_NONE)
        self.assertIsNone(disposition.cooldown_seconds)

    def test_unknown_error_uses_short_model_cooldown(self) -> None:
        disposition = classify_provider_error(
            error=RuntimeError("unexpected provider failure"),
            policy=policy(),
        )

        self.assertEqual(disposition.kind, ERROR_PROVIDER_TRANSIENT)
        self.assertEqual(disposition.cooldown_seconds, 300)

    def test_opencode_free_limit_uses_unknown_reset_quota_state(self) -> None:
        disposition = classify_provider_error(
            error="FreeUsageLimitError: daily free usage limit exceeded",
            policy=policy(quota_exhaustion_mode="unknown_reset_probe"),
        )

        self.assertEqual(disposition.kind, ERROR_QUOTA)
        self.assertEqual(disposition.scope, SCOPE_MODEL)
        self.assertIsNone(disposition.cooldown_seconds)

    def test_local_plan_account_error_opens_source_scope(self) -> None:
        disposition = classify_provider_error(
            error="Error code: 403 AccountOverdueError",
            policy=policy(local_quota_mode="daily"),
        )

        self.assertEqual(disposition.kind, ERROR_PROVIDER_ACCOUNT)
        self.assertEqual(disposition.scope, SCOPE_SOURCE)

    def test_local_plan_authentication_error_opens_source_scope(self) -> None:
        disposition = classify_provider_error(
            error="AuthenticationError: Error code: 401 invalid API key",
            policy=policy(local_quota_mode="daily"),
        )

        self.assertEqual(disposition.kind, ERROR_PROVIDER_ACCOUNT)
        self.assertEqual(disposition.scope, SCOPE_SOURCE)


if __name__ == "__main__":
    unittest.main()
