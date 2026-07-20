from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


def event_platform(event: Any) -> str:
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            value = str(getter() or "").strip()
            if value:
                return value
        except Exception:  # noqa: BLE001
            pass
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    if ":" in origin:
        return origin.split(":", 1)[0]
    return "aiocqhttp"


def resolve_admin_targets(
    *,
    context: Any,
    event: Any,
    configured_admin_ids: Iterable[str],
) -> list[str]:
    raw_targets = [str(item).strip() for item in configured_admin_ids if str(item).strip()]
    if not raw_targets:
        try:
            config = context.get_config() or {}
            raw_targets = [
                str(item).strip()
                for item in config.get("admins_id", []) or []
                if str(item).strip()
            ]
        except Exception:  # noqa: BLE001
            raw_targets = []

    platform = event_platform(event)
    current_origin = str(getattr(event, "unified_msg_origin", "") or "")
    resolved: list[str] = []
    seen: set[str] = set()
    for item in raw_targets:
        target = (
            item
            if item.count(":") >= 2 and "Message" in item
            else f"{platform}:FriendMessage:{item}"
        )
        if not target or target == current_origin or target in seen:
            continue
        seen.add(target)
        resolved.append(target)
    return resolved


def build_provider_error_alert(
    *,
    provider_id: str,
    error_text: str,
    source_origin: str,
    circuit_retry_at: float | None,
    interval_seconds: int,
    model_cooldown_until: float | None = None,
) -> str:
    lines = [
        "[ProviderQuotaRouter] Provider 调用失败",
        f"模型：{provider_id or '-'}",
        f"来源：{source_origin or '-'}",
    ]
    if circuit_retry_at:
        retry_text = datetime.fromtimestamp(float(circuit_retry_at)).astimezone().isoformat(
            timespec="seconds"
        )
        lines.extend(
            [
                "处理：火山模型组已进入 30 分钟冷却，当前请求后续将使用非火山 fallback。",
                f"下次探测：{retry_text}",
            ]
        )
    elif model_cooldown_until:
        retry_text = datetime.fromtimestamp(
            float(model_cooldown_until)
        ).astimezone().isoformat(timespec="seconds")
        lines.extend(
            [
                "处理：该 opencode 免费模型已冷却，其他候选仍可继续使用。",
                f"恢复时间：{retry_text}",
            ]
        )
    else:
        lines.append("处理：错误回复已从原会话静默移除，请检查对应 Provider。")
    normalized_error = " ".join(str(error_text or "未知错误").split())
    lines.append(f"错误：{normalized_error[:800]}")
    lines.append(f"告警限频：{max(1, int(interval_seconds)) // 60} 分钟内不重复发送。")
    return "\n".join(lines)
