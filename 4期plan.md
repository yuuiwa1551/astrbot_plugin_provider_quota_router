# 4期计划：fallback 热更新与费用安全

## 状态

已完成。源码测试、容器测试、实时 API、文件变化热更新和安全路由行为均已验证。

## 范围

本期解决两类已在实时环境复现的问题：

1. 插件启动时复制 AstrBot fallback 链，`cmd_config.json` 更新后仍继续使用旧链。
2. `exhausted_action=use_last` 会在候选 provider 全部不支持图片或音频时强制选择链尾，产生意外付费风险。

## 交付物

- `core/fallback_config.py`
  - 定位 AstrBot `cmd_config.json`。
  - 使用 `utf-8-sig` 安全读取配置。
  - 校验文件读取前后签名，避免消费写到一半的 JSON。
  - 从 `default_provider_id + fallback_chat_models` 构建去重链。
- `main.py`
  - 插件初始化时优先直接读取 `cmd_config.json`。
  - 后台任务按配置间隔检查文件变化并原子替换 router。
  - 配置读取失败时保留最后一份有效链并记录诊断状态。
  - 插件终止时取消监视任务。
- `core/router.py`
  - `use_last` 仅在所有候选都是 `quota_exceeded` 时生效。
  - provider 缺失或模态不支持时返回 block。
- Plugin Page API
  - 返回配置路径、链来源、监视开关、最后加载时间和最近错误。
- 文档、配置 schema、changelog 和 `v0.4.0` 版本元数据。

## 技术决策

- 不新增第三方文件监视依赖；20 KB 左右的配置使用异步后台轮询即可。
- 默认每 2 秒只执行一次 `stat`，文件签名变化时才读取和解析 JSON。
- 自定义 `chains_json` 始终优先，存在自定义链时监视器不覆盖它。
- 新配置必须完整、合法且能生成非空链，才替换当前 router。
- 本期不修改 AstrBot 核心 runner；核心自身的请求失败 fallback 额度校验列入后续工作。

## 验证方法

1. `python -m unittest discover -s tests -v`
2. `python -m compileall .`
3. 容器 package import：将 `/AstrBot/data/plugins` 加入 `sys.path` 后导入插件包。
4. 实时 API：验证 `/chains` 返回当前 `cmd_config.json` 链和 watcher 状态。
5. 修改文件时间或进行等价 JSON 保存，等待超过监视频率后确认自动 reload 日志。
6. 构造全候选 `modality_not_supported`，确认结果为 block 而不是 `use_last`。

## 延后项

- AstrBot 核心 fallback 每一个候选进入前执行额度 guard。
- 运行中 fallback 的实际 provider 即时归因。
- 文件系统原生事件监视；当前轮询已经满足低频配置文件场景。
