from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any


_PATCH_STATE_ATTR = "_provider_quota_router_opencode_quota_guard_state"


class ProviderModelCooldownError(RuntimeError):
    pass


# Backward-compatible import name for existing integrations and tests.
OpenCodeQuotaCooldownError = ProviderModelCooldownError


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
        kwargs = _with_request_max_retries(state, provider, kwargs)
        try:
            return await state["original_text_chat"](provider, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            await _report_error(state, provider, exc)
            raise

    async def guarded_text_chat_stream(
        provider: Any,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        await _raise_if_cooling(state, provider)
        kwargs = _with_request_max_retries(state, provider, kwargs)
        try:
            async for item in state["original_text_chat_stream"](
                provider, *args, **kwargs
            ):
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


async def _raise_if_cooling(state: dict[str, Any], provider: Any) -> None:
    for owner in tuple(state["owners"]):
        checker = getattr(owner, "opencode_quota_guard_cooldown", None)
        if not callable(checker):
            continue
        try:
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
