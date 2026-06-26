# 1期 Plan: MVP

状态：v0.1.0 已完成实现，等待运行期持续观察。

## 范围

实现一个最小可用的 provider/model 日额度路由插件。它只处理聊天模型 provider，不处理 TTS、embedding、图片生成插件自己的外部调用。

## 交付物

### 插件骨架

```text
data/plugins/astrbot_plugin_provider_quota_router/
  main.py
  metadata.yaml
  _conf_schema.json
  README.md
  core/
    config.py
    ledger.py
    router.py
    state.py
    time_window.py
```

### 核心能力

- 读取配置中的 provider chains。
- 计算当前 reset window。
- 从 `ProviderStat` 聚合当前窗口 token。
- 维护 pending reservation 和 overlay usage。
- 在 `on_waiting_llm_request(priority=900)` 中选择未超额 provider。
- 在 `on_llm_request` 中做最终 stop guard。
- 在响应后释放 reservation 并记录实际 usage。
- 写 `route_decisions.jsonl`。
- 提供管理员命令：
  - `/quota status`
  - `/quota reload`
  - `/quota reset-cache`

## 默认配置

- `enabled=true`
- `timezone=Asia/Shanghai`
- `reset_time=00:00`
- `default_daily_limit_tokens=2000000`
- `default_safety_buffer_tokens=100000`
- `default_request_reservation_tokens=50000`
- `hook_priority=900`
- `quota_key_mode=provider_model`
- `exhausted_action=stop`
- `dry_run=false`

## 验证方法

1. 静态检查：
   - Windows: `python -m compileall data/plugins/astrbot_plugin_provider_quota_router`
   - Container: `docker exec astrbot python -m compileall /AstrBot/data/plugins/astrbot_plugin_provider_quota_router`
2. 加载检查：
   - 重载插件或重启 `astrbot` 容器。
   - `docker logs astrbot` 确认插件加载成功，无 traceback。
3. 路由 smoke test：
   - 临时把当前默认 provider 的额度设为低于当前已用 token。
   - 发送一条会触发 LLM 的消息。
   - 日志应显示 `quota_exceeded` 和 `selected_provider=<next provider>`。
   - 查询 `provider_stats` 确认新记录使用下一级 provider。
4. exhausted test：
   - 临时把链上所有 provider 阈值设为 1。
   - 触发 LLM。
   - 应停止请求并发送或记录 configured exhausted message。

## 退出标准

- 正常链路不影响现有默认 provider。
- 超额链路能自动切 provider。
- 所有 provider 超额时行为符合配置。
- 当天窗口统计和 `provider_stats` 查询一致。
- 文档包含配置说明、命令说明、数据文件说明和已知风险。

## 完成记录

- 已实现标准插件骨架和 `core/` 分层。
- 已实现 `on_waiting_llm_request(priority=900)` 预路由。
- 已实现 `on_llm_request` 链路耗尽阻断。
- 已实现 `on_agent_done` 释放 pending reservation 并记录短期 overlay。
- 已实现 `/quota status`、`/quota reload`、`/quota reset-cache`、`/quota dry-run on|off`。
- 已补充 README、CHANGELOG 和本地 spec/plan 备份。

## 暂不处理

- Plugin Page。
- 外部火山账单 API。
- 精确费用换算。
- 非聊天 provider。
- 修改现有 token_controller。
