# astrbot_plugin_provider_quota_router

AstrBot provider/model 日额度路由插件。它按配置的每日 token 额度监控聊天模型消耗，当某个 provider/model 达到阈值后，把后续会落到该 provider 的请求切到路由链的下一级 provider。

这个插件面向火山引擎开发者活动的“单一模型每日免费额度”场景。当前默认业务规则是北京时间 11:00 切换额度窗口、只有配置的火山 Provider Source 模型使用 token 上限并在达线后冷却 24 小时；`opencode-zen/` 只按上游免费额度错误进入未知刷新状态，由后台低频探测恢复；其他非火山 Provider 不参与本地 token 限制。

## 当前功能

- 使用 AstrBot 原生 `ProviderStat` 统计当前窗口 token。
- 默认按 `provider_model` 作为 quota key，也支持按 `provider_id`。
- 默认使用 AstrBot 的 `default_provider_id + fallback_chat_models` 作为路由链。
- 默认 fallback 链直接读取 `data/cmd_config.json`；每次 LLM 请求前检查文件签名并即时热更新，另有每 5 分钟一次的后台兜底，无需重启插件。
- 默认每次从 fallback 链首严格按顺序检查额度和请求模态，不沿用会话停留的旧 provider 作为扫描起点。
- 可按请求禁用 AstrBot 核心的错误 fallback，避免 403、超时等错误绕过额度判断进入后续付费模型。
- 在 provider 选择前通过 `selected_provider` 切换到第一个可用 provider。
- 使用 pending reservation 和短期 overlay 降低并发请求导致的超额风险。
- 额度判断与 reservation 在跨 router 共享临界区内原子完成，避免并发请求或 fallback 热重载同时看到旧 pending 而双放行。
- 达到阈值的受控模型会把 24 小时冷却写入 `quota_state.json`，跨重启和 11:00 窗口仍有效；插件会在 provider manager 就绪后延迟执行启动对账，并在每次受控模型响应后再次检查，避免最后一条请求跨线但没有后续请求时漏记。
- 只有 `volcengine_provider_source_ids` 指定的火山 Provider Source 使用本地 token 上限；按模型名统计时 SQL 还会限定到这些本地额度 Provider ID，付费 Token Plan 的同名模型不会串账。中转站、DeepSeek 及其他非火山 Provider 不阻断也不预占。
- 默认把 `opencode-zen/` 从火山 token 安全阈值中排除；具体模型返回 `FreeUsageLimitError` 后，只冷却该模型，用户请求期间零外呼，后台探测成功后恢复，其他 opencode 模型继续可选。
- opencode 额度保护同时覆盖 Agent 请求与图片描述等直接 Provider 调用；冷却期间直接调用会在发出网络请求前被拦截。
- Provider/SDK 自己报告的超时、连接失败、普通 429、5xx 等明确故障默认只冷却实际失败模型 30 分钟；插件自己的单次 20 秒首响应预算耗尽只 fallback，5 分钟内连续两次才短冷却 5 分钟。未知边界异常也短冷却 5 分钟。上下文、模态、工具、附件、内容审核和 400/422 请求错误不污染模型健康状态。
- Agent 当前模型失败后使用插件过滤过的安全 fallback 继续向下切换；每个模型默认只尝试 1 次，不在明确失败的模型上重复等待。
- 火山开发者计划明确返回 `AccountOverdueError` 等账号级故障后，整组火山模型熔断 30 分钟；普通请求级 403 不连坐。到期由后台从同 Source 的 token 安全文本模型中探测，成功才恢复整组。
- 每条请求保存不可变 RoutePlan；fallback 热重载只影响下一条请求，当前请求始终使用同一份链、策略和安全候选。
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
| `disable_astrbot_error_fallback` | `true` | 兼容配置键；开启后由插件接管并过滤 AstrBot 当前请求的错误 fallback |
| `quota_cooldown_seconds` | `86400` | 受控模型达到阈值后的冷却时间 |
| `unlimited_provider_prefixes` | `["deepseek/"]` | 兼容配置；所有非火山 Provider 均不参与本地 token 限制 |
| `upstream_quota_provider_prefixes` | `["opencode-zen/"]` | 按 `FreeUsageLimitError` 进入未知刷新额度状态的 Provider 前缀 |
| `volcengine_403_circuit_enabled` | `true` | 是否启用火山账号级 Source 熔断 |
| `volcengine_provider_source_ids` | `["openai"]` | 唯一会套用本地 token 上限和火山组级熔断的 provider source ID |
| `volcengine_403_cooldown_seconds` | `1800` | 火山 403 后的整组冷却时间 |
| `volcengine_probe_check_interval_seconds` | `30` | 后台恢复探测检查间隔 |
| `volcengine_probe_timeout_seconds` | `30` | 单次最小探测的超时时间 |
| `provider_error_cooldown_enabled` | `true` | Provider 瞬态故障启用单模型健康冷却 |
| `provider_error_cooldown_seconds` | `1800` | 已知超时、连接、429、5xx 故障冷却时间 |
| `unknown_provider_error_cooldown_seconds` | `300` | 未识别 Provider 边界异常的短冷却时间 |
| `provider_error_request_max_retries` | `1` | 每个受管模型在当前调用中的最大尝试次数；失败后立即切换 |
| `provider_error_attempt_timeout_seconds` | `20` | OpenAI-compatible 模型首响应墙钟预算；耗尽后结束当前尝试并 fallback，`0` 表示关闭 |
| `provider_attempt_timeout_failure_threshold` | `2` | 统计窗口内连续多少次本地首响应超时才开启短冷却；成功会清零 |
| `provider_attempt_timeout_failure_window_seconds` | `300` | 本地首响应超时连续计数窗口 |
| `provider_attempt_timeout_cooldown_seconds` | `300` | 达到连续阈值后的模型短冷却 |
| `upstream_quota_probe_initial_delay_seconds` | `3600` | 未知刷新额度首次后台探测延迟 |
| `upstream_quota_probe_interval_seconds` | `3600` | 额度仍未恢复时的后续探测间隔 |
| `upstream_quota_probe_timeout_seconds` | `20` | 单次额度恢复探测超时 |
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

`admin_user_ids` 为空时管理命令沿用 AstrBot 核心管理员权限；填写后这些 ID 作为额外管理员和错误通知目标。`allow_status_for_all=false` 时，状态查看也会限制为管理员。

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

`quota_state.json` 保存 pending reservation、短期 overlay、本地 token cooldown、未知刷新上游额度 cooldown、按完整 `provider_id` 隔离的单模型健康熔断、火山 Source 熔断/探测状态，以及管理员错误告警限频时间。损坏文件会备份为 `quota_state.corrupt.*.json`，不会被一次状态查询静默覆盖。权威历史用量仍来自 AstrBot 原生 `data_v4.db` 的 `provider_stats` 表。

## 设计说明

AstrBot 的 LLM 流程里，`on_waiting_llm_request` 在 main agent 构建和 provider 选择前触发；插件在这个阶段判断当前 provider 是否超过额度，并通过 `event.set_extra("selected_provider", next_provider_id)` 切换 provider。

请求真实 token 只有响应后才能知道，因此插件使用：

- `ProviderStat` 当前窗口聚合值。
- 当前未完成请求的 pending reservation。
- 响应后短时间 overlay，等待 AstrBot 异步写入 `ProviderStat`。
- safety buffer。

这不能保证绝对不越线，但能显著降低并发和异步落库造成的风险。

只有 `volcengine_provider_source_ids` 命中的火山模型会在 `effective_tokens + request_reservation + safety_buffer >= daily_limit` 时进入冷却。模型恢复必须同时满足：已经进入新的 11:00 日窗口，并且从达线时刻开始的 24 小时冷却已经结束。若模型在 15:00 达线，次日 11:00 仍会跳过，直到 15:00 才恢复。其他 Provider 即使 AstrBot 统计量超过该值也不会被停用。

`upstream_quota_provider_prefixes` 使用另一套策略：不按 AstrBot 本地 token 统计提前停用，也不使用 token 数推算 1 美元额度；只有上游明确返回 `FreeUsageLimitError` 才为实际报错模型写入未知刷新 cooldown。用户请求直接跳过该模型，后台默认一小时后开始最小探测，成功即恢复、失败再顺延一小时。普通每分钟 429 不会进入上游额度状态，也不会连坐同一 opencode source 下的其他模型。

`provider_error_cooldown_enabled=true` 时，错误先经过统一分类。Provider/SDK 自己抛出的超时、连接失败、408、普通 429、5xx 使用 1800 秒模型健康冷却；未知 Provider 边界异常默认短冷却 300 秒；上下文过长、模态/工具不支持、附件非法、内容审核和 400/422 请求错误不写健康状态。`FreeUsageLimitError` 只写 opencode 上游额度状态，明确 `AccountOverdueError` 才允许打开火山 Source 熔断。

`provider_error_attempt_timeout_seconds=20` 会限制 OpenAI-compatible 模型等待首个响应的时间。普通调用在 20 秒内未完成、流式调用在 20 秒内没有首个 chunk，都会结束当前尝试并立即 fallback；后续流式输出不受这个首响应计时器限制。单次本地墙钟预算耗尽不能证明上游故障，因此不会直接写 30 分钟健康冷却。默认同一 Provider 在 `provider_attempt_timeout_failure_window_seconds=300` 内连续达到 `provider_attempt_timeout_failure_threshold=2` 次才短冷却 `provider_attempt_timeout_cooldown_seconds=300` 秒；任一次成功会清零连续计数。Provider 自己抛出的真实超时仍按明确瞬态故障立即冷却 30 分钟。引用消息里的图片和语音会递归识别，路由阶段不会再先选纯文本模型后被 AstrBot 核心打回正在冷却的多模态模型。

默认 fallback 链在每次 LLM 请求前对 `data/cmd_config.json` 做一次轻量 `stat`；只有签名变化时才使用 `utf-8-sig` 读取 JSON 并原子替换 router，因此修改列表后的下一条消息即可看到新链。同时保留低频后台任务，默认每 300 秒在无请求时兜底检查一次。文件有其他配置变化但 fallback 内容相同时，不重建 router。

`strict_priority_order=true` 时，无论某个会话此前停在哪个 provider，每次请求都会重新从链首检查；只有前面的 provider 超过额度、缺失或不支持当前图片/音频模态时，才会检查下一项。

AstrBot 自己还会在 provider 返回 403、超时或错误响应时执行一套运行中 fallback。`disable_astrbot_error_fallback=true` 是早期版本保留的兼容键；现在它不会清空后续候选，而是让插件从实时链中取出当前模型之后的 Provider，按额度、冷却、火山熔断和请求模态过滤后注入 runner。`provider_error_request_max_retries=1` 会把受管 Agent 请求以及 OpenAI-compatible 直连调用的单模型尝试次数收紧为一次，异常抛出后按错误分类更新状态并让 runner 继续下一个安全候选；本地首响应预算首次耗尽只影响当前请求。

该安全 fallback 保护默认开启。当前置 provider 故障时，AstrBot 仍会继续尝试后续 provider，但候选固定来自本请求 RoutePlan，并已按额度、冷却、Source 状态和请求模态过滤。

`volcengine_403_circuit_enabled=true` 时，插件只在火山开发者计划出现明确账号级错误时持久化 Source 熔断；单条请求自己的 403 不默认连坐。同一条请求随后预先注入的其他火山候选会在网络调用前被 guard 拦截。30 分钟到期后，后台从同 Source 的已启用文本 Provider 中排除本地额度或模型健康冷却不安全者，再随机做一次最小半开探测；探测使用 task-local bypass，只绕过正在验证的 Source 熔断，修复探针被自身拦截的问题。

AstrBot 的最终 `role=err` 响应不会经过普通的 Agent done hook，因此插件同时在 Provider 调用 guard、`on_agent_done` 和发送前 `on_decorating_result` 三条路径处理错误。Provider guard 精确记录实际抛错模型，事件路径负责兜底释放 reservation、按统一错误分类更新模型状态、开启火山 403 熔断、私聊管理员并清空原会话结果。告警使用单一全局限频键，默认一小时最多发送一次，且跨重启、`reset-cache` 继续有效。

`exhausted_action=allow_paid/use_last` 只允许绕过火山开发者计划的本地 `quota_exceeded/cooldown_active`。provider 缺失、模态不支持、模型健康冷却、Source 熔断和 opencode 上游额度冷却都会阻断，不再强制发送到不安全候选；`allow_paid` 的 quota key 和 reservation 始终取原 Provider 自己的状态。

## 已知限制

- 处理 AstrBot 内部聊天模型 provider，并保护使用同一 AstrBot OpenAI Provider 实例的直接调用；不处理 TTS、embedding 或完全绕过 AstrBot Provider 的外部 API。
- 如果 provider 不返回 usage，AstrBot `ProviderStat` 可能记录 0，插件会无法准确判断真实额度。
- 如果其他 provider 路由类插件在更低 priority 后覆盖 `selected_provider`，最终 provider 可能不是本插件选择的 provider。
- 图片分类、后台好感度等直接 Provider 调用没有 Agent runner 的 fallback 上下文；插件会让它们按首响应预算快速失败并按错误分类更新状态，但不会擅自替这些业务选择另一个模型。

## Roadmap

- Future：火山引擎官方用量 API 对账、按 API key/account 分组额度、与 provider 负载均衡插件集成。

## 更新历史

### v0.12.1

- 拆分本地 20 秒首响应预算与真实 Provider 超时；首次本地预算耗尽只 fallback，不再误冷却模型 30 分钟。
- 同一 Provider 默认 5 分钟内连续两次本地首响应超时才短冷却 5 分钟，任一次成功会重置连续计数。
- 上游真实超时、连接、408、普通 429、5xx 仍立即进入 30 分钟健康冷却。
- 增加连续阈值、统计窗口和短冷却配置，并修复上游 `TimeoutError` 被本地计时器误包装。

### v0.12.0

- 引入不可变 ProviderPolicy、ErrorDisposition 和请求级 RoutePlan，按本地额度、未知刷新上游额度、模型健康和 Source 健康拆分状态。
- opencode free 不再假设 11:00 刷新；额度错误期间用户请求零外呼，后台探测成功后恢复。
- 请求级错误不再污染模型健康；已知瞬态故障冷却 30 分钟，未知 Provider 边界异常短冷却 5 分钟。
- 修复 `allow_paid/use_last` 越过非额度故障、错误 quota key、probe 自阻断、热重载混链、空管理员列表全员授权和状态损坏静默覆盖。
- 最终 usage/overlay 按 Provider guard 标记的实际成功模型归因；决策日志按 5 MiB 轮转并从文件尾读取。

### v0.11.2

- 递归识别引用消息里的图片和语音，路由阶段提前排除不支持请求模态的 Provider。
- OpenAI-compatible 模型默认最多等待 20 秒首响应；v0.12.1 起首次本地预算耗尽只 fallback，连续超时才短冷却。
- 发布验证逐个真实请求实时链全部 Provider，并通过 WebChat 完成默认模型到可用 Mimo 的端到端回归。

### v0.11.1

- 修复兼容保护清空全部 AstrBot 错误 fallback，导致当前请求卡在失败模型后直接结束的问题。
- 当前请求只注入实时链中通过额度、冷却、熔断和模态检查的后续 Provider，并保持配置顺序。
- 受管 Agent 请求和 OpenAI-compatible 直连调用默认每个模型只尝试 1 次，明确失败后立即进入下一个候选。
- 火山 403 在 Provider 异常层立即开启整组熔断，阻止同一请求继续撞击其他火山模型。

### v0.11.0

- 任意 Provider/模型调用最终失败后，按完整 `provider_id` 独立冷却 30 分钟。
- 冷却期间继续扫描后续 fallback，不连坐同一 Provider Source 的其他模型。
- 单模型熔断跨重启和 `reset-cache` 保留，到期自动恢复；状态 API 与 Plugin Page 显示恢复时间和最近错误。
- 保留火山 403 整组熔断与 opencode 免费额度到次日 11:00 的专用规则。

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
