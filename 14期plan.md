# 14期计划：三策略解耦与可靠故障降级

## 状态

- 实施版本：v0.12.0。
- 已完成源码重构、容器测试、实时部署和端到端验收。
- 运行态插件已在 AstrBot 4.26.4 热重载，状态文件已从 v6 迁移到 v7；部署前插件、配置和状态备份位于 `D:\astrbot\backups\provider_quota_router\20260723-015022`。

## 发布验证结果

- 容器内 `ruff check .` 通过，完整测试 `79 passed`；仅保留 AstrBot 自身 `audioop` 和旧 `register` 装饰器的弃用警告。
- `_conf_schema.json` 与 `RouterSettings.from_raw({})` 默认值一致，真实配置保持 `provider_source_id=openai` 为唯一本地日额度来源、`opencode-zen/` 为未知刷新上游额度来源。
- 插件级热重载成功，日志确认运行 `0.12.0`；安全 fallback guard、Provider 调用 guard 和配置热监听均处于 active。
- v6 实时状态首次加载后迁移为 v7，旧 `cooldowns` 键被拆除；状态 API 返回 `ok=true`、`state_load_error=null`，没有静默覆盖或损坏状态。
- 旧火山 Source 熔断到期后，后台从 fallback 链外找到额度安全的开发者计划模型 `doubao-seed-1-8-251228`，半开探测成功并关闭熔断，验证了 probe bypass 和全 Source 候选扫描。
- 默认真实 WebChat 使用 `volcengine-agent-plan/glm-5.2`，10.27 秒完整返回 `ROUTER_V12_OK`；决策日志的原始与选中 Provider 一致，理由为 `unlimited`，证明付费 Token Plan 没有套用火山本地 token 阈值。测试会话已删除。
- WebChat 的 `selected_provider` 仍服从 `strict_priority_order`，定向选择 opencode 或中转站时会回到链首 Token Plan；因此分类验收改用 AstrBot Provider 直测接口，不把这两次 WebChat 误记为目标 Provider 成功。
- `opencode-zen/deepseek-v4-flash-free` 直测 2.48 秒返回 available，未写本地额度、上游额度或健康冷却。
- `sekirocloud/gpt-5.6-luna` 直测遇到 Cloudflare challenge；插件只为该完整 provider ID 写入 300 秒未知边界模型冷却，没有打开 Source 熔断或连坐其他 Provider。
- 验收结束时状态为 v7：本地额度冷却 0、上游未知刷新冷却 0、模型健康冷却 1、Source 熔断 0、pending reservation 0；唯一模型冷却是上述中转站短冷却，会自动到期。

## 业务基线

本期以三条规则为唯一判断依据，不再按 Provider 名称堆叠互相交叉的特殊分支：

1. 本地额度保护只作用于火山开发者计划。当前通过 `provider_source_id=openai` 识别；其每日用量窗口在北京时间 11:00 切换。
2. 火山开发者计划达到本地安全线后，从达线时刻起滚动冷却 24 小时；即使期间跨过 11:00，也必须等满 24 小时。`opencode-zen/*-free` 不做本地 token 阈值，只在上游明确返回 `FreeUsageLimitError` 后进入“刷新时间未知”的额度冷却。
3. 所有 Provider 都有通用故障冷却：真正的上游故障或首响应超时会让实际失败模型进入短期冷却，并立即尝试下一安全候选，避免一条消息依次等待多个故障模型。

明确不属于本地额度保护的 Provider：

- `volcengine-agent-plan/*` 已付费 Token Plan；
- DeepSeek、自建服务和中转站；
- 除显式配置外的其他 Provider Source。

这里的 `unlimited` 只表示“不受本插件本地 token 阈值限制”，不表示上游真实无限额度或永不故障。

## 本期目标

- 把本地日额度、上游未知刷新额度、通用模型故障拆成三套互不覆盖的策略和状态。
- 修复 `allow_paid`、`use_last` 对非额度失败的错误放行及错误 quota key 归属。
- 只对可归因于 Provider 的错误开启健康冷却，避免请求参数、上下文、模态或插件自身错误污染全局模型状态。
- 让超时、失败、fallback 和最终 usage 都能归因到实际尝试的 Provider。
- 为 opencode 免费额度提供不依赖火山 11:00 的后台恢复探测。
- 修复探测请求被自身 cooldown guard 拦截的问题。
- 提升状态文件损坏、并发热重载、管理权限和日志增长方面的安全性。

## 非目标

- 不给付费 Token Plan、中转站、自建服务新增本地 token 上限。
- 不改变火山开发者计划“11:00 统计窗口 + 达线后滚动冷却 24 小时”的业务规则。
- 不推算 opencode 的美元/token 换算关系，也不假设其真实刷新时区。
- 不在本期改 AstrBot Dashboard 或新建独立 WebUI；只扩展现有 Plugin Page 的状态展示。
- 不默认修改 AstrBot 核心源码；若插件层无法可靠约束 Provider 内部重试，则单独列为上游兼容项。

## 技术方案

### 1. 统一 Provider 策略模型

新增 `core/policies.py`，在请求开始时为每个 Provider 生成不可变策略：

```python
ProviderPolicy(
    local_quota_mode="daily" | "none",
    quota_exhaustion_mode="rolling_24h" | "unknown_reset_probe" | "none",
    health_cooldown_seconds=1800,
    first_response_timeout_seconds=20,
)
```

映射规则：

| Provider | local_quota_mode | quota_exhaustion_mode | 通用健康冷却 |
|---|---|---|---|
| 火山开发者计划 `provider_source_id=openai` | `daily` | `rolling_24h` | 是 |
| `opencode-zen/*-free` | `none` | `unknown_reset_probe` | 是 |
| Token Plan / 中转站 / DeepSeek / 其他 | `none` | `none` | 是 |

兼容现有 `volcengine_provider_source_ids`，但新增更准确的内部名称 `daily_quota_provider_source_ids`；配置迁移时旧键仍可读取，状态 API 同时显示实际命中的策略，避免再次把 Source 名称误解为计费类型。

### 2. 拆分三类运行时状态

将 `quota_state.json` 升级到新版本，拆成：

```text
local_quota_cooldowns
  火山开发者计划达到本地安全线后的滚动 24 小时冷却

upstream_quota_cooldowns
  opencode FreeUsageLimitError，包含 next_probe_at 和 probe lease

model_health_circuits
  单个完整 provider_id 的通用故障冷却

source_health_circuits
  只有明确账号级错误或多个模型共同故障时才使用
```

迁移要求：

- 保留现有火山 token cooldown 的 `started_at/expires_at`，不得因升级提前恢复。
- 现有 `upstream_quota_exhausted` 状态迁移为未知刷新状态，不再写死下一个 11:00。
- 现有单模型和组级 circuit 保留剩余有效期；无法识别的旧记录备份后忽略并告警。
- 状态文件解析失败时保留损坏文件副本，不得用空状态静默覆盖。

### 3. 明确错误分类

新增 `core/error_classifier.py`，统一异常和 `role=err` 响应分类，输出：

```python
ErrorDisposition(
    kind="quota" | "provider_transient" | "provider_account" | "request" | "internal",
    scope="model" | "source" | "none",
    should_fallback=True | False,
    cooldown_seconds=int | None,
)
```

首批规则：

- `FreeUsageLimitError`：opencode 单模型上游额度冷却，等待后台探测恢复。
- 火山开发者计划本地阈值：本地额度冷却 24 小时，不作为 Provider 故障告警。
- 首响应超时、连接失败、408、普通 429、5xx：实际失败模型进入健康冷却。
- 明确账号级 `AccountOverdueError`、鉴权或共享套餐不可用：只对配置允许的 Source 开启 source circuit。
- 上下文过长、函数工具不支持、模态不支持、附件非法、内容审核、400/422 请求错误：允许 AstrBot 做请求修复或选择兼容模型，但不写模型健康冷却。
- 插件内部异常、状态读写异常、主动取消：不写 Provider 冷却。
- 未识别的 Provider 边界异常默认短冷却 300 秒并告警，避免一次未知错误把模型封禁 30 分钟；规则成熟后再提升到明确分类。

### 4. 请求级不可变 RoutePlan

`on_waiting_llm_request` 完成一次原子快照并写入事件：

```text
配置版本/签名
原始 Provider
选中 Provider
安全 fallback 顺序
每个候选的策略和排除原因
请求模态
请求级超时预算
```

同一请求后续不再重新读取 `self.router` 计算候选，避免 fallback 热重载发生在两个 `await` 之间时混用新旧链。下一条请求自然使用新配置。

### 5. 修正 exhausted_action

- `allow_paid` 只允许绕过火山开发者计划的本地 token 阈值；provider 缺失、模态不支持、健康冷却、source circuit 和上游硬额度错误均不得放行。
- `allow_paid` 的 reservation/quota key 必须来自实际选中的原 Provider，不能借用链首候选状态。
- `use_last` 只选择“除可绕过的本地额度外均可调用”的链尾候选。
- `upstream_quota_cooldown`、`provider_group_cooldown/probe` 不再属于可绕过的 quota-only reason。
- 如果没有安全候选，统一返回 `block`，不让 AstrBot core 重新注入未审核 fallback。

### 6. 实际 Provider 归因与延迟上限

- Provider guard 在每次真实尝试前后记录 `provider_id`、开始时间、首响应时间、错误分类和是否产生过流式输出。
- A 失败、B 失败、C 成功时，A/B 分别写健康状态，最终 usage 和 overlay 归到 C。
- reservation 随实际 fallback 转移：释放前一候选预占，再为下一候选建立预占；非本地额度 Provider 不创建 reservation。
- 20 秒是每个候选的首响应墙钟上限；总请求延迟另设候选数预算，默认仍按安全 fallback 顺序逐个尝试。
- 重新核对 AstrBot `ProviderOpenAIOfficial` 内部恢复循环：`request_max_retries=1` 只限制 `_query`，不能完全限制外层上下文修复、Key 轮换等循环。本期以“每候选墙钟上限”为硬保证；若必须精确限制外层网络尝试次数，先增加兼容测试，再决定提交 AstrBot 上游改动，禁止复制整段核心 Provider 实现到插件。
- 已经产生流式 chunk 后发生错误，不自动切换并拼接另一模型输出；记录失败并结束当前流，避免重复或语义断裂。

### 7. opencode 未知刷新额度探测

建议默认值：

```text
首次探测延迟：3600 秒
后续探测间隔：3600 秒
单次探测超时：20 秒
每个冷却模型同时最多一个 probe lease
```

流程：

1. 收到 `FreeUsageLimitError`，写 `upstream_quota_cooldown`，不设置火山 11:00 到期时间。
2. 用户请求直接跳过该模型，不拿用户消息做恢复探针。
3. 后台到 `next_probe_at` 后发送最小文本请求。
4. probe 通过 task-local bypass token 只绕过当前模型的 upstream quota guard，不能绕过健康或其他 Provider 的状态。
5. 成功即清除额度冷却；仍是额度错误则顺延一小时；其他 Provider 故障同时写健康冷却，但不丢失额度状态。
6. 插件终止、重载或多个任务竞争时依靠持久化 lease 保证单探针。

火山账号级 source probe 同样使用显式 probe bypass，修复当前 `probing` 状态被自身 guard 拦截的问题。探测候选不再局限于当前 fallback 链，而是从对应 Source 的已启用文本 Provider 中选择，并继续遵守本地 token 安全线。

### 8. 状态、权限和日志安全

- 状态读取不再每次都同步重写整个 JSON；只在状态发生变更时原子保存。
- 将磁盘读写移出主事件循环，或使用内存状态加受控异步持久化。
- 解析失败时保存 `quota_state.corrupt.<timestamp>.json`，状态 API 标记 degraded；本地额度判断仍以 AstrBot DB 为权威，并采用保守 reservation。
- `admin_user_ids` 为空时管理命令回退到 AstrBot 核心 `admins_id`，不再表示所有用户都是管理员。
- Provider 告警按 provider/source + error kind 限频；发送全部失败时不消耗完整一小时限频。
- `route_decisions.jsonl` 增加大小或日期轮转，读取最近记录不得加载整个文件。

## 预计代码改动

- 新增 `core/policies.py`：Provider 策略解析与兼容映射。
- 新增 `core/error_classifier.py`：错误分类和 cooldown disposition。
- 新增 `core/probes.py`：opencode 与 source circuit 后台探测调度。
- 重构 `core/state.py`：状态 v7、迁移、损坏备份、按变更持久化。
- 重构 `core/router.py`：不可变 RoutePlan、严格 exhausted action、显式状态原因。
- 重构 `core/opencode_quota_guard.py`，并考虑更名为 `core/provider_guard.py`：记录实际尝试、task-local probe bypass、流式保护。
- 调整 `core/core_fallback_guard.py`：只消费请求级 RoutePlan，不重新推导候选。
- 精简 `main.py`：只负责事件入口、服务协调、命令和 API，不继续增加策略分支。
- 更新 `_conf_schema.json`、README、CHANGELOG、Plugin Page 和版本元数据。

## 配置兼容

- 保留现有配置键，升级后不要求用户立即手工重填。
- `volcengine_provider_source_ids` 兼容映射到 `daily_quota_provider_source_ids`，默认仍为 `openai`。
- `quota_cooldown_seconds=86400` 继续表示火山开发者计划达线后的滚动冷却。
- `upstream_quota_provider_prefixes=["opencode-zen/"]` 继续识别 opencode free 模型，但不再共享火山 `reset_time`。
- 新增 opencode probe 的三个时间配置，默认采用 1 小时首次延迟和 1 小时间隔。
- `provider_error_attempt_timeout_seconds=20` 保留；文档改为“每候选首响应墙钟上限”，不再宣称能完全控制 AstrBot Provider 内部所有恢复循环。

## 测试计划

### 策略表测试

- `openai/*` 火山开发者计划命中本地 daily quota 和 24 小时滚动 cooldown。
- 跨过次日 11:00 但未满 24 小时时仍保持冷却。
- `volcengine-agent-plan/*`、中转站、DeepSeek 均不参与本地 token 阈值。
- opencode 只因 `FreeUsageLimitError` 进入未知刷新额度状态。

### 错误分类测试

- timeout、连接错误、普通 429、5xx 冷却实际失败模型。
- context length、tool unsupported、modality、attachment、moderation、400/422 不污染模型健康状态。
- 明确账号级错误才允许 source circuit；单模型 403 不默认连坐整个 Source。
- 未识别异常只进入短冷却并保留原始错误类别。

### 路由与记账测试

- `allow_paid` 不绕过缺失、模态、健康和 source circuit。
- `use_last` 不选择 upstream quota 或 probing/cooling 候选。
- A/B 失败、C 成功时，冷却、reservation、overlay 和最终 Provider 归因正确。
- fallback 配置在请求中途变化时，当前请求仍使用原 RoutePlan，下一请求使用新链。
- 流式首 chunk 后失败不切换到另一模型拼接输出。

### 探测与状态测试

- opencode cooldown 不依赖 11:00，到期前用户请求零外呼。
- 后台探测通过显式 bypass 真正调用目标 Provider，成功恢复、失败续期。
- probe lease 防止并发重复探测，插件重载后仍保持一致。
- 损坏状态文件被备份且不会被空状态覆盖。
- 管理命令默认只允许 AstrBot 管理员。
- 决策日志轮转和尾部读取不随历史文件线性占用内存。

### AstrBot 契约测试

- 使用当前 AstrBot 4.26.4 的真实 `ToolLoopAgentRunner.reset` 和 `ProviderOpenAIOfficial` 签名运行集成测试。
- 验证 core fallback 只收到 RoutePlan 中的安全候选。
- 记录 `request_max_retries` 在 `_query` 与 Provider 外层恢复循环中的实际效果。
- 若 AstrBot 升级导致 monkey patch 目标或签名变化，插件启动时显式降级并告警，不得静默失去保护。

## 实施顺序

### MVP

1. 策略模型、错误分类和状态 v7。
2. RoutePlan 与严格 `allow_paid/use_last`。
3. 实际失败 Provider 归因、reservation 转移和流式保护。
4. opencode 未知刷新额度状态及后台 probe。
5. probe bypass、状态损坏保护和管理员权限修复。
6. 完整单元/集成测试、文档和 v0.12.0 元数据。

### 增强

1. 按多个模型共同失败自动判定 source outage。
2. 可配置的 opencode 探测退避，例如 1h → 2h → 4h。
3. Plugin Page 展示每个候选的 policy、错误类别、下次 probe 和最近尝试轨迹。
4. 告警按 provider/source 和错误类别聚合。

### 后续

1. 向 AstrBot 上游增加正式的 per-provider attempt hook/outer recovery budget，逐步移除类级 monkey patch。
2. 如上游提供请求尝试轨迹，改用官方接口完成实际 Provider 归因。
3. 根据长期运行数据调整默认超时、健康冷却和 probe 间隔。

## 发布与回滚

实施获批后按以下顺序发布：

1. 只在源码仓库开发；备份当前 `quota_state.json` 和插件配置，不把运行时数据复制进 Git。
2. 容器内运行完整测试、语法/import 检查、配置 JSON 校验和状态 v6 → v7 迁移测试。
3. 对当前 fallback 每个 Provider 做最小真实请求，记录耗时与错误类别但不输出密钥。
4. 同时验证火山开发者计划、Token Plan、opencode free、中转站四类策略状态。
5. 部署到 `D:\astrbot\data\plugins\astrbot_plugin_provider_quota_router`，重载插件并核对认证状态 API。
6. 运行真实 WebChat：至少覆盖首模型成功、首模型超时后 fallback、opencode quota cooldown 跳过三条路径。
7. 观察状态迁移、probe 和告警日志；确认无异常后提交并推送源码仓库。

回滚时恢复 v0.11.2 插件代码和部署前状态/配置备份；v0.12.0 状态迁移必须保留向后可识别的原始备份，不依赖手工编辑 JSON。

## 验收标准

- 火山开发者计划继续按 11:00 窗口统计，达线后严格滚动冷却 24 小时。
- 所有付费 Token Plan 和中转站不受本地 token 阈值影响。
- opencode 不再假设 11:00 刷新；额度错误期间用户请求直接跳过，后台探测成功后自动恢复。
- 请求级错误不造成模型全局冷却；Provider 故障在实际失败模型上形成健康冷却。
- 任一故障候选最多占用配置的首响应墙钟预算，不再让一条消息在多个坏模型上无限累积等待。
- `allow_paid/use_last` 不得绕过模态、缺失、健康和 source 状态。
- 状态损坏、热重载和插件重载不得静默丢失费用保护或混用两份路由链。
- 默认管理命令只有 AstrBot 管理员可执行。
- 容器测试、真实 Provider 最小探测、WebChat 端到端验证全部通过后才允许发布。

## 待用户确认的默认参数

- opencode 首次 probe 延迟：建议 3600 秒。
- opencode 后续 probe 间隔：建议 3600 秒。
- 未识别 Provider 异常健康冷却：建议 300 秒；已确认的 timeout/连接/5xx 仍使用 1800 秒。
- source 级故障自动判定：本期先只接受明确账号级错误，多模型聚合判定放到增强项。
