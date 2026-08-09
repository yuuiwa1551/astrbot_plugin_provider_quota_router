from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from core.config import RouterSettings


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT.parent))
try:
    PLUGIN_MODULE = importlib.import_module(f"{PACKAGE_ROOT.name}.main")
except ModuleNotFoundError as exc:  # pragma: no cover - host without AstrBot
    raise unittest.SkipTest("AstrBot runtime is required") from exc

ProviderQuotaRouterPlugin = PLUGIN_MODULE.ProviderQuotaRouterPlugin
PREFERENCE_KEY = PLUGIN_MODULE.SESSION_PROVIDER_PREFERENCE_KEY


class FakeEvent:
    def __init__(self, *, is_admin: bool = True, origin: str = "default:GroupMessage:123456") -> None:
        self.unified_msg_origin = origin
        self._is_admin = is_admin
        self.stopped = False

    def get_sender_id(self) -> str:
        return "admin" if self._is_admin else "member"

    def is_admin(self) -> bool:
        return self._is_admin

    @staticmethod
    def plain_result(message: str) -> str:
        return message

    def stop_event(self) -> None:
        self.stopped = True


async def collect_replies(plugin: ProviderQuotaRouterPlugin, event: FakeEvent) -> list[str]:
    return [reply async for reply in plugin.quota_command(event, "unpin")]


class QuotaUnpinCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self) -> ProviderQuotaRouterPlugin:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin.settings = RouterSettings(admin_user_ids=set())
        return plugin

    async def test_admin_clears_current_umo_provider_preference(self) -> None:
        plugin = self.make_plugin()
        event = FakeEvent()
        session_get = AsyncMock(return_value="deepseek/deepseek-v4-flash")
        session_remove = AsyncMock()

        with (
            patch.object(PLUGIN_MODULE.sp, "session_get", new=session_get),
            patch.object(PLUGIN_MODULE.sp, "session_remove", new=session_remove),
        ):
            replies = await collect_replies(plugin, event)

        session_get.assert_awaited_once_with(
            event.unified_msg_origin,
            PREFERENCE_KEY,
            None,
        )
        session_remove.assert_awaited_once_with(
            event.unified_msg_origin,
            PREFERENCE_KEY,
        )
        self.assertEqual(len(replies), 1)
        self.assertIn("deepseek/deepseek-v4-flash", replies[0])
        self.assertIn("自动选路", replies[0])

    async def test_unpin_is_idempotent_without_saved_preference(self) -> None:
        plugin = self.make_plugin()
        event = FakeEvent()
        session_remove = AsyncMock()

        with (
            patch.object(
                PLUGIN_MODULE.sp,
                "session_get",
                new=AsyncMock(return_value=None),
            ),
            patch.object(PLUGIN_MODULE.sp, "session_remove", new=session_remove),
        ):
            replies = await collect_replies(plugin, event)

        session_remove.assert_awaited_once_with(
            event.unified_msg_origin,
            PREFERENCE_KEY,
        )
        self.assertEqual(
            replies,
            ["当前会话没有固定对话 Provider，已经在跟随全局配置。"],
        )

    async def test_non_admin_cannot_clear_preference(self) -> None:
        plugin = self.make_plugin()
        event = FakeEvent(is_admin=False)
        session_get = AsyncMock()
        session_remove = AsyncMock()

        with (
            patch.object(PLUGIN_MODULE.sp, "session_get", new=session_get),
            patch.object(PLUGIN_MODULE.sp, "session_remove", new=session_remove),
        ):
            replies = await collect_replies(plugin, event)

        self.assertEqual(replies, ["没有权限执行 quota 管理命令。"])
        session_get.assert_not_awaited()
        session_remove.assert_not_awaited()

    async def test_storage_failure_is_not_reported_as_success(self) -> None:
        plugin = self.make_plugin()
        event = FakeEvent()
        log_error = Mock()

        with (
            patch.object(
                PLUGIN_MODULE.sp,
                "session_get",
                new=AsyncMock(return_value="provider/fixed"),
            ),
            patch.object(
                PLUGIN_MODULE.sp,
                "session_remove",
                new=AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            patch.object(PLUGIN_MODULE.logger, "error", new=log_error),
        ):
            replies = await collect_replies(plugin, event)

        self.assertEqual(
            replies,
            ["取消当前会话的固定对话 Provider 失败，请查看平台日志。"],
        )
        log_error.assert_called_once()

    async def test_literal_slash_command_works_without_slash_wake_prefix(self) -> None:
        plugin = self.make_plugin()
        event = FakeEvent()
        unpin = AsyncMock(return_value="unpin complete")
        plugin._unpin_provider_preference = unpin

        replies = [
            reply async for reply in plugin.quota_unpin_slash_command(event)
        ]

        self.assertEqual(replies, ["unpin complete"])
        unpin.assert_awaited_once_with(event)
        self.assertTrue(event.stopped)

    async def test_literal_slash_command_rejects_non_admin(self) -> None:
        plugin = self.make_plugin()
        event = FakeEvent(is_admin=False)
        unpin = AsyncMock(return_value="must not run")
        plugin._unpin_provider_preference = unpin

        replies = [
            reply async for reply in plugin.quota_unpin_slash_command(event)
        ]

        self.assertEqual(replies, ["没有权限执行 quota 管理命令。"])
        unpin.assert_not_awaited()
        self.assertTrue(event.stopped)


if __name__ == "__main__":
    unittest.main()
