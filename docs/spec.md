# AstrBot Provider Quota Router Spec

## v0.12.0 当前契约

- 独立源码仓库：`D:\astrbot\tmp_provider_quota_router_repo`；实时 Docker 数据根：`D:\astrbot\data -> /AstrBot/data`。不得把 `C:\Users\Administrator\astrbot` 当作当前运行目录。
- 只有 `provider_source_id=openai` 命中的火山开发者计划使用本地日 token 保护；`volcengine-agent-plan/*`、中转站、DeepSeek 和其他 Token Plan 不参与该阈值。
- 火山本地统计窗口仍在北京时间 11:00 切换；达到安全线后从达线时刻滚动冷却 24 小时，跨过 11:00 不提前恢复。
- `opencode-zen/*-free` 只在上游明确返回 `FreeUsageLimitError` 后进入未知刷新额度状态；用户请求零外呼，后台持久化 lease 探测成功后恢复，不再假设 11:00。
- Provider 故障先分类：超时、连接、408、普通 429、5xx 冷却实际失败模型 1800 秒；未知边界异常冷却 300 秒；400/422、上下文、模态、工具、附件和内容审核错误不写健康状态。
- 只有火山开发者计划明确账号级错误才允许打开 Source 熔断；普通请求级 403 不连坐整个 Source。
- `provider_model` 额度查询必须同时限定到本地额度策略命中的 Provider ID，禁止把付费 Token Plan 的同名模型计入免费计划。
- 每条请求使用不可变 RoutePlan；额度判断与 reservation 在 StateStore 共享临界区内原子完成，热重载产生的新旧 router 也不能并发双放行。
- `allow_paid/use_last` 只能绕过火山本地额度，不能绕过缺失、模态、模型健康、Source 熔断或上游硬额度。
- 下文保留最初 MVP 的背景和演进依据；凡与本节冲突，以本节和 `14期plan.md` 为准。

## 背景

火山引擎开发者活动存在“单一模型每日免费 token 额度”的使用边界。当前目标不是做群聊或用户限额，而是在 AstrBot 内部按 provider/model 维度监控全局 token 消耗，并在某个模型达到配置阈值后，将后续会落到该 provider 的请求自动路由到优先级更低的 provider。

额度值、重置时间、计费口径必须全部可配置，不能把 2000000 token 写死。活动规则和免费额度可能变化，插件只负责执行本地策略。

## 当前环境确认

- Windows 实时数据目录：`D:\astrbot\data`；源码仓库：`D:\astrbot\tmp_provider_quota_router_repo`。
- Docker 服务：`astrbot` 容器运行 `soulter/astrbot:latest`，暴露 `6185`；`napcat` 容器暴露 `6099`。
- 挂载关系：`./data` 挂载到容器内 `/AstrBot/data`。
- AstrBot 源码位于容器内 `/AstrBot/astrbot`。
- 当前 `provider_settings.default_provider_id` 为 `openai/doubao-seed-2-0-lite-260215`。
- 当前 `provider_settings.fallback_chat_models` 已配置一组豆包优先级链，可作为默认路由链参考。
- AstrBot 原生数据库 `/AstrBot/data/data_v4.db` 已有 `provider_stats` 表，字段包含 `provider_id`、`provider_model`、`token_input_other`、`token_input_cached`、`token_output`、`created_at`。
- 2026-06-26 按 `Asia/Shanghai 00:00` 统计窗口查询，`openai/doubao-seed-2-0-lite-260215` 当日已超过 500 万 token，说明该需求在本机运行状态下有实际必要。

## 源码可行性结论

可以用纯 AstrBot 插件实现 MVP，不需要改 AstrBot 核心。

关键依据：

- `filter.on_waiting_llm_request` 在 AstrBot 构建 main agent 和选择 provider 之前触发，适合提前写入 `event.set_extra("selected_provider", provider_id)`。
- AstrBot 的 `_select_provider()` 会优先读取 `event.get_extra("selected_provider")`，因此插件可以改变本次请求使用的 provider。
- `filter.on_llm_request` 在 provider 已选择、请求体已构建后触发，适合做兜底阻断、记录请求上下文和检查配置，不适合作为主路由入口。
- `filter.on_llm_response` / `filter.on_agent_done` 可用于响应后观察 token usage，但真正权威的 provider/model 消耗仍应以 `provider_stats` 为准。
- AstrBot 会在请求完成后写入 `ProviderStat`，统计字段来自 `LLMResponse.usage`。

## 与现有插件的边界

本机已有 `data/plugins/astrbot_plugin_token_controller`，它是第三方“群聊 Token 流控”插件：

- 主要维度是群聊、用户、群内策略、群内 fallback。
- 统计来源同样是 AstrBot 原生 `ProviderStat`。
- 使用 `on_waiting_llm_request(priority=1000)` 提前写 `selected_provider`。

新插件建议单独实现，不直接改这个第三方插件：

- 新插件维度是全局 provider/model 免费额度。
- 新插件不关心 QQ 群、用户、群备注、群内限流。
- 默认 hook priority 建议低于现有 token_controller，例如 `900`，这样可以在群聊插件先决定 `selected_provider` 后，再做全局额度终审和降级。
- 如果未来确认不需要群聊限额，也可以停用 token_controller；但 MVP 不要求这样做。

## 目标

### MVP

- 按配置的统计窗口统计每个 quota key 的 token 消耗。
- quota key 默认使用 `provider_model`，因为活动语义是“单一模型”；可配置为 `provider_id` 或自定义 quota group。
- 对候选 provider 链逐个检查剩余额度，选择第一个未超过阈值的 provider。
- 如果当前 provider 已超过阈值，自动写入 `selected_provider` 切到下一级 provider。
- 每日到达配置 reset time 后自然进入新窗口，重新从零统计。
- 提供管理员命令查看当前额度、强制重载配置、手动清理本地缓存。
- 所有本地状态保存在 `data/plugin_data/astrbot_plugin_provider_quota_router/`。

### Enhancement

- Plugin Page 可视化 provider/model 当日用量、剩余额度和路由链状态。
- 支持每个模型不同额度、不同 reset time、不同安全 buffer。
- 支持 dry-run，只记录“本应切换”的决策，不实际路由。
- 支持导出每日 CSV/JSON 报表。
- 支持告警：接近额度、已切换、所有候选 provider 都耗尽。

### Future

- 接入火山引擎官方账单或用量 API 做外部校准。
- 按 API key/account 维度分组额度。
- 针对 cached input token、reasoning token、视觉 token 建立更贴近实际计费的换算模式。
- 和 provider 负载均衡插件做显式集成。

## 非目标

- 不替代群聊限流、用户限流、上下文压缩或缓存命中优化。
- 不直接修改 `cmd_config.json` 的默认 provider。
- 不在 MVP 阶段改 AstrBot 核心源码。
- 不保证“绝对不会超过免费额度”，因为 token 在响应后才知道；MVP 通过 safety buffer 和 reservation 降低越线概率。

## 配置模型草案

```json
{
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "reset_time": "00:00",
  "default_daily_limit_tokens": 2000000,
  "default_safety_buffer_tokens": 100000,
  "default_request_reservation_tokens": 50000,
  "hook_priority": 900,
  "dry_run": false,
  "count_cached_input_tokens": true,
  "quota_key_mode": "provider_model",
  "exhausted_action": "stop",
  "chains": [
    {
      "name": "doubao-main",
      "providers": [
        "openai/doubao-seed-2-0-lite-260215",
        "openai/doubao-seed-2-0-mini-260215",
        "openai/doubao-seed-2-0-pro-260215",
        "doubao-seed-1-8-251228"
      ],
      "daily_limit_tokens": 2000000,
      "safety_buffer_tokens": 100000
    }
  ]
}
```

## 路由规则

1. 在 `on_waiting_llm_request` 读取当前事件已选择的 provider：
   - 优先 `event.get_extra("selected_provider")`。
   - 否则用 `context.get_using_provider(umo=event.unified_msg_origin)` 获取当前默认 provider。
2. 找到包含该 provider 的路由链。
3. 按路由链顺序检查 provider：
   - provider 必须存在且是聊天 provider。
   - provider 必须启用。
   - 如果事件包含图片、音频等输入，候选 provider 应支持对应 modality；无法确认时保守交给 AstrBot 原生 fallback 再由 DB 校准。
   - 当前窗口用量 + 未完成请求 reservation + safety buffer 小于 daily limit，才可选。
4. 如果候选 provider 与原 provider 不同：
   - 非 dry-run 时写入 `event.set_extra("selected_provider", candidate_id)`。
   - 记录 `event.set_extra("provider_quota_router_selected", candidate_id)` 方便后续日志和调试。
5. 如果所有候选都耗尽：
   - `exhausted_action=stop`：停止本次 LLM 请求并按配置发送提示。
   - `exhausted_action=allow_paid`：继续使用原 provider，但日志标记为 paid risk。
   - `exhausted_action=use_last`：使用链路最后一个 provider。

## 统计窗口

- 每次判断时根据 `timezone + reset_time` 动态计算当前窗口 `[start, end)`。
- `provider_stats.created_at` 按 UTC 存储形态处理，查询时把本地窗口转换成 UTC。
- 不依赖固定 cron 重置，避免 bot 停机或重启时错过重置。
- 本地缓存只缓存当前窗口聚合结果；窗口切换后自然失效。

## token 口径

默认统计：

```text
token_input_other + token_input_cached + token_output
```

配置项：

- `count_cached_input_tokens=true`：保守计入口径，避免免费额度超出。
- `count_cached_input_tokens=false`：仅统计 `input_other + output`，更接近某些按折扣计费的场景，但可能不适合活动免费额度。

## 并发和越线控制

问题：一次请求的实际 token 只有响应后才知道；多个并发请求可能同时判断“未超限”。

MVP 方案：

- 每个 quota key 使用 `asyncio.Lock` 串行执行路由判断。
- 对已放行但未结束的请求记录 reservation。
- 判断时使用 `db_used + overlay_used + pending_reservation + safety_buffer`。
- 请求完成后释放 reservation，并在响应有 `usage` 时更新 overlay。
- reservation 设置 TTL，避免异常请求永远占用额度。

## 数据存储

```text
data/plugin_data/astrbot_plugin_provider_quota_router/
  quota_state.json
  route_decisions.jsonl
  daily_snapshots/
```

- `quota_state.json`：当前窗口缓存、pending reservations、手动调整。
- `route_decisions.jsonl`：每次路由决策，便于排查。
- `daily_snapshots/`：可选，保存每日汇总备份。

## 管理命令

- `/quota status`：显示所有链路 provider 当日用量和状态。
- `/quota status <provider_id|model>`：显示单个 provider/model。
- `/quota reload`：重新读取配置。
- `/quota reset-cache`：清理插件本地 overlay/reservation，不删除 AstrBot 原生 DB。
- `/quota dry-run on|off`：临时切换 dry-run。

命令仅允许管理员或 AstrBot owner 使用。

## 风险

- 如果某 provider 不返回 usage，`ProviderStat` token 可能为 0，需要在状态页标记“不可准确计量”。
- 如果其他插件在更低 priority 后继续覆盖 `selected_provider`，本插件路由可能被覆盖；需要在文档里要求 provider 路由类插件排序。
- AstrBot 内部 fallback 在 provider 失败后可能切换到其他 provider，这种“运行中 fallback”只能由后续 `ProviderStat` 校准，不能在预路由阶段百分百预测。
- 活动免费额度的真实计费口径可能不等于 AstrBot usage total，必须保留 safety buffer。
