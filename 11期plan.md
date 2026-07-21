# 11期计划：全 Provider 单模型错误冷却

## 目标

- 任意模型调用最终抛错后，只将报错的 Provider/模型配置冷却 30 分钟。
- 冷却期间路由器跳过该模型并继续扫描后续 fallback，不连坐同一 Provider Source 的其他模型。
- 30 分钟到期后自动允许该模型再次参与路由；若再次失败，再开启新一轮冷却。
- 保留火山 HTTP 403 的整组熔断和 opencode 免费额度到次日 11:00 的专用规则。

## 实现

- 新增独立持久化状态 `provider_model_circuits`，以完整 `provider_id` 隔离模型，不复用 token quota key。
- Provider 调用 guard 在请求前检查单模型冷却，并在调用异常后原子开启 1800 秒冷却。
- Agent 最终 `role=err` 路径也补写单模型冷却，覆盖非 OpenAI-compatible Provider。
- 路由决策和状态 API 增加 `provider_error_cooldown`，展示触发时间、恢复时间和最近错误。
- 新增 `provider_error_cooldown_enabled` 与 `provider_error_cooldown_seconds` 配置，默认开启和 1800 秒。

## 验证

- 一个模型失败后只跳过该模型，下一模型仍可选。
- 同一 Provider Source 下的另一个模型不受影响。
- 冷却跨插件重启保留，`reset-cache` 不清除；到期后自动恢复。
- direct provider 调用在冷却期内不访问上游。
- 火山组熔断和 opencode 次日 11:00 冷却的原有测试继续通过。
- 源码与实时容器运行完整单元测试、语法检查、重启和认证状态 API 验证。

## 延后项

- 不为普通单模型冷却增加后台主动探针；首个到期后的真实请求即为惰性恢复探测。
- 不修改 AstrBot 核心的 5 次内部重试；本期在一次调用最终失败后冷却后续请求。

## 状态

- v0.11.0 已完成并同步实时 Docker 环境。
- 容器内 49 项单元测试全部通过，源码语法编译通过。
- 2026-07-21 线上真实验证：`openai/doubao-seed-2-0-mini-260215` 首次返回 403 后，仅该完整 Provider ID 进入 30 分钟冷却（12:34:57 至 13:04:57）；随后两次 AstrBot 内部重试均在本地 guard 被拦截，没有再次请求上游。
