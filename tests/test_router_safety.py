from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

try:
    from astrbot.core.provider.provider import Provider
except ModuleNotFoundError as exc:  # pragma: no cover - host test environment
    raise unittest.SkipTest("AstrBot runtime is required for router integration tests") from exc

from core.config import ChainConfig, RouterSettings
from core.router import ProviderQuotaRouter


class FakeLedger:
    def __init__(self, tokens: int) -> None:
        self.tokens = tokens

    async def query_usage(self, **kwargs) -> int:
        return self.tokens


class MapLedger:
    def __init__(self, tokens: dict[str, int]) -> None:
        self.tokens = tokens

    async def query_usage(self, **kwargs) -> int:
        return self.tokens.get(kwargs["quota_key"], 0)


class FakeState:
    async def usage_overlay(self, **kwargs) -> tuple[int, int]:
        return 0, 0


def make_provider(provider_id: str, modalities: list[str]) -> Provider:
    provider = Mock(spec=Provider)
    provider.get_model.return_value = provider_id
    provider.provider_config = {
        "id": provider_id,
        "model": provider_id,
        "modalities": modalities,
    }
    return provider


class RouterSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_priority_restarts_from_chain_head(self) -> None:
        providers = {
            "provider-a": make_provider("provider-a", ["text"]),
            "provider-b": make_provider("provider-b", ["text"]),
            "provider-c": make_provider("provider-c", ["text"]),
        }
        settings = RouterSettings(
            default_safety_buffer_tokens=0,
            default_request_reservation_tokens=0,
            strict_priority_order=True,
            chains=[ChainConfig(name="test", providers=list(providers))],
        )
        router = ProviderQuotaRouter(
            settings=settings,
            ledger=MapLedger({}),
            state=FakeState(),
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id="provider-c",
            window=SimpleNamespace(window_id="test-window"),
        )

        self.assertEqual(decision.action, "switch")
        self.assertEqual(decision.selected_provider_id, "provider-a")
        self.assertEqual(
            [candidate.provider_id for candidate in decision.candidates],
            ["provider-a"],
        )

    async def test_session_order_can_be_kept_for_compatibility(self) -> None:
        providers = {
            "provider-a": make_provider("provider-a", ["text"]),
            "provider-b": make_provider("provider-b", ["text"]),
        }
        settings = RouterSettings(
            default_safety_buffer_tokens=0,
            default_request_reservation_tokens=0,
            strict_priority_order=False,
            chains=[ChainConfig(name="test", providers=list(providers))],
        )
        router = ProviderQuotaRouter(
            settings=settings,
            ledger=MapLedger({}),
            state=FakeState(),
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id="provider-b",
            window=SimpleNamespace(window_id="test-window"),
        )

        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.selected_provider_id, "provider-b")

    async def test_use_last_blocks_when_modalities_are_unsupported(self) -> None:
        providers = {
            "provider-a": make_provider("provider-a", ["text"]),
            "provider-b": make_provider("provider-b", ["text"]),
        }
        settings = RouterSettings(
            exhausted_action="use_last",
            chains=[ChainConfig(name="test", providers=list(providers))],
        )
        router = ProviderQuotaRouter(
            settings=settings,
            ledger=FakeLedger(0),
            state=FakeState(),
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id="provider-a",
            window=SimpleNamespace(window_id="test-window"),
            required_modalities={"image"},
        )

        self.assertEqual(decision.action, "block")
        self.assertEqual(decision.reason, "chain_unavailable")
        self.assertIsNone(decision.selected_provider_id)

    async def test_use_last_remains_available_for_quota_only_exhaustion(self) -> None:
        providers = {
            "provider-a": make_provider("provider-a", ["text"]),
            "provider-b": make_provider("provider-b", ["text"]),
        }
        settings = RouterSettings(
            default_daily_limit_tokens=100,
            default_safety_buffer_tokens=0,
            default_request_reservation_tokens=0,
            exhausted_action="use_last",
            chains=[ChainConfig(name="test", providers=list(providers))],
        )
        router = ProviderQuotaRouter(
            settings=settings,
            ledger=FakeLedger(100),
            state=FakeState(),
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id="provider-a",
            window=SimpleNamespace(window_id="test-window"),
        )

        self.assertEqual(decision.action, "use_last")
        self.assertEqual(decision.reason, "chain_exhausted_use_last")
        self.assertEqual(decision.selected_provider_id, "provider-b")


if __name__ == "__main__":
    unittest.main()
