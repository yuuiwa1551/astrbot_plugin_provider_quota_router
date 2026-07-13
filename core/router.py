from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astrbot.core.provider.provider import Provider

from .config import ChainConfig, RouterSettings, is_quota_only_exhaustion
from .ledger import QuotaLedger, UsageRecord
from .state import QuotaStateStore
from .time_window import UsageWindow


@dataclass(frozen=True)
class CandidateState:
    provider_id: str
    provider_model: str
    quota_key: str
    usage: UsageRecord
    limit: int
    safety_buffer: int
    reservation_tokens: int
    available: bool
    reason: str


@dataclass(frozen=True)
class RouteDecision:
    action: str
    reason: str
    chain_name: str | None = None
    original_provider_id: str | None = None
    selected_provider_id: str | None = None
    selected_quota_key: str | None = None
    reservation_tokens: int = 0
    candidates: list[CandidateState] = field(default_factory=list)

    @property
    def should_reserve(self) -> bool:
        return self.action in {"allow", "switch", "paid_risk", "use_last"} and bool(
            self.selected_quota_key
        )


class ProviderQuotaRouter:
    def __init__(
        self,
        *,
        settings: RouterSettings,
        ledger: QuotaLedger,
        state: QuotaStateStore,
        get_provider,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.state = state
        self.get_provider = get_provider

    async def decide(
        self,
        *,
        current_provider_id: str,
        window: UsageWindow,
        required_modalities: set[str] | None = None,
    ) -> RouteDecision:
        chain, current_index = self._find_chain(current_provider_id)
        if chain is None:
            return RouteDecision(
                action="skip",
                reason="provider_not_in_chain",
                original_provider_id=current_provider_id,
            )

        required_modalities = required_modalities or set()
        states: list[CandidateState] = []
        start_index = 0 if self.settings.strict_priority_order else current_index
        for provider_id in chain.providers[start_index:]:
            provider = self.get_provider(provider_id)
            if not isinstance(provider, Provider):
                states.append(
                    self._missing_state(provider_id, chain, window, "provider_not_available")
                )
                continue
            provider_model = str(provider.get_model() or provider.provider_config.get("model") or "")
            quota_key = self._quota_key(provider_id, provider_model)
            if not self._supports_modalities(provider, required_modalities):
                usage = await self._usage(quota_key, window)
                states.append(
                    self._candidate(
                        provider_id,
                        provider_model,
                        quota_key,
                        usage,
                        chain,
                        False,
                        "modality_not_supported",
                    )
                )
                continue

            usage = await self._usage(quota_key, window)
            limit = chain.limit(self.settings.default_daily_limit_tokens)
            safety = chain.safety_buffer(self.settings.default_safety_buffer_tokens)
            reservation = chain.reservation(self.settings.default_request_reservation_tokens)
            projected = usage.effective_tokens + reservation + safety
            available = projected < limit
            state = self._candidate(
                provider_id,
                provider_model,
                quota_key,
                usage,
                chain,
                available,
                "ok" if available else "quota_exceeded",
            )
            states.append(state)
            if available:
                action = "allow" if provider_id == current_provider_id else "switch"
                return RouteDecision(
                    action=action,
                    reason=state.reason,
                    chain_name=chain.name,
                    original_provider_id=current_provider_id,
                    selected_provider_id=provider_id,
                    selected_quota_key=quota_key,
                    reservation_tokens=reservation,
                    candidates=states,
                )

        if self.settings.exhausted_action == "allow_paid":
            original_state = states[0] if states else None
            return RouteDecision(
                action="paid_risk",
                reason="chain_exhausted_allow_paid",
                chain_name=chain.name,
                original_provider_id=current_provider_id,
                selected_provider_id=current_provider_id,
                selected_quota_key=original_state.quota_key if original_state else current_provider_id,
                reservation_tokens=chain.reservation(self.settings.default_request_reservation_tokens),
                candidates=states,
            )
        quota_only_exhaustion = is_quota_only_exhaustion(
            [state.reason for state in states]
        )
        if (
            self.settings.exhausted_action == "use_last"
            and states
            and quota_only_exhaustion
        ):
            last = states[-1]
            return RouteDecision(
                action="use_last",
                reason="chain_exhausted_use_last",
                chain_name=chain.name,
                original_provider_id=current_provider_id,
                selected_provider_id=last.provider_id,
                selected_quota_key=last.quota_key,
                reservation_tokens=last.reservation_tokens,
                candidates=states,
            )
        return RouteDecision(
            action="block",
            reason="chain_exhausted" if quota_only_exhaustion else "chain_unavailable",
            chain_name=chain.name,
            original_provider_id=current_provider_id,
            candidates=states,
        )

    async def status(self, *, window: UsageWindow) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chain in self.settings.chains:
            for provider_id in chain.providers:
                provider = self.get_provider(provider_id)
                provider_model = ""
                if isinstance(provider, Provider):
                    provider_model = str(provider.get_model() or provider.provider_config.get("model") or "")
                quota_key = self._quota_key(provider_id, provider_model)
                usage = await self._usage(quota_key, window)
                limit = chain.limit(self.settings.default_daily_limit_tokens)
                safety = chain.safety_buffer(self.settings.default_safety_buffer_tokens)
                status = "available" if usage.effective_tokens + safety < limit else "exhausted"
                rows.append(
                    {
                        "chain": chain.name,
                        "provider_id": provider_id,
                        "provider_model": provider_model,
                        "quota_key": quota_key,
                        "limit": limit,
                        "safety_buffer": safety,
                        "db_tokens": usage.db_tokens,
                        "pending_tokens": usage.pending_tokens,
                        "overlay_tokens": usage.overlay_tokens,
                        "effective_tokens": usage.effective_tokens,
                        "status": status,
                    }
                )
        return rows

    def _find_chain(self, provider_id: str) -> tuple[ChainConfig | None, int]:
        for chain in self.settings.chains:
            if provider_id in chain.providers:
                return chain, chain.providers.index(provider_id)
        return None, -1

    def _quota_key(self, provider_id: str, provider_model: str) -> str:
        if self.settings.quota_key_mode == "provider_id":
            return provider_id
        return provider_model or provider_id

    async def _usage(self, quota_key: str, window: UsageWindow) -> UsageRecord:
        db_tokens = await self.ledger.query_usage(
            quota_key=quota_key,
            quota_key_mode=self.settings.quota_key_mode,
            window=window,
        )
        pending_tokens, overlay_tokens = await self.state.usage_overlay(
            quota_key=quota_key,
            window_id=window.window_id,
        )
        return UsageRecord(
            quota_key=quota_key,
            db_tokens=db_tokens,
            pending_tokens=pending_tokens,
            overlay_tokens=overlay_tokens,
        )

    def _missing_state(
        self,
        provider_id: str,
        chain: ChainConfig,
        window: UsageWindow,
        reason: str,
    ) -> CandidateState:
        quota_key = provider_id
        return CandidateState(
            provider_id=provider_id,
            provider_model="",
            quota_key=quota_key,
            usage=UsageRecord(quota_key=quota_key, db_tokens=0, pending_tokens=0, overlay_tokens=0),
            limit=chain.limit(self.settings.default_daily_limit_tokens),
            safety_buffer=chain.safety_buffer(self.settings.default_safety_buffer_tokens),
            reservation_tokens=chain.reservation(self.settings.default_request_reservation_tokens),
            available=False,
            reason=reason,
        )

    def _candidate(
        self,
        provider_id: str,
        provider_model: str,
        quota_key: str,
        usage: UsageRecord,
        chain: ChainConfig,
        available: bool,
        reason: str,
    ) -> CandidateState:
        return CandidateState(
            provider_id=provider_id,
            provider_model=provider_model,
            quota_key=quota_key,
            usage=usage,
            limit=chain.limit(self.settings.default_daily_limit_tokens),
            safety_buffer=chain.safety_buffer(self.settings.default_safety_buffer_tokens),
            reservation_tokens=chain.reservation(self.settings.default_request_reservation_tokens),
            available=available,
            reason=reason,
        )

    @staticmethod
    def _supports_modalities(provider: Provider, required_modalities: set[str]) -> bool:
        if not required_modalities:
            return True
        modalities = provider.provider_config.get("modalities")
        if not isinstance(modalities, list) or not modalities:
            return True
        return required_modalities.issubset({str(item) for item in modalities})


def decision_payload(
    *,
    request_id: str,
    window: UsageWindow,
    decision: RouteDecision,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "time": datetime.now().astimezone().isoformat(),
        "request_id": request_id,
        "window_id": window.window_id,
        "action": decision.action,
        "reason": decision.reason,
        "dry_run": dry_run,
        "chain": decision.chain_name,
        "original_provider_id": decision.original_provider_id,
        "selected_provider_id": decision.selected_provider_id,
        "selected_quota_key": decision.selected_quota_key,
        "candidates": [
            {
                "provider_id": item.provider_id,
                "provider_model": item.provider_model,
                "quota_key": item.quota_key,
                "db_tokens": item.usage.db_tokens,
                "pending_tokens": item.usage.pending_tokens,
                "overlay_tokens": item.usage.overlay_tokens,
                "effective_tokens": item.usage.effective_tokens,
                "limit": item.limit,
                "safety_buffer": item.safety_buffer,
                "reservation_tokens": item.reservation_tokens,
                "available": item.available,
                "reason": item.reason,
            }
            for item in decision.candidates
        ],
    }
