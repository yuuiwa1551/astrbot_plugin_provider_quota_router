# Changelog

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
