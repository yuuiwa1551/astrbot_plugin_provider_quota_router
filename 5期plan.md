# 5期计划：严格优先级与可选核心 fallback 隔离

## 状态

已完成。源码、容器、实时状态 API、300 秒文件监视和自然请求费用保护均已验证。

## 问题证据

1. 路由器会从当前会话 provider 在链中的位置开始扫描；当会话仍停在 `2.0-lite` 时，链首的 `2.1-turbo`、`2.1-pro` 等候选不会被检查。
2. 图片请求会跳过不支持 `image` 的 provider。当前链中的 `2.0-mini/pro/lite` 和 `deepseek-v4-flash` 都不支持图片，因此从 `2.0-lite` 起扫时会直接命中 `deepseek-v4-pro`。
3. AstrBot 核心 runner 会在 provider 返回 403、超时或错误响应后独立遍历 `fallback_chat_models`；这条路径不会重新进入插件的额度判断。
4. 2026-07-13 22:44 的实时日志同时复现了上述路径：插件先从 `2.0-lite` 跳到 V4 pro，核心随后因 `AccountOverdueError` 从链首一路 fallback 到 V4 flash。

## 目标

- 默认每次请求都从路由链第一项开始，严格按配置顺序选择第一个额度和模态均可用的 provider。
- 保留图片、音频能力过滤，不把不支持请求模态的模型当作可用候选。
- 提供按请求生效的 AstrBot 核心错误 fallback 可选保护；默认保留 AstrBot 的可用性 fallback。
- 将 `cmd_config.json` 文件签名检查间隔由 2 秒改为 300 秒。

## 实现

- `strict_priority_order=true`：忽略当前会话在链中的位置，从链首扫描。
- `disable_astrbot_error_fallback=false`：默认可用性优先；只有确认错误 fallback 不应进入后续模型时才显式开启。
- 通过事件 extra 标记本插件实际接管的请求，并在 `ToolLoopAgentRunner.reset()` 边界清空该请求的 `fallback_providers`；不修改 `cmd_config.json`、AstrBot 内存配置或容器核心文件。
- 核心 guard 支持插件卸载时恢复原方法，并处理插件重载时的多 owner 生命周期。
- Plugin Page 状态 API 暴露严格顺序和核心 guard 的请求值、实际激活状态。

## 验证

1. 当前 provider 为链尾且链首有额度时，决策必须回到链首。
2. 图片请求从链首检查，优先选择前面的图片模型，不得因当前会话停在链尾而直达 V4 pro。
3. 事件启用 guard 时 runner 收到空 fallback；未启用时保持原 fallback。
4. 容器单元测试、`compileall`、插件导入全部通过。
5. 实时配置显示 `fallback_watch_interval_seconds=300`、严格顺序开启，并按部署需要正确显示核心 guard 状态。
6. guard 关闭时，确认火山 provider 403 后 AstrBot 能继续 fallback 到当前可用的 DeepSeek provider。

## 验证结果

- 宿主机单元测试：11 项通过，4 项 AstrBot 路由集成测试因宿主机无 AstrBot 包按设计跳过。
- 容器单元测试：15/15 通过；`compileall` 通过。
- live 插件：AstrBot 加载 `v0.5.0`，状态 API 返回 `watch=300`、`strict_priority=true`、`fallback_source=cmd_config`。
- 文件监视：22:55:42 触碰 `cmd_config.json` 修改时间，22:58:42 在 watcher 启动后的 300 秒检查点完成原子热更新，链未改变且无错误。
- guard 功能曾在 live 自然请求中确认能移除后续候选；用户补充当前火山账号欠费、仅两个 DeepSeek provider 可用后，live guard 改为关闭，以保留这条必要的可用性 fallback。
- 用户随后已为火山账号续费并确认恢复；最终运行策略仍保持 guard 关闭。未额外制造付费模型请求复现 403，状态 API 已确认 `guard_active=false`，重启后的日志无新的欠费错误或异常 fallback。
