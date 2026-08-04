# 17期：显式模型优先与路由快路径

## 目标

修复 `strict_priority_order=true` 把请求级或 UMO 会话级明确 Provider 选择重新覆盖为全局链首的问题；同时减少每条消息为 AstrBot 错误 fallback 重复扫描整条模型链的开销。

## MVP 范围

- 区分三种选择来源：请求显式 `selected_provider`、UMO `provider_perf_chat_completion`、普通默认 Provider。
- 请求或 UMO 明确选择时，将指定 Provider 放在本次不可变候选顺序首位。
- 指定 Provider 不可用时，继续使用原全局优先级链中的其余 Provider，不能因为指定项位于链尾就失去 fallback。
- 未明确选择时，保留 `strict_priority_order=true` 从全局链首恢复优先级的现有行为。

## 性能增强

- 将本次候选顺序固化进 `RouteDecision` / `RoutePlan`，路由和运行中 fallback 共用同一顺序。
- 新增 `provider_error_fallback_max_candidates=1`，明确限制一条消息最多再试一个安全 fallback；按需短路，不再先构造整条链的完整状态表和十几个候选。
- 在决策记录中保存 `selection_origin` 和 `planning_elapsed_ms`，用于区分模型耗时与插件规划耗时。

## 验证

- 单元测试覆盖：请求显式选择、UMO 显式选择、普通严格链首、指定链尾失败后回到全局链、fallback 数量短路、模态和冷却过滤。
- 容器内执行完整 `unittest`、Ruff、JSON 与 diff 检查。
- 部署后验证群 `default:GroupMessage:822728596` 的 `deepseek/deepseek-v4-flash` 不再被健康状态下的链首模型覆盖。
- 使用实时决策记录确认规划耗时，并用 Provider 测试接口确认目标 Provider 可用。

## 延迟分析与延期项

- 2026-08-04 实时日志显示 quota-router 前置阶段约 0.3–0.8 秒，正常主模型生成约 4–11 秒。
- 回复后的情绪分析过去 24 小时 37 次，平均 4.8 秒、P95 13.7 秒、最长 15.3 秒；Doubao 2.0 Mini 同期发生 14 次 20 秒首响应超时。
- 情绪分析属于其他插件，本期不跨插件修改。后续建议为该辅助请求设置 1.5–2.5 秒独立预算并在超时时退回本地规则，或改为不阻塞正文发送的异步装饰。
- 本期不盲目缩短所有 Provider 的全局 20 秒首响应预算，避免再次误伤正常但偶尔较慢的主模型。

## 完成状态

- v0.13.0 已部署到实时 AstrBot 4.26.4。
- 容器内 98 项 `unittest`、Ruff、`compileall`、JSON 与 diff 检查通过。
- 认证状态 API 显示 `strict_priority_order=true`、`provider_error_request_max_retries=1`、`provider_error_fallback_max_candidates=1`，实时链共 15 项。
- 实时 UMO 偏好验证：`default:GroupMessage:822728596` 的 `deepseek/deepseek-v4-flash` 位于本次候选顺序第一项，失败后的首个备用项才是全局链首豆包 Lite。
- DeepSeek V4 Flash Provider 测试接口返回 `ok`，耗时约 950 ms。
- 临时 WebChat 端到端请求显式选择 DeepSeek V4 Flash，1.36 秒返回 `V013_ROUTE_OK`；决策为 `allow`，`selection_origin=request`，规划耗时 56.836 ms，安全 fallback 恰好 1 个。临时会话已删除。
- 上线前备份位于 `D:\astrbot\data\backups\quota_router_v0.13.0_20260804_192632`。
