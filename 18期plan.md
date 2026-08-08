# 18期：专用 Provider 调用预算

## 目标

只修改 quota router，为不经过普通对话路由钩子的直连 `context.llm_generate()` 增加按 Provider 生效的独立调用预算；避免辅助模型长时间占用连接，同时不缩短主路由模型的全局 20 秒首响应预算。

## MVP 范围

- 新增 `provider_policy_overrides_json`，按完整 Provider ID 覆盖首响应超时、单 Provider 请求次数和最大输出 Token。
- 覆盖策略进入不可变 `ProviderPolicy`；运行中的 Provider guard 每次调用读取同一份已解析设置。
- `max_output_tokens` 只做上限：调用方已设置更小值时保持原值，未设置或设置更大值时才收紧。
- 普通调用与流式调用共用 Provider 专属首响应预算；首次本地预算耗尽只结束当前尝试，连续两次才沿用现有 5 分钟短冷却。
- 未配置覆盖的 Provider 完全沿用现有全局策略。

## 本机上线策略

`volcengine-agent-plan/doubao-seed-2.0-mini` 作为辅助请求专用 Provider：

- `first_response_timeout_seconds`: `3`
- `request_max_retries`: `1`
- `max_output_tokens`: `220`

该 Provider 当前不在主 fallback 链和自定义 chain 中。Stealer 与 Affection 仅保留现有 Provider ID 配置，不再修改它们的代码。

## 验证

- 配置测试覆盖 JSON 解析、边界值、重复 Provider 和未命中回退。
- ProviderPolicy 测试覆盖专属覆盖与全局默认继承。
- Provider guard 测试覆盖 Token 上限注入、保留更小上限、专属超时和重试次数。
- 容器内执行完整 `unittest`、Ruff、`compileall`、JSON 和 diff 检查。
- 部署后通过认证状态 API 确认有效覆盖策略，并用真实直连 Mini 请求验证约 3 秒止损与连续超时短冷却。

## 延后项

- 在 AstrBot SDK 增加显式 `request_purpose`，按调用用途而非专用 Provider ID 区分预算。
- 为非 OpenAI-compatible Provider 增加同等调用 guard。

## 状态

v0.14.0 已完成并部署到实时 AstrBot。

- 隔离容器内 109 项测试通过，Ruff、`compileall`、配置 JSON 和 `git diff --check` 全部通过。
- 认证状态 API 返回 `ok=true`、状态版本 7、无加载错误；全局策略保持 20 秒 / 1 次，Mini 专属策略为 3 秒 / 1 次 / 220 Token，两个 guard 均启用。
- Mini Provider 直测为 `available`，耗时 0.653 秒。
- 临时 WebChat 显式选择 Mini 的回复在 1.68 秒完成并完整读到 SSE EOF；随后的两个后台辅助调用均在 3 秒结束，连续两次后按既有规则开启 5 分钟短冷却，未拖慢已经完成的 Bot 回复。
- quota router 源码与实时插件 63 个发布文件哈希一致；Stealer、Affection 源码与实时目录分别 24/24、10/10 一致，未保留跨插件代码改动。
