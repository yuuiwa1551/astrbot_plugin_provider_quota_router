# astrbot_plugin_provider_quota_router

AstrBot provider/model 日额度路由插件。它按配置的每日 token 额度监控聊天模型消耗，当某个 provider/model 达到阈值后，把后续会落到该 provider 的请求切到路由链的下一级 provider。

这个插件面向“单一模型每日免费额度”场景，例如火山引擎开发者活动。额度、重置时间和计数口径都通过配置控制，插件不会硬编码活动规则。

## 当前功能

- 使用 AstrBot 原生 `ProviderStat` 统计当前窗口 token。
- 默认按 `provider_model` 作为 quota key，也支持按 `provider_id`。
- 默认使用 AstrBot 的 `default_provider_id + fallback_chat_models` 作为路由链。
- 默认 fallback 链直接读取 `data/cmd_config.json`，每 5 分钟检查一次配置变化并自动热更新，无需重启插件。
- 默认每次从 fallback 链首严格按顺序检查额度和请求模态，不沿用会话停留的旧 provider 作为扫描起点。
- 可按请求禁用 AstrBot 核心的错误 fallback，避免 403、超时等错误绕过额度判断进入后续付费模型。
- 在 provider 选择前通过 `selected_provider` 切换到第一个可用 provider。
- 使用 pending reservation 和短期 overlay 降低并发请求导致的超额风险。
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
| `reset_time` | `00:00` | 每日额度重置时间 |
| `default_daily_limit_tokens` | `2000000` | 默认单模型日额度 |
| `default_safety_buffer_tokens` | `100000` | 安全余量 |
| `default_request_reservation_tokens` | `50000` | 单请求预占 token |
| `quota_key_mode` | `provider_model` | 按模型名或 provider ID 统计 |
| `exhausted_action` | `stop` | 链路耗尽后的行为 |
| `dry_run` | `false` | 只记录决策，不实际切换 |
| `use_astrbot_fallback_chain` | `true` | 未配置自定义链时使用 AstrBot fallback 链 |
| `fallback_watch_interval_seconds` | `300` | 检查 `cmd_config.json` 变化的间隔，默认 5 分钟 |
| `strict_priority_order` | `true` | 每次从链首严格按顺序检查候选 |
| `disable_astrbot_error_fallback` | `false` | 可选的费用优先保护；开启后 provider 错误不再继续尝试后续模型 |
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
- `/quota reset-cache`：清理本地 pending/overlay 缓存，不删除 AstrBot 原生数据库。
- `/quota dry-run on|off`：临时切换演练模式。

`admin_user_ids` 为空时管理命令不限制，便于首次部署；填写后只有指定发送者可执行 reload/reset/dry-run。`allow_status_for_all=false` 时，状态查看也会限制为管理员。

## WebUI

v0.2.0 提供 AstrBot Plugin Page，不需要额外端口。

页面能力：

- 当前窗口 provider/model 用量表。
- 可用、耗尽、告警数量汇总。
- 80% / 90% / 95% 和链路耗尽告警。
- pending reservation 与短期 overlay 状态。
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

`quota_state.json` 只保存 pending reservation 和短期 overlay。权威历史用量仍来自 AstrBot 原生 `data_v4.db` 的 `provider_stats` 表。

## 设计说明

AstrBot 的 LLM 流程里，`on_waiting_llm_request` 在 main agent 构建和 provider 选择前触发；插件在这个阶段判断当前 provider 是否超过额度，并通过 `event.set_extra("selected_provider", next_provider_id)` 切换 provider。

请求真实 token 只有响应后才能知道，因此插件使用：

- `ProviderStat` 当前窗口聚合值。
- 当前未完成请求的 pending reservation。
- 响应后短时间 overlay，等待 AstrBot 异步写入 `ProviderStat`。
- safety buffer。

这不能保证绝对不越线，但能显著降低并发和异步落库造成的风险。

默认 fallback 链由后台任务监视 `data/cmd_config.json`。任务只做低频文件 `stat`，签名变化后才使用 `utf-8-sig` 读取 JSON 并原子替换 router，因此不会在每个请求路径重复读取整份配置文件。

`strict_priority_order=true` 时，无论某个会话此前停在哪个 provider，每次请求都会重新从链首检查；只有前面的 provider 超过额度、缺失或不支持当前图片/音频模态时，才会检查下一项。

AstrBot 自己还会在 provider 返回 403、超时或错误响应时执行一套运行中 fallback，这不是额度耗尽。`disable_astrbot_error_fallback=true` 会为本插件实际接管的请求清空该运行中 fallback，使模型切换只由本插件的额度决策触发。该保护不会修改 `cmd_config.json` 或 AstrBot Dashboard 中的列表；provider 错误时本次请求会直接失败，不会自动尝试下一级。

默认保持该保护关闭。这样当前置 provider 因欠费、故障或网络错误不可用时，AstrBot 仍会按 `fallback_chat_models` 尝试后续 provider。只有确认后续 provider 不应因错误被使用时，才建议开启。

`exhausted_action=use_last` 只在所有候选都是 `quota_exceeded` 时生效。provider 缺失或不支持请求中的图片、音频时会阻断请求，不再强制发送到链尾。

## 已知限制

- 只处理 AstrBot 内部聊天模型 provider，不处理 TTS、embedding 或其他插件自己直接调用的外部 API。
- 如果 provider 不返回 usage，AstrBot `ProviderStat` 可能记录 0，插件会无法准确判断真实额度。
- 如果其他 provider 路由类插件在更低 priority 后覆盖 `selected_provider`，最终 provider 可能不是本插件选择的 provider。
- 未开启 `disable_astrbot_error_fallback` 时，AstrBot 内部 provider 失败后的运行中 fallback 仍不会再次触发插件的额度判断。

## Roadmap

- Future：火山引擎官方用量 API 对账、按 API key/account 分组额度、与 provider 负载均衡插件集成。

## 更新历史

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
