from __future__ import annotations

import unittest
import time
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
    def __init__(self) -> None:
        self.cooldowns: dict[str, dict] = {}
        self.provider_group_circuits: dict[str, dict] = {}

    async def usage_overlay(self, **kwargs) -> tuple[int, int]:
        return 0, 0

    async def get_cooldown(self, *, quota_key: str):
        item = self.cooldowns.get(quota_key)
        return dict(item) if item else None

    async def start_cooldown(self, **kwargs):
        existing = self.cooldowns.get(kwargs["quota_key"])
        if existing and existing.get("window_id") == kwargs["window_id"]:
            return dict(existing)
        now = time.time()
        item = {
            **kwargs,
            "started_at": now,
            "expires_at": now + kwargs["ttl_seconds"],
        }
        self.cooldowns[kwargs["quota_key"]] = item
        return dict(item)

    async def clear_cooldown(self, *, quota_key: str) -> None:
        self.cooldowns.pop(quota_key, None)

    async def get_provider_group_circuit(self, *, group_id: str):
        item = self.provider_group_circuits.get(group_id)
        return dict(item) if item else None


def make_provider(
    provider_id: str, modalities: list[str], source_id: str = ""
) -> Provider:
    provider = Mock(spec=Provider)
    provider.get_model.return_value = provider_id
    provider.provider_config = {
        "id": provider_id,
        "model": provider_id,
        "modalities": modalities,
        "provider_source_id": source_id,
    }
    return provider


class RouterSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_volcengine_group_circuit_skips_all_fire_models(self) -> None:
        providers = {
            "openai/doubao-a": make_provider(
                "openai/doubao-a", ["text"], "openai"
            ),
            "openai/doubao-b": make_provider(
                "openai/doubao-b", ["text"], "openai"
            ),
            "opencode-zen/mimo": make_provider(
                "opencode-zen/mimo", ["text", "image"], "opencode-zen"
            ),
        }
        state = FakeState()
        state.provider_group_circuits["volcengine"] = {
            "group_id": "volcengine",
            "status": "open",
            "started_at": time.time(),
            "retry_at": time.time() + 1_800,
        }
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                default_safety_buffer_tokens=0,
                default_request_reservation_tokens=0,
                chains=[ChainConfig(name="test", providers=list(providers))],
            ),
            ledger=MapLedger({}),
            state=state,
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id="openai/doubao-a",
            window=SimpleNamespace(window_id="window-a"),
        )

        self.assertEqual(decision.selected_provider_id, "opencode-zen/mimo")
        self.assertEqual(
            [candidate.reason for candidate in decision.candidates[:2]],
            ["provider_group_cooldown", "provider_group_cooldown"],
        )

    async def test_probe_candidates_only_include_quota_safe_fire_models(self) -> None:
        providers = {
            "openai/doubao-full": make_provider(
                "openai/doubao-full", ["text"], "openai"
            ),
            "openai/doubao-safe": make_provider(
                "openai/doubao-safe", ["text"], "openai"
            ),
            "opencode-zen/mimo": make_provider(
                "opencode-zen/mimo", ["text"], "opencode-zen"
            ),
        }
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                default_daily_limit_tokens=100,
                default_safety_buffer_tokens=10,
                default_request_reservation_tokens=0,
                chains=[ChainConfig(name="test", providers=list(providers))],
            ),
            ledger=MapLedger(
                {
                    "openai/doubao-full": 90,
                    "openai/doubao-safe": 20,
                }
            ),
            state=FakeState(),
            get_provider=providers.get,
        )

        candidates = await router.volcengine_probe_candidate_ids(
            window=SimpleNamespace(window_id="window-a")
        )

        self.assertEqual(candidates, ["openai/doubao-safe"])

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

    async def test_deepseek_prefix_is_unlimited_and_not_reserved(self) -> None:
        providers = {
            "openai/doubao": make_provider("openai/doubao", ["text"]),
            "deepseek/deepseek-v4-flash": make_provider(
                "deepseek/deepseek-v4-flash", ["text"]
            ),
        }
        state = FakeState()
        settings = RouterSettings(
            default_daily_limit_tokens=100,
            default_safety_buffer_tokens=0,
            default_request_reservation_tokens=0,
            chains=[ChainConfig(name="test", providers=list(providers))],
        )
        router = ProviderQuotaRouter(
            settings=settings,
            ledger=MapLedger(
                {
                    "openai/doubao": 100,
                    "deepseek/deepseek-v4-flash": 9_999_999,
                }
            ),
            state=state,
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id="openai/doubao",
            window=SimpleNamespace(window_id="window-a"),
        )

        self.assertEqual(decision.action, "switch")
        self.assertEqual(decision.selected_provider_id, "deepseek/deepseek-v4-flash")
        self.assertEqual(decision.reason, "unlimited")
        self.assertFalse(decision.should_reserve)
        self.assertIn("openai/doubao", state.cooldowns)

    async def test_cooldown_survives_new_window_until_24_hours_end(self) -> None:
        providers = {
            "openai/doubao": make_provider("openai/doubao", ["text"]),
            "deepseek/deepseek-v4-flash": make_provider(
                "deepseek/deepseek-v4-flash", ["text"]
            ),
        }
        state = FakeState()
        state.cooldowns["openai/doubao"] = {
            "window_id": "window-a",
            "quota_key": "openai/doubao",
            "started_at": time.time() - 20 * 3600,
            "expires_at": time.time() + 4 * 3600,
        }
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                default_daily_limit_tokens=100,
                default_safety_buffer_tokens=0,
                default_request_reservation_tokens=0,
                chains=[ChainConfig(name="test", providers=list(providers))],
            ),
            ledger=MapLedger({}),
            state=state,
            get_provider=providers.get,
        )

        cooling = await router.decide(
            current_provider_id="openai/doubao",
            window=SimpleNamespace(window_id="window-b"),
        )
        self.assertEqual(cooling.selected_provider_id, "deepseek/deepseek-v4-flash")
        self.assertEqual(cooling.candidates[0].reason, "cooldown_active")

        state.cooldowns["openai/doubao"]["expires_at"] = time.time() - 1
        recovered = await router.decide(
            current_provider_id="deepseek/deepseek-v4-flash",
            window=SimpleNamespace(window_id="window-b"),
        )
        self.assertEqual(recovered.selected_provider_id, "openai/doubao")
        self.assertEqual(recovered.reason, "ok")
        self.assertNotIn("openai/doubao", state.cooldowns)

    async def test_reconcile_starts_cooldown_without_a_followup_request(self) -> None:
        providers = {
            "openai/doubao": make_provider("openai/doubao", ["text"]),
            "deepseek/deepseek-v4-flash": make_provider(
                "deepseek/deepseek-v4-flash", ["text"]
            ),
        }
        state = FakeState()
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                default_daily_limit_tokens=100,
                default_safety_buffer_tokens=10,
                default_request_reservation_tokens=0,
                chains=[ChainConfig(name="test", providers=list(providers))],
            ),
            ledger=MapLedger({"openai/doubao": 90}),
            state=state,
            get_provider=providers.get,
        )

        checked_count, cooldown_count = await router.reconcile_cooldowns(
            window=SimpleNamespace(window_id="window-a")
        )

        self.assertEqual(checked_count, 1)
        self.assertEqual(cooldown_count, 1)
        self.assertIn("openai/doubao", state.cooldowns)
        self.assertNotIn("deepseek/deepseek-v4-flash", state.cooldowns)


if __name__ == "__main__":
    unittest.main()
