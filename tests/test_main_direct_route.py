from __future__ import annotations

import asyncio
import tempfile
import unittest
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

try:
    from astrbot.core.provider.provider import Provider
except ModuleNotFoundError as exc:  # pragma: no cover - host test environment
    raise unittest.SkipTest("AstrBot runtime is required for direct route tests") from exc

from core.attempt_timeout_tracker import AttemptTimeoutTracker
from core.config import ChainConfig, ProviderPolicyOverride, RouterSettings
from core.reports import read_recent_decisions
from core.router import ProviderQuotaRouter
from core.state import QuotaStateStore

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT.parent))
try:
    PLUGIN_MODULE = importlib.import_module(f"{PACKAGE_ROOT.name}.main")
except ModuleNotFoundError as exc:  # pragma: no cover - host without AstrBot
    raise unittest.SkipTest("AstrBot runtime is required") from exc
ProviderQuotaRouterPlugin = PLUGIN_MODULE.ProviderQuotaRouterPlugin


class ZeroLedger:
    async def query_usage(self, **kwargs) -> int:
        return 0


class DirectFakeProvider(Provider):
    def __init__(
        self,
        provider_id: str,
        *,
        modalities: list[str] | None = None,
        response_text: str = "ok",
    ) -> None:
        super().__init__(
            {
                "id": provider_id,
                "type": "openai_chat_completion",
                "provider_source_id": "relay",
                "model": provider_id,
                "modalities": modalities or ["text"],
            },
            {},
        )
        self.set_model(provider_id)
        self.response_text = response_text
        self.response_role = "assistant"
        self.exception: Exception | None = None
        self.delay = 0.0
        self.stream_exception_after_first: Exception | None = None
        self.calls = 0
        self.last_kwargs: dict | None = None

    def get_current_key(self) -> str:
        return ""

    def set_key(self, key: str) -> None:
        return None

    async def get_models(self) -> list[str]:
        return [self.get_model()]

    async def text_chat(self, *args, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exception is not None:
            raise self.exception
        return SimpleNamespace(
            role=self.response_role,
            completion_text=self.response_text,
            usage=SimpleNamespace(total=7),
        )

    async def text_chat_stream(self, *args, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.exception is not None:
            raise self.exception
        yield SimpleNamespace(
            role="assistant",
            completion_text=self.response_text,
            usage=SimpleNamespace(total=7),
        )
        if self.stream_exception_after_first is not None:
            raise self.stream_exception_after_first


class FakeContext:
    def __init__(self, providers: list[DirectFakeProvider]) -> None:
        self.providers = {
            provider.provider_config["id"]: provider for provider in providers
        }

    def get_provider_by_id(self, provider_id: str):
        return self.providers.get(provider_id)

    def get_all_providers(self):
        return list(self.providers.values())


class MainDirectRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_fallback_does_not_charge_previous_response_usage(self) -> None:
        requested = DirectFakeProvider("aux/mini", response_text="service unavailable")
        requested.response_role = "err"
        fallback = DirectFakeProvider("global/model")
        fallback.provider_config["provider_source_id"] = "openai"
        fallback.exception = TimeoutError("upstream timed out")
        plugin = self._plugin(requested, fallback)
        with self.assertRaises(TimeoutError):
            await requested.text_chat(prompt="test")
        snapshot = await plugin.state.snapshot()
        self.assertEqual(snapshot["pending"], {})
        self.assertEqual(snapshot["overlays"], [])
        decisions = read_recent_decisions(plugin.state.decisions_path, limit=1)
        self.assertIn("upstream timed out", decisions[0]["final_error"])

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    async def asyncTearDown(self) -> None:
        plugin = getattr(self, "plugin", None)
        if plugin is not None:
            plugin._disable_opencode_quota_guard()

    def _plugin(
        self,
        requested: DirectFakeProvider,
        fallback: DirectFakeProvider,
    ) -> ProviderQuotaRouterPlugin:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin.context = FakeContext([requested, fallback])
        plugin.settings = RouterSettings(
            route_direct_provider_calls_enabled=True,
            direct_provider_fallback_max_candidates=1,
            provider_error_full_call_timeout_seconds=1,
            provider_error_attempt_timeout_seconds=1,
            provider_error_request_max_retries=1,
            provider_policy_overrides=(
                ProviderPolicyOverride(
                    provider_id=requested.provider_config["id"],
                    max_output_tokens=220,
                ),
            ),
            provider_error_cooldown_enabled=True,
            volcengine_403_circuit_enabled=False,
            chains=[
                ChainConfig(
                    name="global",
                    providers=[fallback.provider_config["id"]],
                )
            ],
        )
        plugin.state = QuotaStateStore(Path(self.temp_dir.name))
        plugin.ledger = ZeroLedger()
        plugin.router = ProviderQuotaRouter(
            settings=plugin.settings,
            ledger=plugin.ledger,
            state=plugin.state,
            get_provider=plugin.context.get_provider_by_id,
            get_all_providers=plugin.context.get_all_providers,
        )
        plugin._fallback_chain_is_dynamic = False
        plugin._opencode_quota_guard_active = False
        plugin._provider_guard_classes = set()
        plugin._attempt_timeout_tracker = AttemptTimeoutTracker()
        plugin._sync_opencode_quota_guard()
        self.plugin = plugin
        return plugin

    async def test_cooled_direct_provider_is_skipped_before_external_call(
        self,
    ) -> None:
        requested = DirectFakeProvider("aux/mini", response_text="mini")
        fallback = DirectFakeProvider("global/model", response_text="fallback")
        plugin = self._plugin(requested, fallback)
        await plugin.state.open_provider_model_circuit(
            provider_id="aux/mini",
            provider_model="aux/mini",
            ttl_seconds=300,
            error="test cooldown",
        )

        response = await requested.text_chat(prompt="test", model="aux-model")
        snapshot = await plugin.state.snapshot()
        decisions = read_recent_decisions(
            plugin.state.decisions_path,
            limit=5,
        )

        self.assertEqual(response.completion_text, "fallback")
        self.assertEqual(requested.calls, 0)
        self.assertEqual(fallback.calls, 1)
        self.assertNotIn("model", fallback.last_kwargs or {})
        self.assertEqual((fallback.last_kwargs or {}).get("max_tokens"), 220)
        self.assertEqual(snapshot["pending"], {})
        self.assertEqual(decisions[-1]["route_kind"], "plugin_direct")
        self.assertEqual(decisions[-1]["requested_provider_id"], "aux/mini")
        self.assertEqual(decisions[-1]["selected_provider_id"], "global/model")

    async def test_provider_error_falls_back_once_in_same_direct_call(self) -> None:
        requested = DirectFakeProvider("aux/mini")
        requested.exception = TimeoutError("upstream timed out")
        fallback = DirectFakeProvider("global/model", response_text="fallback")
        plugin = self._plugin(requested, fallback)

        response = await requested.text_chat(prompt="test")
        circuit = await plugin.state.get_provider_model_circuit(
            provider_id="aux/mini"
        )

        self.assertEqual(response.completion_text, "fallback")
        self.assertEqual(requested.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertIsNotNone(circuit)

    async def test_image_direct_call_skips_text_only_requested_provider(
        self,
    ) -> None:
        requested = DirectFakeProvider(
            "aux/text-only",
            modalities=["text"],
        )
        fallback = DirectFakeProvider(
            "global/vision",
            modalities=["text", "image"],
            response_text="vision",
        )
        self._plugin(requested, fallback)

        response = await requested.text_chat(
            prompt="describe",
            image_urls=["https://example.test/image.png"],
        )

        self.assertEqual(response.completion_text, "vision")
        self.assertEqual(requested.calls, 0)
        self.assertEqual(fallback.calls, 1)

    async def test_stream_falls_back_only_before_first_chunk(self) -> None:
        requested = DirectFakeProvider("aux/mini")
        requested.exception = TimeoutError("first chunk timed out")
        fallback = DirectFakeProvider("global/model", response_text="stream-ok")
        self._plugin(requested, fallback)

        chunks = [
            item.completion_text
            async for item in requested.text_chat_stream(prompt="test")
        ]

        self.assertEqual(chunks, ["stream-ok"])
        self.assertEqual(requested.calls, 1)
        self.assertEqual(fallback.calls, 1)

    async def test_stream_does_not_fallback_after_output_started(self) -> None:
        requested = DirectFakeProvider("aux/mini", response_text="first")
        requested.stream_exception_after_first = RuntimeError("late stream error")
        fallback = DirectFakeProvider("global/model", response_text="wrong")
        plugin = self._plugin(requested, fallback)

        received = []
        with self.assertRaisesRegex(RuntimeError, "late stream error"):
            async for item in requested.text_chat_stream(prompt="test"):
                received.append(item.completion_text)
        snapshot = await plugin.state.snapshot()

        self.assertEqual(received, ["first"])
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(snapshot["pending"], {})

    async def test_cancelled_direct_call_releases_reservation(self) -> None:
        requested = DirectFakeProvider("aux/mini")
        requested.delay = 10
        fallback = DirectFakeProvider("global/model")
        plugin = self._plugin(requested, fallback)

        task = asyncio.create_task(requested.text_chat(prompt="test"))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        snapshot = await plugin.state.snapshot()

        self.assertEqual(snapshot["pending"], {})
        decisions = read_recent_decisions(
            plugin.state.decisions_path,
            limit=5,
        )
        self.assertEqual(decisions[-1]["direct_status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
