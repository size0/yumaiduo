# 店主给的两个真图 case：实测结果 + 查出的两个真 bug

> 日期：2026-10-14
> 店主原话：「这个是座位价格不一致的情况 … 这个是座位无法识别的情况」
> 追加：「这个不需要你判断啊 座位价格不一致良票会返回出票失败的」
> ⇒ 接受这个业务事实，不再研究"要不要拦"，改为**确保出票失败这条路处理正确**。

---

## 一、两张图的实测结果（只识别+询价，都没建单）

### ① 座位价格不一致

```
耗时 2.9s   state=已报价
模板 ['RECOGNIZING_TIP', 'QUOTE_CARD_MULTI']
识别 matchLevel=EXACT
     cinema='完美世界影城（春园杜比全景声激光巨幕店）'
     movie='什么意思夫妇'  hall='5号全沙发座椅激光厅'
     showId=37958957  start=2026-10-14T19:05:00+08:00
     seats=['6排7座', '1排6座']

★ 两个座位的真数据：
   6排7座  seatPriceFen=2990  originalPriceFen=3000  areaId=4909
   1排6座  seatPriceFen=2990  originalPriceFen=3000  areaId=0
   ⇒ 【跨两个分区】，但两个分区的原价相同（都是 3000）

询价（LIMIT）：
   estimateAmount=4635   ← 这才是我们要付的（≈2318×2，与 prices[] 的 LIMIT 价一致）
   totalAmount   =5624   ← 限价上限口径（≈2812×2）
   marketAmount  =6000   ← 买家看到的原价，报价卡「原价 30.00元/张 合计 60.00元」✓ 正确
   三种 areaQuoteStrategy（AVERAGE/HIGHEST/LOWEST）结果【完全相同】

我们报价 46.35 元（成本 4635 分，没规则命中→不加价）
```

**★ 顺带纠正上一轮的一个错误量测**：我先前报过"三种策略给出 8697/9353/8041 三个不同值"，
那是脏数据（座位被别人占过/缓存）。这次干净重测：**同一组座位三种策略完全相同**。
所以拿 `areaQuoteStrategy` 当跨区定价的依据是错的。

**★ 这次确实修了一处真问题**（与"能不能建单"无关，是兜底）：
```
没命中规则时 price_mode=LIMIT 但 maxPrice=None
⇒ 文档 1189 说 LIMIT 模式建议设 maxPrice 防"价格已变动"导致实付超预期
⇒ maxPrice=None 等于【钱上不封顶】
已改：没命中规则也给兜底限价 = 成本 + 3元/张，并【封顶到市场原价】
      （原价是买家自购价，出价超过它没道理）
      case① 上算出来 = 4635 + 600 = 5235 分
```

### ② 座位无法识别

```
耗时 2.0s   ok=False
模板 ['RECOGNIZING_TIP', 'RECOG_FAIL']
notes: 有座位没对齐到平台座位图 —— 场次结论仍有效，
       但下单座位要以座位图接口为准
买家收到：「未识别到您选择的座位信息，辛苦选择一下座位信息截图发我哦」
```

**⇒ 这条路的行为是对的**（引导买家重新截图，没有瞎猜座位、没有下单）。

---

## 二、★★★ 查这条路时抓到两个真 bug（都不是小问题）

### bug 1：出票失败【完全没写订单库】

```python
# _on_failed（改前）
out.add("TICKET_FAILED_REFUND")     # 只发一句给买家
if pending: ...
return out                          # ★ 订单库一个字都没写
```

**后果：**
```
· 订单库里那张单还停在 PAID / payment_state=CONFIRMED（"待出票"）
  ⇒ 订单管理页永远显示它在等出票 —— 可它已经失败退钱了
· 分桶计数里它算"待出票"，永远归不了"需关注"
· 有损关单的损失（lossAmount）只活在一句 note 里，对不上账
```

**已修（`_on_failed` 现在写库）：**
```
is_cancelled（回调无 failCode）→ state=CANCELLED（出票前取消，全额解冻）
否则                          → state=FAILED（进"需关注"桶）
全额退回                       → payment_state=REFUNDED
有损关单                       → 保留 CONFIRMED + loss_fen 记进 payload
                                 （买家确实付了钱，损失由我们承担，要认账）
payload["ticket_failed"] = {fail_code, fail_reason, loss_fen, refund_fen,
                            full_refund, cancelled, at}
```

**实测（三种情形）：**
```
① 全额退回   → FAILED  / payment_state=REFUNDED  ✓
② 有损 300   → FAILED  / payment_state=CONFIRMED / loss_fen=300  ✓
③ 出票前取消 → CANCELLED  ✓
```

### bug 2：付款后建单成功，上游单号【落不进订单库】（静默）

```
现象（调试打出来的）：
  on_payment_confirmed order_no='LP-…' ok=True
  落库后 get('LP-…') = None
  _order_error = 'ValueError: ORDER_IDEMPOTENT_REPLAY:
                  幂等键 v5-liangpiao-local-<quote_id> 已用过'
```

**根因（时序改造引入的）：**
```
确认时落【本地待付款单】  order_id=YMD-…   幂等键 = v5-liangpiao-local-<quote_id>
付款后建单成功再落        order_id=LP-…（上游单号，与本地单号不同）
  ⇒ get(order_id) 查不到（换了单号）
  ⇒ 走 create() → 撞上【同一个幂等键】→ ValueError
  ⇒ 这个 ValueError 被 _persist_order 的 except 归到"存储问题"，
    只记 _order_error 就跳过了落库

结果：订单库里只剩一张【待付款】的僵尸单，
     而那张单的钱早就收了、票也在出 ——
     订单管理页看不到它，出票失败回调也找不到它。
```

**已修（两条路都覆盖）：**
```
① 落库前预检：find_by_idempotency / find_by_quote 命中本地待付款单
   → _attach_upstream() 就地补全（写上 provider_order_id / PAID / CONFIRMED）
② 兜底：create() 仍撞 ORDER_IDEMPOTENT_REPLAY 时，
   再按幂等键找一次、同样就地补全 —— 不再把这个错误当存储故障吞掉
★ 两条路共用 _attach_upstream()，避免"改一处忘另一处"
★ 另外在 _persist_order 开头清掉 _order_error ——
  否则排查时看到的是上一单的陈旧错误（我自己就被骗过一次）
```

**实测（两条路分别验过）：**
```
① 正常路（预检命中）：_attach_upstream 调用 1 次，_order_error=''，
   本地单 provider_order_id='LP-UPSTREAM-9' state=PAID payment_state=CONFIRMED ✓
② 兜底路（屏蔽 find_by_quote 模拟老数据）：
   仍成功补全，_order_error=''  ✓
   ★ 第一次我把 find_by_idempotency 也屏蔽了 → 兜底找不到 → 测试报"不通过"，
     那是【测试写错了】（兜底本来就要靠这个查找），不是代码错。
```

---

## 三、钉住的断言（`_check_ticket_flow.py` 第十四节，10 条）

```
★★ 出票失败 → 订单库状态变 FAILED
★★ 全额退回 → payment_state=REFUNDED（不能再算收入）
★★ 失败细节写进 payload（对账要用）
★★ 有损关单 → 仍是 FAILED
★★ 有损关单 → 保留 CONFIRMED（买家付的钱要认账）
★★ 有损金额记进 payload
★★ 出票前取消 → CANCELLED（不是 FAILED）
★★★ 付款后建单 → 上游单号补到本地单（原来会丢）
★★ 补全后 state=PAID / payment_state=CONFIRMED
★★★ 落库不再报幂等冲突（原来被静默吞掉）
```

**★ 写断言时又踩一个坑**：`A and B` 返回的是 B 本身 ——
`_o is not None and (_o.payload or {}).get("ticket_failed")` 返回**字典**，
直接当 `ok` 传会把 dict 塞进 RESULTS，最后 `sum()` 报 TypeError。
⇒ 必须 `bool(...)`。

---

## 四、状态

```
33 套自检全过（43.9 秒）
  _check_ticket_flow 149 项（+10）
服务 8801 已恢复：34 条模板
两张真图都只走了识别+询价 —— 没建单、没冻结、没动钱
临时文件干净
```

**★ 关于"座位价格不一致"**：按店主给的业务事实（良票会返回出票失败）处理，
现在这条路是通的 —— 失败落库、全额退回标 REFUNDED、有损标 FAILED 进"需关注"。
**没能复现"建单时失败"**；若确有该场景，需要良票返回的 code/message 才能定案。
