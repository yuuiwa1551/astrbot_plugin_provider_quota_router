# 15期：首响应预算与模型健康解耦

## 背景与线上证据

2026-07-23 实时日志中，GLM 5.2 在 14:40、15:17、16:08 三次进入冷却，Doubao 2.0 Mini 在 16:03 一次进入冷却。四次直接触发原因全部是插件生成的 `ProviderAttemptTimeoutError`：20 秒内未拿到首响应，而不是上游 429、5xx、账号额度、连接错误或 SDK 原生超时。

其中前两次 GLM 5.2 来自 Stealer 的情绪分析辅助调用，16:08 来自主对话“详细说说为什么现在的 llm 都是 transformer 架构”，Mini 来自 Affection 的后台意识调用。绕过插件冷却后的 Token Plan 直测仍返回 HTTP 200：GLM 5.2 约 5.21 秒，Mini 约 0.8 秒。这说明原实现把“某一次请求生成超过本地等待预算”过度推断成了“模型未来 30 分钟不健康”。

## 目标

- 保留 20 秒首响应墙钟预算，避免一条消息按 fallback 链连续等待过久。
- 单次本地预算耗尽只影响当前请求，不污染跨请求的模型健康。
- 连续慢响应仍能短暂止损，避免辅助任务密集触发时每次都白等 20 秒。
- 真实上游故障继续快速进入健康冷却。

## 策略

1. `ProviderAttemptTimeoutError` 明确分类为 `local_attempt_timeout`，不直接写模型健康冷却。
2. 同一完整 Provider ID 在 300 秒内连续发生 2 次本地首响应超时后，开启 300 秒短冷却。
3. 任一次正常响应或正常首个流式 chunk 都清零该 Provider 的连续计数。
4. Provider/SDK 自己抛出的 `TimeoutError` 不再被本地计时器重新包装，仍按真实瞬态故障立即冷却 1800 秒。
5. AstrBot 把异常序列化成 `All chat models failed: ProviderAttemptTimeoutError...` 后，事件兜底路径仍识别为本地预算耗尽，不能二次写入 30 分钟冷却。

## 配置

- `provider_error_attempt_timeout_seconds=20`
- `provider_attempt_timeout_failure_threshold=2`
- `provider_attempt_timeout_failure_window_seconds=300`
- `provider_attempt_timeout_cooldown_seconds=300`

## 验证

- 单元测试覆盖首次不冷却、连续两次短冷却、成功重置、窗口外重新计数。
- 单元测试覆盖上游原生 `TimeoutError` 保持原类型、成功响应回调和序列化错误文本。
- 容器内执行完整 `unittest` 套件和 `ruff check`。
- 部署前备份实时插件、配置和 `quota_state.json`，部署后只清除 GLM 5.2 与 Mini 的误判模型健康冷却。
- 使用认证状态 API、Provider 测试、实时日志以及源码/运行目录哈希比对完成上线验证。

## 完成状态

- v0.12.1 已同步到实时目录并由 AstrBot 4.26.4 加载。
- 容器内完整 89 项 `unittest` 全部通过，`ruff check`、JSON 配置解析和 `git diff --check` 通过。
- 认证状态 API 返回首响应预算 20 秒、连续阈值 2、统计窗口 300 秒、短冷却 300 秒，模型与 Source 熔断数均为 0。
- AstrBot Provider 测试中，`volcengine-agent-plan/glm-5.2` 约 6.9 秒返回 `available`，`volcengine-agent-plan/doubao-seed-2.0-mini` 约 1.08 秒返回 `available`。
- 上线前备份位于 `D:\astrbot\data\backups\quota_router_v0.12.1_20260723_163524`。

## 非目标

- 不提高或取消 20 秒单请求首响应预算。
- 不改变火山开发者计划日额度、24 小时冷却、opencode free 未知刷新额度或 Source 账号熔断。
- 不把连续计数持久化；它只用于当前进程的短时慢响应判定，重启后清空是安全的。
