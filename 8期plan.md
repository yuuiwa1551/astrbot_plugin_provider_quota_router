# 8期计划：Provider 错误静默与管理员限频私聊

## 目标

本插件接管的模型最终返回 Provider 错误时，不再把技术错误文本发送到原群聊或私聊；改为私聊 AstrBot 管理员，并以持久化全局窗口限制为每小时最多一条。火山 403 同时继续触发全部火山模型的 30 分钟组级熔断。

## MVP 范围

- 在 `on_agent_done` 和发送前 `on_decorating_result` 两条路径识别 Provider 错误，覆盖 AstrBot 最终 `role=err` 不触发 Agent done hook 的情况。
- 火山 403 继续开启整组 30 分钟熔断，非火山候选不受影响。
- 错误告警默认发送给 AstrBot 核心 `admins_id`；插件 `admin_user_ids` 非空时作为显式覆盖。
- 所有 Provider 错误共用一个 3600 秒持久化限频键，重启和 cache reset 后仍有效。
- 告警发送后清空原会话错误结果并终止发送。
- 状态 API 和 Plugin Page 暴露告警限频状态。

## 验证

- 发送前结果包含最终 Provider 403 时，能释放 reservation、开启火山组熔断、解析管理员私聊目标并清空原结果。
- 一小时内重复错误无法再次领取告警发送权。
- 普通模型文本即使提到 HTTP 403，也不会被误判为 Provider 错误。
- 容器内完成单元测试、compileall、插件加载和状态 API 验证。

## 延后项

- 对完全绕开 AstrBot Agent 和事件流水线、且自行捕获异常的第三方插件做全局 monkey patch。

## 状态

- v0.8.0 已完成并同步实时 Docker 环境；31 个单元测试、`compileall`、发送前错误流模拟、插件加载和状态 API 验证均已通过。
