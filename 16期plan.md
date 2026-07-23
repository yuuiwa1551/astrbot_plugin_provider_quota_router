# 16期：对话路由模型平台日志

## 目标

当 quota router 确实改变本次对话的 Provider 时，在 AstrBot 平台日志中明确显示“从哪个 Provider/模型路由到了哪个 Provider/模型”，并保留会话、原模型跳过原因和目标状态用于追查。

## 范围

- 实际 `switch` 或 `use_last` 且目标 Provider 不同：输出 INFO 级别路由日志。
- 普通 `allow/skip/block`：不输出“已路由”日志。
- 同 Provider `use_last`：不声称发生了路由。
- `dry_run=true`：仅输出 dry-run candidate，不能显示成真实路由。

## 日志格式

```text
[ProviderQuotaRouter] 本次对话已由插件路由:
conversation=<umo>
from_provider=<provider id>
from_model=<model>
to_provider=<provider id>
to_model=<model>
action=<switch|use_last>
trigger=<source skip reason>
target_status=<selected candidate status>
```

实际输出为单行，便于平台日志检索。

## 验证

- 单元测试断言实际切换日志包含会话、两个 Provider ID、两个模型名、动作、原模型跳过原因和目标状态。
- 单元测试断言普通放行不会输出“已路由”日志。
- 容器内执行完整 `unittest` 和 `ruff check`。
- 部署后构造一次受控模型冷却，发送真实 WebChat 请求触发插件路由，并从实时日志确认目标模型字段。

## 非目标

- 不记录用户消息正文。
- 不改变路由顺序、额度、冷却或 fallback 行为。
- 不为普通未切换请求增加日志噪声。

## 完成状态

- v0.12.2 已由 AstrBot 4.26.4 实时加载。
- 容器内完整 92 项 `unittest` 全部通过，`ruff check` 和 `git diff --check` 通过。
- 真实 WebChat 验证临时冷却默认 `volcengine-agent-plan/glm-5.2` 后，插件路由到 `opencode-zen/deepseek-v4-flash-free`，4.79 秒完成并返回 `ROUTE_LOG_V12_2_OK`。
- 实时平台日志准确输出 `from_model=glm-5.2`、`to_model=deepseek-v4-flash-free`、`action=switch`、`trigger=provider_error_cooldown` 和 `target_status=upstream_quota`。
- 受控冷却与临时 WebChat 会话均已清理；验证后模型和 Source 熔断计数均为 0。
- 上线前备份位于 `D:\astrbot\data\backups\quota_router_v0.12.2_20260723_181736`。
