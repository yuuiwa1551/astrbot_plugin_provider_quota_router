from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT.parent))
try:
    PLUGIN_MODULE = importlib.import_module(f"{PACKAGE_ROOT.name}.main")
except ModuleNotFoundError as exc:  # pragma: no cover - host without AstrBot
    raise unittest.SkipTest("AstrBot runtime is required") from exc

ProviderQuotaRouterPlugin = PLUGIN_MODULE.ProviderQuotaRouterPlugin


class FakeEvent:
    def __init__(self, extras: dict | None = None) -> None:
        self.unified_msg_origin = "default:GroupMessage:123456"
        self.extras = dict(extras or {})

    def get_extra(self, key: str):
        return self.extras.get(key)


def make_provider(provider_id: str):
    return SimpleNamespace(meta=lambda: SimpleNamespace(id=provider_id))


class MainProviderSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_selected_provider_is_explicit(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin.context = SimpleNamespace()
        event = FakeEvent({"selected_provider": "deepseek/request"})

        with patch.object(
            PLUGIN_MODULE.sp,
            "session_get",
            new=AsyncMock(),
        ) as session_get:
            selection = await plugin._provider_selection(event)

        self.assertEqual(selection.provider_id, "deepseek/request")
        self.assertEqual(selection.origin, "request")
        self.assertTrue(selection.is_explicit)
        session_get.assert_not_awaited()

    async def test_umo_provider_preference_is_explicit(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        fallback_getter = Mock(return_value=make_provider("provider/default"))
        plugin.context = SimpleNamespace(
            get_provider_by_id=lambda provider_id: (
                make_provider(provider_id)
                if provider_id == "deepseek/deepseek-v4-flash"
                else None
            ),
            get_using_provider=fallback_getter,
        )
        event = FakeEvent()

        with patch.object(
            PLUGIN_MODULE.sp,
            "session_get",
            new=AsyncMock(return_value="deepseek/deepseek-v4-flash"),
        ):
            selection = await plugin._provider_selection(event)

        self.assertEqual(selection.provider_id, "deepseek/deepseek-v4-flash")
        self.assertEqual(selection.origin, "umo")
        self.assertTrue(selection.is_explicit)
        fallback_getter.assert_not_called()

    async def test_default_provider_keeps_global_priority_semantics(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin.context = SimpleNamespace(
            get_provider_by_id=lambda _provider_id: None,
            get_using_provider=Mock(return_value=make_provider("provider/default")),
        )
        event = FakeEvent()

        with patch.object(
            PLUGIN_MODULE.sp,
            "session_get",
            new=AsyncMock(return_value=None),
        ):
            selection = await plugin._provider_selection(event)

        self.assertEqual(selection.provider_id, "provider/default")
        self.assertEqual(selection.origin, "default")
        self.assertFalse(selection.is_explicit)


if __name__ == "__main__":
    unittest.main()
