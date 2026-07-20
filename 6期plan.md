# 6期计划：火山额度冷却与 DeepSeek 不限额

## 目标

只对火山侧 provider/model 执行 200 万 token 日额度保护；`deepseek/` 自有 API 不参与额度判断。每日额度窗口在北京时间 11:00 切换，但达到阈值的火山模型还需要完成从达线时刻开始的 24 小时冷却，防止资源包延迟到账时立即产生按量费用。

## MVP 范围

- 新增可配置的不限额 provider ID 前缀，默认仅包含 `deepseek/`。
- 新增可配置冷却时长，默认 86400 秒。
- 火山模型达到 `usage + reservation + safety >= limit` 时写入持久化冷却状态。
- 插件启动时扫描当前窗口，每次受控模型响应结束后再次对账，补齐没有下一条请求的跨线场景。
- 新窗口开始后，仍处于 24 小时冷却的模型继续跳过；两个条件均满足后自动恢复。
- `reset-cache` 只清理 pending/overlay，不清除费用保护冷却。
- Plugin Page、状态 API、路由决策日志显示 unlimited/cooldown 状态。

## 技术形状

- 数据库：不新增数据库；历史用量继续读取 AstrBot `ProviderStat`。
- 状态：在 `quota_state.json` 增加 `cooldowns` 字典，按 quota key 持久化。
- 路由：复杂判断继续位于 `core/router.py`，`main.py` 只负责事件接入与状态展示。
- WebUI：继续使用现有 AstrBot Plugin Page，不新增独立服务。

## 验证

- DeepSeek 即使已有用量超过 200 万，仍返回 `unlimited` 并可被选择，且不创建 reservation。
- 火山模型达到阈值后创建 24 小时冷却并切到下一级。
- 11:00 新窗口开始但冷却未结束时，仍跳过对应火山模型。
- 冷却结束且已进入新窗口后，模型自动重新可用。
- 重启后冷却仍存在；`reset-cache` 不会清除冷却。
- 容器内通过 compile、单元测试、插件加载和状态 API 验证。

## 延后项

- 火山资源包余额 API 对账。
- 按账号/API Key 分组独立额度。

## 状态

- v0.6.0 已完成并同步实时 Docker 环境。
- Docker 内 20 项测试通过；AstrBot 已加载 0.6.0，启动延迟对账检查 5 个受控火山模型并创建 1 个 cooldown。
- 状态 API 已确认 `reset=11:00`、`cooldown=86400`、DeepSeek 两个 provider 为 `unlimited`、核心错误 fallback guard 已启用。
