# 2期 Plan: Visualization And Operations

## 范围

在 1期可用路由基础上，补齐日常运维能力：可视化、报表、告警和更细配置。

## 交付物

### Plugin Page

- 当前窗口用量：
  - quota key
  - provider id
  - model
  - calls
  - used tokens
  - limit
  - remaining
  - status
- 路由链视图：
  - chain name
  - current active provider
  - exhausted providers
  - next candidate
- 运行状态：
  - pending reservations
  - last decisions
  - dry-run state
  - last DB sync time

### API

- `GET /api/status`
- `GET /api/chains`
- `POST /api/reload`
- `POST /api/dry-run`
- `GET /api/decisions`
- `GET /api/export?date=YYYY-MM-DD`

### 报表

- 每日 JSON snapshot。
- CSV 导出：
  - date
  - quota_key
  - provider_id
  - provider_model
  - calls
  - input_other
  - input_cached
  - output
  - total
  - switched_count

### 告警

- 接近阈值：默认 80%、90%、95%。
- 已切换 provider。
- 链路耗尽。
- provider usage 长期为 0。
- DB 查询失败。

## 验证方法

- 页面在 AstrBot Plugin Page 能打开。
- 所有 API 返回 JSON 且无 traceback。
- 使用 fake 小额度覆盖 normal、warning、switch、exhausted。
- 多并发触发时 pending reservation 能自动释放。
- 导出的 CSV 可以用 Excel 打开。

## 退出标准

- 管理员不看日志也能判断每个模型是否接近免费额度。
- 配置错误能在页面或命令输出中明确提示。
- 报表可作为每日人工核对备份。

## 暂不处理

- 官方账单 API 对账。
- 多账号 API key 额度池。
- 对所有第三方插件外部请求做强制代理。
