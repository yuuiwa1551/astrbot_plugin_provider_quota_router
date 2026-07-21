# 12期计划：当前请求安全错误 fallback

## 根因

- `disable_astrbot_error_fallback=true` 的旧实现会在 Agent runner 初始化时清空全部 `fallback_providers`。
- 单模型错误冷却只能保护后续请求；当前请求收到错误后没有剩余候选，因此直接结束为 `All chat models failed`。
- OpenAI-compatible Provider 默认会先对 429 等错误内部重试 5 次，冷却只能在最终异常抛出后写入，导致明确的额度错误仍等待约 20～30 秒。

## 本期目标

- 保留“核心不能绕过插件额度与冷却判断”的保护，但不再清空所有 fallback。
- 插件从实时 `cmd_config.json` 链中选出当前模型之后仍可用的候选，按原顺序注入 Agent runner。
- 候选必须通过 Provider 存在性、请求模态、火山组熔断、单模型冷却、opencode 上游额度冷却和火山 token 安全线检查。
- 受插件接管的 Agent 请求以及 OpenAI-compatible 直连调用，每个模型最多尝试 1 次；失败立即冷却并切向下一候选。

## 实现边界

- Agent 主请求使用 AstrBot 原生 runner 的 fallback 循环，只替换其候选列表和重试次数，不修改 AstrBot 核心文件。
- 图片分类、好感度后台任务等直接调用没有 Agent runner 的 fallback 上下文，本期只让它们快速失败并进入冷却，不擅自改变其业务模型。
- 继续按完整 `provider_id` 独立冷却 30 分钟；火山 403 整组熔断和 opencode 到次日 11:00 的规则不变。

## 验证

- 首模型失败时，runner 收到按插件链排序的安全后续 Provider，而不是空列表。
- 已冷却、超额、熔断、缺失或模态不兼容的 Provider 不进入当前请求 fallback。
- `request_max_retries` 被收紧为 1，429 不再在单个模型上连续等待 5 次。
- 容器内完整测试、语法编译、实时部署、重启日志和实际路由记录全部通过。

## 状态

- v0.11.1 已完成并同步实时 Docker 环境。
- 修复前线上证据：2026-07-21 14:29:58～14:30:37，`gpt-5.6-sol` 对明确的 `API_KEY_QUOTA_EXHAUSTED` 重试到 5/5；旧 guard 清空后续候选，最终直接返回 `All chat models failed`，没有任何 `Switched ... to fallback` 日志。
- 容器内 51 项单元/集成测试全部通过，源码语法编译和配置 JSON 校验通过。
- 重启后认证状态 API 确认 `core_fallback_guard_active=true`、`provider_guard_active=true`、`provider_error_request_max_retries=1`、fallback 来源为实时 `cmd_config`。
- 没有为验证主动发起外部模型请求；下一条自然流量的决策日志会记录 `safe_fallback_provider_ids` 与 `request_max_retries`。
