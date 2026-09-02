# V4 自研 Agent Harness 优化计划

> 目标：参考 Pi 的 Agent 架构优化 V4 自研 Agent，不直接安装、调用或依赖 Pi。
>
> 核心原则：**自然语言理解和任务编排交给 Agent；价格、库存、座位、订单、付款、出票和退款事实仍由 V4 规则与受控工具决定。**
>
> 本计划只涉及本地代码和工作台模拟验证。任何生产部署、真实交易写动作和无关配置修改必须另行授权。

---

## 1. 当前问题判断

当前 V4 并不是只有一个 Agent，而是三层系统叠加：

```text
平台事件队列
    ↓
RulesFirstRuntime / RulesFirstDecisionEngine
    ↓
CustomerServiceChatService
    ↓
_run_agent_loop()
    ↓
V4 业务工具和平台执行器
```

### 1.1 规则路由位置

实际业务路由位于：

```text
backend/app/plugin_automation.py
  RulesFirstDecisionEngine.process_event()
```

事件队列和串行处理位于：

```text
backend/app/rules_first_runtime.py
  RulesFirstRuntime._process_claimed()
```

Agent loop 位于：

```text
backend/app/chat_service.py
  CustomerServiceChatService._run_agent_loop()
```

插件事件入口位于：

```text
plugin-runtime/wanda-seat-autoquote/src/runtime/event-processor.mjs
```

### 1.2 当前 Agent 的主要问题

1. `chat_service.py` 同时承担上下文、模型调用、工具协议、工具执行、重试、trace 和最终回复。
2. 当前同时兼容 native tool call 和 JSON action，模型协议边界不够单一。
3. 生产 Agent 默认工具轮数较少，复杂任务容易过早结束；单纯增加轮数可能导致重复调用和错误推进。
4. 工具存在重叠，模型可见动作空间过大，例如多个报价工具表达相近能力。
5. 工具结果没有统一的成功、警告、失败、恢复建议格式。
6. Agent 工作状态、V4 交易状态和审计状态没有明确分层。
7. 会话恢复主要依赖当前请求和有限内存上下文，进程重启后 Agent 工作过程不完整。
8. 新买家消息不能稳定地中断旧 Agent run，可能出现旧回复晚到或答非所问。
9. 生产 Agent trace 尚未完整覆盖买家原文、工具过程、门禁和最终回复。
10. 完整编排模式开放了更多能力，但没有对应的任务状态管理和阶段化工具编排，因此回复会显得混乱。
11. 线上插件 UI 与本地独立 UI 是两套实现，UI 混乱不能单靠增强 Agent loop 解决。

结论：当前问题是 **Harness 架构、工具动作空间、上下文管理和 UI 组织共同造成的**，不应只归因于模型能力或循环次数。

---

## 2. 目标架构

```text
CustomerServiceChatService
        ↓
AgentRuntime
├── AgentSessionManager
├── AgentContextManager
├── AgentEventBus
├── ModelGateway
├── ToolRegistry
├── ToolPolicy
├── ToolDispatcher
├── RecoveryPolicy
├── TraceRecorder
└── ReplyValidator
        ↓
V4 Rules / Business Tools / Transaction Gates
```

建议新增目录：

```text
backend/app/agent_runtime/
├── __init__.py
├── contracts.py
├── runtime.py
├── session.py
├── context.py
├── events.py
├── model_gateway.py
├── tool_registry.py
├── tool_policy.py
├── tool_dispatcher.py
├── recovery.py
├── trace.py
└── reply_validator.py
```

当前入口保持不变：

```python
CustomerServiceChatService.reply(...)
```

但内部改为调用：

```python
AgentRuntime.run(request)
```

---

## 3. 参考 Pi 的概念，但不引入 Pi

| Pi 概念 | V4 自研对应物 | 说明 |
|---|---|---|
| `AgentSession` | `AgentSessionState` | 保存当前 Agent 工作上下文，不保存权威订单事实 |
| `AgentSessionRuntime` | `AgentRuntime` | 管理一次完整 Agent run |
| `AgentEvent` | `AgentEventBus` | 统一发布工具、消息、轮次和生命周期事件 |
| `AgentTool` | `ToolRegistry` | 注册模型可见工具及执行契约 |
| `tool_execution_*` | `ToolDispatcher` | 执行请求级工具并回传标准结果 |
| `message_update` | `StreamSink` | 工作台展示流式状态，生产仍只发送最终文本 |
| `SessionManager` | `AgentSessionStore` | 加密保存会话事件和恢复检查点 |
| Compaction | `BusinessContextCompactor` | 只压缩对话，不压缩或替代价格、订单等权威事实 |
| Auto retry | `RecoveryPolicy` | 对模型和只读工具做有限安全重试 |
| `agent_settled` | `AGENT_SETTLED` | 只有最终回复完成并通过门禁才算完成 |

Pi 自身没有内置票务状态机、交易门禁或业务规划能力，因此 V4 必须保留自己的业务层。

---

## 4. 核心接口设计

### 4.1 Agent 请求

```python
@dataclass
class AgentRequest:
    run_id: str
    session_id: str
    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    event_id: str | None
    user_message: str
    history: list[dict[str, Any]]
    runtime_context: Mapping[str, Any]
    mode: str
    deadline_seconds: float
```

### 4.2 Agent 结果

```python
@dataclass
class AgentResult:
    status: str  # settled|waiting_buyer|blocked|failed|interrupted
    reply_text: str | None
    tool_calls: list[dict[str, Any]]
    trace_id: str
    finish_reason: str
    usage: dict[str, Any] | None
```

### 4.3 工具结果

所有工具统一返回：

```json
{
  "status": "success|warning|error",
  "summary": "一句话事实摘要",
  "data": {},
  "next_actions": [],
  "retry": {
    "allowed": false,
    "max_attempts": 0
  },
  "stop_condition": null
}
```

工具结果必须是 Agent-safe 的：

```text
不返回 Token、Cookie、CSRF Token、原始 provider 响应或未经核验的成交金额。
```

---

## 5. Agent loop 优化

### 5.1 从固定轮数改为预算控制

不只使用：

```python
for round_index in range(3):
```

改为：

```text
最大模型轮数：8
最大工具调用数：12
最大总耗时：30秒
同一失败最多重试：1次
同一工具同参数不得重复调用
每轮最多一个写工具
写工具成功后必须读取权威状态
```

所有预算都可按模式配置：

```text
rules：不启动 Agent
hybrid：低预算，只读工具
agent：标准预算
full：高预算，但仍受工具和交易门禁限制
simulation：允许模拟写工具
```

### 5.2 状态机

```text
IDLE
  ↓
RUNNING
  ↓
WAITING_TOOL
  ├── 工具成功 → RUNNING
  ├── 可恢复失败 → RECOVERING
  └── 不可恢复失败 → WAITING_BUYER / FAILED
  ↓
WAITING_BUYER
  ↓
新消息到达 → INTERRUPTED → 新 run
  ↓
SETTLED
```

### 5.3 收敛条件

Agent 只有满足以下条件才结束：

```text
- 有最终回复文本；
- 没有未处理的工具调用；
- 没有待解释的工具错误；
- 回复通过金额、座位、订单状态和敏感信息检查；
- 若涉及交易，已明确处于“等待买家确认”或“V4 状态机已完成”。
```

---

## 6. 工具体系优化

### 6.1 模型只看唯一 canonical 工具名

合并或隐藏重复工具：

```text
get_quote
get_authoritative_quote
reprice_seats
quote.preflight_current
```

建议模型最终只看到：

```text
quote.preflight_current
recognition.resolve_seats
```

内部旧别名可以保留兼容，但不再全部暴露给模型。

### 6.2 按业务阶段动态暴露工具

#### 咨询阶段

```text
recognize_screenshot
cinema.list
show.list
show.detail
seat.list
```

#### 识别和报价阶段

```text
recognition.get_current
recognition.confirm_cinema
recognition.confirm_movie
recognition.confirm_show
recognition.resolve_seats
quote.preflight_current
```

#### 订单阶段

```text
get_order_state
change_order_price
verify_payment
```

#### 履约和售后阶段

```text
submit_fulfillment
send_ticket
urge_order
refund_or_intercept
```

普通咨询阶段不得暴露出票、发货、退款和强制交付工具。

### 6.3 工具前置条件

每个工具定义必须声明：

```text
用途
输入字段
只读或写入
所需前置状态
成功后的下一状态
失败后的安全动作
是否允许重试
```

---

## 7. 会话、上下文和恢复

### 7.1 三种状态分离

```text
Agent 状态：当前目标、阶段、缺失字段、待确认问题
V4 业务状态：识别、座位、报价、订单、付款、出票、退款
审计状态：事件、工具调用、门禁、回复和发送状态
```

Agent 状态可以恢复，V4 业务状态必须重新读取并验证。

### 7.2 会话键

```text
tenant_id + shop_id + buyer_id + chat_id
```

不能仅按买家 ID 或聊天 ID 恢复，防止跨店铺和跨租户串上下文。

### 7.3 新消息中断旧 run

```text
旧 run 正在查询
    ↓
买家发送新消息
    ↓
旧 run 标记 INTERRUPTED
    ↓
旧最终回复禁止发送
    ↓
按新消息和当前权威状态启动新 run
```

例如买家从“第八排”改成“第九排”时，旧查询结果不能晚到并发送。

### 7.4 业务专用上下文压缩

上下文过长时只压缩自然语言历史，必须保留结构化摘要：

```text
已确认影院
已确认影片
已确认场次
已确认座位
当前张数
有效报价引用
未确认字段
最近工具错误
当前业务阶段
```

金额、库存和订单状态每次恢复时重新从 V4 权威服务读取。

---

## 8. 事件和 trace

### 8.1 标准事件

```text
agent_start
turn_start
model_response_start
model_response_end
tool_call_start
tool_call_end
tool_blocked
tool_retry
context_restore
context_compaction
reply_validation
reply_suppressed
reply_sent
agent_settled
agent_failed
agent_interrupted
```

### 8.2 每个事件至少包含

```text
run_id
session_id
trace_id
tenant_id
shop_id
buyer_id
chat_id
event_id
turn_index
timestamp
```

### 8.3 敏感数据控制

保存：

```text
工具名
参数摘要或参数 hash
结果状态
门禁原因
耗时
模型名
回复状态
```

不保存：

```text
原始图片
票码明文
Token
Cookie
CSRF Token
原始 provider 响应
模型隐藏思维链
```

生产 trace 继续使用加密、租户隔离和幂等写入；审计失败不得影响客服业务结果。

---

## 9. 流式输出

### 工作台

实时展示：

```text
Agent 正在理解需求
正在查询场次
正在查询实时座位
正在核验报价
正在等待买家确认
```

### 生产

```text
Agent 内部可以流式运行
    ↓
等待最终结果
    ↓
执行回复门禁
    ↓
唯一发送出口一次性发送
```

禁止把未通过检查的半成品回复直接发送给买家。

---

## 10. 回复质量门禁

最终回复必须检查：

```text
- 是否回答了买家的最新问题；
- 是否漏答价格异议；
- 是否引用了权威工具事实；
- 是否把位置偏好错误当成已选座位；
- 是否把截图金额当成交价格；
- 是否重复询问已确认信息；
- 是否声称付款、出票、发货或退款成功；
- 是否出现未被工具证明的价格、库存、座位或订单状态；
- 是否在工具失败后仍然给出确定性结论；
- 是否需要等待买家确认。
```

`发送成功`不等于`回复正确`。发送前必须保存质量检查结果。

---

## 11. 插件 UI 处理

Agent Harness 重构和插件 UI 重构分开执行。

### 第一阶段

工作台只显示必要信息：

```text
当前会话
当前 Agent 模式
当前阶段
实时运行状态
工具调用时间线
最终回复
门禁结果
```

### 第二阶段

把管理功能分离：

```text
客服工作台
运营配置
订单管理
Agent 审计
人工任务
系统日志
```

不要把店铺开关、报价规则、订单管理和对话编排全部放在同一屏。

线上插件 UI 与本地 `frontend/v4/index.html` 需要最终统一来源；在统一前，不把 UI 问题归因于 Agent loop。

---

## 12. 实施阶段

### 阶段 0：基线和 fixture

建立固定回归场景：

```text
价格咨询
“这么贵吗”
短消息“1”
多影院选择
图片识别
多图冲突
第八排中间
座位查询失败
报价过期
买家修改座位
买家新消息打断旧查询
模拟下单、付款、出票、退款
```

记录当前 Agent 的：

```text
回复
工具顺序
工具参数
工具轮数
耗时
失败原因
发送结果
```

### 阶段 1：抽象 Runtime

新增 `AgentRuntime` 和 `LegacyAgentRuntime`，把当前 `_run_agent_loop()` 包装进去。

要求：

```text
- 行为不变；
- 支持 legacy/pi-inspired 两个 runtime 名称；
- 所有现有测试继续通过；
- 不部署生产。
```

### 阶段 2：拆分 Harness

实现：

```text
AgentSessionState
AgentEventBus
ToolRegistry
ToolPolicy
ToolDispatcher
AgentBudget
RecoveryPolicy
ReplyValidator
```

### 阶段 3：统一工具和结果

完成：

```text
- 工具 canonical 名称；
- 按业务阶段暴露工具；
- 统一工具结果格式；
- 统一错误恢复；
- 禁止重复工具调用。
```

### 阶段 4：会话恢复和新消息中断

完成：

```text
- 加密 Agent session store；
- 运行检查点；
- 旧 run 中断；
- 进程重启恢复；
- 业务状态重新权威读取。
```

### 阶段 5：流式 trace 和回复审计

完成：

```text
- 工作台实时事件；
- 生产脱敏 trace；
- 回复质量检查；
- 抑制原因和发送状态；
- 工具耗时和模型用量统计。
```

### 阶段 6：完整编排回归

对比旧实现和新 Harness：

```text
工具调用准确率
最终回复正确率
漏答率
错误推进率
重复调用率
人工转接率
平均延迟
Token 成本
```

### 阶段 7：生产灰度

在取得单独授权后，按店铺逐步启用：

```text
先只读
再工作台模拟写
再低风险文字咨询
最后才考虑真实交易写动作
```

旧 `LegacyAgentRuntime` 保留为回滚路径，稳定后再移除。

---

## 13. 测试要求

### 单元测试

```text
AgentBudget
ToolRegistry
ToolPolicy
ToolDispatcher
AgentSessionState
ContextCompactor
RecoveryPolicy
ReplyValidator
```

### 集成测试

```text
模型 → 工具 → 工具结果 → 模型
工具失败 → 安全恢复
工具重复 → 去重
新消息 → 中断旧 run
会话恢复 → 重建 V4 权威状态
回复门禁 → 抑制不安全回复
```

### 工作台 E2E

```text
咨询 → 查场次 → 查座位 → 展示候选
识图 → 候选确认 → 权威报价
报价 → 买家确认 → 模拟下单
模拟付款 → 模拟出票 → 模拟发货
失败 → 重试/等待/人工任务
```

验收基线：

```text
后端现有测试全部通过
插件现有测试全部通过
新增 Harness 测试覆盖率不低于 80%
compileall 通过
npm test 通过
git diff --check 通过
```

---

## 14. 成功标准

优化完成后，Agent 应能做到：

```text
- 连续执行多个查询工具而不乱序；
- 正确读取并利用工具结果；
- 工具失败时说明原因并采取安全下一步；
- 买家新消息到达时取消旧回复；
- 进程重启后恢复对话但重新读取业务事实；
- 工作台能展示完整运行轨迹；
- 生产可以审计每一轮 Agent 行为；
- 不把截图、模型猜测或旧上下文当成成交事实；
- 不因为启用 full 模式就绕过 V4 交易门禁；
- 普通咨询回复更自然，交易动作仍然严格受控。
```

最终状态：

```text
自然语言和任务规划：Agent
会话、事件、工具、恢复：自研 Harness（参考 Pi）
价格、库存、座位、订单事实：V4
真实平台写操作：V4 规则和安全门禁
生产审计：V4 加密审计系统
```
