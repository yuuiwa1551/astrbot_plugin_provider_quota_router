# 21期：插件 Provider 直调统一路由

## 目标

只修改 quota router，使所有通过 AstrBot Chat Provider 层发出的插件 LLM 调用复用
现有安全路由；插件自行创建外部 SDK/HTTP 客户端的私有调用不在无侵入覆盖范围内。

## MVP 范围

- 增加默认关闭的 `route_direct_provider_calls_enabled`，本机配置显式开启。
- 增加不可变请求级 `DirectRoutePlan`，候选顺序固定为“请求 Provider → 实时全局链”，
  去重后逐一执行额度、冷却、熔断、可用性和模态检查。
- 在所有已加载的具体 Chat Provider `text_chat/text_chat_stream` 安装可卸载 guard；
  `StarContext.llm_generate` 与 `tool_loop_agent` 最终都汇入这一统一边界，无需修改或重复
  monkey-patch 高层 API。
- 正常会话主调用、内部探测和 guard bypass 不进入直调路由；第三方插件即使在主会话
  生命周期内嵌套调用仍会被识别。使用 ContextVar 与 `route_kind` 防止 fallback 递归和
  并发串扰。
- 每次直调最多使用 `direct_provider_fallback_max_candidates` 个备用模型，默认 1；
  请求错误和取消不误冷却，流式输出开始后不重启其他模型。
- 直调独立完成 reservation 的预占、fallback 重定向、usage 归因和最终释放，不建立
  或更新 UMO 会话亲和。
- 将流式首包超时与非流式完整调用超时拆开，避免短输出辅助调用被 3 秒整段超时误冷却。
- 日志和最近决策记录 `route_kind=plugin_direct`、调用插件、请求/实际 Provider、模态、
  尝试结果和耗时；重复冷却跳过日志限频。

## 增强项

- 动态发现并保护已加载的 OpenAI、Anthropic、Gemini 及第三方 Chat Provider 具体类。
- 父类与子类 Provider 同时被保护时，同一实例只执行一次 guard，不重复计错或超时。
- Plugin Page 增加路由类型、调用插件、请求 Provider 与实际 Provider 列。

## 延后项

- 插件自己创建 `AsyncOpenAI`、`httpx` 或厂商 SDK 的私有连接；这类调用需要插件改用
  AstrBot Provider API 或显式接入 quota router 适配器。
- Embedding、rerank、STT、TTS、图片/视频生成不复用 Chat Provider 路由。

## 验证

- 单元测试覆盖 `llm_generate` 汇入的 Provider 边界、原始 `text_chat`、首包前流式
  fallback、正常 RoutePlan 与嵌套插件调用、probe bypass、首选不在全局链、额度预占
  释放、模态过滤、取消、父子 Provider 和并发隔离。
- 回归测试证明非流式调用不再受 `first_response_timeout_seconds` 整段限制，流式首包仍受限。
- 容器内运行完整测试、`ruff check . --isolated`、`compileall` 和 JSON/版本校验。
- 插件文件部署后比对源码/运行时 SHA-256，保留 `quota_state.json`、决策日志和配置。
- 强制 Mini 处于冷却时触发 Stealer 情绪分析，平台日志应显示插件直调立即选择健康 Provider，
  且不再出现对已冷却 Mini 的重复调用。

## 状态

v0.16.0 实施中。
