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

当前发布门禁仍将新运行版本保持在 `shadow`；只有完成回放审核并显式批准 `wanda-agent-runtime-v37-model-led-native-tools` 后才可成为 `active` 执行所有者。旧确定性链保留为迁移期回滚路径。

## 2. 插件服务职责

### 入口与生命周期

- `index.mjs`：唯一进程入口。
- `src/application.mjs`：组合根，只装配依赖并委托启动、停止、健康检查和 HTTP。
- `src/platform-runtime.mjs`、`src/http-server.mjs`：鱼麦多平台注册、Webhook 和管理请求边界。
- `src/bootstrap/`：存储、Worker、Agent Runtime 和生命周期装配。

### 事件与会话

- `src/event-store.mjs`：加密事件队列、租约、重试、幂等和发送记录。
- `src/conversation-context-store.mjs`：店铺×买家的追加式原始消息、Agent/Tool事件、10分钟识图草稿、有效报价与订单权威投影；模型窗口在调用时按字符预算动态构建，不再固定截取20条。
- `src/event-router.mjs`、`src/conversation/message-classifier.mjs`：过滤平台通知并分类业务事件。

### 确定性业务编排

- `src/workflow.mjs`：当前唯一总编排入口；负责路由与事务顺序。
- `src/workflow-support.mjs`：消息地址、上下文图片、延迟通知、Agent快照、后端动作归一化、报价成本证据和安全重试等无状态支持逻辑。
- `src/quote/quote-orchestrator.mjs`：识图预取、草稿融合、重复试价门禁和一次实时核价调用。
- `src/orders/order-orchestrator.mjs`：订单创建、付款、改价和人工接管状态机。
- `src/reply/reply-orchestrator.mjs`：买家回复的唯一策略边界。
- `src/action-executor.mjs`：最终平台发送、去重和人工接管检查。

### AI 与 Agent

- `src/quote-preview-client.mjs`：调用V3语义抽取、视觉识别、场次解析和报价接口；不自行决定金额。
- `src/ai/ai-orchestrator.mjs`：Provider 中立的AI边界，同时保留迁移期旧Plan接口和v2原生completion接口。
- `src/agent/model-driven-agent-loop.mjs`：模型主导的原生Tool Calling循环；工具错误回注后由模型自行恢复，只读工具可并行，写工具串行。
- `src/agent/native-tool-registry.mjs`：模型可见工具名与本地权威实现的显式映射，不包含固定工作流或next_actions。
- `src/agent/agent-run-store.mjs`、`shadow-agent-runtime.mjs`：12次工具调用、240秒、租约、幂等日志、未知写结果禁止重试、Shadow/Evaluation/Canary和回复Outbox。

## 3. V3 服务职责

- `app/main.py`：FastAPI组合根、生命周期和依赖装配。
- `app/routes/plugin_bridge.py`：插件Bridge鉴权、运行时设置、Canary审批、报价策略和知识库路由。
- `app/routes/quote_preview.py`、`app/routes/agent_reply.py`：报价预览、迁移期旧AgentPlan路由，以及`POST /api/agents/v2/completions`原生模型网关。
- `app/quote_reply.py`、`app/quote_preview_support.py`：确定性回复渲染与报价预览共享安全辅助逻辑。
- `app/schemas.py`：所有内部API与Agent工具的严格Schema。
- `app/conversation_agent.py`：自然语言报价事实抽取、迁移期受限Plan，以及极简System Prompt、reasoning和原生tools调用。
- `app/agent_tool_registry.py`：能力/输入/结果导向的模型工具描述与工具版本。
- `app/knowledge_base_store.py`：租户知识、会话经验，以及“纠正本轮→审核→启用知识版本”的闭环。
- `app/vision.py`：图片视觉Schema抽取与归一化。
- `app/local_catalog.py`、`app/match_candidate_resolver.py`：本地官方影院目录与候选解析。
- `app/wanda_quote.py`：城市硬边界、联合场次匹配、实时座位、报价规则和诊断。
- `app/wanda_direct_gateway.py`：账号租约、临时订单、W+活动、取消和释放复核。
- `app/wanda_official_api.py`：万达官方HTTP协议与响应归一化。
- `app/plugin_bridge_store.py`：运行设置、报价策略和买家回复模板。

V3和插件工具层共同构成价格、库存、临时订单、释放状态、订单状态与副作用门禁的权威边界。模型可自由理解、规划和表达，但不能向高风险工具注入订单号或金额。

## 4. 模型主导会话流

```text
追加式标准messages + 当前权威投影 + 已审核租户知识
  → v2 completion（reasoning开启，tool_choice=auto）
  → 自然回复，或原生tool_calls
  → 工具内验证租户、事实、权限、幂等与副作用
  → {status, code, summary, facts, missing, retryable}
  → 回注同一模型会话，由模型继续选择工具、追问或回复
  → 显式交易事实冲突时回注fact_check_failed并允许模型修正一次
```

模型循环不调用`guardAgentPlan`、意图正则或`next_actions`。旧`/api/agents/turn`和Plan链仅供迁移期Shadow对照，待真实失败会话回放达标后删除。

运营可在Agent抽查表点击“纠正本轮”。纠正记录绑定原事件、run、模型回复、工具轨迹和版本；只有再次审核通过后才生成当前租户启用的知识条目。

## 5. 一次图片核价的确定性回滚流


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

## 6. `temporary_lock_release_unverified` 的含义

该状态不表示“整场会员座都售罄”。它只表示：系统为了读取W+官方活动而临时试价了特定座位，随后已尝试取消，但这些特定座位没有在本轮0/2/5秒实时座位复核中全部重新出现为可售。为避免重复占座或基于不完整证据报价，本轮必须失败关闭。

安全门禁：

- 同一场次临时试价串行。
- 后台仍在复核任一座位组时，同一场次的后续请求在读取账号池和创建订单前直接拒绝。
- 不允许换另一个账号继续试价，避免依次占用该场其他可售座位。
- 即使15/30秒后台复核后来确认释放，原报价仍保持失败；买家需要发起新一轮请求。

## 7. 清理与拆分优先级

### 已确认可删除

- `src/settings-store.mjs`：已无生产引用，设置权威已迁移到V3 Plugin Bridge。
- `src/vision-client.mjs`：已无生产引用，视觉调用已迁移到V3 `vision.py`。
- 两者的孤立单元测试随实现一起删除。

### P0：交易安全和审计

1. 保持同场次串行和“待释放场次”熔断，不得退回仅按账号租约隔离。
2. 将后台释放复核结果输出为脱敏结构化日志，不记录账号、订单号或座位ID。
3. 将全部买家失败文案收敛到V3模板；插件只保留不可达的通用安全兜底。

### P1：降低大文件与双重职责

1. `workflow.mjs` 已从约1635行收敛至约1176行：40个无状态支持函数已下沉到 `workflow-support.mjs`；下一步继续抽离图片上下文准备、Agent会话执行和首次回复协调器，使主文件只保留路由与事务顺序。
2. `quote-preview-client.mjs` 已从935行收敛至约365行，仅保留受控HTTP、识图并发与缓存协调；文字事实、识图融合、失败映射和回复展示已分别下沉到 `quote/quote-text-facts.mjs`、`quote/quote-recognition-fusion.mjs`、`quote/quote-failure-mapper.mjs` 和 `quote/quote-response-presenter.mjs`。
3. `wanda_quote.py` 已从1124行收敛至约425行：实时座位、选座、W+活动和价格边界位于 `wanda_quote_domain.py`；脱敏失败结构位于 `wanda_quote_diagnostics.py`；旧本地出票网关传输适配位于 `wanda_quote_gateway.py`；城市硬边界、官方联合场次验证、15秒只读成功缓存和并发合并位于 `wanda_showtime_matcher.py`。主服务只保留只读座位查询、临时活动探测和报价响应编排。
4. `main.py` 已从约950行收敛至约207行：插件Bridge、报价预览、Agent与回复端点已模块化；组合根只保留依赖注入、生命周期、基础设置/存储/识图端点和路由装配。

### P2：存储与隐私

1. 会话上下文目前与事件队列的加密策略不一致；应将仍需保留的文字、图片URL和草稿改为加密或进一步最小化。
2. 持久化文件存储最终应替换为具备事务、索引和迁移能力的数据库；迁移前保持单进程唯一写入者。
3. 删除兼容导出前先统计生产调用者，并保留完整回归测试与独立回滚点。

## 8. 禁止清理的代码

以下看起来重复，但职责不同，不能按文件名直接删除：

- 插件 `src/agent/` 与V3 `conversation_agent.py`：前者是持久化运行时和策略守卫，后者是模型Provider与Schema入口。
- 插件报价编排与V3报价服务：前者管理会话和调用顺序，后者掌管交易事实。
- Shadow/Evaluation/Canary代码：生产Active虽关闭，但它们是上线门禁和审计设施。
- 取消、0/2/5秒复核、15/30秒后台复核与账号租约：都属于交易安全链，不能为了精简而合并或删除。

## 9. v37 Active 发布边界

- `/agent-release activate` 只接受服务端签发的 `evidence_id`；客户端健康、SHA 或评测数字一律拒绝。
- Evidence 必须绑定最终 commit、Manifest/Plugin/V3 SHA、两个 systemd WorkingDirectory、三次冻结评测、真实回滚演练和最近60秒守卫检查。
- AgentRun、Tool journal 与 Outbox 携带 `release_id/release_generation`。rollback 递增 generation；旧代任务只能停止或对账，不能继续写入或发送。
- `wanda-agent-release-guard.timer` 每30秒检查服务、重启数、运行身份、窗口指标和零容忍事故，触发时只调用原子 rollback，不自动恢复 Active。
- `npm run eval:v37` 只接受独立人工/权威 Gold 的冻结数据，图片结果必须声明重新执行当前识图及完整工具路径；最终 Evidence 要求同一数据和版本连续三次通过。
