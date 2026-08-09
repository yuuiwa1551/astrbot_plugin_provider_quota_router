# 19期：会话 Provider 指定解除命令

## 目标

允许 AstrBot 管理员直接在当前群聊或私聊中取消 UMO 范围的对话 Provider 指定，无需进入 WebUI；解除后继续由全局配置和 quota router 决定下一条消息的模型。

## MVP 范围

- 新增管理员命令 `/quota unpin`。
- 对字面量 `/quota unpin` 增加不依赖 `wake_prefix` 的严格正则入口，同时保留标准的 `<唤醒前缀>quota unpin` 用法。
- 只删除当前会话的 `provider_perf_chat_completion` 偏好，不改全局默认 Provider、请求级 `selected_provider`、额度、冷却、熔断、会话历史或其他会话规则。
- 命令可重复执行：没有固定 Provider 时返回明确提示，不产生其他副作用。
- 存储读写失败时不返回虚假成功，并在平台日志记录异常。

## 验证

- 单元测试覆盖成功解除、无指定幂等、非管理员拒绝和存储失败。
- 执行完整单元测试、Ruff、`compileall`、版本一致性和 `git diff --check`。
- 部署后在容器内验证命令处理器确实删除当前 UMO 的 Provider 偏好，且下一次 Provider 选择恢复为普通默认来源。

## 延后项

- 不修改 AstrBot 内置 `/provider` 命令。
- 不尝试清除其他插件为单次请求注入的 `selected_provider`。

## 状态

v0.14.1 已完成并部署到实时 AstrBot。

- 隔离容器内 115 项测试通过，Ruff、`compileall`、配置 JSON、版本一致性和 `git diff --check` 均通过。
- 实时插件重载成功，平台日志确认加载版本 0.14.1。
- 在临时 WebChat 会话中预置固定 Provider 后，字面量 `/quota unpin` 返回解除提示；随后查询确认 `provider_perf_chat_completion` 已删除。
- 临时 WebChat 会话和测试规则均已清理，未修改真实群聊偏好、额度、冷却或熔断状态。
