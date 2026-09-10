from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
import time
from typing import Any
import uuid

from .config import ChainConfig, RouterSettings
from .error_classifier import classify_provider_error, should_fallback_direct
from .opencode_quota_guard import (
    bind_provider_guard_route_plan,
    current_provider_guard_provider_id,
    detect_plugin_caller,
    reset_provider_guard_route_plan,
)
from .policies import build_provider_policy, provider_identity
from .provider_errors import is_provider_error_response, response_error_text
from .router import (
    ProviderQuotaRouter,
    RouteDecision,
    decision_payload,
)
from .time_window import UsageWindow, current_window


ROUTE_KIND_PLUGIN_DIRECT = "plugin_direct"


class DirectRouteUnavailableError(RuntimeError):
    """No safe chat provider can serve a direct plugin request."""


@dataclass(frozen=True)
class DirectRoutePlan:
    """Immutable snapshot for one direct plugin Provider attempt."""

    request_id: str
    router: ProviderQuotaRouter
    settings: RouterSettings
    window: UsageWindow
    required_modalities: frozenset[str]
    requested_provider_id: str
    provider_order: tuple[str, ...]
    decision: RouteDecision
    caller_plugin: str
    stream: bool
    started_at: float
    route_kind: str = ROUTE_KIND_PLUGIN_DIRECT


@dataclass(frozen=True)
class _DirectCallContext:
    router: ProviderQuotaRouter
    settings: RouterSettings
    requested_provider_id: str
    caller_plugin: str
    required_modalities: frozenset[str]
    provider_order: tuple[str, ...]
    request_id: str
    started_at: float
    requested_max_output_tokens: int | None


class DirectRouteService:
    """Route direct plugin Chat Provider calls through the quota policy layer."""

    def __init__(
        self,
        *,
        state: Any,
        refresh_routes: Callable[[], Awaitable[bool]],
        get_router: Callable[[], ProviderQuotaRouter],
        get_provider: Callable[[str], Any],
        report_provider_error: Callable[[Any, Exception], Awaitable[None]],
        logger: Any,
    ) -> None:
        self.state = state
        self.refresh_routes = refresh_routes
        self.get_router = get_router
        self.get_provider = get_provider
        self.report_provider_error = report_provider_error
        self.logger = logger

    async def execute_text(
        self,
        provider: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        context = await self._prepare(provider, args, kwargs)
        remaining_order = context.provider_order
        fallback_remaining = (
            context.settings.direct_provider_fallback_max_candidates
        )
        attempts: list[dict[str, Any]] = []
        last_plan: DirectRoutePlan | None = None
        final_response: Any = None
        actual_provider_id = ""
        final_status = "failed"
        fallback_reason = ""
        try:
            while remaining_order:
                plan = await self._plan(context, remaining_order, stream=False)
                last_plan = plan
                selected_provider_id, selected_provider = self._selected(plan)
                if selected_provider is None:
                    remaining_order = _without(
                        remaining_order,
                        selected_provider_id,
                    )
                    continue
                call_kwargs = direct_call_kwargs(
                    kwargs=kwargs,
                    requested_provider_id=context.requested_provider_id,
                    actual_provider_id=selected_provider_id,
                    requested_max_output_tokens=(
                        context.requested_max_output_tokens
                    ),
                )
                token = bind_provider_guard_route_plan(plan)
                attempt_started = time.perf_counter()
                try:
                    final_response = await selected_provider.text_chat(
                        *args,
                        **call_kwargs,
                    )
                    actual_provider_id = _actual_provider_id(
                        response=final_response,
                        fallback=selected_provider_id,
                    )
                except asyncio.CancelledError:
                    final_status = "cancelled"
                    attempts.append(
                        _attempt_payload(
                            provider_id=selected_provider_id,
                            outcome="cancelled",
                            started_at=attempt_started,
                        )
                    )
                    raise
                except Exception as exc:  # noqa: BLE001
                    actual_provider_id = (
                        current_provider_guard_provider_id()
                        or selected_provider_id
                    )
                    attempts.append(
                        _attempt_payload(
                            provider_id=actual_provider_id,
                            outcome="error",
                            started_at=attempt_started,
                            error=exc,
                        )
                    )
                    if fallback_remaining > 0 and self._allows_fallback(
                        provider=selected_provider,
                        error=exc,
                        settings=context.settings,
                    ):
                        fallback_remaining -= 1
                        fallback_reason = _error_text(exc)
                        remaining_order = _without(
                            remaining_order,
                            selected_provider_id,
                        )
                        continue
                    raise
                finally:
                    reset_provider_guard_route_plan(token)

                if is_provider_error_response(final_response):
                    error = RuntimeError(response_error_text(final_response))
                    attempts.append(
                        _attempt_payload(
                            provider_id=actual_provider_id,
                            outcome="provider_error",
                            started_at=attempt_started,
                            error=error,
                        )
                    )
                    if fallback_remaining > 0 and self._allows_fallback(
                        provider=selected_provider,
                        error=error,
                        settings=context.settings,
                    ):
                        fallback_remaining -= 1
                        fallback_reason = _error_text(error)
                        remaining_order = _without(
                            remaining_order,
                            selected_provider_id,
                        )
                        continue
                    final_status = "provider_error"
                    return final_response

                attempts.append(
                    _attempt_payload(
                        provider_id=actual_provider_id,
                        outcome="success",
                        started_at=attempt_started,
                    )
                )
                final_status = "success"
                return final_response
            raise DirectRouteUnavailableError(
                f"No safe provider is available for "
                f"{context.requested_provider_id}"
            )
        finally:
            await self._finalize(
                plan=last_plan,
                requested_provider_id=context.requested_provider_id,
                actual_provider_id=actual_provider_id,
                response=final_response,
                attempts=attempts,
                final_status=final_status,
                fallback_reason=fallback_reason,
            )

    async def stream(
        self,
        provider: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> AsyncGenerator[Any, None]:
        context = await self._prepare(provider, args, kwargs)
        remaining_order = context.provider_order
        fallback_remaining = (
            context.settings.direct_provider_fallback_max_candidates
        )
        attempts: list[dict[str, Any]] = []
        last_plan: DirectRoutePlan | None = None
        final_response: Any = None
        actual_provider_id = ""
        final_status = "failed"
        fallback_reason = ""
        output_started = False
        try:
            while remaining_order:
                plan = await self._plan(context, remaining_order, stream=True)
                last_plan = plan
                selected_provider_id, selected_provider = self._selected(plan)
                if selected_provider is None:
                    remaining_order = _without(
                        remaining_order,
                        selected_provider_id,
                    )
                    continue
                call_kwargs = direct_call_kwargs(
                    kwargs=kwargs,
                    requested_provider_id=context.requested_provider_id,
                    actual_provider_id=selected_provider_id,
                    requested_max_output_tokens=(
                        context.requested_max_output_tokens
                    ),
                )
                token = bind_provider_guard_route_plan(plan)
                attempt_started = time.perf_counter()
                iterator: Any = None
                try:
                    iterator = selected_provider.text_chat_stream(
                        *args,
                        **call_kwargs,
                    ).__aiter__()
                    try:
                        first = await iterator.__anext__()
                    except StopAsyncIteration as exc:
                        error = RuntimeError("Provider returned an empty stream")
                        await self.report_provider_error(selected_provider, error)
                        raise error from exc
                    actual_provider_id = _actual_provider_id(
                        response=first,
                        fallback=selected_provider_id,
                    )
                    if is_provider_error_response(first):
                        raise RuntimeError(response_error_text(first))
                    final_response = first
                    output_started = True
                    yield first
                    async for item in iterator:
                        final_response = item
                        yield item
                    attempts.append(
                        _attempt_payload(
                            provider_id=actual_provider_id,
                            outcome="success",
                            started_at=attempt_started,
                        )
                    )
                    final_status = "success"
                    return
                except asyncio.CancelledError:
                    final_status = "cancelled"
                    attempts.append(
                        _attempt_payload(
                            provider_id=(
                                actual_provider_id or selected_provider_id
                            ),
                            outcome="cancelled",
                            started_at=attempt_started,
                        )
                    )
                    raise
                except Exception as exc:  # noqa: BLE001
                    actual_provider_id = (
                        current_provider_guard_provider_id()
                        or actual_provider_id
                        or selected_provider_id
                    )
                    attempts.append(
                        _attempt_payload(
                            provider_id=actual_provider_id,
                            outcome="error",
                            started_at=attempt_started,
                            error=exc,
                        )
                    )
                    if (
                        not output_started
                        and fallback_remaining > 0
                        and self._allows_fallback(
                            provider=selected_provider,
                            error=exc,
                            settings=context.settings,
                        )
                    ):
                        fallback_remaining -= 1
                        fallback_reason = _error_text(exc)
                        remaining_order = _without(
                            remaining_order,
                            selected_provider_id,
                        )
                        if iterator is not None:
                            try:
                                await iterator.aclose()
                            except Exception:  # noqa: BLE001
                                pass
                        continue
                    raise
                finally:
                    reset_provider_guard_route_plan(token)
            raise DirectRouteUnavailableError(
                f"No safe provider is available for "
                f"{context.requested_provider_id}"
            )
        finally:
            await self._finalize(
                plan=last_plan,
                requested_provider_id=context.requested_provider_id,
                actual_provider_id=actual_provider_id,
                response=final_response,
                attempts=attempts,
                final_status=final_status,
                fallback_reason=fallback_reason,
            )

    async def _prepare(
        self,
        provider: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> _DirectCallContext:
        await self.refresh_routes()
        router = self.get_router()
        settings = router.settings
        requested_provider_id, _, _ = provider_identity(provider)
        if not requested_provider_id:
            raise DirectRouteUnavailableError(
                "Direct plugin Provider has no configured ID"
            )
        policy = build_provider_policy(provider=provider, settings=settings)
        return _DirectCallContext(
            router=router,
            settings=settings,
            requested_provider_id=requested_provider_id,
            caller_plugin=detect_plugin_caller(),
            required_modalities=direct_required_modalities(args, kwargs),
            provider_order=build_direct_provider_order(
                requested_provider_id=requested_provider_id,
                chains=settings.chains,
            ),
            request_id=f"direct-{uuid.uuid4().hex}",
            started_at=time.time(),
            requested_max_output_tokens=policy.max_output_tokens,
        )

    async def _plan(
        self,
        context: _DirectCallContext,
        provider_order: tuple[str, ...],
        *,
        stream: bool,
    ) -> DirectRoutePlan:
        window = current_window(
            timezone_name=context.settings.timezone,
            reset_time=context.settings.reset_time,
        )
        decision = await context.router.decide_order_and_reserve(
            request_id=context.request_id,
            current_provider_id=context.requested_provider_id,
            provider_order=provider_order,
            window=window,
            required_modalities=set(context.required_modalities),
        )
        return DirectRoutePlan(
            request_id=context.request_id,
            router=context.router,
            settings=context.settings,
            window=window,
            required_modalities=context.required_modalities,
            requested_provider_id=context.requested_provider_id,
            provider_order=provider_order,
            decision=decision,
            caller_plugin=context.caller_plugin,
            stream=stream,
            started_at=context.started_at,
        )

    def _selected(self, plan: DirectRoutePlan) -> tuple[str, Any]:
        selected_provider_id = str(plan.decision.selected_provider_id or "")
        if not selected_provider_id:
            raise DirectRouteUnavailableError(
                unavailable_message(
                    requested_provider_id=plan.requested_provider_id,
                    decision=plan.decision,
                )
            )
        return selected_provider_id, self.get_provider(selected_provider_id)

    @staticmethod
    def _allows_fallback(
        *,
        provider: Any,
        error: Exception,
        settings: RouterSettings,
    ) -> bool:
        policy = build_provider_policy(provider=provider, settings=settings)
        disposition = classify_provider_error(error=error, policy=policy)
        return should_fallback_direct(error=error, disposition=disposition)

    async def _finalize(
        self,
        *,
        plan: DirectRoutePlan | None,
        requested_provider_id: str,
        actual_provider_id: str,
        response: Any,
        attempts: list[dict[str, Any]],
        final_status: str,
        fallback_reason: str,
    ) -> None:
        if plan is None:
            return
        actual_provider_id = str(
            actual_provider_id or plan.decision.selected_provider_id or ""
        )
        actual_provider = self.get_provider(actual_provider_id)
        _, _, actual_provider_model = provider_identity(actual_provider)
        usage = getattr(response, "usage", None)
        actual_tokens = int(getattr(usage, "total", 0) or 0) if usage else None
        actual_is_quota_managed = plan.router.is_token_quota_managed(
            actual_provider_id
        )
        try:
            await self.state.release(
                request_id=plan.request_id,
                actual_tokens=(
                    actual_tokens if actual_is_quota_managed else None
                ),
                overlay_ttl_seconds=plan.settings.overlay_ttl_seconds,
                actual_provider_id=(
                    actual_provider_id if actual_is_quota_managed else None
                ),
                actual_provider_model=(
                    actual_provider_model if actual_is_quota_managed else None
                ),
                actual_quota_key=(
                    plan.router.quota_key_for(
                        actual_provider_id,
                        actual_provider_model,
                    )
                    if actual_is_quota_managed
                    else None
                ),
            )
            if actual_is_quota_managed and actual_provider_id:
                await plan.router.ensure_cooldown(
                    provider_id=actual_provider_id,
                    provider_model=actual_provider_model,
                    window=plan.window,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "[ProviderQuotaRouter] failed to finalize direct reservation: "
                "request_id=%s error=%s",
                plan.request_id,
                exc,
                exc_info=True,
            )
        payload = decision_payload(
            request_id=plan.request_id,
            window=plan.window,
            decision=plan.decision,
            dry_run=plan.settings.dry_run,
        )
        payload.update(
            {
                "route_kind": ROUTE_KIND_PLUGIN_DIRECT,
                "caller_plugin": plan.caller_plugin,
                "conversation": f"plugin:{plan.caller_plugin}",
                "requested_provider_id": requested_provider_id,
                "selected_provider_id": actual_provider_id or None,
                "required_modalities": sorted(plan.required_modalities),
                "stream": plan.stream,
                "direct_status": final_status,
                "direct_attempts": attempts,
                "fallback_reason": fallback_reason or None,
                "planning_elapsed_ms": None,
                "elapsed_ms": round(
                    (time.time() - plan.started_at) * 1000,
                    3,
                ),
                "affinity_status": "bypass_direct",
                "affinity_provider_id": None,
                "affinity_modality": None,
                "affinity_expires_at": None,
            }
        )
        try:
            await self.state.record_decision(payload)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "[ProviderQuotaRouter] failed to record direct route decision: %s",
                exc,
                exc_info=True,
            )
        if actual_provider_id and actual_provider_id != requested_provider_id:
            requested_state = next(
                (
                    item
                    for item in plan.decision.candidates
                    if item.provider_id == requested_provider_id
                ),
                None,
            )
            reason = fallback_reason or (
                requested_state.reason if requested_state else "fallback"
            )
            self.logger.info(
                "[ProviderQuotaRouter] plugin direct route: "
                "caller=%s requested_provider=%s actual_provider=%s "
                "status=%s reason=%s elapsed_ms=%.3f",
                plan.caller_plugin,
                requested_provider_id,
                actual_provider_id,
                final_status,
                reason,
                payload["elapsed_ms"],
            )


def build_direct_provider_order(
    *,
    requested_provider_id: str,
    chains: list[ChainConfig],
) -> tuple[str, ...]:
    """Keep the requested Provider first, then append its global chain."""
    requested_provider_id = str(requested_provider_id or "").strip()
    selected_chain = next(
        (
            chain
            for chain in chains
            if requested_provider_id in chain.providers
        ),
        chains[0] if chains else None,
    )
    ordered = [requested_provider_id]
    if selected_chain is not None:
        ordered.extend(selected_chain.providers)
    return tuple(dict.fromkeys(item for item in ordered if item))


def direct_required_modalities(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> frozenset[str]:
    """Infer chat modalities from Provider.text_chat compatible arguments."""
    required = {"text"}
    image_urls = _argument(args, kwargs, "image_urls", 2)
    audio_urls = _argument(args, kwargs, "audio_urls", 3)
    func_tool = _argument(args, kwargs, "func_tool", 4)
    if image_urls:
        required.add("image")
    if audio_urls:
        required.add("audio")
    if func_tool:
        required.add("tool_use")
    return frozenset(required)


def direct_call_kwargs(
    *,
    kwargs: dict[str, Any],
    requested_provider_id: str,
    actual_provider_id: str,
    requested_max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Do not leak one Provider's explicit model override to a fallback."""
    result = kwargs
    if requested_provider_id != actual_provider_id and "model" in result:
        result = dict(result)
        result.pop("model", None)
    if requested_max_output_tokens is None or requested_max_output_tokens <= 0:
        return result
    if result is kwargs:
        result = dict(result)
    token_keys = tuple(
        key
        for key in ("max_tokens", "max_completion_tokens")
        if key in result
    )
    if not token_keys:
        result["max_tokens"] = requested_max_output_tokens
        return result
    for key in token_keys:
        try:
            requested = int(result[key])
        except (TypeError, ValueError):
            requested = 0
        result[key] = (
            min(requested, requested_max_output_tokens)
            if requested > 0
            else requested_max_output_tokens
        )
    return result


def unavailable_message(
    *,
    requested_provider_id: str,
    decision: RouteDecision,
) -> str:
    reasons = ", ".join(
        f"{item.provider_id}={item.reason}" for item in decision.candidates
    )
    suffix = f": {reasons}" if reasons else ""
    return (
        f"No safe provider is available for direct plugin request "
        f"{requested_provider_id}{suffix}"
    )


def _argument(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    name: str,
    index: int,
) -> Any:
    if name in kwargs:
        return kwargs[name]
    return args[index] if len(args) > index else None


def _without(
    provider_order: tuple[str, ...],
    provider_id: str,
) -> tuple[str, ...]:
    return tuple(item for item in provider_order if item != provider_id)


def _actual_provider_id(*, response: Any, fallback: str) -> str:
    return str(
        getattr(response, "_provider_quota_router_provider_id", "")
        or current_provider_guard_provider_id()
        or fallback
    )


def _attempt_payload(
    *,
    provider_id: str,
    outcome: str,
    started_at: float,
    error: Exception | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider_id": provider_id,
        "outcome": outcome,
        "elapsed_ms": round(
            (time.perf_counter() - started_at) * 1000,
            3,
        ),
    }
    if error is not None:
        payload["error"] = _error_text(error)
    return payload


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:300]
