from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import RouterSettings, is_quota_only_exhaustion
from core.fallback_config import (
    ConfigChangedDuringRead,
    ConfigFileSignature,
    build_astrbot_fallback_chain,
    file_signature,
    load_astrbot_fallback_chain,
)


class FallbackConfigTests(unittest.TestCase):
    def test_volcengine_safety_defaults_exempt_deepseek(self) -> None:
        settings = RouterSettings.from_raw({})

        self.assertEqual(settings.reset_time, "11:00")
        self.assertEqual(settings.quota_cooldown_seconds, 86_400)
        self.assertTrue(settings.is_unlimited_provider("deepseek/deepseek-v4-pro"))
        self.assertFalse(
            settings.is_unlimited_provider("openai/deepseek-v4-pro-260425")
        )
        self.assertTrue(settings.volcengine_403_circuit_enabled)
        self.assertTrue(settings.is_volcengine_source("openai"))
        self.assertEqual(settings.volcengine_403_cooldown_seconds, 1_800)
        self.assertEqual(settings.volcengine_probe_check_interval_seconds, 30)
        self.assertTrue(settings.provider_error_admin_notify_enabled)
        self.assertEqual(
            settings.provider_error_admin_notify_interval_seconds, 3_600
        )
        self.assertTrue(settings.provider_error_suppress_current_chat)

    def test_build_chain_deduplicates_default_provider(self) -> None:
        chain = build_astrbot_fallback_chain(
            {
                "default_provider_id": "provider-a",
                "fallback_chat_models": ["provider-a", "provider-b", "provider-b"],
            }
        )

        self.assertIsNotNone(chain)
        self.assertEqual(chain.providers, ["provider-a", "provider-b"])

    def test_load_chain_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cmd_config.json"
            payload = {
                "provider_settings": {
                    "default_provider_id": "provider-a",
                    "fallback_chat_models": ["provider-b"],
                }
            }
            path.write_text(json.dumps(payload), encoding="utf-8-sig")

            chain, signature = load_astrbot_fallback_chain(path)

            self.assertEqual(chain.providers, ["provider-a", "provider-b"])
            self.assertEqual(signature, file_signature(path))

    def test_load_chain_rejects_invalid_fallback_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cmd_config.json"
            path.write_text(
                json.dumps(
                    {
                        "provider_settings": {
                            "default_provider_id": "provider-a",
                            "fallback_chat_models": "provider-b",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must be a list"):
                load_astrbot_fallback_chain(path)

    def test_load_chain_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cmd_config.json"
            path.write_text('{"provider_settings":', encoding="utf-8")

            with self.assertRaises(json.JSONDecodeError):
                load_astrbot_fallback_chain(path)

    def test_load_chain_rejects_file_changed_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cmd_config.json"
            path.write_text(
                json.dumps(
                    {
                        "provider_settings": {
                            "default_provider_id": "provider-a",
                            "fallback_chat_models": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            first = ConfigFileSignature(mtime_ns=1, size=10)
            second = ConfigFileSignature(mtime_ns=2, size=10)

            with patch(
                "core.fallback_config.file_signature",
                side_effect=[first, second],
            ):
                with self.assertRaises(ConfigChangedDuringRead):
                    load_astrbot_fallback_chain(path)

    def test_watch_interval_has_one_second_minimum(self) -> None:
        settings = RouterSettings.from_raw({"fallback_watch_interval_seconds": 0})
        self.assertEqual(settings.fallback_watch_interval_seconds, 1)

    def test_cost_safety_defaults(self) -> None:
        settings = RouterSettings.from_raw({})
        self.assertEqual(settings.fallback_watch_interval_seconds, 300)
        self.assertTrue(settings.strict_priority_order)
        self.assertFalse(settings.disable_astrbot_error_fallback)

    def test_use_last_is_only_safe_for_quota_exhaustion(self) -> None:
        self.assertTrue(
            is_quota_only_exhaustion(["quota_exceeded", "quota_exceeded"])
        )
        self.assertFalse(
            is_quota_only_exhaustion(
                ["modality_not_supported", "quota_exceeded"]
            )
        )
        self.assertFalse(is_quota_only_exhaustion([]))


if __name__ == "__main__":
    unittest.main()
