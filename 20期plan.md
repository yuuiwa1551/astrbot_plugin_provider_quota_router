# 20期：会话模型固定亲和

## 目标

让每个群聊和私聊在固定一小时内尽量沿用同一成功模型，减少上游缓存未命中和 Bot 风格突变，同时保证亲和不会绕过额度或健康保护。

## MVP 范围

- 新增默认关闭的 `route_affinity_enabled` 和固定租约 `route_affinity_ttl_seconds=3600`；本机部署显式开启。
- 按 UMO 哈希与 `text`、`image`、`audio`、`image+audio` 等标准化模态隔离持久状态。
- 普通自动路由优先检查有效亲和 Provider，再回到去重后的全局链；显式请求/UMO Provider 完全绕过亲和。
- 亲和候选必须通过本地日额度、reservation、安全余量、上游额度、模型/Source 冷却及模态检查。
- 只有最终成功模型才能建立或替换亲和；同模型成功不滑动续期，旧慢请求不能覆盖较新结果或 unpin 清除操作。
- `.quota unpin` / `。quota unpin` 同时清除显式 Provider 与当前 UMO 的全部模态亲和；`.quota reset-cache` 保留亲和。
- 状态版本升级为 v8；决策日志、平台日志、状态 API 和 Plugin Page 展示亲和状态。

## 验证

- 测试固定到期、不滑动续期、重启持久化、模态隔离、显式选择、链变化、模型变化、额度/冷却绕过、fallback 替换及并发乱序。
- 容器内运行完整测试、`ruff check . --isolated`、`compileall`、JSON 与版本一致性检查。
- 部署后验证状态 API、源码/运行时哈希、连续 WebChat 路由、媒体隔离和 unpin 后重新选路。

## 状态

v0.15.0 已完成并部署：133 项容器测试、ruff、compileall、JSON/版本检查均通过，源码与运行时关键文件哈希一致。

独立 WebChat 会话已验证：文字请求从 `miss` 建立亲和后转为 `hit`，后续请求保持同一 Provider 且固定到期时间不滑动；图片请求建立独立 `image` 亲和且不改变 `text` 亲和；`.quota unpin` 与 `。quota unpin` 均能清除全部模态亲和并可幂等重复执行，清除后的下一条文字请求重新显示 `miss`。测试会话与临时验收权限已清理，本机保持 `route_affinity_enabled=true`、`route_affinity_ttl_seconds=3600`。
