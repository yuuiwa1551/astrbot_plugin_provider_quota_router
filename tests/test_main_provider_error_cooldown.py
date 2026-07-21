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
    async def test_guard_error_opens_thirty_minute_model_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(ProviderQuotaRouterPlugin)
            plugin.settings = RouterSettings(
                provider_error_cooldown_enabled=True,
                provider_error_cooldown_seconds=1_800,
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
                1_800,
                delta=1,
            )
            self.assertEqual(
                await plugin.opencode_quota_guard_cooldown(provider), circuit
            )


if __name__ == "__main__":
    unittest.main()
