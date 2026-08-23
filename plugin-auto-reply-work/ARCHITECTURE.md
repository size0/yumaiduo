# 万达电影票插件当前架构

## 1. 运行边界

生产系统由两个独立服务组成：

```text
鱼麦多平台
  │ webhook / IM / order event
  ▼
插件服务（Node.js）
  │ 事件持久化、会话编排、回复唯一出口、Shadow Agent
  │ 受鉴权的内部 HTTP
  ▼
V3 服务（FastAPI）
  │ AI 语义/视觉抽取、官方影院与场次匹配、确定性报价
  ▼
万达官方接口
  └─ 实时座位、临时试价订单、W+活动、取消、释放复核
```

当前执行所有者是 `deterministic`。Agent 处于 `shadow`，只评估规划，不发送交易回复、不创建真实人工任务，也不执行改价。

## 2. 插件服务职责

### 入口与生命周期

- `index.mjs`：唯一进程入口。
- `src/application.mjs`：组合根，只装配依赖并委托启动、停止、健康检查和 HTTP。
- `src/platform-runtime.mjs`、`src/http-server.mjs`：鱼麦多平台注册、Webhook 和管理请求边界。
- `src/bootstrap/`：存储、Worker、Agent Runtime 和生命周期装配。

### 事件与会话

- `src/event-store.mjs`：加密事件队列、租约、重试、幂等和发送记录。
- `src/conversation-context-store.mjs`：24小时会话事实、10分钟识图草稿、有效报价与订单阶段。
- `src/event-router.mjs`、`src/conversation/message-classifier.mjs`：过滤平台通知并分类业务事件。

### 确定性业务编排

- `src/workflow.mjs`：当前唯一总编排入口；仍是最大的迁移热点。
- `src/quote/quote-orchestrator.mjs`：识图预取、草稿融合、重复试价门禁和一次实时核价调用。
- `src/orders/order-orchestrator.mjs`：订单创建、付款、改价和人工接管状态机。
- `src/reply/reply-orchestrator.mjs`：买家回复的唯一策略边界。
- `src/action-executor.mjs`：最终平台发送、去重和人工接管检查。

### AI 与 Agent

- `src/quote-preview-client.mjs`：调用V3语义抽取、视觉识别、场次解析和报价接口；不自行决定金额。
- `src/ai/ai-orchestrator.mjs`：Provider 中立的AI边界。
- `src/agent/`：持久化Agent运行、工具契约、策略守卫、Shadow/Evaluation、Canary和回复Outbox。

## 3. V3 服务职责

- `app/main.py`：FastAPI组合根、鉴权路由和依赖装配。
- `app/schemas.py`：所有内部API与Agent工具的严格Schema。
- `app/conversation_agent.py`：自然语言报价事实抽取和受限Agent规划。
- `app/vision.py`：图片视觉Schema抽取与归一化。
- `app/local_catalog.py`、`app/match_candidate_resolver.py`：本地官方影院目录与候选解析。
- `app/wanda_quote.py`：城市硬边界、联合场次匹配、实时座位、报价规则和诊断。
- `app/wanda_direct_gateway.py`：账号租约、临时订单、W+活动、取消和释放复核。
- `app/wanda_official_api.py`：万达官方HTTP协议与响应归一化。
- `app/plugin_bridge_store.py`：运行设置、报价策略和买家回复模板。

V3是价格、库存、临时订单、释放状态和报价结果的唯一权威；AI输出只可作为待验证的身份事实。

## 4. 一次图片核价的数据流

```text
Webhook验签并持久入队
  → 同会话合并窗口
  → AI抽取文字事实（失败时正则降级）
  → 视觉模型抽取图片事实
  → 会话草稿确定性融合
  → 官方影院/场次唯一匹配（明确城市为硬边界）
  → 万达实时座位
  → 临时试价读取唯一W+活动
  → 取消临时订单
  → 实时座位0/2/5秒释放复核
  → 后台15/30秒只读复核（仅审计，不恢复原报价）
  → 确定性报价或失败模板
  → 回复唯一出口发送并登记平台消息ID
```

## 5. `temporary_lock_release_unverified` 的含义

该状态不表示“整场会员座都售罄”。它只表示：系统为了读取W+官方活动而临时试价了特定座位，随后已尝试取消，但这些特定座位没有在本轮0/2/5秒实时座位复核中全部重新出现为可售。为避免重复占座或基于不完整证据报价，本轮必须失败关闭。

安全门禁：

- 同一场次临时试价串行。
- 后台仍在复核任一座位组时，同一场次的后续请求在读取账号池和创建订单前直接拒绝。
- 不允许换另一个账号继续试价，避免依次占用该场其他可售座位。
- 即使15/30秒后台复核后来确认释放，原报价仍保持失败；买家需要发起新一轮请求。

## 6. 清理与拆分优先级

### 已确认可删除

- `src/settings-store.mjs`：已无生产引用，设置权威已迁移到V3 Plugin Bridge。
- `src/vision-client.mjs`：已无生产引用，视觉调用已迁移到V3 `vision.py`。
- 两者的孤立单元测试随实现一起删除。

### P0：交易安全和审计

1. 保持同场次串行和“待释放场次”熔断，不得退回仅按账号租约隔离。
2. 将后台释放复核结果输出为脱敏结构化日志，不记录账号、订单号或座位ID。
3. 将全部买家失败文案收敛到V3模板；插件只保留不可达的通用安全兜底。

### P1：降低大文件与双重职责

1. 拆分 `workflow.mjs`：只保留路由与事务顺序，图片、报价跟进、订单和首次回复继续下沉到现有 orchestrator。
2. 拆分 `quote-preview-client.mjs`：transport、文字事实融合、视觉事实融合、失败映射四个模块。
3. 拆分 `wanda_quote.py`：场次解析、座位选择、优惠探测、报价计算和诊断。
4. 拆分 `main.py`：路由模块化，组合根不再包含具体端点实现。

### P2：存储与隐私

1. 会话上下文目前与事件队列的加密策略不一致；应将仍需保留的文字、图片URL和草稿改为加密或进一步最小化。
2. 持久化文件存储最终应替换为具备事务、索引和迁移能力的数据库；迁移前保持单进程唯一写入者。
3. 删除兼容导出前先统计生产调用者，并保留完整回归测试与独立回滚点。

## 7. 禁止清理的代码

以下看起来重复，但职责不同，不能按文件名直接删除：

- 插件 `src/agent/` 与V3 `conversation_agent.py`：前者是持久化运行时和策略守卫，后者是模型Provider与Schema入口。
- 插件报价编排与V3报价服务：前者管理会话和调用顺序，后者掌管交易事实。
- Shadow/Evaluation/Canary代码：生产Active虽关闭，但它们是上线门禁和审计设施。
- 取消、0/2/5秒复核、15/30秒后台复核与账号租约：都属于交易安全链，不能为了精简而合并或删除。
