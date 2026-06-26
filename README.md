# astrbot_plugin_provider_quota_router

AstrBot provider/model 日额度路由插件。它按配置的每日 token 额度监控聊天模型消耗，当某个 provider/model 达到阈值后，把后续会落到该 provider 的请求切到路由链的下一级 provider。

这个插件面向“单一模型每日免费额度”场景，例如火山引擎开发者活动。额度、重置时间和计数口径都通过配置控制，插件不会硬编码活动规则。

## 当前功能

- 使用 AstrBot 原生 `ProviderStat` 统计当前窗口 token。
- 默认按 `provider_model` 作为 quota key，也支持按 `provider_id`。
- 默认使用 AstrBot 的 `default_provider_id + fallback_chat_models` 作为路由链。
- 在 provider 选择前通过 `selected_provider` 切换下一级 provider。
- 使用 pending reservation 和短期 overlay 降低并发请求导致的超额风险。
- 支持链路耗尽后的 `stop`、`allow_paid`、`use_last` 三种行为。
- 提供 `/quota` 管理命令。
- 提供 Plugin Page 状态面板、告警、最近路由决策和 CSV 导出。

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

后端 API：

- `GET /api/plug/astrbot_plugin_provider_quota_router/status`
- `GET /api/plug/astrbot_plugin_provider_quota_router/chains`
- `GET /api/plug/astrbot_plugin_provider_quota_router/decisions`
- `GET /api/plug/astrbot_plugin_provider_quota_router/export?date=YYYY-MM-DD`

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

## 已知限制

- 只处理 AstrBot 内部聊天模型 provider，不处理 TTS、embedding 或其他插件自己直接调用的外部 API。
- 如果 provider 不返回 usage，AstrBot `ProviderStat` 可能记录 0，插件会无法准确判断真实额度。
- 如果其他 provider 路由类插件在更低 priority 后覆盖 `selected_provider`，最终 provider 可能不是本插件选择的 provider。
- AstrBot 内部 provider 失败后运行中 fallback 的最终归因只能靠后续 `ProviderStat` 校准。

## Roadmap

- Future：火山引擎官方用量 API 对账、按 API key/account 分组额度、与 provider 负载均衡插件集成。

## 更新历史

### v0.2.0

- 增加 Plugin Page 状态面板。
- 增加 status、chains、decisions、export Web API。
- 增加告警、每日 snapshot 和 CSV 导出。

### v0.1.0

- 实现 provider/model 日额度路由 MVP。
- 支持 AstrBot fallback 链、自定义链、pending reservation、overlay 和 `/quota` 命令。
- 增加本地 spec、总体 plan、1 期 plan、2 期 plan 备份。
