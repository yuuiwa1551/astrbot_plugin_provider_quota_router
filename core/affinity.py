from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class RouteAffinity:
    conversation_key: str
    modality: str
    provider_id: str
    provider_model: str
    assigned_at: float
    expires_at: float
    last_request_started_at: float
    generation: str

    @classmethod
    def from_mapping(
        cls,
        *,
        conversation_key: str,
        modality: str,
        value: Any,
    ) -> "RouteAffinity | None":
        if not isinstance(value, dict):
            return None
        provider_id = str(value.get("provider_id") or "").strip()
        generation = str(value.get("generation") or "").strip()
        if not provider_id or not generation:
            return None
        try:
            assigned_at = float(value.get("assigned_at") or 0)
            expires_at = float(value.get("expires_at") or 0)
            last_request_started_at = float(
                value.get("last_request_started_at") or assigned_at
            )
        except (TypeError, ValueError):
            return None
        if assigned_at <= 0 or expires_at <= assigned_at:
            return None
        return cls(
            conversation_key=conversation_key,
            modality=modality,
            provider_id=provider_id,
            provider_model=str(value.get("provider_model") or ""),
            assigned_at=assigned_at,
            expires_at=expires_at,
            last_request_started_at=last_request_started_at,
            generation=generation,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_model": self.provider_model,
            "assigned_at": self.assigned_at,
            "expires_at": self.expires_at,
            "last_request_started_at": self.last_request_started_at,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class AffinityLookup:
    status: str
    affinity: RouteAffinity | None = None


@dataclass(frozen=True)
class AffinityCommitResult:
    status: str
    affinity: RouteAffinity | None = None
    previous_provider_id: str | None = None


@dataclass(frozen=True)
class RouteAffinityContext:
    conversation_key: str = ""
    modality: str = "text"
    status: str = "disabled"
    provider_id: str = ""
    provider_model: str = ""
    expires_at: float | None = None
    generation: str = ""
    request_started_at: float = 0.0


class RouteAffinityService:
    def __init__(
        self,
        *,
        state: Any,
        get_provider: Callable[[str], Any],
        get_provider_model: Callable[[str], str],
    ) -> None:
        self.state = state
        self.get_provider = get_provider
        self.get_provider_model = get_provider_model

    async def prepare(
        self,
        *,
        unified_msg_origin: str,
        selection_provider_id: str,
        selection_is_explicit: bool,
        router: Any,
        enabled: bool,
        required_modalities: set[str],
        request_started_at: float,
    ) -> RouteAffinityContext:
        modality = affinity_modality(required_modalities)
        if not enabled:
            return RouteAffinityContext(
                modality=modality,
                status="disabled",
                request_started_at=request_started_at,
            )
        if selection_is_explicit:
            return RouteAffinityContext(
                modality=modality,
                status="explicit_bypass",
                request_started_at=request_started_at,
            )
        conversation_key = conversation_affinity_key(unified_msg_origin)
        if not conversation_key:
            return RouteAffinityContext(
                modality=modality,
                status="missing_origin",
                request_started_at=request_started_at,
            )

        lookup = await self.state.lookup_route_affinity(
            conversation_key=conversation_key,
            modality=modality,
        )
        affinity = lookup.affinity
        context = RouteAffinityContext(
            conversation_key=conversation_key,
            modality=modality,
            status=lookup.status,
            provider_id=affinity.provider_id if affinity else "",
            provider_model=affinity.provider_model if affinity else "",
            expires_at=affinity.expires_at if affinity else None,
            generation=affinity.generation if affinity else "",
            request_started_at=request_started_at,
        )
        if lookup.status != "hit" or affinity is None:
            return context

        provider = self.get_provider(affinity.provider_id)
        if provider is None:
            await self._remove(context)
            return replace(context, status="invalid_provider")
        current_model = self.get_provider_model(affinity.provider_id)
        if affinity.provider_model and current_model != affinity.provider_model:
            await self._remove(context)
            return replace(context, status="provider_changed")
        if not router.provider_belongs_to_current_chain(
            current_provider_id=selection_provider_id,
            provider_id=affinity.provider_id,
        ):
            await self._remove(context)
            return replace(context, status="out_of_chain")
        return replace(context, status="candidate")

    @staticmethod
    def resolve(
        *,
        affinity: RouteAffinityContext,
        decision: Any,
    ) -> RouteAffinityContext:
        if affinity.status != "candidate":
            return affinity
        candidate = next(
            (
                item
                for item in decision.candidates
                if item.provider_id == affinity.provider_id
            ),
            None,
        )
        if (
            candidate is not None
            and candidate.available
            and decision.selected_provider_id == affinity.provider_id
        ):
            return replace(affinity, status="hit")
        return replace(affinity, status="ineligible")

    async def discard_if_ineligible(
        self,
        affinity: RouteAffinityContext,
    ) -> bool:
        if affinity.status != "ineligible":
            return False
        return await self._remove(affinity)

    async def commit(
        self,
        *,
        route_plan: Any,
        provider_id: str,
        provider_model: str,
    ) -> AffinityCommitResult | None:
        affinity = route_plan.affinity
        if (
            not route_plan.settings.route_affinity_enabled
            or route_plan.settings.dry_run
            or route_plan.selection_origin != "default"
            or not affinity.conversation_key
            or route_plan.decision.action in {"skip", "block"}
        ):
            return None
        planned_affinity_provider_id = (
            affinity.provider_id
            if affinity.status in {"hit", "ineligible"}
            else ""
        )
        return await route_plan.router.state.commit_route_affinity(
            conversation_key=affinity.conversation_key,
            modality=affinity.modality,
            provider_id=provider_id,
            provider_model=provider_model,
            request_started_at=affinity.request_started_at,
            ttl_seconds=route_plan.settings.route_affinity_ttl_seconds,
            expected_generation=(
                affinity.generation if planned_affinity_provider_id else ""
            ),
            planned_affinity_provider_id=planned_affinity_provider_id,
        )

    async def _remove(self, affinity: RouteAffinityContext) -> bool:
        if not affinity.conversation_key:
            return False
        return await self.state.remove_route_affinity(
            conversation_key=affinity.conversation_key,
            modality=affinity.modality,
            generation=affinity.generation,
        )


def conversation_affinity_key(unified_msg_origin: str) -> str:
    value = str(unified_msg_origin or "").strip()
    if not value:
        return ""
    return sha256(value.encode("utf-8")).hexdigest()


def affinity_modality(required_modalities: Iterable[str]) -> str:
    modalities = {
        str(item).strip().casefold()
        for item in required_modalities
        if str(item).strip()
    }
    order = {"image": 0, "audio": 1}
    modalities = sorted(modalities, key=lambda item: (order.get(item, 99), item))
    return "+".join(modalities) if modalities else "text"
