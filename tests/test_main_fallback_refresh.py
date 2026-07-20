from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from core.config import ChainConfig, RouterSettings
from core.fallback_config import file_signature


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT.parent))
PLUGIN_MODULE = importlib.import_module(f"{PACKAGE_ROOT.name}.main")
ProviderQuotaRouterPlugin = PLUGIN_MODULE.ProviderQuotaRouterPlugin


class MainFallbackRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_fallback_is_applied_before_next_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cmd_config.json"
            path.write_text(
                json.dumps(
                    {
                        "provider_settings": {
                            "default_provider_id": "provider-a",
                            "fallback_chat_models": ["provider-b"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            plugin = object.__new__(ProviderQuotaRouterPlugin)
            plugin._fallback_chain_is_dynamic = True
            plugin._fallback_reload_lock = asyncio.Lock()
            plugin._cmd_config_path = path
            plugin._fallback_config_signature = file_signature(path)
            plugin._fallback_chain_source = "cmd_config"
            plugin._fallback_last_reload_at = None
            plugin._fallback_last_error = None
            plugin.settings = RouterSettings(
                chains=[ChainConfig(name="astrbot-default", providers=["provider-a", "provider-b"])]
            )
            old_router = object()
            new_router = object()
            plugin.router = old_router
            plugin._build_router = lambda: new_router

            path.write_text(
                json.dumps(
                    {
                        "provider_settings": {
                            "default_provider_id": "provider-a",
                            "fallback_chat_models": ["provider-c"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            changed = await plugin._refresh_fallback_config_if_changed()

            self.assertTrue(changed)
            self.assertEqual(
                plugin.settings.chains[0].providers,
                ["provider-a", "provider-c"],
            )
            self.assertIs(plugin.router, new_router)
            self.assertIsNotNone(plugin._fallback_last_reload_at)
            expected_signature = file_signature(path)
            self.assertEqual(
                (
                    plugin._fallback_config_signature.mtime_ns,
                    plugin._fallback_config_signature.size,
                ),
                (expected_signature.mtime_ns, expected_signature.size),
            )

    async def test_unchanged_signature_does_not_rebuild_router(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cmd_config.json"
            path.write_text(
                json.dumps(
                    {
                        "provider_settings": {
                            "default_provider_id": "provider-a",
                            "fallback_chat_models": ["provider-b"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            plugin = object.__new__(ProviderQuotaRouterPlugin)
            plugin._fallback_chain_is_dynamic = True
            plugin._fallback_reload_lock = asyncio.Lock()
            plugin._cmd_config_path = path
            plugin._fallback_config_signature = file_signature(path)
            plugin._fallback_chain_source = "cmd_config"
            plugin._fallback_last_reload_at = None
            plugin._fallback_last_error = None
            plugin.settings = RouterSettings(
                chains=[ChainConfig(name="astrbot-default", providers=["provider-a", "provider-b"])]
            )
            router = object()
            plugin.router = router
            plugin._build_router = lambda: self.fail("router should not be rebuilt")

            changed = await plugin._refresh_fallback_config_if_changed()

            self.assertFalse(changed)
            self.assertIs(plugin.router, router)


if __name__ == "__main__":
    unittest.main()
