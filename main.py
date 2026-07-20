from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
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
from .core.core_fallback_guard import (
    CORE_FALLBACK_DROPPED_EXTRA_KEY,
    CORE_FALLBACK_GUARD_EXTRA_KEY,
    install_core_fallback_guard,
    uninstall_core_fallback_guard,
)
from .core.fallback_config import (
    ConfigFileSignature,
    build_astrbot_fallback_chain,
    file_signature,
    load_astrbot_fallback_chain,
    resolve_cmd_config_path,
)
from .core.ledger import QuotaLedger
from .core.provider_errors import is_http_403_response, response_error_text
from .core.reports import (
    build_alerts,
    build_summary,
    export_usage_csv,
    read_recent_decisions,
    write_snapshot,
)
from .core.router import VOLCENGINE_GROUP_ID, ProviderQuotaRouter, decision_payload
from .core.state import QuotaStateStore
from .core.time_window import current_window, window_for_local_date


PLUGIN_NAME = "astrbot_plugin_provider_quota_router"
PLUGIN_VERSION = "0.7.0"
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
    "fallback_watch_interval_seconds",
    "strict_priority_order",
    "disable_astrbot_error_fallback",
    "quota_cooldown_seconds",
    "unlimited_provider_prefixes",
    "volcengine_403_circuit_enabled",
    "volcengine_provider_source_ids",
    "volcengine_403_cooldown_seconds",
    "volcengine_probe_check_interval_seconds",
    "volcengine_probe_timeout_seconds",
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
        self._cmd_config_path = resolve_cmd_config_path(__file__)
        self._fallback_config_signature: ConfigFileSignature | None = None
        self._fallback_watch_task: asyncio.Task | None = None
        self._cooldown_reconcile_task: asyncio.Task | None = None
        self._volcengine_probe_task: asyncio.Task | None = None
        self._fallback_chain_is_dynamic = False
        self._fallback_chain_source = "none"
        self._fallback_last_reload_at: str | None = None
        self._fallback_last_error: str | None = None
        self._core_fallback_guard_owner = object()
        self._core_fallback_guard_active = False
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
            "[ProviderQuotaRouter] loaded: enabled=%s chains=%d quota_key_mode=%s dry_run=%s fallback_source=%s",
            self.settings.enabled,
            len(self.settings.chains),
            self.settings.quota_key_mode,
            self.settings.dry_run,
            self._fallback_chain_source,
        )

    async def initialize(self) -> None:
        self._sync_core_fallback_guard()
        self._cooldown_reconcile_task = asyncio.create_task(
            self._reconcile_cooldowns_after_startup(),
            name="provider-quota-router-cooldown-reconcile",
        )
        self._fallback_watch_task = asyncio.create_task(
            self._watch_fallback_config(),
            name="provider-quota-router-fallback-watch",
        )
        self._volcengine_probe_task = asyncio.create_task(
            self._watch_volcengine_circuit(),
            name="provider-quota-router-volcengine-probe",
        )
        logger.info(
            "[ProviderQuotaRouter] fallback watch started: active=%s path=%s interval=%ss",
            self._fallback_chain_is_dynamic,
            self._cmd_config_path,
            self.settings.fallback_watch_interval_seconds,
        )

    async def terminate(self) -> None:
        task = self._fallback_watch_task
        self._fallback_watch_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        reconcile_task = self._cooldown_reconcile_task
        self._cooldown_reconcile_task = None
        if reconcile_task and not reconcile_task.done():
            reconcile_task.cancel()
            try:
                await reconcile_task
            except asyncio.CancelledError:
                pass
        probe_task = self._volcengine_probe_task
        self._volcengine_probe_task = None
        if probe_task and not probe_task.done():
            probe_task.cancel()
            try:
                await probe_task
            except asyncio.CancelledError:
                pass
        self._disable_core_fallback_guard()
        logger.info("[ProviderQuotaRouter] fallback watch stopped")

    async def _watch_volcengine_circuit(self) -> None:
        while True:
            await asyncio.sleep(
                self.settings.volcengine_probe_check_interval_seconds
            )
            if (
                not self.settings.enabled
                or not self.settings.volcengine_403_circuit_enabled
                or self.settings.dry_run
            ):
                continue
            try:
                await self._probe_volcengine_if_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ProviderQuotaRouter] Volcengine circuit probe loop failed: %s",
                    exc,
                )

    async def _probe_volcengine_if_due(self) -> None:
        circuit = await self.state.get_provider_group_circuit(
            group_id=VOLCENGINE_GROUP_ID
        )
        if not circuit or float(circuit.get("retry_at") or 0) > time.time():
            return
        if (
            circuit.get("status") == "probing"
            and float(circuit.get("probe_lease_until") or 0) > time.time()
        ):
            return

        window = current_window(
            timezone_name=self.settings.timezone,
            reset_time=self.settings.reset_time,
        )
        candidates = await self.router.volcengine_probe_candidate_ids(window=window)
        if not candidates:
            await self.state.defer_provider_group_probe(
                group_id=VOLCENGINE_GROUP_ID,
                delay_seconds=max(
                    300, self.settings.volcengine_probe_check_interval_seconds
                ),
                error="没有仍处于 token 安全线内的火山探测候选",
            )
            logger.warning(
                "[ProviderQuotaRouter] Volcengine probe deferred: no quota-safe candidates"
            )
            return

        provider_id = secrets.choice(candidates)
        lease = await self.state.acquire_provider_group_probe(
            group_id=VOLCENGINE_GROUP_ID,
            provider_id=provider_id,
            lease_seconds=self.settings.volcengine_probe_timeout_seconds + 15,
        )
        if not lease:
            return
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            await self.state.finish_provider_group_probe(
                group_id=VOLCENGINE_GROUP_ID,
                success=False,
                cooldown_seconds=self.settings.volcengine_403_cooldown_seconds,
                error=f"探测 Provider 不存在: {provider_id}",
            )
            return

        logger.warning(
            "[ProviderQuotaRouter] Volcengine half-open probe started: provider=%s",
            provider_id,
        )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt="连接测试：请只回复 OK。",
                    session_id=f"provider-quota-probe-{uuid.uuid4().hex}",
                    request_max_retries=1,
                ),
                timeout=self.settings.volcengine_probe_timeout_seconds,
            )
            if getattr(response, "role", "") == "err":
                raise RuntimeError(
                    str(getattr(response, "completion_text", "") or "模型返回错误")
                )
        except Exception as exc:  # noqa: BLE001
            await self.state.finish_provider_group_probe(
                group_id=VOLCENGINE_GROUP_ID,
                success=False,
                cooldown_seconds=self.settings.volcengine_403_cooldown_seconds,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.warning(
                "[ProviderQuotaRouter] Volcengine half-open probe failed; circuit reopened: provider=%s error=%s",
                provider_id,
                exc,
            )
            return

        await self.state.finish_provider_group_probe(
            group_id=VOLCENGINE_GROUP_ID,
            success=True,
            cooldown_seconds=self.settings.volcengine_403_cooldown_seconds,
        )
        logger.warning(
            "[ProviderQuotaRouter] Volcengine half-open probe succeeded; circuit closed: provider=%s",
            provider_id,
        )

    async def _reconcile_cooldowns_after_startup(self) -> None:
        for delay_seconds in (10, 20, 30):
            await asyncio.sleep(delay_seconds)
            try:
                checked_count, cooldown_count = await self.router.reconcile_cooldowns(
                    window=current_window(
                        timezone_name=self.settings.timezone,
                        reset_time=self.settings.reset_time,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ProviderQuotaRouter] startup cooldown reconciliation failed; retrying: %s",
                    exc,
                )
                continue
            if checked_count:
                logger.info(
                    "[ProviderQuotaRouter] startup cooldown reconciliation complete: checked=%s active=%s",
                    checked_count,
                    cooldown_count,
                )
                return
        logger.warning(
            "[ProviderQuotaRouter] startup cooldown reconciliation skipped: no managed providers became available"
        )

    def _sync_core_fallback_guard(self) -> None:
        should_enable = (
            self.settings.enabled
            and self.settings.disable_astrbot_error_fallback
            and not self.settings.dry_run
        )
        if should_enable and not self._core_fallback_guard_active:
            try:
                self._core_fallback_guard_active = install_core_fallback_guard(
                    self._core_fallback_guard_owner
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[ProviderQuotaRouter] failed to install AstrBot core fallback guard: %s",
                    exc,
                )
                self._core_fallback_guard_active = False
            else:
                logger.info(
                    "[ProviderQuotaRouter] AstrBot core error fallback guard enabled"
                )
        elif not should_enable:
            self._disable_core_fallback_guard()

    def _disable_core_fallback_guard(self) -> None:
        if not self._core_fallback_guard_active:
            return
        try:
            uninstall_core_fallback_guard(self._core_fallback_guard_owner)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ProviderQuotaRouter] failed to remove AstrBot core fallback guard: %s",
                exc,
            )
        self._core_fallback_guard_active = False

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
        self._fallback_chain_is_dynamic = (
            not settings.chains and settings.use_astrbot_fallback_chain
        )
        if self._fallback_chain_is_dynamic:
            chain = self._default_chain_from_cmd_config()
            if chain is None:
                chain = self._default_chain_from_astrbot()
                if chain:
                    self._fallback_chain_source = "provider_manager"
            if chain:
                settings = replace(settings, chains=[chain])
        elif settings.chains:
            self._fallback_chain_source = "custom"
            self._fallback_last_error = None
        else:
            self._fallback_chain_source = "none"
            self._fallback_last_error = None
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

    def _default_chain_from_cmd_config(self) -> ChainConfig | None:
        try:
            chain, signature = load_astrbot_fallback_chain(self._cmd_config_path)
        except Exception as exc:  # noqa: BLE001
            self._record_fallback_error(exc)
            return None
        self._fallback_config_signature = signature
        self._fallback_chain_source = "cmd_config"
        self._fallback_last_reload_at = datetime.now().astimezone().isoformat()
        self._fallback_last_error = None
        return chain

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
        try:
            return build_astrbot_fallback_chain(provider_settings)
        except Exception as exc:  # noqa: BLE001
            self._record_fallback_error(exc)
            return None

    async def _watch_fallback_config(self) -> None:
        while True:
            await asyncio.sleep(self.settings.fallback_watch_interval_seconds)
            if not self._fallback_chain_is_dynamic:
                continue
            try:
                signature = await asyncio.to_thread(
                    file_signature, self._cmd_config_path
                )
                if signature == self._fallback_config_signature:
                    continue
                chain, loaded_signature = await asyncio.to_thread(
                    load_astrbot_fallback_chain, self._cmd_config_path
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._record_fallback_error(exc)
                continue
            self._apply_watched_fallback_chain(chain, loaded_signature)

    def _apply_watched_fallback_chain(
        self,
        chain: ChainConfig,
        signature: ConfigFileSignature,
    ) -> None:
        old_providers = (
            self.settings.chains[0].providers if self.settings.chains else []
        )
        self.settings = replace(self.settings, chains=[chain])
        self.router = self._build_router()
        self._fallback_config_signature = signature
        self._fallback_chain_source = "cmd_config"
        self._fallback_last_reload_at = datetime.now().astimezone().isoformat()
        self._fallback_last_error = None
        logger.info(
            "[ProviderQuotaRouter] fallback chain hot-reloaded: old=%s new=%s",
            old_providers,
            chain.providers,
        )

    def _record_fallback_error(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        if message != self._fallback_last_error:
            logger.warning(
                "[ProviderQuotaRouter] fallback config reload failed; keeping last valid chain: %s",
                message,
            )
        self._fallback_last_error = message

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
        if self._core_fallback_guard_active and not self.settings.dry_run:
            event.set_extra(CORE_FALLBACK_GUARD_EXTRA_KEY, True)
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
        dropped_fallbacks = event.get_extra(CORE_FALLBACK_DROPPED_EXTRA_KEY)
        if dropped_fallbacks:
            logger.info(
                "[ProviderQuotaRouter] blocked AstrBot error fallback candidates: %s",
                dropped_fallbacks,
            )
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
        if (
            pending
            and self.settings.volcengine_403_circuit_enabled
            and not self.settings.dry_run
            and self.router.is_volcengine_provider(
                str(pending.get("provider_id") or "")
            )
            and is_http_403_response(response)
        ):
            error_text = response_error_text(response)
            circuit = await self.state.open_provider_group_circuit(
                group_id=VOLCENGINE_GROUP_ID,
                trigger_provider_id=str(pending.get("provider_id") or ""),
                ttl_seconds=self.settings.volcengine_403_cooldown_seconds,
                error=error_text,
            )
            logger.error(
                "[ProviderQuotaRouter] Volcengine HTTP 403 circuit opened: provider=%s retry_at=%s",
                pending.get("provider_id"),
                datetime.fromtimestamp(
                    float(circuit.get("retry_at") or 0)
                ).astimezone().isoformat(timespec="seconds"),
            )
        if pending and actual_tokens:
            logger.info(
                "[ProviderQuotaRouter] usage recorded: provider=%s quota_key=%s tokens=%s",
                pending.get("provider_id"),
                pending.get("quota_key"),
                actual_tokens,
            )
        if pending:
            cooldown = await self.router.ensure_cooldown(
                provider_id=str(pending.get("provider_id") or ""),
                provider_model=str(pending.get("provider_model") or ""),
                window=current_window(
                    timezone_name=self.settings.timezone,
                    reset_time=self.settings.reset_time,
                ),
            )
            if cooldown:
                logger.warning(
                    "[ProviderQuotaRouter] quota cooldown active: provider=%s quota_key=%s until=%s",
                    pending.get("provider_id"),
                    cooldown.get("quota_key"),
                    datetime.fromtimestamp(
                        float(cooldown.get("expires_at") or 0)
                    ).astimezone().isoformat(timespec="seconds"),
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
            self._sync_core_fallback_guard()
            checked_count, cooldown_count = await self.router.reconcile_cooldowns(
                window=current_window(
                    timezone_name=self.settings.timezone,
                    reset_time=self.settings.reset_time,
                )
            )
            yield event.plain_result(
                "Provider quota router 已重载："
                f"chains={len(self.settings.chains)}, dry_run={self.settings.dry_run}, "
                f"fallback_source={self._fallback_chain_source}, "
                f"checked={checked_count}, cooldowns={cooldown_count}"
            )
            return

        if subcommand == "reset-cache":
            await self.state.reset_cache()
            yield event.plain_result(
                "Provider quota router 本地 pending/overlay 缓存已清理；费用保护冷却状态已保留。"
            )
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
            limit_text = (
                _format_tokens(row["limit"])
                if row.get("quota_managed", True)
                else "unlimited"
            )
            cooldown_text = ""
            if row.get("cooldown_until"):
                cooldown_text = " cooldown_until=" + datetime.fromtimestamp(
                    float(row["cooldown_until"])
                ).astimezone().isoformat(timespec="seconds")
            lines.append(
                "{status} {provider_id} model={model} used={used}/{limit} pending={pending} overlay={overlay}{cooldown}".format(
                    status=row["status"],
                    provider_id=row["provider_id"],
                    model=row["provider_model"] or "-",
                    used=_format_tokens(row["effective_tokens"]),
                    limit=limit_text,
                    pending=_format_tokens(row["pending_tokens"]),
                    overlay=_format_tokens(row["overlay_tokens"]),
                    cooldown=cooldown_text,
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
                "cooldown_count": len(state.get("cooldowns", {}) or {}),
                "provider_group_circuit_count": len(
                    state.get("provider_group_circuits", {}) or {}
                ),
                "pending": list((state.get("pending", {}) or {}).values()),
                "overlays": state.get("overlays", []) or [],
                "cooldowns": list((state.get("cooldowns", {}) or {}).values()),
                "provider_group_circuits": list(
                    (state.get("provider_group_circuits", {}) or {}).values()
                ),
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
            "fallback_watch_interval_seconds": self.settings.fallback_watch_interval_seconds,
            "strict_priority_order": self.settings.strict_priority_order,
            "disable_astrbot_error_fallback": self.settings.disable_astrbot_error_fallback,
            "quota_cooldown_seconds": self.settings.quota_cooldown_seconds,
            "unlimited_provider_prefixes": list(
                self.settings.unlimited_provider_prefixes
            ),
            "volcengine_403_circuit_enabled": self.settings.volcengine_403_circuit_enabled,
            "volcengine_provider_source_ids": list(
                self.settings.volcengine_provider_source_ids
            ),
            "volcengine_403_cooldown_seconds": self.settings.volcengine_403_cooldown_seconds,
            "volcengine_probe_check_interval_seconds": self.settings.volcengine_probe_check_interval_seconds,
            "volcengine_probe_timeout_seconds": self.settings.volcengine_probe_timeout_seconds,
            "core_fallback_guard_active": self._core_fallback_guard_active,
            "fallback_watch_active": self._fallback_chain_is_dynamic,
            "fallback_config_path": str(self._cmd_config_path),
            "fallback_chain_source": self._fallback_chain_source,
            "fallback_last_reload_at": self._fallback_last_reload_at,
            "fallback_last_error": self._fallback_last_error,
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
