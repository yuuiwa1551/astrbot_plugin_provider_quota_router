from __future__ import annotations

from typing import Any


CORE_FALLBACK_GUARD_EXTRA_KEY = "provider_quota_router_disable_core_fallback"
CORE_FALLBACK_DROPPED_EXTRA_KEY = "provider_quota_router_core_fallback_dropped"

_PATCH_STATE_ATTR = "_provider_quota_router_fallback_guard_state"
_POSITIONAL_FALLBACK_INDEX = 14


def install_core_fallback_guard(owner: object, runner_cls: type | None = None) -> bool:
    runner_cls = runner_cls or _load_runner_class()
    state = getattr(runner_cls, _PATCH_STATE_ATTR, None)
    if isinstance(state, dict):
        state["owners"].add(owner)
        return runner_cls.reset is state["wrapper"]

    original_reset = runner_cls.reset

    async def guarded_reset(runner: Any, *args: Any, **kwargs: Any) -> None:
        event = _event_from_reset_call(args, kwargs)
        if _event_requests_guard(event):
            args, kwargs, dropped = _without_fallback_providers(args, kwargs)
            if dropped:
                setter = getattr(event, "set_extra", None)
                if callable(setter):
                    setter(CORE_FALLBACK_DROPPED_EXTRA_KEY, dropped)
        await original_reset(runner, *args, **kwargs)

    state = {
        "original": original_reset,
        "wrapper": guarded_reset,
        "owners": {owner},
    }
    setattr(runner_cls, _PATCH_STATE_ATTR, state)
    runner_cls.reset = guarded_reset
    return True


def uninstall_core_fallback_guard(owner: object, runner_cls: type | None = None) -> None:
    runner_cls = runner_cls or _load_runner_class()
    state = getattr(runner_cls, _PATCH_STATE_ATTR, None)
    if not isinstance(state, dict):
        return
    state["owners"].discard(owner)
    if state["owners"]:
        return
    if runner_cls.reset is state["wrapper"]:
        runner_cls.reset = state["original"]
    delattr(runner_cls, _PATCH_STATE_ATTR)


def is_core_fallback_guard_installed(runner_cls: type | None = None) -> bool:
    runner_cls = runner_cls or _load_runner_class()
    state = getattr(runner_cls, _PATCH_STATE_ATTR, None)
    return isinstance(state, dict) and runner_cls.reset is state.get("wrapper")


def _load_runner_class() -> type:
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner

    return ToolLoopAgentRunner


def _event_from_reset_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    run_context = kwargs.get("run_context")
    if run_context is None and len(args) > 2:
        run_context = args[2]
    return getattr(getattr(run_context, "context", None), "event", None)


def _event_requests_guard(event: Any) -> bool:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return False
    return bool(getter(CORE_FALLBACK_GUARD_EXTRA_KEY))


def _without_fallback_providers(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any], list[str]]:
    updated_kwargs = dict(kwargs)
    if "fallback_providers" in updated_kwargs:
        fallback_providers = updated_kwargs.get("fallback_providers") or []
        updated_kwargs["fallback_providers"] = []
        return args, updated_kwargs, _provider_ids(fallback_providers)

    if len(args) > _POSITIONAL_FALLBACK_INDEX:
        updated_args = list(args)
        fallback_providers = updated_args[_POSITIONAL_FALLBACK_INDEX] or []
        updated_args[_POSITIONAL_FALLBACK_INDEX] = []
        return tuple(updated_args), updated_kwargs, _provider_ids(fallback_providers)

    return args, updated_kwargs, []


def _provider_ids(providers: Any) -> list[str]:
    result: list[str] = []
    for provider in providers:
        provider_config = getattr(provider, "provider_config", {}) or {}
        provider_id = str(provider_config.get("id") or "")
        result.append(provider_id or type(provider).__name__)
    return result
