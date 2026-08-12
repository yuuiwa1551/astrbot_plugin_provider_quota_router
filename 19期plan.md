# 19期：会话 Provider 指定解除命令

## 目标

允许 AstrBot 管理员直接在当前群聊或私聊中取消 UMO 范围的对话 Provider 指定，无需进入 WebUI；解除后继续由全局配置和 quota router 决定下一条消息的模型。

## MVP 范围

- 新增管理员子命令 `<唤醒前缀>quota unpin`；当前部署使用 `.quota unpin` 或 `。quota unpin`。
- 严格服从 AstrBot `wake_prefix`，不注册任何绕过前缀的 `/quota unpin` 入口，避免抢占 Haruki Bot 的 `/` 指令命名空间。
- 删除当前会话的 `provider_perf_chat_completion` 偏好；v0.15.0 起同时清除当前 UMO 的自动路由亲和，不改全局默认 Provider、请求级 `selected_provider`、额度、冷却、熔断、会话历史或其他会话规则。
- 命令可重复执行：没有固定 Provider 时返回明确提示，不产生其他副作用。
- 存储读写失败时不返回虚假成功，并在平台日志记录异常。

## 验证

- 单元测试覆盖成功解除、无指定幂等、非管理员拒绝和存储失败。
- 命名空间测试覆盖 `quota` 命令仍已注册，且插件没有任何 `/quota unpin` 正则处理器；用法提示只展示点号前缀。
- 执行完整单元测试、Ruff、`compileall`、版本一致性和 `git diff --check`。
- 部署后在容器内验证命令处理器确实删除当前 UMO 的 Provider 偏好，且下一次 Provider 选择恢复为普通默认来源。

## 延后项

- 不修改 AstrBot 内置 `/provider` 命令。
- 不尝试清除其他插件为单次请求注入的 `selected_provider`。

## 状态

已并入 v0.15.0：撤销 v0.14.1 中错误加入的斜杠兼容入口，并让 unpin 同时清除自动亲和。

- 容器测试已验证 `.quota unpin` / `。quota unpin` 的标准处理路径与幂等语义，同时 `/quota unpin` 不再由本插件注册。
