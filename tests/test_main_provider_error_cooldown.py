from __future__ import annotations

import tempfile
import unittest
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

from core.config import RouterSettings
from core.state import QuotaStateStore

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT.parent))
try:
    PLUGIN_MODULE = importlib.import_module(f"{PACKAGE_ROOT.name}.main")
except ModuleNotFoundError as exc:  # pragma: no cover - host without AstrBot
    raise unittest.SkipTest("AstrBot runtime is required") from exc
ProviderQuotaRouterPlugin = PLUGIN_MODULE.ProviderQuotaRouterPlugin


class MainProviderErrorCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_error_does_not_open_model_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(ProviderQuotaRouterPlugin)
            plugin.settings = RouterSettings(
                provider_error_cooldown_enabled=True,
                volcengine_403_circuit_enabled=False,
            )
            plugin.state = QuotaStateStore(Path(temp_dir))
            provider = SimpleNamespace(
                provider_config={"id": "relay/model-a", "model": "model-a"},
                get_model=lambda: "model-a",
            )

            await plugin.opencode_quota_guard_error(
                provider,
                RuntimeError("Error code: 400 maximum context length"),
            )

            self.assertIsNone(
                await plugin.state.get_provider_model_circuit(
                    provider_id="relay/model-a"
                )
            )

    async def test_opencode_limit_starts_unknown_reset_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(ProviderQuotaRouterPlugin)
            plugin.settings = RouterSettings(
                provider_error_cooldown_enabled=True,
                volcengine_403_circuit_enabled=False,
            )
            plugin.state = QuotaStateStore(Path(temp_dir))
            provider = SimpleNamespace(
                provider_config={
                    "id": "opencode-zen/free-model",
                    "model": "free-model",
                },
                get_model=lambda: "free-model",
            )

            await plugin.opencode_quota_guard_error(
                provider,
                RuntimeError("FreeUsageLimitError: daily limit exceeded"),
            )

            cooldown = await plugin.state.get_cooldown(quota_key="free-model")
            self.assertIsNotNone(cooldown)
            self.assertIsNone(cooldown["expires_at"])
            self.assertGreater(cooldown["next_probe_at"], cooldown["started_at"])

    async def test_unknown_guard_error_uses_short_model_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(ProviderQuotaRouterPlugin)
            plugin.settings = RouterSettings(
                provider_error_cooldown_enabled=True,
                provider_error_cooldown_seconds=1_800,
                volcengine_403_circuit_enabled=False,
            )
            plugin.state = QuotaStateStore(Path(temp_dir))
            provider = SimpleNamespace(
                provider_config={
                    "id": "relay/model-a",
                    "model": "model-a",
                },
                get_model=lambda: "model-a",
            )

            await plugin.opencode_quota_guard_error(
                provider,
                RuntimeError("API_KEY_QUOTA_EXHAUSTED"),
            )

            circuit = await plugin.state.get_provider_model_circuit(
                provider_id="relay/model-a"
            )
            self.assertIsNotNone(circuit)
            self.assertEqual(circuit["provider_model"], "model-a")
            self.assertAlmostEqual(
                circuit["retry_at"] - circuit["started_at"],
                300,
                delta=1,
            )
            self.assertEqual(
                await plugin.opencode_quota_guard_cooldown(provider), circuit
            )

    async def test_guard_error_opens_volcengine_group_before_agent_finishes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(ProviderQuotaRouterPlugin)
            plugin.settings = RouterSettings(
                provider_error_cooldown_enabled=True,
                provider_error_cooldown_seconds=1_800,
                volcengine_403_circuit_enabled=True,
                volcengine_403_cooldown_seconds=1_800,
            )
            plugin.state = QuotaStateStore(Path(temp_dir))
            plugin.router = SimpleNamespace(
                is_volcengine_provider=lambda provider_id: provider_id.startswith(
                    "openai/"
                )
            )
            provider = SimpleNamespace(
                provider_config={
                    "id": "openai/doubao-a",
                    "model": "doubao-a",
                    "provider_source_id": "openai",
                },
                get_model=lambda: "doubao-a",
            )

            await plugin.opencode_quota_guard_error(
                provider,
                RuntimeError("Error code: 403 AccountOverdueError"),
            )

            group = await plugin.state.get_provider_group_circuit(
                group_id="volcengine"
            )
            self.assertIsNotNone(group)
            self.assertEqual(group["trigger_provider_id"], "openai/doubao-a")

    async def test_plain_403_does_not_open_volcengine_source_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(ProviderQuotaRouterPlugin)
            plugin.settings = RouterSettings(
                provider_error_cooldown_enabled=True,
                volcengine_403_circuit_enabled=True,
            )
            plugin.state = QuotaStateStore(Path(temp_dir))
            plugin.router = SimpleNamespace(
                is_volcengine_provider=lambda provider_id: True
            )
            provider = SimpleNamespace(
                provider_config={
                    "id": "openai/doubao-a",
                    "model": "doubao-a",
                    "provider_source_id": "openai",
                },
                get_model=lambda: "doubao-a",
            )

            await plugin.opencode_quota_guard_error(
                provider,
                RuntimeError("Error code: 403 request forbidden"),
            )

            self.assertIsNone(
                await plugin.state.get_provider_group_circuit(
                    group_id="volcengine"
                )
            )


if __name__ == "__main__":
    unittest.main()
