# Changelog

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
