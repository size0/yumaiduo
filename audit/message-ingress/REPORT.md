# V4 消息入口恢复与报价接续验证（2026-09-09）

## 本轮实际修复

北京时间 17:36:29，仅修改 `/etc/nginx/sites-enabled/wd.xdw0.cn` 并 reload nginx。
增加 exact `/__plugin__/webhook/im.message.received` → `127.0.0.1:4003`，共11行。
后端、插件、出票系统 release、环境变量、数据库及业务开关均未修改。

- 原配置 SHA256：`3763757f3e6d99f239c1114715308ed55a02e8d41e1de0d24dfc71038a121307`
- 新配置 SHA256：`4d3440a5c90a07f70e88d87050ca88dfa6aebdef2a8268c5e9ac7d6ff9414847`
- 备份：`/var/backups/wanda-v4-message-ingress-20260909/wd.xdw0.cn.before`
- 回滚：恢复该文件到上述 active 路径，`nginx -t` 通过后 `systemctl reload nginx`。
- 没有修改 `/api/`、UI、订单回调路由；没有重启或重新注册插件。

## 当前事故的直接根因

实际 manifest 的 `entrypoint.webhookPath=/__plugin__/webhook`，但 active nginx 只配置了 UI、
`/api/v1/plugin/`、`/api/` 和静态前端兜底，遗漏了插件 webhook。
在最初抽取的日志窗口中，真实 `POST /__plugin__/webhook/im.message.received` 有141次405。
包含截图对应的北京时间14:23:23请求（无正文，不能仅靠时间证明其买家ID）。
插件持久事件文件中 events=0，V4 inbox 最新记录停在9月8日。

鉴权边界验证（空JSON、无签名，不构造有效事件，不入队）：

| 检查 | 修复前 | 修复后 |
|---|---|---|
| 插件本机相同路径 | 401 invalid_signature | 不变 |
| 公网相同路径 | nginx 405 | 401 invalid_signature |
| 自然平台消息 | 405 | 17:37:27起202 |

独立 nginx candidate 和全局 `nginx -t` 均通过；全局有 realtime-voice 既有协议选项警告，未修改该站点。
配置与测试已经过现有审查者 review_raw_order_signature 的消息审查，无阻塞问题；随后该审查任务收到容量错误，不能声称完整审查流程正常结束。

## 纠正此前错误结论

86533729 的两条事件此前被说反。以本轮重新解密的数据为准：

- E1图片 `68db2321f0cff4f3452c244cf8df4b5f57416249c73c6a134421603fa2793592`
  已有有效 Recognition、QUOTED、`record-7483f84a28da4bbfb4ce3f9872cfbc7c`。
  存在命令 `cmd-326e1e425e0b2f247645ef2969143b0aea8208eb`；发送结果是
  `skipped / buyer_message_arrived_before_send`。数据库外层 status=failed 不等于平台发送失败。
- E2文字“银川” `f27bb2fdfada886b71e8b694207aeb2663cded892f8e9e3efaaa5509f4f25bb4`
  是 `AGENT_REPLY_UNAVAILABLE / reply_fact_unverified`，没有新Quote ID和新命令证据。
- 没有证据证明 E1 识图失败；E2 的 Recognition=null 不能倒推 E1 无识别。
- 没有 agent_runs 不等于 Legacy；图片直报价不需要 Agent。
- 插件实际 config client 使用 `WANDA_AI_V2_BACKEND_URL=8012`；未使用旧8011变量。
- 13:55:51的 fetch failed 与此前后端重启重合，不能独立证明持续通信故障。

## 至少10个真实买家样本

来源为目标店真实 type1/2消息，不按agent_runs筛选，不把通知伪装为买家。
每人最多4条最近事件，完整脱敏关联在 `ten-buyers.json`；以下都是观察范围内结果。

| 买家 | 实际结果 |
|---|---|
| 2755491711 | 修复后自然消息进入；一次模型503，随后生成回复；部分被人工/新消息保护阻止 |
| 86533729 | 图片Quote成功但命令被新消息跳过；后续文字reply_fact_unverified |
| 3512353025 | 图片Quote命令被新消息跳过；后续文字发送成功4290752944814.PNM |
| 3077576603 | 图片PROBE_REQUIRED，存在安全回复回执；后续文字也有成功回执；未执行Probe |
| 2208075880535 | automation_hold_active，保留挂起，不强发 |
| 4031701933 | agent_model_failed，旧记录无法恢复HTTP原因 |
| 2216210070249 | 图片Quote及若干文字命令被更新消息跳过；后续模型失败 |
| 3705917086 | CANONICAL_QUOTE_UNAVAILABLE；后来automation_hold_active |
| 4108845945 | 图片Quote发送成功4297837807913.PNM |
| 2214596868738 | 若干type1记录completed但无Canonical结果，具体入口原因未确认 |

2663387748 在当前已查 V4 消息范围内未出现。截图14:23时域名回调405可以解释入口丢失，
但没有请求正文/平台历史关联，不能宣称精确识别到该条HTTP请求的买家。

## E1/E2 隔离可执行验证

`test_e2_reply_pipeline.py` 使用生产快照的真实 CanonicalQuoteRuntime、
CanonicalConversationAgent、CanonicalEventHandler、RulesFirstRuntime、
RuleStateCoordinator、QuoteRecordStore、RulesFirstStore。
仅外部识图/Provider/模型固定；不发送、不注册平台、不使用生产数据库。
这是 durable worker composition 验证，不是完整 Plugin HTTP 规范化测试。

场景A：E1图片真实生成Quote和命令，Fake发送结果按真实Outbox方法记录 skipped；
E2调用真实update_purchase_request → 后继Quote → Renderer → 独立E2命令。
场景B：相同真实图片结果但无回复文本，仍保存图片事实；E2仍生成独立命令。
两例都确认重开SQLite及重复E2不增加Quote数量、不新建回复。
NoLegacy会在旧事件handler被调用时直接使测试失败。

隔离价格：SHOWTIME_WPLUS=4490分、r1且markup disabled；E2数量2、总价8980分。
使用历史日期合成fixture，不是旧截图实时验证成功。准确文案：

> 影片：测试电影；影院：广州测试万达；场次：2026-09-05 13:35；W+这场44.9/张，共2张89.8。麻烦确认一下影院和场次，确认后直接拍就行哈

两例当前实现均通过，没有复现“成功续报后必然漏建Outbox”，所以未猜测修改该分支。
初始测试的model方法签名和金额文本格式预期不匹配是fixture错误，已按实际合同修正；
金额8980、数量、Quote lineage、event_id和完整文案断言均保留。

聚焦测试：32 passed。扩大Quote回归：36 passed、2 failed，未跳过或放宽原断言：

- test_wplus_unknown_count_is_preview_and_not_authorized
- test_exact_seats_persist_concrete_seat_identity

两者都是持久Quote输出缺少 `has_selected_seats` 导致KeyError。
本轮没有发布业务代码，不能称业务回归全绿；相关合同修复仍待下一阶段。
测试期间发现旧隔离快照Recognition模型与现在线上包含的质量字段不同，已同步现行模型后重跑上述结果。

## 自然流量验收及后续阶段

修复后自然消息从17:37:27起获202并进入V4，可见正式处理结果。
一次模型请求返回HTTP503，request_id=`efac03de-20d6-4644-866f-65d64d5661ac`，10497ms。
后续文字有AGENT_REPLY_READY，人工/更新消息保护依然阻止陈旧命令。
存在消息发送回执 `4288632252341.PNM`，后续IM历史快照中匹配同ID、outbound，
sentAt=`2026-09-09T09:39:03.685Z`。该回执不是图片报价，不能作为图片报价成功证据。
截至此次采样无新的type2图片报价，图片Quote→发送链真实验收待完成。

| 阶段 | 状态 | 最小后续动作/验收 |
|---|---|---|
| 消息回调入口 | 已修复、自然202验证 | 已完成，保留配置hash与回滚点 |
| E2成功工具→Quote→Outbox | 两个隔离场景通过 | 不凭错误历史结论改业务分支 |
| 86533729文本回复校验 | 历史reason已确认、具体violations未保存 | 在现有审计中保留安全guard诊断，再复现实际被拒绝文案 |
| 当前模型失败 | 一次503已确认、后续有ready | 记录失败阶段/请求ID；不擅换模型密钥，评估有限重试收益与延迟 |
| Quote字段合同 | 两项基线失败 | 核对has_selected_seats存储owner及消费者，隔离复现后修 |
| 全链路真实图片验收 | 待完成 | 等自然新图及补数量/城市，按Quote/Outbox/消息ID/IM历史验收，不回放历史 |

当前release仍为后端quote-repair-dd3d3ba-20260909、插件cockpit-4925798-r2-20260909T000313Z。
出票系统保持ticket-unified-queue-20260907-0455。本轮没有交易操作、发送历史报价或人为生成买家消息。
