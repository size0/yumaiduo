# Canonical Agent 模型边界与数量接续修复（2026-09-09）

## 范围与版本

仅 Agent/model client 和报价接续字段合同；未部署、未重启、未改开关，未发送消息，未操作订单。
独立 worktree：`E:/鱼麦多/v4-worktrees/agent-model-repair-20260909`。
分支 `codex/agent-model-repair-20260909`，基础提交 a75460b。
eb2f92d 仅将 Agent 文件对齐生产快照，不是本轮功能修复。
生产 Backend release：`/opt/wanda-v4/releases/v4-ratio-percent-20260909T025005`。
Agent 原始 SHA256：`918a54745b9fb6036504771aad2941988361bba6789ac7ac715a839d14411654`。
本地共享工作区的 `backend/app` 缺少此 Agent，不能用其旧测试数量作为本次验收。
本次下载了生产 app 的 97 个 Python 源文件，只读快照位于 audit/baseline-runtime；
audit/runtime 为同一快照加本轮两文件补丁。没有加载生产数据库到测试写入路径。

## 历史事件（已脱敏）

tenant 107 / shop 2313315754 / buyer 尾号5965 / chat 尾号2180。

* agent-run-816605…：2026-09-06T15:49:46Z，agent_model_failed。
  完整关联 event 已解密核对：messageType=26 平台交易通知，并非“2张”纯文本；
  Recognition 存在，购买上下文仅有事件标识，已有 LIANGPIAO EXACT_SEATS Quote，
  generation=7、数量1、TRANSACTION_READY。工具0条，Outbox0条。
  原始异常/HTTP状态没有保存，不能恢复成“鉴权失败”或“模型不支持工具”的确定结论。
* agent-run-446064…：2026-09-05T16:07:50Z，agent_response_missing。
  输入为5字符买家文字，涉及价格/张数；Recognition 存在，已有 Wanda WPLUS_AREA
  PREVIEW，generation=2、数量未知。工具依次为：
  request_quote(INVALID) → get_current_context/get_current_quote/get_transaction_state/get_order(success)
  → update_purchase_request(INVALID) → update_purchase_request(QUOTED)。
  最后成功工具时间16:08:40.291Z，run结束16:08:40.300Z，Outbox0条。
  与默认4轮工具用尽后直接missing的实现吻合；历史没有完整模型响应，不能还原所有原始response。
* 两条历史模型均记录 config_id=model-config-35d1ca62d7ab649b / revision=1 /
  OpenAI-compatible / gpt-5.5 / airelvo.cc。历史release未记录，UNKNOWN。
* 两条event的IM历史均含图片（41/43条），没有把历史列表最后一张当作必然正确的前一图。
  历史购买上下文未保存明确图片关联ID，精确图片血缘仍UNKNOWN。

## 当前有效配置与真实模型验证

生产进程环境在主机内解析，未输出/导出密钥。
当前 resolver 每次调用 PersistentSettingsStore.current()，忽略tenant/shop参数，
读取 `/var/lib/ticket-system/wanda-v4/vision-settings.json`。
插件当前 UI 也是 GET/PUT `/api/settings/vision`；这是共享配置，并非每店铺独立scope继承。
当前模型 gpt-5.5，provider OpenAI-compatible，host airelvo.cc，base path为空，
实际 Chat Completions `/v1/chat/completions`，非流式；timeout=60秒，key存在。
当前 resolver 不返回config_id/revision，二者UNKNOWN，不能套用历史revision=1。

使用生产同一出网环境、原有 model client，请求只包含内部测试文字：

|测试|状态|request_id|耗时|结果|
|---|---|---|---|---|
|A 普通文本|200|a48fd3df-59e9-4a05-b31d-8a3c9b00acd5|5.90s|红色 (hóngsè)|
|B1 工具调用|200|b8c12122-6ab6-4201-ad88-05058be0ab1c|3.58s|get_current_context|
|B2 回传工具结果|200|462b9d41-a1e9-419b-8223-ceb63a2adaff|4.30s|隔离测试影片|

A/B外层每次25秒上限，最多3次，无真实IM发送。

## 实际复现与修复

1. 生产 quote_structured 传 cinema_address，即使None；生产 RecognitionResult extra=forbid
   却没有此字段。真实模型选择 update_purchase_request 后得到QUOTE_REQUEST_INVALID。
   修复：只增加可选 cinema_address(max_length=500)，未放宽其他额外字段校验。
2. for range(max_tool_rounds) 把最后一次工具执行当最终一轮，结果没有机会回传生成文案。
   修复：允许一次无tools最终轮；越界tool_calls不再执行，返回agent_tool_round_limit。
3. HTTP错误、拒绝、截断、解析错误、超时均被统一吞掉；缺key反而返回固定文案被当成功。
   修复：缺key fail closed；分类安全diagnostic，保存到已有审计context（如果注入了store），
   同时写安全日志（当前composition未注入audit store也可留证）。不保存原始敏感body。

修改owner：backend/app/canonical_conversation_agent.py、backend/app/recognition_v2/models.py。
没有更换模型/provider/key，没有新增Agent/store/报价引擎，没有调用Legacy。

## C 图片报价后“2张”：同一入口前后

合成Provider事实：广州测试万达 / 测试电影 / 2026-09-05 13:35 / IMAX厅。
这是明确的历史日期合成fixture，不是原历史截图实时重放成功。
固定成本SHOWTIME_WPLUS=4490分、原价6200分；隔离规则r1、revision0、markup disabled。
真实链：process_image_event → CanonicalQuoteRuntime → WandaPricingV2Service/V4PricingEngine
→ QuoteV2Service/QuoteRecordStore → AgentContextBuilder → CanonicalConversationAgent
→ update_purchase_request → 同一个quote_structured/引擎/store → tool结果 → 模型文案/actions。
只替换Provider事实和模型HTTP边界；未mock报价保存或整个处理器。

修复前（生产原始代码，真实模型，隔离store）：
图片PREVIEW成功；补2张后update_purchase_request/request_quote均QUOTE_REQUEST_INVALID，
最终agent_response_missing，只有一份PREVIEW，无后继Quote、无回复actions。
完整合成记录：probe-c-live.txt。

修复后（仅独立进程内加载补丁，生产文件未改）：
模型两次HTTP200：a210b026-b086-479c-aac0-ee3d359d2e4e(14.12s)，
79307d91-979a-44e3-95ea-1a40cf36b09a(10.38s)。
模型实际调用update_purchase_request，工具QUOTED。
Q1 quote-df280998e6524a30b1d664a908504068：4490分/张、数量未知、PREVIEW → SUPERSEDED。
Q2 quote-29de1b29063246c3990af786cf4619a2：generation2，supersedes=Q1，
数量2、单价4490分、总价8980分、TRANSACTION_READY，purchase_context保持synthetic-purchase。

真实模型最终文案（合成内容）：
> 影片：测试电影
> 影院：广州测试万达
> 场次：2026年9月5日 13:35，IMAX厅
> W+区域：44.9元/张，共2张89.8元。
> 麻烦确认一下影院和场次，确认后直接拍就行哈。

生成send_message action但无transport执行；这不是买家真实发送验收，也未验收下单能力。
C补丁进程最多4次模型调用、每次20秒、整体90秒、SSH110秒。
实际2次；全部数据在TemporaryDirectory中，退出已自动清理。

## 测试结果及已知基线差异

before.txt：6个边界回归测试在原始Agent失败。
quote-before.txt：接续集成在生产schema上失败。
focused-after.txt：30 passed（模型边界9、真实报价接续1、既有Agent20）。
扩大QuoteV2回归：66 passed / 2 failed；2个失败为旧分支测试要求 has_selected_seats 字段，
生产快照本来不输出；原始快照同组36 passed / 2 failed，证明不是补丁新增回归。
不为消除无关基线差异改动报价持久化字段。
人工代码审查通过（Agent边界及地址合同）。git diff --check通过。

执行方式：
`python -m pytest -q -o pythonpath=<worktree>/audit/runtime backend/tests/test_agent_model_boundary_repair.py backend/tests/test_agent_quote_continuation_repair.py backend/tests/test_canonical_conversation_agent.py`

不能在缺少生产快照时改用旧backend依赖并宣称通过。audit/runtime是有意保留的隔离证据，
未纳入业务提交；production-source-hashes.json记录源版本。

## 交付界限

需要代码修复：是，已在独立worktree完成；当前模型凭据/供应商无须更换的证据为A/B/C成功。
历史agent_model_failed原始异常不可恢复，不能断言其HTTP错误是什么。
需要部署：本轮禁止部署，未执行；线上仍未应用补丁。
后续仍需经过批准的部署与自然买家消息验收；本报告不声称线上已修好。
