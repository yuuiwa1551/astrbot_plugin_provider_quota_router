# 10期计划：火山专属 token 上限与请求前 fallback 热加载

## 目标

- 只有 `volcengine_provider_source_ids` 指定的火山 Provider Source 使用本地 token 上限、预占与 24 小时 cooldown。
- 中转站、DeepSeek、opencode 及未来新增的其他非火山 Provider 不因 AstrBot token 统计量被停用。
- 修改 `cmd_config.json` 的 fallback 后，在下一条 LLM 请求路由前生效，不必等待后台轮询或重启。

## 实现

- 路由器通过已注册 Provider 的 `provider_source_id` 判断是否属于火山，不再使用“排除少数前缀后其余全部受控”的逻辑。
- 每次 LLM 请求前只执行文件 `stat`；签名变化后才读取 `cmd_config.json` 并替换 router。
- 保留默认 300 秒后台检查，保证长时间没有 LLM 请求时状态面板也能最终更新。
- 使用异步锁串行化请求路径与后台任务的配置刷新；fallback 内容未变化时不重建 router。

## 验证

- 火山 source 的模型仍显示 token limit、安全余量和预占。
- `中转站1/gpt-5.4` 等非火山模型显示 `quota_managed=false`、`limit=0`，即使用量超过阈值也可选。
- 修改 fallback 后，下一条 LLM 请求触发热加载；无请求时后台仍按 300 秒兜底。
- 容器内运行完整单元测试、语法检查、插件重启与认证状态 API 检查。

## 状态

- v0.10.0 已完成并同步实时 Docker 环境。
- 源码与实时容器各通过 43 个单元测试，包含请求前 fallback 变化与未变化两条专门用例。
- 认证状态 API 已确认：火山 `openai` source 保持 `quota_managed=true`；`中转站1/gpt-5.4`、DeepSeek 与 opencode 均为 `quota_managed=false`。
- 当前 fallback 共 16 项，新模型 `中转站1/gpt-5.4` 已加载在链尾；请求前即时检查已启用，后台兜底间隔为 300 秒。
