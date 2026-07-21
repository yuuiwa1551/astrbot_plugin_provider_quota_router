from __future__ import annotations

import unittest

from core.config import RouterSettings


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


if __name__ == "__main__":
    unittest.main()
