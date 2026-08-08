from __future__ import annotations

import unittest

from core.config import RouterSettings
from core.policies import build_provider_policy


class FakeProvider:
    def __init__(
        self,
        provider_id: str,
        source_id: str = "relay",
        model: str = "model",
    ) -> None:
        self.provider_config = {
            "id": provider_id,
            "provider_source_id": source_id,
            "model": model,
        }

    def get_model(self) -> str:
        return str(self.provider_config["model"])


class ProviderPolicyTests(unittest.TestCase):
    def test_exact_provider_override_replaces_only_call_budgets(self) -> None:
        provider_id = "volcengine-agent-plan/doubao-seed-2.0-mini"
        settings = RouterSettings.from_raw(
            {
                "provider_error_attempt_timeout_seconds": 20,
                "provider_error_request_max_retries": 3,
                "provider_policy_overrides": [
                    {
                        "provider_id": provider_id,
                        "first_response_timeout_seconds": 3,
                        "request_max_retries": 1,
                        "max_output_tokens": 220,
                    }
                ],
            }
        )

        policy = build_provider_policy(
            provider=FakeProvider(provider_id, source_id="openai"),
            settings=settings,
        )

        self.assertEqual(policy.first_response_timeout_seconds, 3)
        self.assertEqual(policy.request_max_retries, 1)
        self.assertEqual(policy.max_output_tokens, 220)
        self.assertTrue(policy.manages_local_quota)

    def test_unmatched_provider_inherits_global_call_budgets(self) -> None:
        settings = RouterSettings.from_raw(
            {
                "provider_error_attempt_timeout_seconds": 20,
                "provider_error_request_max_retries": 3,
                "provider_policy_overrides": [
                    {
                        "provider_id": "dedicated/helper",
                        "first_response_timeout_seconds": 3,
                        "request_max_retries": 1,
                        "max_output_tokens": 220,
                    }
                ],
            }
        )

        policy = build_provider_policy(
            provider=FakeProvider("normal/main"),
            settings=settings,
        )

        self.assertEqual(policy.first_response_timeout_seconds, 20)
        self.assertEqual(policy.request_max_retries, 3)
        self.assertIsNone(policy.max_output_tokens)

    def test_zero_provider_limits_disable_timeout_and_output_cap(self) -> None:
        settings = RouterSettings.from_raw(
            {
                "provider_policy_overrides": [
                    {
                        "provider_id": "provider/model",
                        "first_response_timeout_seconds": 0,
                        "max_output_tokens": 0,
                    }
                ]
            }
        )

        policy = build_provider_policy(
            provider=FakeProvider("provider/model"),
            settings=settings,
        )

        self.assertEqual(policy.first_response_timeout_seconds, 0)
        self.assertIsNone(policy.max_output_tokens)


if __name__ == "__main__":
    unittest.main()
