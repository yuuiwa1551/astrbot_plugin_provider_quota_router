from __future__ import annotations

import unittest

from core.config import ChainConfig, RouterSettings


class RouterSettingsTests(unittest.TestCase):
    def test_provider_error_cooldown_defaults_to_thirty_minutes(self) -> None:
        settings = RouterSettings.from_raw({})

        self.assertTrue(settings.provider_error_cooldown_enabled)
        self.assertEqual(settings.provider_error_cooldown_seconds, 1_800)
        self.assertEqual(settings.provider_error_request_max_retries, 1)
        self.assertEqual(settings.provider_error_fallback_max_candidates, 1)
        self.assertEqual(
            settings.provider_attempt_timeout_failure_threshold,
            2,
        )
        self.assertEqual(
            settings.provider_attempt_timeout_failure_window_seconds,
            300,
        )
        self.assertEqual(
            settings.provider_attempt_timeout_cooldown_seconds,
            300,
        )
        self.assertEqual(settings.provider_policy_overrides, ())
        self.assertFalse(settings.route_affinity_enabled)
        self.assertEqual(settings.route_affinity_ttl_seconds, 3_600)

    def test_provider_error_cooldown_can_be_configured(self) -> None:
        settings = RouterSettings.from_raw(
            {
                "provider_error_cooldown_enabled": False,
                "provider_error_cooldown_seconds": 900,
                "provider_error_request_max_retries": 2,
                "provider_error_fallback_max_candidates": 0,
                "provider_attempt_timeout_failure_threshold": 3,
                "provider_attempt_timeout_failure_window_seconds": 600,
                "provider_attempt_timeout_cooldown_seconds": 120,
            }
        )

        self.assertFalse(settings.provider_error_cooldown_enabled)
        self.assertEqual(settings.provider_error_cooldown_seconds, 900)
        self.assertEqual(settings.provider_error_request_max_retries, 2)
        self.assertEqual(settings.provider_error_fallback_max_candidates, 0)
        self.assertEqual(
            settings.provider_attempt_timeout_failure_threshold,
            3,
        )
        self.assertEqual(
            settings.provider_attempt_timeout_failure_window_seconds,
            600,
        )
        self.assertEqual(
            settings.provider_attempt_timeout_cooldown_seconds,
            120,
        )

    def test_route_affinity_can_be_enabled_with_minimum_ttl(self) -> None:
        settings = RouterSettings.from_raw(
            {
                "route_affinity_enabled": True,
                "route_affinity_ttl_seconds": 1,
            }
        )

        self.assertTrue(settings.route_affinity_enabled)
        self.assertEqual(settings.route_affinity_ttl_seconds, 60)

    def test_explicit_zero_chain_limit_is_not_replaced_by_default(self) -> None:
        chain = ChainConfig(name="disabled", providers=["provider"], daily_limit_tokens=0)

        self.assertEqual(chain.limit(2_000_000), 0)

    def test_provider_policy_overrides_are_parsed_by_exact_provider_id(
        self,
    ) -> None:
        settings = RouterSettings.from_raw(
            {
                "provider_policy_overrides_json": """
                [
                  {
                    "provider_id": "volcengine-agent-plan/doubao-seed-2.0-mini",
                    "first_response_timeout_seconds": 3,
                    "request_max_retries": 1,
                    "max_output_tokens": 220
                  }
                ]
                """
            }
        )

        override = settings.provider_policy_override(
            "VOLCENGINE-AGENT-PLAN/DOUBAO-SEED-2.0-MINI"
        )
        self.assertIsNotNone(override)
        assert override is not None
        self.assertEqual(override.first_response_timeout_seconds, 3)
        self.assertEqual(override.request_max_retries, 1)
        self.assertEqual(override.max_output_tokens, 220)
        self.assertIsNone(settings.provider_policy_override("another/provider"))

    def test_provider_policy_override_object_and_boundaries(self) -> None:
        settings = RouterSettings.from_raw(
            {
                "provider_policy_overrides": {
                    "provider/model": {
                        "provider_id": "ignored/provider",
                        "first_response_timeout_seconds": -3,
                        "request_max_retries": 0,
                        "max_output_tokens": -1,
                    }
                }
            }
        )

        override = settings.provider_policy_override("provider/model")
        self.assertIsNotNone(override)
        assert override is not None
        self.assertEqual(override.first_response_timeout_seconds, 0)
        self.assertEqual(override.request_max_retries, 1)
        self.assertEqual(override.max_output_tokens, 0)
        self.assertIsNone(
            settings.provider_policy_override("ignored/provider")
        )

    def test_duplicate_provider_policy_override_uses_last_entry(self) -> None:
        settings = RouterSettings.from_raw(
            {
                "provider_policy_overrides": [
                    {"provider_id": "provider/model", "max_output_tokens": 100},
                    {"provider_id": "PROVIDER/MODEL", "max_output_tokens": 50},
                ]
            }
        )

        self.assertEqual(len(settings.provider_policy_overrides), 1)
        override = settings.provider_policy_override("provider/model")
        self.assertIsNotNone(override)
        assert override is not None
        self.assertEqual(override.provider_id, "PROVIDER/MODEL")
        self.assertEqual(override.max_output_tokens, 50)

    def test_invalid_provider_policy_override_json_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            RouterSettings.from_raw(
                {"provider_policy_overrides_json": "[{invalid]"}
            )


if __name__ == "__main__":
    unittest.main()
