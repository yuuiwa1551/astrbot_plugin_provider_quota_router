from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.core_fallback_guard import (
    CORE_FALLBACK_APPLIED_EXTRA_KEY,
    CORE_FALLBACK_DROPPED_EXTRA_KEY,
    CORE_FALLBACK_GUARD_EXTRA_KEY,
    CORE_FALLBACK_SAFE_PROVIDERS_EXTRA_KEY,
    CORE_REQUEST_MAX_RETRIES_EXTRA_KEY,
    install_core_fallback_guard,
    is_core_fallback_guard_installed,
    uninstall_core_fallback_guard,
)


class FakeEvent:
    def __init__(self, guarded: bool) -> None:
        self.extras = {CORE_FALLBACK_GUARD_EXTRA_KEY: guarded}

    def get_extra(self, key: str):
        return self.extras.get(key)

    def set_extra(self, key: str, value) -> None:
        self.extras[key] = value


class FakeRunner:
    async def reset(self, *args, **kwargs) -> None:
        self.received_fallbacks = kwargs.get("fallback_providers")
        self.request_max_retries = kwargs.get("request_max_retries")


def fake_provider(provider_id: str):
    return SimpleNamespace(provider_config={"id": provider_id})


class CoreFallbackGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.owner = object()
        self.original_reset = FakeRunner.reset
        install_core_fallback_guard(self.owner, FakeRunner)

    async def asyncTearDown(self) -> None:
        uninstall_core_fallback_guard(self.owner, FakeRunner)

    async def test_guard_replaces_fallbacks_with_safe_order_and_one_retry(
        self,
    ) -> None:
        event = FakeEvent(True)
        event.set_extra(
            CORE_FALLBACK_SAFE_PROVIDERS_EXTRA_KEY,
            [fake_provider("provider-c"), fake_provider("provider-b")],
        )
        event.set_extra(CORE_REQUEST_MAX_RETRIES_EXTRA_KEY, 1)
        run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        runner = FakeRunner()

        await runner.reset(
            None,
            None,
            run_context,
            None,
            None,
            fallback_providers=[
                fake_provider("provider-b"),
                fake_provider("provider-unsafe"),
            ],
            request_max_retries=5,
        )

        self.assertEqual(
            [item.provider_config["id"] for item in runner.received_fallbacks],
            ["provider-c", "provider-b"],
        )
        self.assertEqual(runner.request_max_retries, 1)
        self.assertEqual(
            event.get_extra(CORE_FALLBACK_APPLIED_EXTRA_KEY),
            ["provider-c", "provider-b"],
        )
        self.assertEqual(
            event.get_extra(CORE_FALLBACK_DROPPED_EXTRA_KEY),
            ["provider-unsafe"],
        )

    async def test_guard_preserves_fallbacks_for_unmarked_event(self) -> None:
        event = FakeEvent(False)
        run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        runner = FakeRunner()
        fallbacks = [fake_provider("provider-b")]

        await runner.reset(
            None,
            None,
            run_context,
            None,
            None,
            fallback_providers=fallbacks,
        )

        self.assertIs(runner.received_fallbacks, fallbacks)

    async def test_uninstall_restores_original_reset(self) -> None:
        self.assertTrue(is_core_fallback_guard_installed(FakeRunner))
        uninstall_core_fallback_guard(self.owner, FakeRunner)
        self.assertIs(FakeRunner.reset, self.original_reset)
        self.owner = object()


if __name__ == "__main__":
    unittest.main()
