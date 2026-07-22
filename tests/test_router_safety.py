from __future__ import annotations

import asyncio
import tempfile
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

try:
    from astrbot.core.provider.provider import Provider
except ModuleNotFoundError as exc:  # pragma: no cover - host test environment
    raise unittest.SkipTest("AstrBot runtime is required for router integration tests") from exc

from core.config import ChainConfig, RouterSettings
from core.router import ProviderQuotaRouter
from core.state import QuotaStateStore


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


class CapturingLedger:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def query_usage(self, **kwargs) -> int:
        self.calls.append(kwargs)
        return 0


class FakeState:
    def __init__(self) -> None:
        self.cooldowns: dict[str, dict] = {}
        self.provider_model_circuits: dict[str, dict] = {}
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

    async def get_provider_model_circuit(self, *, provider_id: str):
        item = self.provider_model_circuits.get(provider_id)
        if item and float(item.get("retry_at") or 0) > time.time():
            return dict(item)
        self.provider_model_circuits.pop(provider_id, None)
        return None

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
    async def test_concurrent_decisions_reserve_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            provider_id = "openai/local"
            providers = {
                provider_id: make_provider(provider_id, ["text"], "openai")
            }
            router = ProviderQuotaRouter(
                settings=RouterSettings(
                    default_daily_limit_tokens=100,
                    default_safety_buffer_tokens=0,
                    default_request_reservation_tokens=60,
                    chains=[
                        ChainConfig(name="test", providers=[provider_id])
                    ],
                ),
                ledger=FakeLedger(0),
                state=QuotaStateStore(Path(temp_dir)),
                get_provider=providers.get,
            )

            decisions = await asyncio.gather(
                router.decide_and_reserve(
                    request_id="request-a",
                    current_provider_id=provider_id,
                    window=SimpleNamespace(window_id="window-a"),
                ),
                router.decide_and_reserve(
                    request_id="request-b",
                    current_provider_id=provider_id,
                    window=SimpleNamespace(window_id="window-a"),
                ),
            )

            self.assertEqual(
                sorted(decision.action for decision in decisions),
                ["allow", "block"],
            )

    async def test_model_quota_query_is_scoped_to_local_plan_providers(self) -> None:
        local_a = make_provider("openai/local-a", ["text"], "openai")
        local_b = make_provider("openai/local-b", ["text"], "openai")
        paid = make_provider(
            "volcengine-agent-plan/paid",
            ["text"],
            "volcengine-agent-plan",
        )
        for provider in (local_a, local_b, paid):
            provider.get_model.return_value = "same-model"
            provider.provider_config["model"] = "same-model"
        providers = {
            provider.provider_config["id"]: provider
            for provider in (local_a, local_b, paid)
        }
        ledger = CapturingLedger()
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                default_safety_buffer_tokens=0,
                default_request_reservation_tokens=0,
                chains=[
                    ChainConfig(
                        name="test",
                        providers=list(providers),
                    )
                ],
            ),
            ledger=ledger,
            state=FakeState(),
            get_provider=providers.get,
            get_all_providers=lambda: list(providers.values()),
        )

        await router.decide(
            current_provider_id="openai/local-a",
            window=SimpleNamespace(window_id="window-a"),
        )

        self.assertEqual(
            ledger.calls[0]["provider_ids"],
            ("openai/local-a", "openai/local-b"),
        )
        self.assertNotIn(
            "volcengine-agent-plan/paid",
            ledger.calls[0]["provider_ids"],
        )

    async def test_provider_error_cooldown_only_skips_the_failed_model(self) -> None:
        providers = {
            "relay/model-a": make_provider("relay/model-a", ["text"], "relay"),
            "relay/model-b": make_provider("relay/model-b", ["text"], "relay"),
        }
        state = FakeState()
        state.provider_model_circuits["relay/model-a"] = {
            "provider_id": "relay/model-a",
            "provider_model": "model-a",
            "started_at": time.time(),
            "retry_at": time.time() + 1_800,
            "last_error": "HTTP 429",
        }
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                chains=[ChainConfig(name="test", providers=list(providers))],
            ),
            ledger=MapLedger({}),
            state=state,
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id="relay/model-a",
            window=SimpleNamespace(window_id="window-a"),
        )

        self.assertEqual(decision.selected_provider_id, "relay/model-b")
        self.assertEqual(
            decision.candidates[0].reason, "provider_error_cooldown"
        )
        self.assertEqual(len(state.provider_model_circuits), 1)

    async def test_safe_fallback_ids_skip_cooling_and_wrong_modality(self) -> None:
        providers = {
            "relay/model-a": make_provider("relay/model-a", ["text"], "relay"),
            "relay/model-b": make_provider("relay/model-b", ["text"], "relay"),
            "relay/model-c": make_provider("relay/model-c", ["text"], "relay"),
            "relay/model-d": make_provider(
                "relay/model-d", ["text", "image"], "relay"
            ),
        }
        state = FakeState()
        state.provider_model_circuits["relay/model-b"] = {
            "provider_id": "relay/model-b",
            "provider_model": "model-b",
            "started_at": time.time(),
            "retry_at": time.time() + 1_800,
            "last_error": "HTTP 429",
        }
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                chains=[ChainConfig(name="test", providers=list(providers))],
            ),
            ledger=MapLedger({}),
            state=state,
            get_provider=providers.get,
        )

        fallback_ids = await router.eligible_fallback_provider_ids(
            selected_provider_id="relay/model-a",
            window=SimpleNamespace(window_id="window-a"),
            required_modalities={"image"},
        )

        self.assertEqual(fallback_ids, ["relay/model-d"])

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

    async def test_probe_candidates_can_come_from_source_outside_chain(self) -> None:
        providers = {
            "opencode-zen/free": make_provider(
                "opencode-zen/free", ["text"], "opencode-zen"
            ),
            "openai/source-probe": make_provider(
                "openai/source-probe", ["text"], "openai"
            ),
        }
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                default_daily_limit_tokens=100,
                default_safety_buffer_tokens=0,
                default_request_reservation_tokens=0,
                chains=[
                    ChainConfig(
                        name="test",
                        providers=["opencode-zen/free"],
                    )
                ],
            ),
            ledger=MapLedger({"openai/source-probe": 10}),
            state=FakeState(),
            get_provider=providers.get,
            get_all_providers=lambda: list(providers.values()),
        )

        candidates = await router.volcengine_probe_candidate_ids(
            window=SimpleNamespace(window_id="window-a")
        )

        self.assertEqual(candidates, ["openai/source-probe"])

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

    async def test_allow_paid_does_not_bypass_modality_failures(self) -> None:
        providers = {
            "provider-a": make_provider("provider-a", ["text"], "openai"),
            "provider-b": make_provider("provider-b", ["text"], "openai"),
        }
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                exhausted_action="allow_paid",
                chains=[ChainConfig(name="test", providers=list(providers))],
            ),
            ledger=FakeLedger(0),
            state=FakeState(),
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id="provider-b",
            window=SimpleNamespace(window_id="test-window"),
            required_modalities={"image"},
        )

        self.assertEqual(decision.action, "block")
        self.assertEqual(decision.reason, "chain_unavailable")
        self.assertIsNone(decision.selected_provider_id)

    async def test_allow_paid_uses_the_original_provider_quota_key(self) -> None:
        providers = {
            "provider-a": make_provider("provider-a", ["text"], "openai"),
            "provider-b": make_provider("provider-b", ["text"], "openai"),
        }
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                default_daily_limit_tokens=100,
                default_safety_buffer_tokens=0,
                default_request_reservation_tokens=7,
                exhausted_action="allow_paid",
                chains=[ChainConfig(name="test", providers=list(providers))],
            ),
            ledger=FakeLedger(100),
            state=FakeState(),
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id="provider-b",
            window=SimpleNamespace(window_id="test-window"),
        )

        self.assertEqual(decision.action, "paid_risk")
        self.assertEqual(decision.selected_provider_id, "provider-b")
        self.assertEqual(decision.selected_quota_key, "provider-b")
        self.assertEqual(decision.reservation_tokens, 7)

    async def test_use_last_remains_available_for_quota_only_exhaustion(self) -> None:
        providers = {
            "provider-a": make_provider("provider-a", ["text"], "openai"),
            "provider-b": make_provider("provider-b", ["text"], "openai"),
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
            "openai/doubao": make_provider(
                "openai/doubao", ["text"], "openai"
            ),
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

    async def test_opencode_ignores_token_threshold_but_respects_error_cooldown(self) -> None:
        provider_id = "opencode-zen/mimo-v2.5-free"
        providers = {
            provider_id: make_provider(provider_id, ["text", "image"]),
            "deepseek/deepseek-v4-flash": make_provider(
                "deepseek/deepseek-v4-flash", ["text", "image"]
            ),
        }
        state = FakeState()
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                default_daily_limit_tokens=100,
                default_safety_buffer_tokens=0,
                default_request_reservation_tokens=0,
                chains=[ChainConfig(name="test", providers=list(providers))],
            ),
            ledger=MapLedger({provider_id: 9_999_999}),
            state=state,
            get_provider=providers.get,
        )

        available = await router.decide(
            current_provider_id=provider_id,
            window=SimpleNamespace(window_id="window-a"),
        )
        self.assertEqual(available.selected_provider_id, provider_id)
        self.assertEqual(available.reason, "upstream_quota")
        self.assertFalse(available.should_reserve)

        state.cooldowns[provider_id] = {
            "window_id": "window-a",
            "quota_key": provider_id,
            "provider_id": provider_id,
            "provider_model": provider_id,
            "started_at": time.time(),
            "expires_at": time.time() + 3_600,
            "reason": "upstream_quota_exhausted",
        }
        cooling = await router.decide(
            current_provider_id=provider_id,
            window=SimpleNamespace(window_id="window-a"),
        )
        self.assertEqual(
            cooling.selected_provider_id, "deepseek/deepseek-v4-flash"
        )
        self.assertEqual(
            cooling.candidates[0].reason, "upstream_quota_cooldown"
        )

        state.cooldowns[provider_id].update(
            {
                "window_id": "window-old",
                "expires_at": time.time() - 1,
            }
        )
        still_cooling = await router.decide(
            current_provider_id=provider_id,
            window=SimpleNamespace(window_id="window-new"),
        )
        self.assertEqual(
            still_cooling.candidates[0].reason,
            "upstream_quota_cooldown",
        )

    async def test_non_volcengine_provider_ignores_token_threshold(self) -> None:
        provider_id = "中转站1/gpt-5.4"
        providers = {
            provider_id: make_provider(
                provider_id, ["text", "image", "tool_use"], "中转站1"
            ),
        }
        state = FakeState()
        router = ProviderQuotaRouter(
            settings=RouterSettings(
                default_daily_limit_tokens=100,
                default_safety_buffer_tokens=0,
                default_request_reservation_tokens=0,
                chains=[ChainConfig(name="test", providers=[provider_id])],
            ),
            ledger=MapLedger({provider_id: 9_999_999}),
            state=state,
            get_provider=providers.get,
        )

        decision = await router.decide(
            current_provider_id=provider_id,
            window=SimpleNamespace(window_id="window-a"),
        )

        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.reason, "unlimited")
        self.assertFalse(decision.should_reserve)
        self.assertNotIn(provider_id, state.cooldowns)

    async def test_cooldown_survives_new_window_until_24_hours_end(self) -> None:
        providers = {
            "openai/doubao": make_provider(
                "openai/doubao", ["text"], "openai"
            ),
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
            "openai/doubao": make_provider(
                "openai/doubao", ["text"], "openai"
            ),
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
