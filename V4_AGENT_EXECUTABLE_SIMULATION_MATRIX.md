# V4 Agent 可执行会话模拟矩阵

本矩阵分为三层。A 层不访问外部服务；B 层使用真实聊天模型但使用结构化票务 fixture；C 层在服务器良票凭证环境调用真实识图和只读预检。任何 fixture 结果不得写成“真实良票验证”。

## A. 每次修改必须执行的离线矩阵

```powershell
Set-Location E:\鱼麦多\v4\backend
$env:PYTHONPATH='.'
python -m pytest -q `
  tests/test_agent_simulation_matrix.py `
  tests/test_agent_mode.py `
  tests/test_chat_agent_loop.py `
  tests/test_chat_ai.py `
  tests/test_plugin_automation.py `
  tests/test_plugin_automation_liangpiao.py `
  tests/test_liangpiao_client.py `
  tests/test_liangpiao_recognition.py `
  tests/test_selected_seat_quote.py `
  tests/test_liangpiao_order.py `
  tests/test_liangpiao_callbacks.py
```

| 编号 | 输入/轮次 | 必须发生 | 禁止发生 | 主要自动化证据 |
|---|---|---|---|---|
| A01 | 买家问“多少钱一张” | Agent 引导发送完整选座截图 | 猜价格、问无关字段、转人工 | `test_chat_ai.py`、`test_plugin_automation.py` |
| A02 | 指定选座图 + 指定场次图同时发送 | 识别两张，合并为一个 target，只预检一次 | 把场次图当第二张票、重复报价 | `test_agent_simulation_matrix.py`、`test_agent_mode.py::test_agent_merges_multiple_images_once_before_authoritative_quote` |
| A03 | 两张不同选座图同时发送 | 形成两个 target，分别预检、分别说明座位与合计 | 合并座位、只报其中一张 | `test_agent_simulation_matrix.py`、`test_agent_mode.py::test_agent_tool_quotes_two_seat_screenshots_independently` |
| A04 | 第一张识别成功、第二张超时 | 保留第一张 target，明确第二张未识别 | 整批失败、丢第一张报价 | `test_agent_mode.py::test_agent_multi_image_keeps_success_when_another_image_recognition_fails` |
| A05 | 两图影院/场次冲突 | 阻止预检并追问具体冲突项 | 默认第一张、模型自行挑选 | `test_agent_mode.py::test_agent_blocks_quote_when_multiple_images_conflict_on_transaction_fields` |
| A06 | 只有场次列表图、没有座位 | 说明已识别影院/影片/场次并引导选座 | 进入预检、把展示价当报价 | `test_agent_simulation_matrix.py::test_show_image_without_selected_seats_cannot_reach_preflight` |
| A07 | `CANDIDATE` 多影院/多影片/多场次 | 展示候选，买家下一轮确认后调用良票 confirm | 默认候选第一项、本地改 DTO 伪确认 | `test_agent_mode.py` 候选两轮测试 |
| A08 | `SHOW_EXPIRED` / `seatMatched=false` / `priceMismatch=true` | 给对应可执行追问或换图提示 | 调预检、猜测可售 | `test_agent_mode.py` 安全阻断测试 |
| A09 | 权威报价后买家回复“好的” | Agent 承接已知座位、张数、报价并引导拍下 | 再问几张、重复要截图、仅因“好的”创建良票订单 | `test_chat_ai.py`、订单确认门禁测试 |
| A10 | 已付款后问“出票了吗” | 读取当前订单事实；未出票只说处理中 | 把已付款说成已出票、查询任意订单 | `test_plugin_automation.py::test_paid_order_status_question_uses_authoritative_order_instead_of_gpt` |
| A11 | 普通问候/感谢/改座表达 | Agent 自然回复或用当前上下文处理 | 关键词模板抢答、无任务却声称已转人工 | `test_plugin_automation.py` acknowledgement 与 seat correction 测试 |
| A12 | 模型超时/工具失败 | 基于已核验事实给安全降级回复，必要时才创建真实人工任务 | 静默、裸报异常、动不动转人工 | `test_agent_mode.py::test_hybrid_image_agent_failure_uses_grounded_fixed_reply` |
| A13 | 识图缺影院/影片/场次 | 只追问实际缺失字段；下一轮确认后继续原 target | 清空上下文、重复要图、编造 ID | `test_agent_mode.py::test_missing_show_recognition_survives_restart_for_the_next_buyer_turn` |
| A14 | 买家把座位纠正为“9排的13 14”等四种表达 | 标准化为 9排13座、9排14座并基于原场次重新预检 | 强制重发截图、复用旧座位报价 | `test_plugin_automation.py` 座位解析测试；`test_plugin_automation_liangpiao.py::test_natural_language_seat_correction_reprices_from_existing_quote_context` 当前为迁移期 xfail，必须由新的 Agent snapshot 工具 E2E 替代后才能算完成 |
| A15 | LIMIT 出票失败→买家同意换 FIXED | 验签回调生成耐久闲鱼关单 command；原单权威 closed 后才询问 FIXED；重新拍下、付款后才创建新良票单 | 对 FAILED 良票单退款、原单未关闭就询问/切换、unknown 重复取消、第三渠道 | `test_liangpiao_callbacks.py`、`test_rule_state_coordinator.py`、`test_rules_first_runtime.py`、`test_api.py`、Node `v2-runtime.test.mjs`；本地全链已通过，真实闲鱼灰度待执行 |

## B. 真实聊天模型多轮矩阵

每个场景使用新的 `tenant/shop/buyer/chat`，同一场景内保持同一 chat。记录每轮：收到事件时间、识图耗时、预检耗时、模型耗时、发送耗时、工具调用、最终回复。

1. `多少钱一张？`
2. 发送指定两张图片。
3. `是下午3点10分这场。`
4. `就8排7座。`
5. `好的。`
6. `我已经拍下了。`
7. `我付款了，出票了吗？`
8. `谢谢。`

逐轮断言：

- 没有权威预检前，回复不得出现成交金额；
- 已有 target 后不得重复索要同一截图；
- 已有一张票时不得再问张数；
- “好的/谢谢”交给 Agent，但不能推进未满足门禁的订单；
- 已付款不等于已出票；
- 除非真实创建了人工任务，否则回复不得声称“已转人工”。

## C. 服务器真实良票只读验证

使用服务器已有 AppKey/Secret，保持 `EXTERNAL_WRITES_ENABLED=false`、`LIANGPIAO_ORDER_CREATE_ENABLED=false`。先调用 `ping`，再对两张图片逐张执行识别，最后仅对有效选座 target 执行 `/order/preflight`。

指定图片：

- `https://img.alicdn.com/imgextra/i1/2464035965/O1CN0133v5VBg1lvC0sG0H_!!2464035965-2-xy_chat.png`
- `https://img.alicdn.com/imgextra/i1/2313315754/O1CN01T4hCpHVyl7L0sG0H_!!2313315754-0-xy_chat.jpg`

必须保存但不得公开敏感价格的证据：`requestId`、`recognizeId`、`matchLevel`、`noMatchReason`、图片类型、候选数量、`showId`、座位映射状态、target 数、预检 `available/estimated`、响应耗时。预期第一张为选座图，第二张为场次辅助图；若真实返回不同，以良票原始响应为准并更新 fixture，禁止为了让测试通过而改写真实结果。

真实验证停止条件：

- 图片 URL 过期、防盗链或供应商错误时，记录良票错误码和 requestId；
- 没有唯一场次或座位未匹配时，不执行预检；
- 不创建订单、不冻结资金、不触发退款或发货。
