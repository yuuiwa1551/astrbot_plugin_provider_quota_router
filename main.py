from __future__ import annotations

import time
from datetime import date, timedelta
from dataclasses import replace
from pathlib import Path
from typing import Any

from quart import request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
try:
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover
    StarTools = None  # type: ignore[assignment]
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from .core.config import ChainConfig, RouterSettings
from .core.ledger import QuotaLedger
from .core.reports import (
    build_alerts,
    build_summary,
    export_usage_csv,
    read_recent_decisions,
    write_snapshot,
)
from .core.router import ProviderQuotaRouter, decision_payload
from .core.state import QuotaStateStore
from .core.time_window import current_window, window_for_local_date


PLUGIN_NAME = "astrbot_plugin_provider_quota_router"
PLUGIN_VERSION = "0.3.0"
PLUGIN_REPOSITORY = "https://github.com/yuuiwa1551/astrbot_plugin_provider_quota_router"
PLUGIN_DESCRIPTION = "按 provider/model 每日 token 额度自动降级路由 AstrBot 聊天模型。"
HOOK_PRIORITY = 900

CONFIG_KEYS = {
    "enabled",
    "timezone",
    "reset_time",
    "default_daily_limit_tokens",
    "default_safety_buffer_tokens",
    "default_request_reservation_tokens",
    "reservation_ttl_seconds",
    "overlay_ttl_seconds",
    "count_cached_input_tokens",
    "quota_key_mode",
    "exhausted_action",
    "dry_run",
    "use_astrbot_fallback_chain",
    "allow_status_for_all",
    "admin_user_ids",
    "exhausted_message",
    "chains",
    "chains_json",
}


@register(
    PLUGIN_NAME,
    "yuuiwa1551",
    PLUGIN_DESCRIPTION,
    PLUGIN_VERSION,
    PLUGIN_REPOSITORY,
)
class ProviderQuotaRouterPlugin(Star):
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict | None = None,
    ) -> None:
        super().__init__(context)
        self.config = config or {}
        self.settings = self._load_settings()
        self.data_dir = self._resolve_data_dir()
        self.state = QuotaStateStore(self.data_dir)
        self.ledger = QuotaLedger(
            self.context.get_db(),
            count_cached_input_tokens=self.settings.count_cached_input_tokens,
        )
        self.router = self._build_router()
        self._register_web_apis()
        logger.info(
            "[ProviderQuotaRouter] loaded: enabled=%s chains=%d quota_key_mode=%s dry_run=%s",
            self.settings.enabled,
            len(self.settings.chains),
            self.settings.quota_key_mode,
            self.settings.dry_run,
        )

    def _register_web_apis(self) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/status",
            self.api_get_status,
            ["GET"],
            "获取 provider/model 额度路由状态",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/chains",
            self.api_get_chains,
            ["GET"],
            "获取 provider/model 额度路由链",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/decisions",
            self.api_get_decisions,
            ["GET"],
            "获取 provider/model 额度路由决策日志",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/export",
            self.api_get_export,
            ["GET"],
            "导出 provider/model 额度用量 CSV",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/history",
            self.api_get_history,
            ["GET"],
            "获取 provider/model 历史日用量统计",
        )

    def _resolve_data_dir(self) -> Path:
        if StarTools is not None:
            try:
                return StarTools.get_data_dir(PLUGIN_NAME)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ProviderQuotaRouter] failed to get data dir: %s", exc)
        return Path(__file__).resolve().with_name("data")

    def _load_settings(self) -> RouterSettings:
        raw = self._config_to_dict(self.config)
        settings = RouterSettings.from_raw(raw)
        if not settings.chains and settings.use_astrbot_fallback_chain:
            chain = self._default_chain_from_astrbot()
            if chain:
                settings = replace(settings, chains=[chain])
        return settings

    def _build_router(self) -> ProviderQuotaRouter:
        return ProviderQuotaRouter(
            settings=self.settings,
            ledger=self.ledger,
            state=self.state,
            get_provider=self.context.get_provider_by_id,
        )

    def _reload_runtime_settings(self) -> None:
        self.settings = self._load_settings()
        self.ledger = QuotaLedger(
            self.context.get_db(),
            count_cached_input_tokens=self.settings.count_cached_input_tokens,
        )
        self.router = self._build_router()

    @staticmethod
    def _config_to_dict(config: AstrBotConfig | dict | None) -> dict[str, Any]:
        if isinstance(config, dict):
            return dict(config)
        result: dict[str, Any] = {}
        if config is None:
            return result
        getter = getattr(config, "get", None)
        if getter is None:
            return result
        for key in CONFIG_KEYS:
            try:
                value = getter(key)
            except Exception:  # noqa: BLE001
                continue
            if value is not None:
                result[key] = value
        return result

    def _default_chain_from_astrbot(self) -> ChainConfig | None:
        manager = getattr(self.context, "provider_manager", None)
        provider_settings = getattr(manager, "provider_settings", {}) or {}
        default_id = str(provider_settings.get("default_provider_id") or "").strip()
        fallback_ids = provider_settings.get("fallback_chat_models") or []
        providers: list[str] = []
        for provider_id in [default_id, *fallback_ids]:
            provider_id = str(provider_id or "").strip()
            if provider_id and provider_id not in providers:
                providers.append(provider_id)
        if not providers:
            return None
        return ChainConfig(name="astrbot-default", providers=providers)

    @filter.on_waiting_llm_request(priority=HOOK_PRIORITY)
    async def on_waiting_llm_request(self, event: AstrMessageEvent) -> None:
        if not self.settings.enabled:
            return
        current_provider_id = self._current_provider_id(event)
        if not current_provider_id:
            return
        window = current_window(
            timezone_name=self.settings.timezone,
            reset_time=self.settings.reset_time,
        )
        request_id = self._request_id(event)
        decision = await self.router.decide(
            current_provider_id=current_provider_id,
            window=window,
            required_modalities=self._required_modalities(event),
        )
        await self.state.record_decision(
            decision_payload(
                request_id=request_id,
                window=window,
                decision=decision,
                dry_run=self.settings.dry_run,
            )
        )
        if decision.action == "skip":
            return
        event.set_extra("provider_quota_router_request_id", request_id)
        event.set_extra("provider_quota_router_decision", decision.action)
        event.set_extra("provider_quota_router_reason", decision.reason)

        if decision.action == "block":
            event.set_extra("provider_quota_router_blocked", True)
            return

        if decision.selected_provider_id and decision.action in {"switch", "use_last"}:
            if not self.settings.dry_run:
                event.set_extra("selected_provider", decision.selected_provider_id)
            logger.info(
                "[ProviderQuotaRouter] route %s -> %s action=%s reason=%s dry_run=%s",
                decision.original_provider_id,
                decision.selected_provider_id,
                decision.action,
                decision.reason,
                self.settings.dry_run,
            )

        if decision.should_reserve and not self.settings.dry_run:
            await self.state.reserve(
                request_id=request_id,
                window_id=window.window_id,
                quota_key=str(decision.selected_quota_key),
                provider_id=str(decision.selected_provider_id or current_provider_id),
                provider_model=self._provider_model(str(decision.selected_provider_id or current_provider_id)),
                tokens=decision.reservation_tokens,
                ttl_seconds=self.settings.reservation_ttl_seconds,
            )

    @filter.on_llm_request(priority=HOOK_PRIORITY)
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self.settings.enabled:
            return
        if event.get_extra("provider_quota_router_blocked"):
            message = self.settings.exhausted_message.format(
                refresh_time=self.settings.reset_time,
            )
            await event.send(MessageChain().message(message))
            event.stop_event()
            logger.info("[ProviderQuotaRouter] blocked LLM request: quota chain exhausted")

    @filter.on_agent_done(priority=HOOK_PRIORITY)
    async def on_agent_done(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        response: LLMResponse,
    ) -> None:
        request_id = str(event.get_extra("provider_quota_router_request_id") or "")
        if not request_id:
            return
        usage = getattr(response, "usage", None)
        actual_tokens = int(getattr(usage, "total", 0) or 0) if usage else None
        pending = await self.state.release(
            request_id=request_id,
            actual_tokens=actual_tokens,
            overlay_ttl_seconds=self.settings.overlay_ttl_seconds,
        )
        if pending and actual_tokens:
            logger.info(
                "[ProviderQuotaRouter] usage recorded: provider=%s quota_key=%s tokens=%s",
                pending.get("provider_id"),
                pending.get("quota_key"),
                actual_tokens,
            )

    @filter.command("quota", desc="查看或管理 provider/model token 额度路由。")
    async def quota_command(self, event: AstrMessageEvent, args: GreedyStr = ""):
        parts = str(args or "").strip().split()
        subcommand = parts[0].lower() if parts else "status"

        if subcommand == "status":
            if not self._can_view_status(event):
                yield event.plain_result("没有权限查看 quota 状态。")
                return
            yield event.plain_result(await self._status_text())
            return

        if not self._is_admin(event):
            yield event.plain_result("没有权限执行 quota 管理命令。")
            return

        if subcommand == "reload":
            self._reload_runtime_settings()
            yield event.plain_result(
                f"Provider quota router 已重载：chains={len(self.settings.chains)}, dry_run={self.settings.dry_run}"
            )
            return

        if subcommand == "reset-cache":
            await self.state.reset_cache()
            yield event.plain_result("Provider quota router 本地 pending/overlay 缓存已清理。")
            return

        if subcommand == "dry-run" and len(parts) >= 2:
            value = parts[1].lower()
            if value not in {"on", "off"}:
                yield event.plain_result("用法：/quota dry-run on|off")
                return
            self.settings = replace(self.settings, dry_run=value == "on")
            self.router = self._build_router()
            yield event.plain_result(f"dry-run 已切换为 {self.settings.dry_run}。")
            return

        yield event.plain_result(
            "用法：/quota status | /quota reload | /quota reset-cache | /quota dry-run on|off"
        )

    async def api_get_status(self) -> dict:
        try:
            window = self._request_window()
            payload = await self._status_payload(window)
            if request.args.get("snapshot", "1") != "0":
                snapshot_path = write_snapshot(self.data_dir, window, payload)
                payload["snapshot_path"] = str(snapshot_path)
            return _ok(payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("[ProviderQuotaRouter] status API failed: %s", exc, exc_info=True)
            return _error(f"获取状态失败: {exc}")

    async def api_get_chains(self) -> dict:
        try:
            return _ok(
                {
                    "settings": self._settings_payload(),
                    "chains": self._chains_payload(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[ProviderQuotaRouter] chains API failed: %s", exc, exc_info=True)
            return _error(f"获取链路失败: {exc}")

    async def api_get_decisions(self) -> dict:
        try:
            limit = int(request.args.get("limit", 50) or 50)
        except ValueError:
            limit = 50
        return _ok(
            {
                "decisions": read_recent_decisions(
                    self.state.decisions_path,
                    limit=limit,
                )
            }
        )

    async def api_get_export(self) -> dict:
        try:
            window = self._request_window()
            rows = await self.router.status(window=window)
            content = export_usage_csv(rows, window)
            filename = f"provider_quota_{window.start_local:%Y%m%d}.csv"
            return _ok(
                {
                    "filename": filename,
                    "content_type": "text/csv; charset=utf-8",
                    "content": content,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[ProviderQuotaRouter] export API failed: %s", exc, exc_info=True)
            return _error(f"导出失败: {exc}")

    async def api_get_history(self) -> dict:
        try:
            start_date, end_date = self._request_history_range()
            model = str(request.args.get("model", "") or "").strip()
            payload = await self.ledger.query_daily_model_usage(
                start_date=start_date,
                end_date=end_date,
                timezone_name=self.settings.timezone,
                reset_time=self.settings.reset_time,
                model_filter=model,
            )
            payload["settings"] = self._settings_payload()
            payload["model_filter"] = model
            return _ok(payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("[ProviderQuotaRouter] history API failed: %s", exc, exc_info=True)
            return _error(f"获取历史统计失败: {exc}")

    async def _status_text(self) -> str:
        window = current_window(
            timezone_name=self.settings.timezone,
            reset_time=self.settings.reset_time,
        )
        payload = await self._status_payload(window)
        rows = payload["rows"]
        if not rows:
            return "Provider quota router 未配置任何链路。"
        lines = [
            "Provider quota router",
            f"window: {window.start_local:%Y-%m-%d %H:%M} -> {window.end_local:%Y-%m-%d %H:%M}",
            f"mode: {self.settings.quota_key_mode}, dry_run: {self.settings.dry_run}",
            f"alerts: {payload['summary']['alert_count']} critical={payload['summary']['critical_alert_count']}",
        ]
        for row in rows:
            lines.append(
                "{status} {provider_id} model={model} used={used}/{limit} pending={pending} overlay={overlay}".format(
                    status=row["status"],
                    provider_id=row["provider_id"],
                    model=row["provider_model"] or "-",
                    used=_format_tokens(row["effective_tokens"]),
                    limit=_format_tokens(row["limit"]),
                    pending=_format_tokens(row["pending_tokens"]),
                    overlay=_format_tokens(row["overlay_tokens"]),
                )
            )
        return "\n".join(lines)

    async def _status_payload(self, window) -> dict[str, Any]:
        rows = await self.router.status(window=window)
        alerts = build_alerts(rows)
        state = await self.state.snapshot()
        return {
            "settings": self._settings_payload(),
            "window": {
                "id": window.window_id,
                "start_local": window.start_local.isoformat(),
                "end_local": window.end_local.isoformat(),
                "start_utc": window.start_utc.isoformat(),
                "end_utc": window.end_utc.isoformat(),
            },
            "summary": build_summary(rows, alerts),
            "rows": rows,
            "alerts": alerts,
            "state": {
                "pending_count": len(state.get("pending", {}) or {}),
                "overlay_count": len(state.get("overlays", []) or []),
                "pending": list((state.get("pending", {}) or {}).values()),
                "overlays": state.get("overlays", []) or [],
            },
            "decisions": read_recent_decisions(self.state.decisions_path, limit=30),
        }

    def _settings_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.enabled,
            "timezone": self.settings.timezone,
            "reset_time": self.settings.reset_time,
            "default_daily_limit_tokens": self.settings.default_daily_limit_tokens,
            "default_safety_buffer_tokens": self.settings.default_safety_buffer_tokens,
            "default_request_reservation_tokens": self.settings.default_request_reservation_tokens,
            "reservation_ttl_seconds": self.settings.reservation_ttl_seconds,
            "overlay_ttl_seconds": self.settings.overlay_ttl_seconds,
            "count_cached_input_tokens": self.settings.count_cached_input_tokens,
            "quota_key_mode": self.settings.quota_key_mode,
            "exhausted_action": self.settings.exhausted_action,
            "dry_run": self.settings.dry_run,
            "use_astrbot_fallback_chain": self.settings.use_astrbot_fallback_chain,
            "allow_status_for_all": self.settings.allow_status_for_all,
        }

    def _chains_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "name": chain.name,
                "providers": chain.providers,
                "daily_limit_tokens": chain.limit(self.settings.default_daily_limit_tokens),
                "safety_buffer_tokens": chain.safety_buffer(self.settings.default_safety_buffer_tokens),
                "request_reservation_tokens": chain.reservation(self.settings.default_request_reservation_tokens),
            }
            for chain in self.settings.chains
        ]

    def _request_window(self):
        date_arg = str(request.args.get("date", "") or "").strip()
        if date_arg:
            return window_for_local_date(
                timezone_name=self.settings.timezone,
                reset_time=self.settings.reset_time,
                local_date=date.fromisoformat(date_arg),
            )
        return current_window(
            timezone_name=self.settings.timezone,
            reset_time=self.settings.reset_time,
        )

    def _request_history_range(self) -> tuple[date, date]:
        today = current_window(
            timezone_name=self.settings.timezone,
            reset_time=self.settings.reset_time,
        ).start_local.date()
        end_arg = str(request.args.get("end_date", "") or "").strip()
        start_arg = str(request.args.get("start_date", "") or "").strip()
        days_arg = str(request.args.get("days", "") or "").strip()

        end_date = date.fromisoformat(end_arg) if end_arg else today
        if start_arg:
            start_date = date.fromisoformat(start_arg)
        else:
            try:
                days = int(days_arg or 14)
            except ValueError:
                days = 14
            days = max(1, min(days, 90))
            start_date = end_date - timedelta(days=days - 1)

        if end_date < start_date:
            start_date, end_date = end_date, start_date
        if (end_date - start_date).days > 89:
            start_date = end_date - timedelta(days=89)
        return start_date, end_date

    def _current_provider_id(self, event: AstrMessageEvent) -> str:
        selected = event.get_extra("selected_provider")
        if selected and isinstance(selected, str):
            return selected
        try:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ProviderQuotaRouter] no current provider: %s", exc)
            return ""
        try:
            return str(provider.meta().id or "")
        except Exception:  # noqa: BLE001
            return str(getattr(provider, "provider_config", {}).get("id") or "")

    def _provider_model(self, provider_id: str) -> str:
        provider = self.context.get_provider_by_id(provider_id)
        if not provider:
            return ""
        model = getattr(provider, "get_model", lambda: "")()
        if model:
            return str(model)
        return str(getattr(provider, "provider_config", {}).get("model") or "")

    @staticmethod
    def _request_id(event: AstrMessageEvent) -> str:
        existing = event.get_extra("provider_quota_router_request_id")
        if existing:
            return str(existing)
        return f"{id(event)}-{time.time_ns()}"

    @staticmethod
    def _required_modalities(event: AstrMessageEvent) -> set[str]:
        required: set[str] = set()
        for comp in getattr(getattr(event, "message_obj", None), "message", []) or []:
            name = comp.__class__.__name__.lower()
            if "image" in name:
                required.add("image")
            elif "record" in name or "audio" in name:
                required.add("audio")
        return required

    def _can_view_status(self, event: AstrMessageEvent) -> bool:
        return self.settings.allow_status_for_all or self._is_admin(event)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        if not self.settings.admin_user_ids:
            return True
        try:
            sender_id = event.get_sender_id()
        except Exception:  # noqa: BLE001
            sender_id = ""
        return str(sender_id) in self.settings.admin_user_ids


def _format_tokens(value: int) -> str:
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _ok(data: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"ok": True}
    if data:
        payload.update(data)
    return payload


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "message": message}
