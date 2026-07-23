from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextvars import ContextVar, Token
from datetime import datetime
from typing import Any


_PATCH_STATE_ATTR = "_provider_quota_router_opencode_quota_guard_state"
_BYPASS_SCOPES: ContextVar[frozenset[str]] = ContextVar(
    "provider_quota_router_guard_bypass_scopes",
    default=frozenset(),
)
_ROUTE_PLAN: ContextVar[Any | None] = ContextVar(
    "provider_quota_router_route_plan",
    default=None,
)
_ACTUAL_PROVIDER_ID: ContextVar[str] = ContextVar(
    "provider_quota_router_actual_provider_id",
    default="",
)


class ProviderModelCooldownError(RuntimeError):
    pass


class ProviderAttemptTimeoutError(TimeoutError):
    pass


# Backward-compatible import name for existing integrations and tests.
OpenCodeQuotaCooldownError = ProviderModelCooldownError


def begin_provider_guard_bypass(*scopes: str) -> Token[frozenset[str]]:
    """Bypass selected cooldown scopes in only the current async task."""
    return _BYPASS_SCOPES.set(
        frozenset(str(scope) for scope in scopes if str(scope))
    )


def end_provider_guard_bypass(token: Token[frozenset[str]]) -> None:
    _BYPASS_SCOPES.reset(token)


def bind_provider_guard_route_plan(
    plan: Any,
) -> tuple[Token[Any | None], Token[str]]:
    return _ROUTE_PLAN.set(plan), _ACTUAL_PROVIDER_ID.set("")


def reset_provider_guard_route_plan(
    token: tuple[Token[Any | None], Token[str]],
) -> None:
    route_token, provider_token = token
    _ROUTE_PLAN.reset(route_token)
    _ACTUAL_PROVIDER_ID.reset(provider_token)


def current_provider_guard_provider_id() -> str:
    return _ACTUAL_PROVIDER_ID.get()


def install_opencode_quota_guard(
    owner: object,
    provider_cls: type | None = None,
) -> bool:
    provider_cls = provider_cls or _load_provider_class()
    existing = getattr(provider_cls, _PATCH_STATE_ATTR, None)
    if isinstance(existing, dict):
        existing["owners"].add(owner)
        return (
            provider_cls.text_chat is existing["text_chat_wrapper"]
            and provider_cls.text_chat_stream is existing["text_chat_stream_wrapper"]
        )

    state: dict[str, Any] = {
        "owners": {owner},
        "original_text_chat": provider_cls.text_chat,
        "original_text_chat_stream": provider_cls.text_chat_stream,
    }

    async def guarded_text_chat(provider: Any, *args: Any, **kwargs: Any) -> Any:
        await _raise_if_cooling(state, provider)
        await _report_attempt(state, provider)
        kwargs = _with_request_max_retries(state, provider, kwargs)
        try:
            call = state["original_text_chat"](provider, *args, **kwargs)
            response = await _with_attempt_timeout(state, provider, call)
            _mark_response_provider(response, provider)
            is_error = await _report_response_error_if_needed(
                state,
                provider,
                response,
            )
            if not is_error:
                await _report_success(state, provider)
            return response
        except Exception as exc:  # noqa: BLE001
            await _report_error(state, provider, exc)
            raise

    async def guarded_text_chat_stream(
        provider: Any,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        await _raise_if_cooling(state, provider)
        await _report_attempt(state, provider)
        kwargs = _with_request_max_retries(state, provider, kwargs)
        stream = state["original_text_chat_stream"](provider, *args, **kwargs)
        iterator = stream.__aiter__()
        try:
            try:
                first = await _with_attempt_timeout(
                    state,
                    provider,
                    iterator.__anext__(),
                )
            except StopAsyncIteration:
                return
            _mark_response_provider(first, provider)
            is_error = await _report_response_error_if_needed(
                state,
                provider,
                first,
            )
            if not is_error:
                await _report_success(state, provider)
            yield first
            async for item in iterator:
                _mark_response_provider(item, provider)
                await _report_response_error_if_needed(state, provider, item)
                yield item
        except Exception as exc:  # noqa: BLE001
            await _report_error(state, provider, exc)
            raise

    state["text_chat_wrapper"] = guarded_text_chat
    state["text_chat_stream_wrapper"] = guarded_text_chat_stream
    setattr(provider_cls, _PATCH_STATE_ATTR, state)
    provider_cls.text_chat = guarded_text_chat
    provider_cls.text_chat_stream = guarded_text_chat_stream
    return True


def uninstall_opencode_quota_guard(
    owner: object,
    provider_cls: type | None = None,
) -> None:
    provider_cls = provider_cls or _load_provider_class()
    state = getattr(provider_cls, _PATCH_STATE_ATTR, None)
    if not isinstance(state, dict):
        return
    state["owners"].discard(owner)
    if state["owners"]:
        return
    if provider_cls.text_chat is state["text_chat_wrapper"]:
        provider_cls.text_chat = state["original_text_chat"]
    if provider_cls.text_chat_stream is state["text_chat_stream_wrapper"]:
        provider_cls.text_chat_stream = state["original_text_chat_stream"]
    delattr(provider_cls, _PATCH_STATE_ATTR)


def is_opencode_quota_guard_installed(provider_cls: type | None = None) -> bool:
    provider_cls = provider_cls or _load_provider_class()
    state = getattr(provider_cls, _PATCH_STATE_ATTR, None)
    return bool(
        isinstance(state, dict)
        and provider_cls.text_chat is state.get("text_chat_wrapper")
        and provider_cls.text_chat_stream is state.get("text_chat_stream_wrapper")
    )


def _load_provider_class() -> type:
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

    return ProviderOpenAIOfficial


def _with_request_max_retries(
    state: dict[str, Any],
    provider: Any,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    values: list[int] = []
    for owner in tuple(state["owners"]):
        getter = getattr(owner, "opencode_quota_guard_request_max_retries", None)
        if not callable(getter):
            continue
        try:
            values.append(max(1, int(getter(provider))))
        except Exception:  # noqa: BLE001
            continue
    if not values:
        return kwargs
    guarded_kwargs = dict(kwargs)
    guarded_kwargs["request_max_retries"] = min(values)
    return guarded_kwargs


async def _with_attempt_timeout(
    state: dict[str, Any],
    provider: Any,
    awaitable: Any,
) -> Any:
    timeout_seconds = _attempt_timeout_seconds(state, provider)
    if timeout_seconds <= 0:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if task in done:
        return task.result()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    provider_id = str(
        getattr(provider, "provider_config", {}).get("id") or "provider"
    )
    raise ProviderAttemptTimeoutError(
        f"{provider_id} first response timed out after "
        f"{timeout_seconds:g} seconds"
    )


def _attempt_timeout_seconds(state: dict[str, Any], provider: Any) -> float:
    values: list[float] = []
    for owner in tuple(state["owners"]):
        getter = getattr(owner, "opencode_quota_guard_timeout_seconds", None)
        if not callable(getter):
            continue
        try:
            value = float(getter(provider))
        except Exception:  # noqa: BLE001
            continue
        if value > 0:
            values.append(value)
    return min(values) if values else 0.0


async def _raise_if_cooling(state: dict[str, Any], provider: Any) -> None:
    bypass_scopes = _BYPASS_SCOPES.get()
    for owner in tuple(state["owners"]):
        checker = getattr(owner, "opencode_quota_guard_cooldown", None)
        if not callable(checker):
            continue
        try:
            try:
                cooldown = await checker(
                    provider,
                    bypass_scopes=bypass_scopes,
                )
            except TypeError as exc:
                if "bypass_scopes" not in str(exc):
                    raise
                cooldown = await checker(provider)
        except Exception:  # noqa: BLE001
            continue
        if not cooldown:
            continue
        retry_at = float(
            cooldown.get("expires_at") or cooldown.get("retry_at") or 0
        )
        retry_text = (
            datetime.fromtimestamp(retry_at).astimezone().isoformat(timespec="seconds")
            if retry_at
            else "next reset"
        )
        provider_id = str(
            getattr(provider, "provider_config", {}).get("id") or "opencode"
        )
        reason = str(cooldown.get("reason") or "provider_error")
        raise ProviderModelCooldownError(
            f"{provider_id} is cooling down until {retry_text} ({reason})"
        )


def _mark_response_provider(response: Any, provider: Any) -> None:
    provider_id = str(
        getattr(provider, "provider_config", {}).get("id") or ""
    )
    if not provider_id or response is None:
        return
    try:
        setattr(response, "_provider_quota_router_provider_id", provider_id)
    except Exception:  # noqa: BLE001
        return


async def _report_response_error_if_needed(
    state: dict[str, Any],
    provider: Any,
    response: Any,
) -> bool:
    if str(getattr(response, "role", "") or "").casefold() != "err":
        return False
    error_text = str(
        getattr(response, "completion_text", "") or "Provider returned role=err"
    )
    await _report_error(state, provider, RuntimeError(error_text))
    return True


async def _report_error(
    state: dict[str, Any],
    provider: Any,
    exc: Exception,
) -> None:
    for owner in tuple(state["owners"]):
        handler = getattr(owner, "opencode_quota_guard_error", None)
        if not callable(handler):
            continue
        try:
            await handler(provider, exc)
        except Exception:  # noqa: BLE001
            continue


async def _report_success(
    state: dict[str, Any],
    provider: Any,
) -> None:
    for owner in tuple(state["owners"]):
        handler = getattr(owner, "opencode_quota_guard_success", None)
        if not callable(handler):
            continue
        try:
            await handler(provider)
        except Exception:  # noqa: BLE001
            continue


async def _report_attempt(state: dict[str, Any], provider: Any) -> None:
    route_plan = _ROUTE_PLAN.get()
    if route_plan is None:
        return
    provider_id = str(
        getattr(provider, "provider_config", {}).get("id") or ""
    )
    if provider_id:
        _ACTUAL_PROVIDER_ID.set(provider_id)
    for owner in tuple(state["owners"]):
        handler = getattr(owner, "opencode_quota_guard_attempt", None)
        if not callable(handler):
            continue
        try:
            await handler(provider, route_plan)
        except Exception:  # noqa: BLE001
            continue
