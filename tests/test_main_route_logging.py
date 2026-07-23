from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT.parent))
try:
    PLUGIN_MODULE = importlib.import_module(f"{PACKAGE_ROOT.name}.main")
except ModuleNotFoundError as exc:  # pragma: no cover - host without AstrBot
    raise unittest.SkipTest("AstrBot runtime is required") from exc
ProviderQuotaRouterPlugin = PLUGIN_MODULE.ProviderQuotaRouterPlugin
RouteDecision = PLUGIN_MODULE.RouteDecision


class MainRouteLoggingTests(unittest.TestCase):
    def test_applied_route_log_includes_conversation_provider_and_model(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin._provider_model = lambda provider_id: {
            "provider/source": "source-model",
            "provider/target": "target-model",
        }.get(provider_id, "")
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:123456"
        )
        decision = RouteDecision(
            action="switch",
            reason="upstream_quota",
            original_provider_id="provider/source",
            selected_provider_id="provider/target",
            candidates=(
                SimpleNamespace(
                    provider_id="provider/source",
                    reason="provider_error_cooldown",
                ),
                SimpleNamespace(
                    provider_id="provider/target",
                    reason="upstream_quota",
                ),
            ),
        )

        with patch.object(PLUGIN_MODULE.logger, "info") as log_info:
            logged = plugin._log_applied_route(
                event=event,
                decision=decision,
            )

        self.assertTrue(logged)
        arguments = log_info.call_args.args
        self.assertIn("本次对话已由插件路由", arguments[0])
        self.assertEqual(arguments[1], event.unified_msg_origin)
        self.assertEqual(arguments[2:6], (
            "provider/source",
            "source-model",
            "provider/target",
            "target-model",
        ))
        self.assertEqual(arguments[6:], (
            "switch",
            "provider_error_cooldown",
            "upstream_quota",
        ))

    def test_non_route_decision_does_not_emit_applied_route_log(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin._provider_model = lambda _provider_id: "unused"
        event = SimpleNamespace(unified_msg_origin="webchat:FriendMessage:1")
        decision = RouteDecision(
            action="allow",
            reason="within_limit",
            original_provider_id="provider/source",
            selected_provider_id="provider/source",
        )

        with patch.object(PLUGIN_MODULE.logger, "info") as log_info:
            logged = plugin._log_applied_route(
                event=event,
                decision=decision,
            )

        self.assertFalse(logged)
        log_info.assert_not_called()

    def test_use_last_without_provider_change_does_not_claim_routing(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin._provider_model = lambda _provider_id: "same-model"
        event = SimpleNamespace(unified_msg_origin="webchat:FriendMessage:1")
        decision = RouteDecision(
            action="use_last",
            reason="chain_exhausted_use_last",
            original_provider_id="provider/same",
            selected_provider_id="provider/same",
        )

        with patch.object(PLUGIN_MODULE.logger, "info") as log_info:
            logged = plugin._log_applied_route(
                event=event,
                decision=decision,
            )

        self.assertFalse(logged)
        log_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
