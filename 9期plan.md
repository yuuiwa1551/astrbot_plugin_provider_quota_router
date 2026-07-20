# 9期计划：opencode 免费额度错误冷却

## 目标

将 `opencode-zen/` 免费模型从火山的固定 token 安全线策略中拆开。opencode 返回 `FreeUsageLimitError` 时，仅把实际报错的模型冷却到下一个北京时间 11:00；其他 opencode 模型和 DeepSeek 自有 API 继续可选。

## MVP 范围

- 默认将 `opencode-zen/` 识别为“按上游免费额度错误冷却”的 Provider 前缀，不再套用火山的 110 万 token 安全阈值和 24 小时冷却。
- 精确识别 HTTP 429 `FreeUsageLimitError`，兼容其 `Rate limit exceeded. Please try again later.` 文本。
- 在普通 Agent 请求以及图片描述等直接调用 Provider 的路径捕获该错误。
- 按具体 Provider/model 写入持久化 cooldown，截止时间为当前额度窗口的 `end_local`，即下一个 `11:00`。
- 路由时跳过仍在冷却的 opencode 模型；同一 source 下其他未报错模型不连坐。
- 启动时把当前窗口内由旧 token 阈值生成的 opencode 24 小时 cooldown 收敛到下一个 11:00。

## 技术决策

- 不新增数据库；继续复用 `quota_state.json` 的模型级 cooldown。
- 不新增定时任务；恢复时间由现有 `timezone + reset_time` 窗口计算。
- 增加可卸载的 Provider 调用 guard，以覆盖绕开 Agent 事件流水线的图片描述等直接调用；插件卸载时恢复原方法。
- 不修改 AstrBot 核心文件，不修改 `cmd_config.json` 中的 fallback 顺序。

## 验证

- opencode `FreeUsageLimitError` 只冷却报错模型，`expires_at` 等于下一个 11:00。
- 普通瞬时错误或其他 Provider 的 429 不会触发 opencode 日冷却。
- 冷却中的 opencode 模型不会发起外部请求；到新窗口后自动恢复。
- 图片描述直接调用也能触发同一模型 cooldown。
- 容器内通过单元测试、`compileall`、插件加载与状态 API 验证。

## 延后项

- 为不同 opencode 模型配置不同重置时间。
- 对上游未来新增、且不再返回 `FreeUsageLimitError` 的错误格式做远端规则下发。

## 状态

- v0.9.0 已完成并同步实时 Docker 环境。
- 容器内 `compileall` 与 39 个单元测试通过；包导入、插件加载、状态 API 和持久化状态均已验证。
- 实时两个 opencode 模型的旧 token cooldown 已迁移为 `upstream_quota_migrated`，统一于 2026-07-21 11:00（北京时间）恢复；后续只会冷却实际返回 `FreeUsageLimitError` 的模型。
