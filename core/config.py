from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


EXHAUSTED_ACTIONS = {"stop", "allow_paid", "use_last"}
QUOTA_KEY_MODES = {"provider_model", "provider_id"}


@dataclass(frozen=True)
class ChainConfig:
    name: str
    providers: list[str]
    daily_limit_tokens: int | None = None
    safety_buffer_tokens: int | None = None
    request_reservation_tokens: int | None = None

    def limit(self, default_value: int) -> int:
        return max(0, int(self.daily_limit_tokens or default_value))

    def safety_buffer(self, default_value: int) -> int:
        return max(0, int(self.safety_buffer_tokens if self.safety_buffer_tokens is not None else default_value))

    def reservation(self, default_value: int) -> int:
        return max(0, int(self.request_reservation_tokens if self.request_reservation_tokens is not None else default_value))


@dataclass(frozen=True)
class RouterSettings:
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    reset_time: str = "11:00"
    default_daily_limit_tokens: int = 2_000_000
    default_safety_buffer_tokens: int = 100_000
    default_request_reservation_tokens: int = 50_000
    reservation_ttl_seconds: int = 1800
    overlay_ttl_seconds: int = 180
    count_cached_input_tokens: bool = True
    quota_key_mode: str = "provider_model"
    exhausted_action: str = "stop"
    dry_run: bool = False
    use_astrbot_fallback_chain: bool = True
    fallback_watch_interval_seconds: int = 300
    strict_priority_order: bool = True
    disable_astrbot_error_fallback: bool = False
    quota_cooldown_seconds: int = 86_400
    unlimited_provider_prefixes: tuple[str, ...] = ("deepseek/",)
    upstream_quota_provider_prefixes: tuple[str, ...] = ("opencode-zen/",)
    volcengine_403_circuit_enabled: bool = True
    volcengine_provider_source_ids: tuple[str, ...] = ("openai",)
    volcengine_403_cooldown_seconds: int = 1_800
    volcengine_probe_check_interval_seconds: int = 30
    volcengine_probe_timeout_seconds: int = 30
    provider_error_cooldown_enabled: bool = True
    provider_error_cooldown_seconds: int = 1_800
    provider_error_admin_notify_enabled: bool = True
    provider_error_admin_notify_interval_seconds: int = 3_600
    provider_error_suppress_current_chat: bool = True
    allow_status_for_all: bool = True
    admin_user_ids: set[str] = field(default_factory=set)
    exhausted_message: str = (
        "当前模型链路今日免费 token 额度已用尽，将在 {refresh_time} 后恢复。"
    )
    chains: list[ChainConfig] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "RouterSettings":
        raw = dict(raw or {})
        chains = _load_chains(raw.get("chains"), raw.get("chains_json"))
        exhausted_action = str(raw.get("exhausted_action", "stop") or "stop")
        if exhausted_action not in EXHAUSTED_ACTIONS:
            exhausted_action = "stop"
        quota_key_mode = str(raw.get("quota_key_mode", "provider_model") or "provider_model")
        if quota_key_mode not in QUOTA_KEY_MODES:
            quota_key_mode = "provider_model"
        return cls(
            enabled=bool(raw.get("enabled", True)),
            timezone=str(raw.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"),
            reset_time=str(raw.get("reset_time", "11:00") or "11:00"),
            default_daily_limit_tokens=_positive_int(raw.get("default_daily_limit_tokens"), 2_000_000),
            default_safety_buffer_tokens=_positive_int(raw.get("default_safety_buffer_tokens"), 100_000),
            default_request_reservation_tokens=_positive_int(raw.get("default_request_reservation_tokens"), 50_000),
            reservation_ttl_seconds=_positive_int(raw.get("reservation_ttl_seconds"), 1800),
            overlay_ttl_seconds=_positive_int(raw.get("overlay_ttl_seconds"), 180),
            count_cached_input_tokens=bool(raw.get("count_cached_input_tokens", True)),
            quota_key_mode=quota_key_mode,
            exhausted_action=exhausted_action,
            dry_run=bool(raw.get("dry_run", False)),
            use_astrbot_fallback_chain=bool(raw.get("use_astrbot_fallback_chain", True)),
            fallback_watch_interval_seconds=max(
                1, _positive_int(raw.get("fallback_watch_interval_seconds"), 300)
            ),
            strict_priority_order=bool(raw.get("strict_priority_order", True)),
            disable_astrbot_error_fallback=bool(
                raw.get("disable_astrbot_error_fallback", False)
            ),
            quota_cooldown_seconds=_positive_int(
                raw.get("quota_cooldown_seconds"), 86_400
            ),
            unlimited_provider_prefixes=_string_tuple(
                raw.get("unlimited_provider_prefixes"), ("deepseek/",)
            ),
            upstream_quota_provider_prefixes=_string_tuple(
                raw.get("upstream_quota_provider_prefixes"),
                ("opencode-zen/",),
            ),
            volcengine_403_circuit_enabled=bool(
                raw.get("volcengine_403_circuit_enabled", True)
            ),
            volcengine_provider_source_ids=_string_tuple(
                raw.get("volcengine_provider_source_ids"), ("openai",)
            ),
            volcengine_403_cooldown_seconds=_positive_int(
                raw.get("volcengine_403_cooldown_seconds"), 1_800
            ),
            volcengine_probe_check_interval_seconds=max(
                5,
                _positive_int(
                    raw.get("volcengine_probe_check_interval_seconds"), 30
                ),
            ),
            volcengine_probe_timeout_seconds=max(
                5, _positive_int(raw.get("volcengine_probe_timeout_seconds"), 30)
            ),
            provider_error_cooldown_enabled=bool(
                raw.get("provider_error_cooldown_enabled", True)
            ),
            provider_error_cooldown_seconds=_positive_int(
                raw.get("provider_error_cooldown_seconds"), 1_800
            ),
            provider_error_admin_notify_enabled=bool(
                raw.get("provider_error_admin_notify_enabled", True)
            ),
            provider_error_admin_notify_interval_seconds=max(
                60,
                _positive_int(
                    raw.get("provider_error_admin_notify_interval_seconds"),
                    3_600,
                ),
            ),
            provider_error_suppress_current_chat=bool(
                raw.get("provider_error_suppress_current_chat", True)
            ),
            allow_status_for_all=bool(raw.get("allow_status_for_all", True)),
            admin_user_ids={str(item).strip() for item in raw.get("admin_user_ids", []) if str(item).strip()},
            exhausted_message=str(raw.get("exhausted_message") or cls.exhausted_message),
            chains=chains,
        )

    def is_unlimited_provider(self, provider_id: str) -> bool:
        normalized = str(provider_id or "").casefold()
        return any(
            normalized.startswith(prefix.casefold())
            for prefix in self.unlimited_provider_prefixes
            if prefix
        )

    def is_upstream_quota_provider(self, provider_id: str) -> bool:
        normalized = str(provider_id or "").casefold()
        return any(
            normalized.startswith(prefix.casefold())
            for prefix in self.upstream_quota_provider_prefixes
            if prefix
        )

    def is_token_quota_managed(self, provider_source_id: str) -> bool:
        """Only configured Volcengine provider sources use the local token cap."""
        return self.is_volcengine_source(provider_source_id)

    def is_volcengine_source(self, provider_source_id: str) -> bool:
        normalized = str(provider_source_id or "").casefold()
        return any(
            normalized == source_id.casefold()
            for source_id in self.volcengine_provider_source_ids
            if source_id
        )


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, parsed)


def _load_chains(raw_chains: Any, raw_json: Any) -> list[ChainConfig]:
    data: Any = raw_chains
    if raw_json:
        try:
            data = json.loads(str(raw_json))
        except json.JSONDecodeError as exc:
            raise ValueError(f"chains_json is not valid JSON: {exc}") from exc
    if not data:
        return []
    if not isinstance(data, list):
        raise ValueError("chains must be a list")
    chains: list[ChainConfig] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        providers = [str(p).strip() for p in item.get("providers", []) if str(p).strip()]
        if not providers:
            continue
        chains.append(
            ChainConfig(
                name=str(item.get("name") or f"chain-{idx + 1}"),
                providers=providers,
                daily_limit_tokens=_optional_int(item.get("daily_limit_tokens")),
                safety_buffer_tokens=_optional_int(item.get("safety_buffer_tokens")),
                request_reservation_tokens=_optional_int(item.get("request_reservation_tokens")),
            )
        )
    return chains


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        return default
    normalized = tuple(
        dict.fromkeys(str(item).strip() for item in items if str(item).strip())
    )
    return normalized


def is_quota_only_exhaustion(reasons: list[str]) -> bool:
    return bool(reasons) and all(
        reason
        in {
            "quota_exceeded",
            "cooldown_active",
            "upstream_quota_cooldown",
            "provider_group_cooldown",
            "provider_group_probe",
        }
        for reason in reasons
    )
