from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from core.config import RouterSettings


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT.parent))
try:
    PLUGIN_MODULE = importlib.import_module(f"{PACKAGE_ROOT.name}.main")
except ModuleNotFoundError as exc:  # pragma: no cover - host without AstrBot
    raise unittest.SkipTest("AstrBot runtime is required") from exc
ProviderQuotaRouterPlugin = PLUGIN_MODULE.ProviderQuotaRouterPlugin


class FakeEvent:
    def __init__(self, sender_id: str, core_admin: bool) -> None:
        self.sender_id = sender_id
        self.core_admin = core_admin

    def get_sender_id(self) -> str:
        return self.sender_id

    def is_admin(self) -> bool:
        return self.core_admin


class AdminAuthTests(unittest.TestCase):
    def test_empty_plugin_admin_list_uses_core_permission(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin.settings = RouterSettings(admin_user_ids=set())

        self.assertFalse(plugin._is_admin(FakeEvent("member", False)))
        self.assertTrue(plugin._is_admin(FakeEvent("core-admin", True)))

    def test_explicit_plugin_admin_is_additive(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin.settings = RouterSettings(admin_user_ids={"plugin-admin"})

        self.assertTrue(plugin._is_admin(FakeEvent("plugin-admin", False)))


if __name__ == "__main__":
    unittest.main()
