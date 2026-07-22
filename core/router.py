from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Any

from astrbot.core.provider.provider import Provider

from .config import ChainConfig, RouterSettings, is_quota_only_exhaustion
from .ledger import QuotaLedger, UsageRecord
from .policies import ProviderPolicy, build_provider_policy
from .state import QuotaStateStore
from .time_window import UsageWindow


VOLCENGINE_GROUP_ID = "volcengine"


@dataclass(frozen=True)
class CandidateState:
    provider_id: str
    provider_model: str
    quota_key: str
    usage: UsageRecord
    limit: int
    safety_buffer: int
    reservation_tokens: int
    quota_managed: bool
    cooldown_started_at: float | None
    cooldown_until: float | None
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
    candidates: tuple[CandidateState, ...] = ()

    @property
    def should_reserve(self) -> bool:
        return self.action in {"allow", "switch", "paid_risk", "use_last"} and bool(
            self.selected_quota_key
        )


@dataclass(frozen=True)
class RoutePlan:
    """Immutable request-level snapshot used across AstrBot hook phases."""

    request_id: str
    router: "ProviderQuotaRouter"
    settings: RouterSettings
    window: UsageWindow
    required_modalities: frozenset[str]
    decision: RouteDecision
    safe_fallback_provider_ids: tuple[str, ...] = ()


class ProviderQuotaRouter:
    def __init__(
        self,
        *,
        settings: RouterSettings,
        ledger: QuotaLedger,
        state: QuotaStateStore,
        get_provider,
        get_all_providers=None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.state = state
        self.get_provider = get_provider
        self.get_all_providers = get_all_providers

    async def decide_and_reserve(
        self,
        *,
        request_id: str,
        current_provider_id: str,
        window: UsageWindow,
        required_modalities: set[str] | None = None,
    ) -> RouteDecision:
        """Atomically decide and reserve across every hot-reloaded router."""
        async with self.state.route_lock:
            decision = await self.decide(
                current_provider_id=current_provider_id,
                window=window,
                required_modalities=required_modalities,
            )
            if not decision.should_reserve or self.settings.dry_run:
                return decision
            selected_provider_id = str(
                decision.selected_provider_id or current_provider_id
            )
            selected_state = next(
                (
                    candidate
                    for candidate in decision.candidates
                    if candidate.provider_id == selected_provider_id
                ),
                None,
            )
            await self.state.reserve(
                request_id=request_id,
                window_id=window.window_id,
                quota_key=str(decision.selected_quota_key),
                provider_id=selected_provider_id,
                provider_model=(
                    selected_state.provider_model
                    if selected_state
                    else ""
                ),
                tokens=decision.reservation_tokens,
                ttl_seconds=self.settings.reservation_ttl_seconds,
            )
            return decision

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
        group_circuit = (
            await self.state.get_provider_group_circuit(group_id=VOLCENGINE_GROUP_ID)
            if self.settings.volcengine_403_circuit_enabled
            else None
        )
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
            quota_managed = self.is_token_quota_managed(provider_id)
            if group_circuit and self.is_volcengine_provider(provider_id):
                usage = await self._usage(quota_key, window, provider_id)
                group_status = str(group_circuit.get("status") or "open")
                states.append(
                    self._candidate(
                        provider_id,
                        provider_model,
                        quota_key,
                        usage,
                        chain,
                        quota_managed,
                        False,
                        (
                            "provider_group_probe"
                            if group_status == "probing"
                            else "provider_group_cooldown"
                        ),
                        cooldown={
                            "started_at": group_circuit.get("started_at"),
                            "expires_at": group_circuit.get("retry_at"),
                        },
                    )
                )
                continue
            model_circuit = (
                await self.state.get_provider_model_circuit(provider_id=provider_id)
                if self.settings.provider_error_cooldown_enabled
                else None
            )
            if model_circuit:
                usage = await self._usage(quota_key, window, provider_id)
                states.append(
                    self._candidate(
                        provider_id,
                        provider_model,
                        quota_key,
                        usage,
                        chain,
                        quota_managed,
                        False,
                        "provider_error_cooldown",
                        cooldown={
                            "started_at": model_circuit.get("started_at"),
                            "expires_at": model_circuit.get("retry_at"),
                        },
                    )
                )
                continue
            if not self._supports_modalities(provider, required_modalities):
                usage = await self._usage(quota_key, window, provider_id)
                states.append(
                    self._candidate(
                        provider_id,
                        provider_model,
                        quota_key,
                        usage,
                        chain,
                        quota_managed,
                        False,
                        "modality_not_supported",
                    )
                )
                continue

            usage = await self._usage(quota_key, window, provider_id)
            cooldown = await self.state.get_cooldown(quota_key=quota_key)
            if cooldown:
                same_window = cooldown.get("window_id") == window.window_id
                cooldown_active = float(cooldown.get("expires_at") or 0) > time.time()
                cooldown_reason = str(cooldown.get("reason") or "")
                is_upstream_cooldown = cooldown_reason.startswith("upstream_quota")
                if (quota_managed or is_upstream_cooldown) and (
                    is_upstream_cooldown or same_window or cooldown_active
                ):
                    reason = (
                        "upstream_quota_cooldown"
                        if is_upstream_cooldown
                        else ("quota_exceeded" if same_window else "cooldown_active")
                    )
                    states.append(
                        self._candidate(
                            provider_id,
                            provider_model,
                            quota_key,
                            usage,
                            chain,
                            quota_managed,
                            False,
                            reason,
                            cooldown=cooldown,
                        )
                    )
                    continue
                await self.state.clear_cooldown(quota_key=quota_key)
                cooldown = None

            if not quota_managed:
                upstream_quota = self.settings.is_upstream_quota_provider(provider_id)
                state = self._candidate(
                    provider_id,
                    provider_model,
                    quota_key,
                    usage,
                    chain,
                    False,
                    True,
                    "upstream_quota" if upstream_quota else "unlimited",
                )
                states.append(state)
                action = "allow" if provider_id == current_provider_id else "switch"
                return RouteDecision(
                    action=action,
                    reason=state.reason,
                    chain_name=chain.name,
                    original_provider_id=current_provider_id,
                    selected_provider_id=provider_id,
                    selected_quota_key=None,
                    reservation_tokens=0,
                    candidates=tuple(states),
                )

            limit = chain.limit(self.settings.default_daily_limit_tokens)
            safety = chain.safety_buffer(self.settings.default_safety_buffer_tokens)
            reservation = chain.reservation(self.settings.default_request_reservation_tokens)
            projected = usage.effective_tokens + reservation + safety
            available = projected < limit
            if not available:
                if self.settings.dry_run:
                    now = time.time()
                    cooldown = {
                        "started_at": now,
                        "expires_at": now + self.settings.quota_cooldown_seconds,
                    }
                else:
                    cooldown = await self.state.start_cooldown(
                        quota_key=quota_key,
                        window_id=window.window_id,
                        provider_id=provider_id,
                        provider_model=provider_model,
                        ttl_seconds=self.settings.quota_cooldown_seconds,
                    )
            state = self._candidate(
                provider_id,
                provider_model,
                quota_key,
                usage,
                chain,
                True,
                available,
                "ok" if available else "quota_exceeded",
                cooldown=cooldown if not available else None,
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
                    candidates=tuple(states),
                )

        quota_only_exhaustion = is_quota_only_exhaustion(
            [state.reason for state in states]
        )
        original_state = next(
            (
                state
                for state in states
                if state.provider_id == current_provider_id
            ),
            None,
        )
        if (
            self.settings.exhausted_action == "allow_paid"
            and quota_only_exhaustion
            and original_state is not None
            and original_state.quota_managed
        ):
            return RouteDecision(
                action="paid_risk",
                reason="chain_exhausted_allow_paid",
                chain_name=chain.name,
                original_provider_id=current_provider_id,
                selected_provider_id=current_provider_id,
                selected_quota_key=original_state.quota_key,
                reservation_tokens=original_state.reservation_tokens,
                candidates=tuple(states),
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
                candidates=tuple(states),
            )
        return RouteDecision(
            action="block",
            reason="chain_exhausted" if quota_only_exhaustion else "chain_unavailable",
            chain_name=chain.name,
            original_provider_id=current_provider_id,
            candidates=tuple(states),
        )

    async def status(self, *, window: UsageWindow) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        group_circuit = (
            await self.state.get_provider_group_circuit(group_id=VOLCENGINE_GROUP_ID)
            if self.settings.volcengine_403_circuit_enabled
            else None
        )
        for chain in self.settings.chains:
            for provider_id in chain.providers:
                provider = self.get_provider(provider_id)
                provider_model = ""
                if isinstance(provider, Provider):
                    provider_model = str(provider.get_model() or provider.provider_config.get("model") or "")
                quota_key = self._quota_key(provider_id, provider_model)
                usage = await self._usage(quota_key, window, provider_id)
                quota_managed = self.is_token_quota_managed(provider_id)
                limit = chain.limit(self.settings.default_daily_limit_tokens) if quota_managed else 0
                safety = chain.safety_buffer(self.settings.default_safety_buffer_tokens) if quota_managed else 0
                reservation = chain.reservation(self.settings.default_request_reservation_tokens) if quota_managed else 0
                cooldown = await self.state.get_cooldown(quota_key=quota_key)
                model_circuit = (
                    await self.state.get_provider_model_circuit(
                        provider_id=provider_id
                    )
                    if self.settings.provider_error_cooldown_enabled
                    else None
                )
                is_upstream_cooldown = bool(
                    cooldown
                    and str(cooldown.get("reason") or "").startswith(
                        "upstream_quota"
                    )
                )
                if cooldown and not is_upstream_cooldown and (
                    not quota_managed
                    or (
                        cooldown.get("window_id") != window.window_id
                        and float(cooldown.get("expires_at") or 0)
                        <= time.time()
                    )
                ):
                    await self.state.clear_cooldown(quota_key=quota_key)
                    cooldown = None
                is_group_blocked = bool(
                    group_circuit and self.is_volcengine_provider(provider_id)
                )
                if is_group_blocked:
                    status = (
                        "provider_group_probe"
                        if group_circuit.get("status") == "probing"
                        else "provider_group_cooldown"
                    )
                elif model_circuit:
                    status = "provider_error_cooldown"
                elif cooldown and str(cooldown.get("reason") or "").startswith(
                    "upstream_quota"
                ):
                    status = "upstream_quota_cooldown"
                elif not quota_managed:
                    status = (
                        "upstream_quota"
                        if self.settings.is_upstream_quota_provider(provider_id)
                        else "unlimited"
                    )
                elif cooldown and cooldown.get("window_id") == window.window_id:
                    status = "exhausted"
                elif cooldown and float(cooldown.get("expires_at") or 0) > time.time():
                    status = "cooldown"
                else:
                    if cooldown:
                        await self.state.clear_cooldown(quota_key=quota_key)
                        cooldown = None
                    status = (
                        "available"
                        if usage.effective_tokens + reservation + safety < limit
                        else "exhausted"
                    )
                display_cooldown = (
                    {
                        "started_at": model_circuit.get("started_at"),
                        "expires_at": model_circuit.get("retry_at"),
                    }
                    if model_circuit
                    else cooldown
                )
                rows.append(
                    {
                        "chain": chain.name,
                        "provider_id": provider_id,
                        "provider_model": provider_model,
                        "quota_key": quota_key,
                        "limit": limit,
                        "safety_buffer": safety,
                        "reservation_tokens": reservation,
                        "quota_managed": quota_managed,
                        "cooldown_started_at": display_cooldown.get("started_at") if display_cooldown else None,
                        "cooldown_until": display_cooldown.get("expires_at") if display_cooldown else None,
                        "next_probe_at": (
                            cooldown.get("next_probe_at")
                            if is_upstream_cooldown and cooldown
                            else None
                        ),
                        "provider_error": (
                            model_circuit.get("last_error")
                            if model_circuit
                            else None
                        ),
                        "db_tokens": usage.db_tokens,
                        "pending_tokens": usage.pending_tokens,
                        "overlay_tokens": usage.overlay_tokens,
                        "effective_tokens": usage.effective_tokens,
                        "status": status,
                        "provider_group": (
                            VOLCENGINE_GROUP_ID
                            if self.is_volcengine_provider(provider_id)
                            else None
                        ),
                        "provider_group_retry_at": (
                            group_circuit.get("retry_at")
                            if is_group_blocked
                            else None
                        ),
                    }
                )
        return rows

    async def eligible_fallback_provider_ids(
        self,
        *,
        selected_provider_id: str,
        window: UsageWindow,
        required_modalities: set[str] | None = None,
    ) -> list[str]:
        """Return quota-safe providers after the selected model in chain order."""
        chain, selected_index = self._find_chain(selected_provider_id)
        if chain is None:
            return []

        status_rows = await self.status(window=window)
        status_by_provider = {
            str(row.get("provider_id") or ""): str(row.get("status") or "")
            for row in status_rows
            if str(row.get("chain") or "") == chain.name
        }
        allowed_statuses = {"available", "unlimited", "upstream_quota"}
        required_modalities = required_modalities or set()
        result: list[str] = []
        for provider_id in chain.providers[selected_index + 1 :]:
            if status_by_provider.get(provider_id) not in allowed_statuses:
                continue
            provider = self.get_provider(provider_id)
            if not isinstance(provider, Provider):
                continue
            if not self._supports_modalities(provider, required_modalities):
                continue
            result.append(provider_id)
        return result

    def is_volcengine_provider(self, provider_id: str) -> bool:
        provider = self.get_provider(provider_id)
        if not isinstance(provider, Provider):
            return False
        return self.settings.is_volcengine_source(
            str(provider.provider_config.get("provider_source_id") or "")
        )

    def policy_for(self, provider_id: str) -> ProviderPolicy | None:
        provider = self.get_provider(provider_id)
        if not isinstance(provider, Provider):
            return None
        return build_provider_policy(provider=provider, settings=self.settings)

    def quota_key_for(self, provider_id: str, provider_model: str) -> str:
        return self._quota_key(provider_id, provider_model)

    def reservation_tokens_for(self, provider_id: str) -> int:
        if not self.is_token_quota_managed(provider_id):
            return 0
        chain, _ = self._find_chain(provider_id)
        return (
            chain.reservation(
                self.settings.default_request_reservation_tokens
            )
            if chain
            else self.settings.default_request_reservation_tokens
        )

    def is_token_quota_managed(self, provider_id: str) -> bool:
        policy = self.policy_for(provider_id)
        return bool(policy and policy.manages_local_quota)

    async def volcengine_probe_candidate_ids(
        self, *, window: UsageWindow
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        provider_ids: list[str] = []
        if callable(self.get_all_providers):
            try:
                provider_ids = [
                    str(provider.provider_config.get("id") or "")
                    for provider in self.get_all_providers()
                    if isinstance(provider, Provider)
                ]
            except Exception:  # noqa: BLE001
                provider_ids = []
        if not provider_ids:
            provider_ids = [
                provider_id
                for chain in self.settings.chains
                for provider_id in chain.providers
            ]
        for provider_id in provider_ids:
            if provider_id in seen:
                continue
            seen.add(provider_id)
            provider = self.get_provider(provider_id)
            if not isinstance(provider, Provider):
                continue
            if not self.is_volcengine_provider(provider_id):
                continue
            if not self._supports_modalities(provider, {"text"}):
                continue
            model_circuit = await self.state.get_provider_model_circuit(
                provider_id=provider_id
            )
            if model_circuit:
                continue
            provider_model = str(
                provider.get_model()
                or provider.provider_config.get("model")
                or ""
            )
            quota_key = self._quota_key(provider_id, provider_model)
            cooldown = await self.state.get_cooldown(quota_key=quota_key)
            if cooldown:
                same_window = cooldown.get("window_id") == window.window_id
                active = float(cooldown.get("expires_at") or 0) > time.time()
                if same_window or active:
                    continue
                await self.state.clear_cooldown(quota_key=quota_key)
            usage = await self._usage(quota_key, window, provider_id)
            chain, _ = self._find_chain(provider_id)
            quota_config = chain or ChainConfig(
                name="source-probe-default",
                providers=[provider_id],
            )
            projected = (
                usage.effective_tokens
                + quota_config.reservation(
                    self.settings.default_request_reservation_tokens
                )
                + quota_config.safety_buffer(
                    self.settings.default_safety_buffer_tokens
                )
            )
            if projected < quota_config.limit(
                self.settings.default_daily_limit_tokens
            ):
                result.append(provider_id)
        return result

    async def ensure_cooldown(
        self,
        *,
        provider_id: str,
        provider_model: str,
        window: UsageWindow,
    ) -> dict[str, Any] | None:
        if self.settings.dry_run or not self.is_token_quota_managed(provider_id):
            return None
        chain, _ = self._find_chain(provider_id)
        if chain is None:
            return None
        quota_key = self._quota_key(provider_id, provider_model)
        usage = await self._usage(quota_key, window, provider_id)
        projected = (
            usage.effective_tokens
            + chain.reservation(self.settings.default_request_reservation_tokens)
            + chain.safety_buffer(self.settings.default_safety_buffer_tokens)
        )
        if projected < chain.limit(self.settings.default_daily_limit_tokens):
            return None

        existing = await self.state.get_cooldown(quota_key=quota_key)
        if existing:
            same_window = existing.get("window_id") == window.window_id
            active = float(existing.get("expires_at") or 0) > time.time()
            if same_window or active:
                return existing
            await self.state.clear_cooldown(quota_key=quota_key)
        return await self.state.start_cooldown(
            quota_key=quota_key,
            window_id=window.window_id,
            provider_id=provider_id,
            provider_model=provider_model,
            ttl_seconds=self.settings.quota_cooldown_seconds,
        )

    async def reconcile_cooldowns(self, *, window: UsageWindow) -> tuple[int, int]:
        checked_count = 0
        active_count = 0
        for chain in self.settings.chains:
            for provider_id in chain.providers:
                if not self.is_token_quota_managed(provider_id):
                    continue
                provider = self.get_provider(provider_id)
                if not isinstance(provider, Provider):
                    continue
                checked_count += 1
                provider_model = str(
                    provider.get_model()
                    or provider.provider_config.get("model")
                    or ""
                )
                cooldown = await self.ensure_cooldown(
                    provider_id=provider_id,
                    provider_model=provider_model,
                    window=window,
                )
                if cooldown:
                    active_count += 1
        return checked_count, active_count

    def _find_chain(self, provider_id: str) -> tuple[ChainConfig | None, int]:
        for chain in self.settings.chains:
            if provider_id in chain.providers:
                return chain, chain.providers.index(provider_id)
        return None, -1

    def _quota_key(self, provider_id: str, provider_model: str) -> str:
        if self.settings.quota_key_mode == "provider_id":
            return provider_id
        return provider_model or provider_id

    async def _usage(
        self,
        quota_key: str,
        window: UsageWindow,
        provider_id: str,
    ) -> UsageRecord:
        provider_scope: tuple[str, ...] | None = None
        if self.settings.quota_key_mode == "provider_model":
            provider_scope = (
                self._local_quota_provider_ids()
                if self.is_token_quota_managed(provider_id)
                else (provider_id,)
            )
        db_tokens = await self.ledger.query_usage(
            quota_key=quota_key,
            quota_key_mode=self.settings.quota_key_mode,
            window=window,
            provider_ids=provider_scope,
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

    def _local_quota_provider_ids(self) -> tuple[str, ...]:
        providers: list[Provider] = []
        if callable(self.get_all_providers):
            try:
                providers = [
                    provider
                    for provider in self.get_all_providers()
                    if isinstance(provider, Provider)
                ]
            except Exception:  # noqa: BLE001
                providers = []
        if not providers:
            providers = [
                provider
                for chain in self.settings.chains
                for provider_id in chain.providers
                if isinstance(
                    (provider := self.get_provider(provider_id)),
                    Provider,
                )
            ]
        return tuple(
            dict.fromkeys(
                str(provider.provider_config.get("id") or "")
                for provider in providers
                if build_provider_policy(
                    provider=provider,
                    settings=self.settings,
                ).manages_local_quota
                and str(provider.provider_config.get("id") or "")
            )
        )

    def _missing_state(
        self,
        provider_id: str,
        chain: ChainConfig,
        window: UsageWindow,
        reason: str,
    ) -> CandidateState:
        quota_key = provider_id
        quota_managed = self.is_token_quota_managed(provider_id)
        return CandidateState(
            provider_id=provider_id,
            provider_model="",
            quota_key=quota_key,
            usage=UsageRecord(quota_key=quota_key, db_tokens=0, pending_tokens=0, overlay_tokens=0),
            limit=chain.limit(self.settings.default_daily_limit_tokens) if quota_managed else 0,
            safety_buffer=chain.safety_buffer(self.settings.default_safety_buffer_tokens) if quota_managed else 0,
            reservation_tokens=chain.reservation(self.settings.default_request_reservation_tokens) if quota_managed else 0,
            quota_managed=quota_managed,
            cooldown_started_at=None,
            cooldown_until=None,
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
        quota_managed: bool,
        available: bool,
        reason: str,
        *,
        cooldown: dict[str, Any] | None = None,
    ) -> CandidateState:
        return CandidateState(
            provider_id=provider_id,
            provider_model=provider_model,
            quota_key=quota_key,
            usage=usage,
            limit=chain.limit(self.settings.default_daily_limit_tokens) if quota_managed else 0,
            safety_buffer=chain.safety_buffer(self.settings.default_safety_buffer_tokens) if quota_managed else 0,
            reservation_tokens=chain.reservation(self.settings.default_request_reservation_tokens) if quota_managed else 0,
            quota_managed=quota_managed,
            cooldown_started_at=float(cooldown.get("started_at") or 0) if cooldown else None,
            cooldown_until=float(cooldown.get("expires_at") or 0) if cooldown else None,
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
                "quota_managed": item.quota_managed,
                "cooldown_started_at": item.cooldown_started_at,
                "cooldown_until": item.cooldown_until,
                "available": item.available,
                "reason": item.reason,
            }
            for item in decision.candidates
        ],
    }
