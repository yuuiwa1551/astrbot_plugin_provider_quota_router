# Changelog

## v0.12.0

- 新增不可变 `ProviderPolicy`、`ErrorDisposition` 和请求级 `RoutePlan`，将火山本地日额度、opencode 未知刷新额度、单模型健康与 Source 账号故障解耦。
- opencode `FreeUsageLimitError` 不再按北京时间 11:00 到期；用户请求直接跳过，后台使用持久 probe lease 每小时探测，成功后恢复。
- 只有超时、连接、408/429/5xx 等 Provider 瞬态故障进入 30 分钟健康冷却；未知边界异常短冷却 5 分钟；400/422、上下文、模态、工具、附件和内容审核错误不污染模型。
- 火山 Source 熔断收紧为明确账号级错误，半开探测使用 task-local bypass，并从同 Source 的已启用安全文本模型中选择候选。
- 修复 `allow_paid/use_last` 绕过非额度故障、`allow_paid` 借用链首 quota key、fallback 热重载混用 router、最终 overlay 归错 Provider、空管理员列表放行所有用户等问题。
- 修复 `provider_model` 账本查询跨计费体系串账：本地日额度 SQL 只汇总命中火山开发者计划策略的 Provider ID，付费 Token Plan 的同名模型不再占用免费计划额度。
- 将路由判断与 reservation 合并为 StateStore 共享原子操作，修复并发请求以及热重载新旧 router 间的双放行窗口。
- 状态升级为 v7；损坏 JSON 自动备份，读操作只在清理发生时落盘；决策日志按 5 MiB 轮转并使用尾读。
- 版本统一为 0.12.0。

## v0.11.2

- 递归识别引用消息 `Reply.chain` 内的图片与语音，在路由和安全 fallback 阶段提前排除不支持请求模态的模型。
- 新增 `provider_error_attempt_timeout_seconds`，默认将 OpenAI-compatible 模型首响应等待限制为 20 秒；超时后立即冷却当前模型并继续后续 fallback。
- 发布验证首次包含实时链全部 Provider 的逐个真实请求，以及默认模型失败后由 AstrBot WebChat 进入可用 fallback 的端到端请求。

## v0.10.0

- 将本地 token 上限严格限定到 `volcengine_provider_source_ids` 对应的火山 Provider Source；中转站、DeepSeek、opencode 及其他非火山模型均不套用 110 万阈值。
- 每次 LLM 请求前只检查一次 `cmd_config.json` 文件签名，fallback 变化时立即热加载；300 秒后台监视保留为无请求时兜底。
- fallback 内容未变化时只更新文件签名，不再重复重建 router 或打印误导性的热加载日志。

## v0.9.1

- 修正旧状态迁移：清除 opencode 由 110 万 token 阈值生成的 cooldown，不再将其延期到次日 11:00。
- 仅保留上游实际返回 `FreeUsageLimitError` 后生成的模型级 cooldown；opencode 的 1 美元额度不通过本地 token 数推算。

## v0.9.0

- 新增 `upstream_quota_provider_prefixes`，默认 `opencode-zen/` 不再使用火山 token 安全阈值。
- 精确识别 opencode HTTP 429 `FreeUsageLimitError`，按具体模型持久冷却到下一个北京时间 11:00，不连坐其他 opencode 模型。
- 增加可卸载的 Provider 调用 guard，普通 Agent、图片描述及其他直接调用同一 AstrBot Provider 的路径均能触发或遵守 cooldown。
- 状态 API 与 Plugin Page 展示 opencode 上游额度策略与模型级 cooldown。

## v0.8.0

- 本插件接管的任意模型最终返回 Provider 错误时，默认清空原会话错误回复并私聊 AstrBot 管理员。
- 全部 Provider 错误共用一个持久化 3600 秒限频键，重启和 `/quota reset-cache` 后仍不会重复刷屏。
- 在发送前 `on_decorating_result` 增加错误兜底，覆盖 AstrBot 最终 `role=err` 不调用 Agent done hook 的路径，并确保火山 403 仍会开启整组熔断。
- 状态 API 和 Plugin Page 增加管理员告警限频状态。

## v0.7.0

- 任一火山模型返回 HTTP 403 / `AccountOverdueError` 后，持久化整组火山熔断并跳过全部火山候选，默认冷却 30 分钟。
- 熔断期间继续路由到非火山 fallback；冷却到期后，后台随机挑选仍处于单模型 token 安全线内的火山文本模型做最小探测。
- 探测成功才恢复整组；失败、超时或无可用探测候选时继续保持保护状态，用户请求不会被用作恢复探针。
- 状态 API 和 Plugin Page 增加火山组级熔断、最近错误、下次探测和探测模型信息。

## v0.6.0

- 每日额度窗口默认调整为北京时间 11:00。
- 新增 `quota_cooldown_seconds`；受控模型达到阈值后默认持久冷却 24 小时，跨 11:00 新窗口和插件重启仍有效。
- provider manager 就绪后执行延迟启动对账，并在受控模型响应结束后再次检查阈值，避免在 11:00 前最后一条请求跨线时漏建 cooldown。
- 新增 `unlimited_provider_prefixes`，默认 `deepseek/` 自有 API 不参与额度判断、reservation 或 cooldown。
- `/quota reset-cache` 保留费用保护冷却，避免管理操作意外解锁火山模型。
- 状态 API、Plugin Page、CSV 与路由决策日志增加 unlimited/cooldown 信息。

## v0.5.0

- 增加 `strict_priority_order`，默认每次从 fallback 链首严格按顺序检查，避免会话当前 provider 导致前置模型被跳过。
- 增加 `disable_astrbot_error_fallback`，可为本插件接管的请求禁用 AstrBot 核心错误 fallback，阻止 403、超时等错误绕过额度路由进入后续付费模型。
- 核心 fallback guard 仅使用事件 extra 和 runner reset 边界，插件卸载后恢复原方法，不修改 `cmd_config.json` 或 AstrBot 核心文件。
- fallback 配置文件默认检查间隔从 2 秒调整为 300 秒。
- Plugin Page 状态栏显示严格优先级和核心 fallback guard 状态。

## v0.4.0

- 默认 fallback 链改为直接读取 AstrBot `cmd_config.json`，不再只复制插件启动时的 provider manager 快照。
- 增加后台文件签名监视，默认每 2 秒检测一次并原子热更新路由链。
- 配置读取失败、写入竞争或无效 JSON 时保留最后一份有效链，并在状态 API 暴露错误。
- Plugin Page 显示 fallback 来源、watch 状态、配置路径和最后加载时间。
- 修复 `use_last` 在 provider 缺失或模态不支持时仍强行选择链尾的问题；现在只有纯额度耗尽才允许该行为。

## v0.3.0

- 增加 `/history` Web API，按 AstrBot `provider_stats` 聚合历史日用量。
- Plugin Page 增加每日模型堆叠柱状图、单日模型占比饼图、单模型趋势图。
- 历史统计窗口与 `timezone + reset_time` 保持一致，token 口径遵循 `count_cached_input_tokens`。

## v0.2.0

- 增加 AstrBot Plugin Page 状态面板。
- 增加 `/status`、`/chains`、`/decisions`、`/export` Web API。
- 增加 80% / 90% / 95% 用量告警、链路耗尽告警、每日 snapshot 和 CSV 导出。

## v0.1.0

- 初始 1 期版本。
- 支持按 provider/model 当前窗口 token 用量自动路由到下一级 provider。
- 支持 `/quota status`、`/quota reload`、`/quota reset-cache`、`/quota dry-run on|off`。
- 使用 AstrBot 原生 `ProviderStat`，并通过 pending reservation 与短期 overlay 处理并发和异步落库窗口。
