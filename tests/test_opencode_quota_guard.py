from __future__ import annotations

import unittest

from core.opencode_quota_guard import (
    OpenCodeQuotaCooldownError,
    install_opencode_quota_guard,
    is_opencode_quota_guard_installed,
    uninstall_opencode_quota_guard,
)


class FakeProvider:
    def __init__(self) -> None:
        self.provider_config = {
            "id": "opencode-zen/mimo-v2.5-free",
            "model": "mimo-v2.5-free",
        }
        self.calls = 0

    async def text_chat(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("FreeUsageLimitError: Rate limit exceeded")

    async def text_chat_stream(self, *args, **kwargs):
        self.calls += 1
        yield "chunk"


class FakeOwner:
    def __init__(self) -> None:
        self.cooldown = None
        self.errors = []

    async def opencode_quota_guard_cooldown(self, provider):
        return self.cooldown

    async def opencode_quota_guard_error(self, provider, exc):
        self.errors.append((provider, exc))


class OpenCodeQuotaGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.owner = FakeOwner()
        self.original_text_chat = FakeProvider.text_chat
        install_opencode_quota_guard(self.owner, FakeProvider)

    async def asyncTearDown(self) -> None:
        uninstall_opencode_quota_guard(self.owner, FakeProvider)

    async def test_reports_provider_error_and_preserves_original_exception(self) -> None:
        provider = FakeProvider()

        with self.assertRaisesRegex(RuntimeError, "FreeUsageLimitError"):
            await provider.text_chat(prompt="test")

        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(self.owner.errors), 1)

    async def test_active_cooldown_blocks_before_external_call(self) -> None:
        provider = FakeProvider()
        self.owner.cooldown = {"expires_at": 1_900_000_000.0}

        with self.assertRaises(OpenCodeQuotaCooldownError):
            await provider.text_chat(prompt="test")

        self.assertEqual(provider.calls, 0)

    async def test_uninstall_restores_original_methods(self) -> None:
        self.assertTrue(is_opencode_quota_guard_installed(FakeProvider))
        uninstall_opencode_quota_guard(self.owner, FakeProvider)
        self.assertIs(FakeProvider.text_chat, self.original_text_chat)
        self.owner = FakeOwner()


if __name__ == "__main__":
    unittest.main()
