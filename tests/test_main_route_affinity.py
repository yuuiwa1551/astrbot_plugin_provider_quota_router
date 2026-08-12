from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.affinity import (
    AffinityCommitResult,
    AffinityLookup,
    RouteAffinity,
    RouteAffinityContext,
    conversation_affinity_key,
)
from core.config import RouterSettings
from core.provider_selection import ProviderSelection
from core.router import RouteDecision, RoutePlan


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT.parent))
try:
    PLUGIN_MODULE = importlib.import_module(f"{PACKAGE_ROOT.name}.main")
except ModuleNotFoundError as exc:  # pragma: no cover - host without AstrBot
    raise unittest.SkipTest("AstrBot runtime is required") from exc

ProviderQuotaRouterPlugin = PLUGIN_MODULE.ProviderQuotaRouterPlugin


class MainRouteAffinityTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_selection_prepares_existing_affinity(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        affinity = RouteAffinity(
            conversation_key=conversation_affinity_key("umo-a"),
            modality="text",
            provider_id="provider/b",
            provider_model="model-b",
            assigned_at=100,
            expires_at=3_700,
            last_request_started_at=90,
            generation="generation-a",
        )
        plugin.state = SimpleNamespace(
            lookup_route_affinity=AsyncMock(
                return_value=AffinityLookup(status="hit", affinity=affinity)
            ),
            remove_route_affinity=AsyncMock(),
        )
        plugin.context = SimpleNamespace(
            get_provider_by_id=lambda provider_id: (
                object() if provider_id == "provider/b" else None
            )
        )
        plugin._provider_model = lambda _provider_id: "model-b"
        router = SimpleNamespace(
            provider_belongs_to_current_chain=lambda **_kwargs: True
        )
        event = SimpleNamespace(unified_msg_origin="umo-a")

        prepared = await plugin._prepare_route_affinity(
            event=event,
            selection=ProviderSelection("provider/a", "default"),
            router=router,
            settings=RouterSettings(route_affinity_enabled=True),
            required_modalities=set(),
            request_started_at=200,
        )

        self.assertEqual(prepared.status, "candidate")
        self.assertEqual(prepared.provider_id, "provider/b")
        self.assertEqual(prepared.modality, "text")

    async def test_explicit_selection_does_not_read_affinity(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        plugin.state = SimpleNamespace(lookup_route_affinity=AsyncMock())
        event = SimpleNamespace(unified_msg_origin="umo-a")

        prepared = await plugin._prepare_route_affinity(
            event=event,
            selection=ProviderSelection("provider/a", "umo"),
            router=SimpleNamespace(),
            settings=RouterSettings(route_affinity_enabled=True),
            required_modalities={"image"},
            request_started_at=200,
        )

        self.assertEqual(prepared.status, "explicit_bypass")
        self.assertEqual(prepared.modality, "image")
        plugin.state.lookup_route_affinity.assert_not_awaited()

    async def test_provider_model_change_invalidates_affinity(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        affinity = RouteAffinity(
            conversation_key=conversation_affinity_key("umo-a"),
            modality="text",
            provider_id="provider/b",
            provider_model="old-model",
            assigned_at=100,
            expires_at=3_700,
            last_request_started_at=90,
            generation="generation-a",
        )
        plugin.state = SimpleNamespace(
            lookup_route_affinity=AsyncMock(
                return_value=AffinityLookup(status="hit", affinity=affinity)
            ),
            remove_route_affinity=AsyncMock(return_value=True),
        )
        plugin.context = SimpleNamespace(get_provider_by_id=lambda _provider_id: object())
        plugin._provider_model = lambda _provider_id: "new-model"

        prepared = await plugin._prepare_route_affinity(
            event=SimpleNamespace(unified_msg_origin="umo-a"),
            selection=ProviderSelection("provider/a", "default"),
            router=SimpleNamespace(),
            settings=RouterSettings(route_affinity_enabled=True),
            required_modalities=set(),
            request_started_at=200,
        )

        self.assertEqual(prepared.status, "provider_changed")
        plugin.state.remove_route_affinity.assert_awaited_once()

    async def test_provider_removed_from_chain_invalidates_affinity(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        affinity = RouteAffinity(
            conversation_key=conversation_affinity_key("umo-a"),
            modality="text",
            provider_id="provider/b",
            provider_model="model-b",
            assigned_at=100,
            expires_at=3_700,
            last_request_started_at=90,
            generation="generation-a",
        )
        plugin.state = SimpleNamespace(
            lookup_route_affinity=AsyncMock(
                return_value=AffinityLookup(status="hit", affinity=affinity)
            ),
            remove_route_affinity=AsyncMock(return_value=True),
        )
        plugin.context = SimpleNamespace(get_provider_by_id=lambda _provider_id: object())
        plugin._provider_model = lambda _provider_id: "model-b"
        router = SimpleNamespace(
            provider_belongs_to_current_chain=lambda **_kwargs: False
        )

        prepared = await plugin._prepare_route_affinity(
            event=SimpleNamespace(unified_msg_origin="umo-a"),
            selection=ProviderSelection("provider/a", "default"),
            router=router,
            settings=RouterSettings(route_affinity_enabled=True),
            required_modalities=set(),
            request_started_at=200,
        )

        self.assertEqual(prepared.status, "out_of_chain")
        plugin.state.remove_route_affinity.assert_awaited_once()

    def test_candidate_becomes_ineligible_when_quota_rejects_it(self) -> None:
        affinity = RouteAffinityContext(
            status="candidate",
            provider_id="provider/b",
        )
        decision = RouteDecision(
            action="allow",
            reason="within_limit",
            selected_provider_id="provider/a",
            candidates=(
                SimpleNamespace(provider_id="provider/b", available=False),
                SimpleNamespace(provider_id="provider/a", available=True),
            ),
        )

        resolved = ProviderQuotaRouterPlugin._resolve_route_affinity(
            affinity=affinity,
            decision=decision,
        )

        self.assertEqual(resolved.status, "ineligible")

    async def test_successful_fallback_commits_new_affinity(self) -> None:
        plugin = object.__new__(ProviderQuotaRouterPlugin)
        commit = AsyncMock(
            return_value=AffinityCommitResult(status="created")
        )
        state = SimpleNamespace(commit_route_affinity=commit)
        plugin.state = state
        plugin.context = SimpleNamespace(get_provider_by_id=lambda _provider_id: None)
        settings = RouterSettings(
            route_affinity_enabled=True,
            route_affinity_ttl_seconds=3_600,
        )
        route_plan = RoutePlan(
            request_id="request-a",
            router=SimpleNamespace(state=state),
            settings=settings,
            window=SimpleNamespace(window_id="window-a"),
            required_modalities=frozenset(),
            decision=RouteDecision(action="switch", reason="fallback"),
            selection_origin="default",
            affinity=RouteAffinityContext(
                conversation_key="conversation-a",
                modality="text",
                status="ineligible",
                provider_id="provider/a",
                generation="generation-a",
                request_started_at=100,
            ),
        )

        await plugin._commit_route_affinity(
            route_plan=route_plan,
            provider_id="provider/b",
            provider_model="model-b",
        )

        commit.assert_awaited_once_with(
            conversation_key="conversation-a",
            modality="text",
            provider_id="provider/b",
            provider_model="model-b",
            request_started_at=100,
            ttl_seconds=3_600,
            expected_generation="generation-a",
            planned_affinity_provider_id="provider/a",
        )


if __name__ == "__main__":
    unittest.main()
