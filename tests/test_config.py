from __future__ import annotations

import unittest

from core.config import RouterSettings
from core.config import ChainConfig


class RouterSettingsTests(unittest.TestCase):
    def test_provider_error_cooldown_defaults_to_thirty_minutes(self) -> None:
        settings = RouterSettings.from_raw({})

        self.assertTrue(settings.provider_error_cooldown_enabled)
        self.assertEqual(settings.provider_error_cooldown_seconds, 1_800)
        self.assertEqual(settings.provider_error_request_max_retries, 1)

    def test_provider_error_cooldown_can_be_configured(self) -> None:
        settings = RouterSettings.from_raw(
            {
                "provider_error_cooldown_enabled": False,
                "provider_error_cooldown_seconds": 900,
                "provider_error_request_max_retries": 2,
            }
        )

        self.assertFalse(settings.provider_error_cooldown_enabled)
        self.assertEqual(settings.provider_error_cooldown_seconds, 900)
        self.assertEqual(settings.provider_error_request_max_retries, 2)

    def test_explicit_zero_chain_limit_is_not_replaced_by_default(self) -> None:
        chain = ChainConfig(name="disabled", providers=["provider"], daily_limit_tokens=0)

        self.assertEqual(chain.limit(2_000_000), 0)


if __name__ == "__main__":
    unittest.main()
