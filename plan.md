# AstrBot Provider Quota Router Plan

## 总体策略

新建独立插件 `astrbot_plugin_provider_quota_router`，负责全局 provider/model 日额度路由。插件不修改 AstrBot 核心，不改 `cmd_config.json` 默认 provider，不合并到现有第三方 `astrbot_plugin_token_controller`。

实施按八期推进：

- 1期：命令行可管理的 MVP，先做到稳定路由和可验证统计。
- 2期：Plugin Page、报表、告警和更细计费口径。
- 3期：在 Plugin Page 增加历史 token 图表，覆盖每日模型消耗、单日占比和单模型趋势。
- 4期：修复 AstrBot fallback 链热更新与不安全的 `use_last` 降级行为。
- 5期：严格按 fallback 全局优先级路由，并可隔离 AstrBot 核心错误 fallback。
- 6期：只限制火山 provider，DeepSeek 不限额；增加跨 11:00 窗口的 24 小时冷却。
- 7期：火山 403 组级熔断 30 分钟，到期后台随机半开探测并自动恢复。
- 8期：Provider 错误不在原会话展示，改为每小时最多一次的管理员私聊告警。

## 技术决策

- 数据库：不新增业务数据库；读取 AstrBot 原生 `ProviderStat`，本地只保存 JSON 状态和日志。
- 调度：不依赖每日 cron 清零；每次计算当前 reset window。
- 路由：使用 `on_waiting_llm_request` 写 `selected_provider`。
- 兜底：使用 `on_llm_request` 做最终 guard，使用 `on_llm_response` 或 `on_agent_done` 更新本地 overlay。
- WebUI：1期不做；2期做独立 Plugin Page，不耦合 AstrBot Dashboard 核心。
- 配置：所有额度、reset time、priority、链路和超限行为都可配置。

## 1期 MVP

目标：在当前 Docker AstrBot 里可加载、可配置、可查状态，能在某个 provider/model 超过阈值后自动切到下一个 provider。

状态：v0.1.0 已实现。

交付：

- 标准插件结构：
  - `main.py`
  - `metadata.yaml`
  - `_conf_schema.json`
  - `README.md`
  - `core/ledger.py`
  - `core/router.py`
  - `core/config.py`
  - `core/state.py`
- `on_waiting_llm_request` provider 路由。
- `on_llm_request` 兜底阻断和日志补充。
- `on_llm_response` / `on_agent_done` overlay 更新。
- `ProviderStat` 当前窗口查询。
- `/quota status`、`/quota reload`、`/quota reset-cache`。
- 本地状态和路由日志写入 `data/plugin_data/astrbot_plugin_provider_quota_router/`。

验证：

- `python -m compileall` Windows 路径和容器路径各跑一次。
- 用小额度阈值做 smoke test：把当前 provider 阈值设成低于已有用量，触发一次请求，日志应显示切到下一级 provider。
- 查询 `provider_stats`，确认后续记录落到新 provider。
- 检查 `docker logs astrbot` 没有插件加载错误。

## 2期增强

目标：降低日常运维成本，提供可视化和更强的费用风险控制。

状态：v0.2.0 已实现，v0.3.0 已补充历史图表。

交付：

- Plugin Page：
  - 当前窗口 provider/model 用量表。
  - 路由链状态。
  - pending reservations。
  - 最近路由决策日志。
- CSV/JSON 每日报表。
- 告警：
  - approaching limit。
  - switched provider。
  - chain exhausted。
  - provider usage unavailable。
- 每个模型独立配置 quota key、额度、buffer、reset time。
- dry-run 面板开关。
- 历史 token 图表：
  - 每天每个模型消耗堆叠柱状图。
  - 单日模型占比饼图。
  - 某个模型每天消耗趋势图。
  - `GET /history` 按 `provider_stats` 聚合 1-90 天历史。

验证：

- 页面能在 AstrBot 插件页打开。
- API 返回结构稳定。
- 低额度 smoke test 覆盖 normal、switch、exhausted 三种状态。
- 多并发请求下 reservation 不丢、不永久占用。

## 4期配置热更新与费用安全

目标：当 AstrBot `cmd_config.json` 的 `default_provider_id + fallback_chat_models` 发生变化时，插件在无需重启的情况下自动换链，并避免把“不支持请求模态”的 provider 当作额度耗尽后的可用兜底。

状态：v0.4.0 已完成并同步实时 Docker 环境。

交付：

- 后台低频轮询 `cmd_config.json` 文件签名，默认 2 秒检查一次。
- 使用 `utf-8-sig` 直接读取 AstrBot 配置，构建新的 fallback 链。
- 仅在没有自定义 `chains_json` 且 `use_astrbot_fallback_chain=true` 时启用自动换链。
- 配置写入中、JSON 无效或链为空时保留上一份有效链，不清空运行路由。
- 在 Plugin Page API 中暴露配置来源、监视状态、最后加载时间和最近错误。
- `use_last` 只允许处理纯额度耗尽；provider 缺失或模态不支持时改为阻断。

验证：

- 纯 Python 单元测试覆盖 BOM、链去重、写入竞争、无效 JSON 和签名变化。
- 容器内执行 `compileall` 和 package import。
- 同步实时插件后重载 AstrBot，确认 `/chains` 立即显示磁盘当前链。
- 触发一次无语义配置文件变更，确认监视器自动记录重新加载且链保持正确。
- 验证图片请求不会再因 `modality_not_supported` 被 `use_last` 强制送往链尾。

## 延后项

- 接入火山引擎官方用量 API。
- 根据 API key 或账号做 quota 分组。
- 与 `astrbot_plugin_lb_provider` 做专门集成。

## 5期严格优先级与可选核心 fallback 隔离

目标：让 fallback 列表真正表示全局优先级，而不是从会话当前 provider 开始；同时提供可选的 AstrBot 错误 fallback 费用保护，默认保留可用性兜底。

状态：v0.5.0 已完成并同步实时 Docker 环境，详见 `5期plan.md`。

交付：

- `strict_priority_order=true` 时每次从链首扫描，依次检查额度和请求模态。
- `disable_astrbot_error_fallback` 可通过事件标记精确保护插件已接管的请求；默认关闭。
- runner guard 在构建 agent 时清空该请求的 fallback providers，插件卸载时恢复原方法。
- 默认配置文件检查间隔调整为 300 秒。
- 状态 API 和 Plugin Page 显示严格优先级及核心 guard 实际状态。

验证：

- 当前 provider 位于链尾时，链首仍有额度则必须切回链首。
- 图片请求优先命中靠前且支持图片的 provider。
- 标记请求不携带 AstrBot 核心 fallback，未标记请求保持原行为。
- guard 关闭时，火山 provider 403 后仍能按配置 fallback 到可用 DeepSeek provider。

## 本机当前参考链

当前 `cmd_config.json` 的 AstrBot fallback 链可先作为默认配置：

```text
openai/doubao-seed-2-1-turbo-260628
openai/doubao-seed-2-1-pro-260628
openai/doubao-seed-2-0-mini-260215
openai/doubao-seed-2-0-pro-260215
openai/doubao-seed-2-0-lite-260215
deepseek/deepseek-v4-flash
deepseek/deepseek-v4-pro
```

注意：如果额度是按“单一模型”而不是 provider ID 计算，应优先以 `provider_model` 作为 quota key。

## 6期火山额度冷却与 DeepSeek 不限额

目标：火山模型继续按 200 万额度和 11:00 日窗口路由；达到阈值后持久冷却 24 小时。`deepseek/` 自有 API 始终作为不限额候选，不因本插件统计用量而停用。

状态：v0.6.0 已完成并同步实时 Docker 环境，详见 `6期plan.md`。

交付：

- `unlimited_provider_prefixes=["deepseek/"]` 将 DeepSeek 自有 API 排除在额度判断外。
- `quota_cooldown_seconds=86400` 保存火山模型达线后的冷却时间。
- 恢复必须同时满足：当前 11:00 日窗口已经更新，且 24 小时冷却已经结束。
- `quota_state.json` 持久化 cooldown；普通 cache reset 不删除费用保护状态。

## 7期火山 403 组级熔断与半开探测

目标：火山任一模型返回 403 时整组熔断 30 分钟，避免欠费或权限异常期间逐个撞击火山模型；冷却到期后后台随机探测一个仍在 token 安全线内的火山模型，成功恢复整组，失败继续使用非火山 fallback。

状态：v0.7.0 已完成，详见 `7期plan.md`。

交付：

- 按 provider source ID 识别火山模型组。
- 持久化 403 组级熔断、最近错误、下次探测与探测租约。
- 熔断期间跳过全部火山候选，继续扫描非火山模型。
- 后台半开探测成功恢复、失败续期 30 分钟。
- 状态 API 和 Plugin Page 展示熔断与探测状态。

## 8期 Provider 错误静默与管理员限频私聊

目标：本插件接管的模型最终报错时，不在原群聊或私聊窗口展示技术错误；改为私聊 Bot 管理员，全部 Provider 错误共用一小时限频，同时确保最终 `role=err` 路径仍会触发火山 403 组级熔断。

状态：v0.8.0 已完成并同步实时 Docker 环境，详见 `8期plan.md`。

## 9期 opencode 免费额度错误冷却

目标：`opencode-zen/` 免费模型不再套用火山 token 阈值；实际返回 `FreeUsageLimitError` 后，只冷却报错模型到下一个北京时间 11:00，并覆盖图片描述等直接 Provider 调用。

状态：v0.9.1 已修正旧 token cooldown 迁移，opencode 只以上游 1 美元额度错误为准，详见 `9期plan.md`。

## 10期 火山专属 token 上限与请求前 fallback 热加载

目标：本地 token 阈值只约束火山 Provider Source；所有其他 Provider 不受 110 万限制。`cmd_config.json` 的 fallback 变化在下一条 LLM 请求前即时生效，同时保留 300 秒后台兜底。

状态：v0.10.0 已完成并同步实时 Docker 环境，详见 `10期plan.md`。

## 11期 全 Provider 单模型错误冷却

目标：任意模型调用最终失败后，仅将该完整 Provider/模型配置冷却 30 分钟；后续请求跳过它并继续 fallback，不连坐同源的其他模型。火山 403 整组熔断和 opencode 免费额度到次日 11:00 的专用规则保持不变。

状态：v0.11.0 已完成并同步实时 Docker 环境；容器内 49 项测试通过，且已由线上真实 403 验证单模型独立冷却，详见 `11期plan.md`。

## 12期 当前请求安全错误 fallback

目标：修复 `disable_astrbot_error_fallback=true` 清空全部后续候选的问题。当前模型最终失败后，立即按实时 fallback 顺序切换到下一个通过额度、冷却、熔断和模态检查的模型；单模型请求重试收紧为 1 次。

状态：v0.11.1 已完成并同步实时 Docker 环境；容器内 51 项测试通过，认证状态 API 已验证安全 fallback guard 与单次模型尝试生效，详见 `12期plan.md`。

## 13期 真实链路回归与首响应止损

目标：避免多个故障 Provider 串行等待造成 Bot 长时间无响应，并修复引用消息内嵌图片未参与模态过滤的问题。

状态：v0.11.2 已完成并同步实时 Docker 环境；容器内 55 项测试通过，全部 7 个实时 Provider 已逐个真实请求，WebChat 端到端链路在 10.25 秒内由 Mimo 返回，详见 `13期plan.md`。

## 14期 三策略解耦与可靠故障降级

目标：按真实计费语义拆分三套互不覆盖的策略：仅火山开发者计划使用本地日额度和达线后滚动 24 小时冷却；opencode free 只依据上游额度错误进入未知刷新状态并由后台探测恢复；所有 Provider 的真实上游故障使用独立模型健康冷却并快速 fallback。

状态：v0.12.0 已完成并热重载到实时 AstrBot；容器内 79 项测试通过，v6 状态已迁移到 v7。火山 Source 恢复探测、付费 Token Plan WebChat、opencode free 直测和中转站故障短冷却均已实测，详见 `14期plan.md`。

计划交付：

- 新增不可变 ProviderPolicy、ErrorDisposition 和请求级 RoutePlan。
- 拆分本地额度、上游未知刷新额度、模型健康、Source 健康四类持久状态。
- 修复 `allow_paid/use_last` 非额度放行、实际 Provider 归因、probe 自阻断和热重载竞态。
- opencode 不再假设北京时间 11:00 刷新，改为冷却期间用户请求零外呼、后台低频探测成功后恢复。
- 请求级错误不污染全局模型状态；首响应墙钟预算继续限制坏模型造成的累计延迟。
- 加入状态损坏备份、默认管理员授权、决策日志轮转和 AstrBot 真实契约测试。
