from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.admin_alerts import build_provider_error_alert, resolve_admin_targets


class FakeContext:
    def get_config(self):
        return {"admins_id": ["10001", "10002", "10001"]}


class AdminAlertTests(unittest.TestCase):
    def test_uses_astrbot_admins_and_skips_current_origin(self) -> None:
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:10001",
            get_platform_name=lambda: "aiocqhttp",
        )

        targets = resolve_admin_targets(
            context=FakeContext(),
            event=event,
            configured_admin_ids=[],
        )

        self.assertEqual(targets, ["aiocqhttp:FriendMessage:10002"])

    def test_configured_full_origin_is_preserved(self) -> None:
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:20001",
            get_platform_name=lambda: "aiocqhttp",
        )

        targets = resolve_admin_targets(
            context=FakeContext(),
            event=event,
            configured_admin_ids=["telegram:FriendMessage:42"],
        )

        self.assertEqual(targets, ["telegram:FriendMessage:42"])

    def test_alert_mentions_circuit_and_hourly_throttle(self) -> None:
        text = build_provider_error_alert(
            provider_id="openai/doubao",
            error_text="Error code: 403 AccountOverdueError",
            source_origin="aiocqhttp:GroupMessage:123",
            circuit_retry_at=1_800_000_000,
            interval_seconds=3_600,
        )

        self.assertIn("火山模型组已进入 30 分钟冷却", text)
        self.assertIn("60 分钟内不重复发送", text)


if __name__ == "__main__":
    unittest.main()
