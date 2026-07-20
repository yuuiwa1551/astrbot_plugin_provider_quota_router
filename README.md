# astrbot_plugin_provider_quota_router

AstrBot provider/model 日额度路由插件。它按配置的每日 token 额度监控聊天模型消耗，当某个 provider/model 达到阈值后，把后续会落到该 provider 的请求切到路由链的下一级 provider。

这个插件面向火山引擎开发者活动的“单一模型每日免费额度”场景。当前默认业务规则是北京时间 11:00 切换额度窗口、只有配置的火山 Provider Source 模型使用 token 上限并在达线后冷却 24 小时；`opencode-zen/` 按上游免费额度错误冷却到下一个 11:00，其他非火山 Provider 不参与本地 token 限制。

## 当前功能

- 使用 AstrBot 原生 `ProviderStat` 统计当前窗口 token。
- 默认按 `provider_model` 作为 quota key，也支持按 `provider_id`。
- 默认使用 AstrBot 的 `default_provider_id + fallback_chat_models` 作为路由链。
- 默认 fallback 链直接读取 `data/cmd_config.json`；每次 LLM 请求前检查文件签名并即时热更新，另有每 5 分钟一次的后台兜底，无需重启插件。
- 默认每次从 fallback 链首严格按顺序检查额度和请求模态，不沿用会话停留的旧 provider 作为扫描起点。
- 可按请求禁用 AstrBot 核心的错误 fallback，避免 403、超时等错误绕过额度判断进入后续付费模型。
- 在 provider 选择前通过 `selected_provider` 切换到第一个可用 provider。
- 使用 pending reservation 和短期 overlay 降低并发请求导致的超额风险。
- 达到阈值的受控模型会把 24 小时冷却写入 `quota_state.json`，跨重启和 11:00 窗口仍有效；插件会在 provider manager 就绪后延迟执行启动对账，并在每次受控模型响应后再次检查，避免最后一条请求跨线但没有后续请求时漏记。
- 只有 `volcengine_provider_source_ids` 指定的火山 Provider Source 使用本地 token 上限；中转站、DeepSeek 及其他非火山 Provider 只统计用量，不阻断也不预占。
- 默认把 `opencode-zen/` 从火山 token 安全阈值中排除；具体模型返回 HTTP 429 `FreeUsageLimitError` 后，只冷却该模型到下一个 11:00，其他 opencode 模型继续可选。
- opencode 额度保护同时覆盖 Agent 请求与图片描述等直接 Provider 调用；冷却期间直接调用会在发出网络请求前被拦截。
- 任一火山模型返回 HTTP 403 / `AccountOverdueError` 后，整组火山模型熔断 30 分钟；期间继续使用非火山 fallback，到期由后台随机探测一个仍在 token 安全线内的火山模型，成功才恢复整组。
- 本插件接管的任意模型最终返回 Provider 错误时，默认不在原会话展示技术错误，改为私聊 Bot 管理员；全部错误共用持久化的一小时告警窗口。
- 支持链路耗尽后的 `stop`、`allow_paid`、`use_last` 三种行为。
- 提供 `/quota` 管理命令。
- 提供 Plugin Page 状态面板、告警、最近路由决策和 CSV 导出。
- 提供历史 token 用量报表：每日各模型消耗、单日模型占比、单模型每日趋势。

## 配置

主要配置项：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用插件 |
| `timezone` | `Asia/Shanghai` | 统计窗口时区 |
| `reset_time` | `11:00` | 每日额度窗口切换时间 |
| `default_daily_limit_tokens` | `2000000` | 默认单模型日额度 |
| `default_safety_buffer_tokens` | `100000` | 安全余量 |
| `default_request_reservation_tokens` | `50000` | 单请求预占 token |
| `quota_key_mode` | `provider_model` | 按模型名或 provider ID 统计 |
| `exhausted_action` | `stop` | 链路耗尽后的行为 |
| `dry_run` | `false` | 只记录决策，不实际切换 |
| `use_astrbot_fallback_chain` | `true` | 未配置自定义链时使用 AstrBot fallback 链 |
| `fallback_watch_interval_seconds` | `300` | 无请求时检查 `cmd_config.json` 的兜底间隔；请求前会即时检查文件签名 |
| `strict_priority_order` | `true` | 每次从链首严格按顺序检查候选 |
| `disable_astrbot_error_fallback` | `false` | 可选的费用优先保护；开启后 provider 错误不再继续尝试后续模型 |
| `quota_cooldown_seconds` | `86400` | 受控模型达到阈值后的冷却时间 |
| `unlimited_provider_prefixes` | `["deepseek/"]` | 兼容配置；所有非火山 Provider 均不参与本地 token 限制 |
| `upstream_quota_provider_prefixes` | `["opencode-zen/"]` | 按 `FreeUsageLimitError` 冷却到下一个重置时间的 Provider 前缀 |
| `volcengine_403_circuit_enabled` | `true` | 是否启用火山 403 组级熔断 |
| `volcengine_provider_source_ids` | `["openai"]` | 唯一会套用本地 token 上限和火山组级熔断的 provider source ID |
| `volcengine_403_cooldown_seconds` | `1800` | 火山 403 后的整组冷却时间 |
| `volcengine_probe_check_interval_seconds` | `30` | 后台恢复探测检查间隔 |
| `volcengine_probe_timeout_seconds` | `30` | 单次最小探测的超时时间 |
| `provider_error_admin_notify_enabled` | `true` | Provider 错误时私聊管理员 |
| `provider_error_admin_notify_interval_seconds` | `3600` | 全局管理员错误告警间隔 |
| `provider_error_suppress_current_chat` | `true` | 不在触发错误的原会话展示技术错误 |
| `chains_json` | 空 | 自定义路由链 JSON |

自定义链示例：

```json
[
  {
    "name": "doubao-main",
    "providers": [
      "openai/doubao-seed-2-0-lite-260215",
      "openai/doubao-seed-2-0-mini-260215",
      "openai/doubao-seed-2-0-pro-260215"
    ],
    "daily_limit_tokens": 2000000,
    "safety_buffer_tokens": 100000,
    "request_reservation_tokens": 50000
  }
]
```

当 `chains_json` 非空时，自定义链优先，`cmd_config.json` 监视器不会覆盖它。使用 AstrBot 默认链时，插件会校验配置文件读取前后的文件签名；遇到配置正在写入、JSON 无效或链为空，会继续保留上一份有效链，并把错误暴露到状态 API 和 Plugin Page。

## 命令

- `/quota status`：查看当前窗口各 provider/model 用量。
- `/quota reload`：重载插件配置。
- `/quota reset-cache`：清理本地 pending/overlay 缓存，不删除 AstrBot 原生数据库，也不清除费用保护冷却。
- `/quota dry-run on|off`：临时切换演练模式。

`admin_user_ids` 为空时管理命令不限制，错误通知目标自动使用 AstrBot 核心 `admins_id`；填写后只有指定发送者可执行 reload/reset/dry-run，并把这些 ID 作为错误通知目标。`allow_status_for_all=false` 时，状态查看也会限制为管理员。

## WebUI

v0.2.0 提供 AstrBot Plugin Page，不需要额外端口。

页面能力：

- 当前窗口 provider/model 用量表。
- 可用、耗尽、告警数量汇总。
- 80% / 90% / 95% 和链路耗尽告警。
- pending reservation 与短期 overlay 状态。
- 火山模型的额度 cooldown、403 组级熔断与半开探测状态，以及 DeepSeek 的 unlimited 状态。
- Provider 错误管理员告警的持久化限频状态。
- 最近路由决策日志。
- 当前窗口或指定日期 CSV 导出。
- 最近 7/14/30/60/90 天历史图表：
  - 每天每个模型消耗堆叠柱状图。
  - 某一天各模型 token 占比饼图。
  - 某个模型每天消耗趋势图。

后端 API：

- `GET /api/plug/astrbot_plugin_provider_quota_router/status`
- `GET /api/plug/astrbot_plugin_provider_quota_router/chains`
- `GET /api/plug/astrbot_plugin_provider_quota_router/decisions`
- `GET /api/plug/astrbot_plugin_provider_quota_router/export?date=YYYY-MM-DD`
- `GET /api/plug/astrbot_plugin_provider_quota_router/history?days=14`

历史 API 支持 `days=1..90`，也支持 `start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`。统计窗口遵循插件配置的 `timezone + reset_time`，token 口径遵循 `count_cached_input_tokens`。

## 数据位置

运行时数据保存在 AstrBot 插件数据目录：

```text
data/plugin_data/astrbot_plugin_provider_quota_router/
  quota_state.json
  route_decisions.jsonl
  daily_snapshots/
```

`quota_state.json` 保存 pending reservation、短期 overlay、按 quota key 持久化的 token/opencode cooldown、火山 provider 组的 403 熔断/探测状态，以及管理员错误告警限频时间。权威历史用量仍来自 AstrBot 原生 `data_v4.db` 的 `provider_stats` 表。

## 设计说明

AstrBot 的 LLM 流程里，`on_waiting_llm_request` 在 main agent 构建和 provider 选择前触发；插件在这个阶段判断当前 provider 是否超过额度，并通过 `event.set_extra("selected_provider", next_provider_id)` 切换 provider。

请求真实 token 只有响应后才能知道，因此插件使用：

- `ProviderStat` 当前窗口聚合值。
- 当前未完成请求的 pending reservation。
- 响应后短时间 overlay，等待 AstrBot 异步写入 `ProviderStat`。
- safety buffer。

这不能保证绝对不越线，但能显著降低并发和异步落库造成的风险。

只有 `volcengine_provider_source_ids` 命中的火山模型会在 `effective_tokens + request_reservation + safety_buffer >= daily_limit` 时进入冷却。模型恢复必须同时满足：已经进入新的 11:00 日窗口，并且从达线时刻开始的 24 小时冷却已经结束。若模型在 15:00 达线，次日 11:00 仍会跳过，直到 15:00 才恢复。其他 Provider 即使 AstrBot 统计量超过该值也不会被停用。

`upstream_quota_provider_prefixes` 使用另一套策略：不按 AstrBot 本地 token 统计提前停用，也不使用 110 万安全阈值推算 1 美元额度；只有上游明确返回 `FreeUsageLimitError` 才为报错模型写入 cooldown，截止当前窗口的 `end_local`，即下一个北京时间 11:00。普通每分钟 429 不会触发整日冷却，也不会连坐同一 opencode source 下的其他模型。启动或重载时会清除旧版本误按 token 阈值生成的 opencode cooldown。

默认 fallback 链在每次 LLM 请求前对 `data/cmd_config.json` 做一次轻量 `stat`；只有签名变化时才使用 `utf-8-sig` 读取 JSON 并原子替换 router，因此修改列表后的下一条消息即可看到新链。同时保留低频后台任务，默认每 300 秒在无请求时兜底检查一次。文件有其他配置变化但 fallback 内容相同时，不重建 router。

`strict_priority_order=true` 时，无论某个会话此前停在哪个 provider，每次请求都会重新从链首检查；只有前面的 provider 超过额度、缺失或不支持当前图片/音频模态时，才会检查下一项。

AstrBot 自己还会在 provider 返回 403、超时或错误响应时执行一套运行中 fallback，这不是额度耗尽。`disable_astrbot_error_fallback=true` 会为本插件实际接管的请求清空该运行中 fallback，使模型切换只由本插件的额度决策触发。该保护不会修改 `cmd_config.json` 或 AstrBot Dashboard 中的列表；provider 错误时本次请求会直接失败，不会自动尝试下一级。

默认保持该保护关闭。这样当前置 provider 因欠费、故障或网络错误不可用时，AstrBot 仍会按 `fallback_chat_models` 尝试后续 provider。只有确认后续 provider 不应因错误被使用时，才建议开启。

`volcengine_403_circuit_enabled=true` 时，插件会在一次受控 Agent 请求最终返回 403 后持久化整组火山熔断。从下一次请求开始，路由器会跳过全部火山候选并继续选择非火山 fallback。30 分钟到期后，后台只从仍满足单模型 token 安全线的火山文本模型中随机选一个，使用单次重试的最小提示做半开探测；成功清除熔断，失败则重新冷却 30 分钟。用户请求不会被拿来当恢复探针。

AstrBot 的最终 `role=err` 响应不会经过普通的 Agent done hook，因此插件同时在 `on_agent_done` 和发送前 `on_decorating_result` 两条路径处理错误。后者负责兜底释放 reservation、识别最终 Provider 错误、开启火山 403 熔断、私聊管理员并清空原会话结果。告警使用单一全局限频键，默认一小时最多发送一次，且跨重启、`reset-cache` 继续有效。

`exhausted_action=use_last` 只在所有候选都是 `quota_exceeded` 时生效。provider 缺失或不支持请求中的图片、音频时会阻断请求，不再强制发送到链尾。

## 已知限制

- 处理 AstrBot 内部聊天模型 provider，并额外保护使用同一 AstrBot OpenAI Provider 实例的 opencode 直接调用；不处理 TTS、embedding 或完全绕过 AstrBot Provider 的外部 API。
- 如果 provider 不返回 usage，AstrBot `ProviderStat` 可能记录 0，插件会无法准确判断真实额度。
- 如果其他 provider 路由类插件在更低 priority 后覆盖 `selected_provider`，最终 provider 可能不是本插件选择的 provider。
- 未开启 `disable_astrbot_error_fallback` 时，AstrBot 内部 provider 失败后的运行中 fallback 仍不会再次触发插件的额度判断。

## Roadmap

- Future：火山引擎官方用量 API 对账、按 API key/account 分组额度、与 provider 负载均衡插件集成。

## 更新历史

### v0.10.0

- 本地 token 上限严格限定为火山 Provider Source，所有其他 Provider 不受 110 万阈值影响。
- fallback 在下一条 LLM 请求前即时检查并热加载，300 秒后台监视作为兜底。

### v0.9.1

- opencode 的 1 美元额度只以上游 `FreeUsageLimitError` 为准，不套用本地 110 万 token 阈值。
- 启动与重载时清除旧版 token 阈值产生的 opencode cooldown，只保留真实上游额度耗尽状态。

### v0.9.0

- `opencode-zen/` 不再套用火山的 110 万 token 安全阈值和 24 小时冷却。
- 精确识别上游 HTTP 429 `FreeUsageLimitError`，仅冷却实际报错模型到下一个北京时间 11:00。
- 增加可卸载的 OpenAI Provider 调用 guard，覆盖图片描述等绕开 Agent 事件流水线的直接调用。

### v0.8.0

- Provider 技术错误默认不再发送到原群聊或私聊，改为私聊 Bot 管理员。
- 全部 Provider 错误共用持久化的一小时告警限频，避免重复刷屏。
- 增加发送前错误兜底，修复最终 `role=err` 不触发 Agent done hook 时未开启火山熔断的问题。

### v0.7.0

- 火山任一模型返回 403 后整组熔断 30 分钟，防止欠费或权限故障时逐个撞击火山模型。
- 熔断期间继续使用非火山 fallback；到期后后台随机探测 quota 安全的火山文本模型，成功才恢复，失败自动续期。
- 熔断、最近错误、下次探测与探测租约持久化到 `quota_state.json`，并暴露到状态 API 和 Plugin Page。

### v0.6.0

- 每日额度窗口默认改为北京时间 11:00。
- 受控模型达到阈值后持久冷却 24 小时，跨窗口和重启仍有效。
- `deepseek/` provider 默认不限额，不创建 reservation 或 cooldown。
- 状态 API、Plugin Page、CSV 和命令状态增加 unlimited/cooldown 信息。

### v0.5.0

- 默认从链首严格按配置顺序检查 provider，修复会话停在链尾时跳过前面免费模型的问题。
- 增加按请求禁用 AstrBot 核心错误 fallback 的费用保护开关。
- `cmd_config.json` 默认检查间隔由 2 秒改为 300 秒。

### v0.4.0

- 增加 `cmd_config.json` fallback 链自动热更新和状态诊断。
- 无效或写入中的配置保留最后有效链。
- 修复模态不支持时 `use_last` 强行选择链尾的费用风险。

### v0.3.0

- 增加历史 token 用量报表 API。
- 在 Plugin Page 增加每日模型堆叠柱状图、单日占比饼图、单模型趋势图。
- 历史统计直接聚合 AstrBot 原生 `provider_stats`，无需重新开始记录。

### v0.2.0

- 增加 Plugin Page 状态面板。
- 增加 status、chains、decisions、export Web API。
- 增加告警、每日 snapshot 和 CSV 导出。

### v0.1.0

- 实现 provider/model 日额度路由 MVP。
- 支持 AstrBot fallback 链、自定义链、pending reservation、overlay 和 `/quota` 命令。
- 增加本地 spec、总体 plan、1 期 plan、2 期 plan 备份。
