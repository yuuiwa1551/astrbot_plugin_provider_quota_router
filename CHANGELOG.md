# Changelog

## v0.1.0

- 初始 1 期版本。
- 支持按 provider/model 当前窗口 token 用量自动路由到下一级 provider。
- 支持 `/quota status`、`/quota reload`、`/quota reset-cache`、`/quota dry-run on|off`。
- 使用 AstrBot 原生 `ProviderStat`，并通过 pending reservation 与短期 overlay 处理并发和异步落库窗口。
