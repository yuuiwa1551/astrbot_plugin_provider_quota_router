# AstrBot Provider Quota Router Plan

## 总体策略

新建独立插件 `astrbot_plugin_provider_quota_router`，负责全局 provider/model 日额度路由。插件不修改 AstrBot 核心，不改 `cmd_config.json` 默认 provider，不合并到现有第三方 `astrbot_plugin_token_controller`。

实施按三期推进：

- 1期：命令行可管理的 MVP，先做到稳定路由和可验证统计。
- 2期：Plugin Page、报表、告警和更细计费口径。
- 3期：在 Plugin Page 增加历史 token 图表，覆盖每日模型消耗、单日占比和单模型趋势。

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

## 延后项

- 接入火山引擎官方用量 API。
- 根据 API key 或账号做 quota 分组。
- 与 `astrbot_plugin_lb_provider` 做专门集成。
- provider 运行中 fallback 的即时精确归因。

## 本机当前参考链

当前 `cmd_config.json` 的 AstrBot fallback 链可先作为默认配置：

```text
openai/doubao-seed-2-0-lite-260215
openai/doubao-seed-2-0-mini-260215
openai/doubao-seed-2-0-pro-260215
doubao-seed-1-8-251228
openai/doubao-seed-1-6-flash-250828
openai/doubao-seed-1-6-lite-251015
openai/doubao-seed-1-6-250615
openai/doubao-seed-1-6-251015
```

注意：如果额度是按“单一模型”而不是 provider ID 计算，应优先以 `provider_model` 作为 quota key。
