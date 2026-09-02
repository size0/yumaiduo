# V3 W+ Active Probe 行为规格与迁移盘点

- 文档类型：V3 行为冻结 / V4 迁移基线
- 文档版本：`probe-protocol-v3-audit-1`
- 适用范围：W+ Active Probe、临时订单清理、座位释放复核和探针报价输入
- 状态：M1 盘点完成；未执行真实 Probe；未创建真实临时订单
- 约束：本文档不依赖 V3 源码即可作为后续 V4 实现和 Shadow Replay 的协议基线

> 本文档记录的是 V3 当前工作树中的实际行为，包括 direct official path 和 legacy local gateway path 的差异。V3 当前工作树存在其他未提交修改，因此不能只用 V3 commit 作为行为身份；本次盘点同时记录核心文件的 SHA-256。

## 1. V3 冻结范围与文件身份

V3 根目录：`E:/鱼麦多/V3`

参考提交：`67d875b feat: add trusted v37 active release gates`

核心文件工作树 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `v3-backend-gateway-work/app/wanda_quote.py` | `10f7dc56004cb6ed3240affc5337e6f945c482b0b25b5243e7798fe707189a11` |
| `v3-backend-gateway-work/app/wanda_direct_gateway.py` | `fb1ee928e59da06d7aecd342f24a4eb4a9b30672423815dd156b7df525bae385` |
| `v3-backend-gateway-work/app/wanda_official_api.py` | `83da5e2a49e3030a48a954ca4020b56d5f10a643af055476005c43ccfa3e197c` |
| `v3-backend-gateway-work/app/wanda_quote_domain.py` | `040e9e0be5ef8c29aa330f20dd1d4d924ee14db8ba6e7c9b58f231d2a9764a42` |
| `v3-backend-gateway-work/app/wanda_quote_gateway.py` | `a3274f7b0ce3aa8d8cfb9e6690e52e4030ba2932389db985d2bd8121842c0a3a` |
| `v3-backend-gateway-work/app/wanda_showtime_matcher.py` | `a01fc91c30ae22b96c9b9470a9269571d0bac417b96715a1a9709a4a7a2a25b4` |

V3 源码冻结原则：

1. M1 不修改上述 V3 文件。
2. M2 只能基于本协议抽取 V4 组件，不能把 V3 文件直接复制到 V4。
3. V3 删除前必须保留该行为基线、Golden Result 和 Shadow 对照结果。

## 2. Dependency Graph

### 2.1 总体图

```text
RealtimeQuoteService.quote(request)
│
├─ ShowtimeMatcher.catalog_match_input()
│  └─ LocalWandaCatalog
│     └─ cinema_cache.sqlite（immutable / mode=ro）
│
├─ TicketGateway.for_quote()
│  ├─ LocalTicketGateway（legacy local gateway path）
│  │  ├─ GET  /api/auth/internal/wplus-accounts
│  │  ├─ POST /api/order/match
│  │  ├─ GET  /api/showtime/seats
│  │  ├─ POST /api/order/create-ticket-flow
│  │  ├─ GET  /api/order/available-offers
│  │  └─ POST /api/order/cancel
│  │
│  └─ WandaDirectGateway（direct official path，显式开关）
│     ├─ JsonWandaAccountSource
│     │  └─ accounts.json（只读）
│     ├─ AccountLeaseRegistry（进程内账号租约）
│     ├─ _AsyncKeyLockRegistry（showtime 级串行）
│     ├─ WandaOfficialApiClient（账号级 HTTP client）
│     │  ├─ POST /order/create_order.api
│     │  ├─ POST /order/order_status.api
│     │  ├─ GET  /mkt/activity/secret/list.api
│     │  ├─ POST /order/cancel.api
│     │  └─ GET  /order/real_time_seat.api
│     └─ delayed release reconciliation（15s + 15s）
│
├─ gateway.match() / ShowtimeMatcher
│  └─ 影院、影片、日期、时间、影厅 → 唯一 showtime_id + cinema_id
│
├─ gateway.realtime_seats(showtime_id)
│  └─ SeatFact[]
│
├─ _select_seats()
│  ├─ 官方已选座：逐座匹配
│  └─ 无官方已选座：只从实时 W+ 可售座位中确定性选择代表座
│
├─ _locked_member_offer()
│  ├─ DirectGateway.probe_activity_offers()
│  └─ LocalTicketGateway.lock() → available_offers() → cancel()
│
├─ _all_seats_released() / release checks
│
└─ wanda_quote_domain
   ├─ _locked_offer_unit_cents()
   ├─ _bounded_quote_for_seat()
   └─ _round_quote_cents_to_tenth()
```

### 2.2 Direct Probe 生命周期

```text
Probe request
→ showtime lock(showtime_id)
→ pending-release gate
→ account pool read
→ eligible account filter
→ account rotation
→ account lease acquire
→ account-scoped official client
→ create_order
→ orderId extraction
→ create verification
→ order_status lock verification
→ W+ activity offers
→ cancel_order
→ cancel status verification
→ seat release checks at 0/2/5 seconds
→ release_verified
→ release lease + close client
→ ProbeResult / pricing fact
```

### 2.3 V3 当前存在两套 Probe 实现

#### Direct official path

由以下链路启用：

```text
WANDA_DIRECT_GATEWAY_ENABLED=true
→ build_wanda_direct_gateway_from_env()
→ WandaDirectGateway
→ WandaOfficialApiClient
```

`RealtimeQuoteService._locked_member_offer()` 优先使用该路径。

#### Legacy local gateway path

当 direct gateway 未启用时，默认使用：

```text
LocalTicketGateway
→ WANDA_QUOTE_GATEWAY_URL
→ 本地 ticket gateway
```

该路径仍然执行临时锁座、活动读取和取消，但账号、签名、Token 由本地 gateway 持有。它的取消适配器不在本地 V3 代码中直接验证 `orderStatus=60` 和 `lockSeatTime=-1`，主要依赖 gateway 返回结果及实时座位释放复核。

这两个路径不能在 V4 中继续作为两个未声明的工具真相源；迁移后必须统一到同一个 `WandaActiveProbe` 协议。

## 3. V3 Probe 输入规格

### 3.1 业务输入

V3 API 模型使用 `Recognition` 和 `QuoteRealtimeRequest`。迁移协议中的规范字段为：

```json
{
  "cinema_id": "官方影院ID或可验证影院ID",
  "movie": "影片名称",
  "show_id": "官方场次ID",
  "showtime": "HH:mm",
  "show_date": "YYYY-MM-DD",
  "hall": "影厅名称",
  "selected_seats": ["12排16座", "12排17座"],
  "quantity": 2,
  "seat_types": ["W+", "普通", "特惠", "优选"]
}
```

V3 实际输入字段映射：

| 协议字段 | V3 字段 |
|---|---|
| `cinema_id` | match 返回的 `cinema.cinemaId` / `cinema_id` |
| `movie` | `Recognition.movie` |
| `show_id` | match 返回的 `showtime.showtimeId` / `showtime_id` |
| `showtime` | `Recognition.showtime` 的起始时间 |
| `show_date` | `Recognition.date` |
| `hall` | `Recognition.hall` |
| `selected_seats` | `Recognition.official_selection.selected_seat_numbers` |
| `quantity` | `QuoteRealtimeRequest.ticket_count`，或官方已选座数量 |
| `seat_types` | 实时座位图解析出的 `SeatZoneType` |

### 3.2 账号要求

账号必须同时满足：

```text
status == "online"
risk_status ∈ {"", "normal", "ok", "safe", "正常"}
is_wplus == true 或 account_type == "wplus"
token 非空
phone/mobile 非空
remaining 未提供，或 remaining 为正整数
```

Direct path 还要求：

```text
WANDA_PRICING_ACCOUNT_REF_KEY 至少 32 字节
```

账号 Token、手机号、用户标识、设备标识只能存在于内部账号和 HTTP 层，不能进入 Agent Observation、ProbeResult、回复或普通日志。

## 4. Probe Seat Selection 行为

### 4.1 有官方已选座

1. 只使用官方已选座号码，不使用截图展示价格决定报价。
2. 将每个截图座位与实时座位图逐一匹配。
3. 必须满足：匹配数量等于已选座数量，且每个座位当前可售。
4. 任意一个座位无法匹配时，失败关闭：

```text
official_selection_unverifiable
```

5. 根据 `(area_code, zone_type)` 分组。
6. 每个座位类型/区域只选择一个真实代表座执行 W+ 活动探价。
7. 不能把 W+ 区代表座替换普通区或其他区域座位。
8. 同一座位类型的其他座位只能复用该类型探针得到的会员单价；不同类型必须分别探针。
9. 不允许跨区域平均价格。

例如：

```text
W+区：12排16座、12排17座 → 只探针其中一张
普通区：12排18座           → 另探针一张
```

### 4.2 没有官方已选座

1. 只从实时座位图中选择状态为可售的 W+ 座位。
2. 不使用截图中的手绘圈、展示价格或模型推断座位作为官方选座。
3. 不使用手绘圈推断数量。
4. `ticket_count` 未知时，V3 默认用一张代表座探价，并返回 `needs_ticket_count=true`。
5. `ticket_count` 已知时，实时可售 W+ 座位数量必须满足需求。
6. 代表座按以下稳定顺序选择：

```text
area_code
→ original_price_cents
→ wplus_member_price_cents
→ seat_id
```

7. Area Probe 的结果只能代表实时 W+ 区域价格，不能声称是买家圈选的具体座位价格。

### 4.3 V3 当前实际探针数量

- Exact selection：每个 `(area_code, zone_type)` 一张代表座。
- Area probe：最终只锁定一个 W+ 代表座，即使前面为了选择检查了多个候选座。
- 每次传给活动优惠接口的 Probe seat 数量必须为 1；`allotSeat.totalPayPrice` 被视为该座位类型的单座可支付价格，不能把订单总额除以张数。

## 5. 创建临时订单与锁座规格

### 5.1 创建成功条件

必须同时满足：

```text
top-level code == 0 或 "0"
data.bizCode == 0 或 "0"
orderId 存在且非空
```

V3 Direct Client 的行为：

- 有 `orderId` 但 `code/bizCode` 不成功：返回 `create_verified=false`。
- Gateway 发现 `create_verified=false` 后必须尝试取消该订单，不能把它当作成功锁座。
- 有明确的创建前 401/403：允许切换下一个账号。
- 创建请求发生未知异常时，不得盲目切换账号，因为请求可能已经在 provider 侧创建订单。

### 5.2 锁座成功条件

在读取活动优惠前，必须验证：

```text
orderStatus == "40"
lockSeatTime >= 0
```

Direct `activity_offers()` 最多进行 3 次状态读取，非成功状态之间等待 1 秒。锁座未确认时：

```text
temporary_lock_state_unknown
```

不得读取活动优惠，不得返回报价。

## 6. W+ 活动有效条件

从活动列表中只接受满足以下全部条件的项目：

```text
able == true
name 包含 “W+会员专享”
allotSeat 存在且为对象
allotSeat.totalPayPrice 为正整数
```

V3 默认接受：

```text
W+会员专享优惠
```

只有配置允许时才额外接受：

```text
W+周五会员日专享
```

活动价格语义：

```text
allotSeat.totalPayPrice = 当前 Probe 代表座位类型的单座会员支付价
```

不能：

```text
把多座临时订单总价除以数量
把普通区活动当成 W+ 区活动
把截图价格当作会员价
接受多个不同 totalPayPrice 而自行挑一个
```

没有唯一有效活动时：

```text
wplus_price_unavailable
```

如果当前账号无有效活动，但订单已经安全取消且座位已经释放，才允许在有限账号次数内切换下一个账号。

## 7. Cancel 与 Release 规格

### 7.1 finally 语义

只要 `temporary_order_id` 已经成功取得：

```text
无论活动接口失败
无论解析失败
无论 timeout
无论 Agent Turn stale
无论上层异常
都必须进入 cancel/release 流程。
```

Agent Turn 的取消不能取消 Probe 清理任务。

### 7.2 Cancel 成功不是最终成功

取消请求返回成功后，仍必须验证：

```text
orderStatus == "60"
lockSeatTime == -1
```

V3 Direct `cancel_order()` 内部完成：

```text
POST /order/cancel.api
→ POST /order/order_status.api
→ 验证 60/-1
```

若取消 API 成功但状态不满足：

```text
cancel_confirmed = false
```

整体不得返回成功报价。

### 7.3 座位释放复核

取消后必须在同一 `show_id` 查询实时座位：

```text
0 秒
2 秒
5 秒
```

所有 Probe Seat 必须重新处于可售状态：

```text
expected_seat_ids ⊆ available_seat_ids
```

成功条件：

```text
cancel confirmed
AND orderStatus == 60
AND lockSeatTime == -1
AND 所有 Probe Seat 恢复可售
```

否则返回：

```text
temporary_lock_release_unverified
```

### 7.4 后台释放复核

Direct path 在 0/2/5 秒仍无法证明释放时，会：

```text
标记 showtime 为 pending release
保留账号 lease
启动后台只读复核
延迟 15 秒复核
再次延迟 15 秒复核
```

后台复核期间，同一 `showtime_id` 禁止新 Probe，即使更换账号也禁止。

V3 后台复核结果包括：

```text
release_confirmed_after_failure
release_still_unverified_after_background_recheck
release_recheck_lease_lost
```

V3 当前这些状态只在内存中，未持久化，这是 V4 迁移 blocker。

## 8. 并发、账号和配额行为

### 8.1 同场次串行

Direct path 使用：

```text
_AsyncKeyLockRegistry.hold(showtime_id)
```

同一场次同时最多一个 Active Probe。

### 8.2 同账号串行

Direct path 使用：

```text
AccountLeaseRegistry.try_acquire(account_id)
```

默认 builder 租约 TTL：

```text
180 秒
```

租约支持：

```text
try_acquire
renew
release
```

账号已被租用时跳过，不能并发使用同一账号。

### 8.3 账号轮换

- 账号按可用列表轮换。
- 默认最多尝试 3 个账号。
- 只有在确认没有创建订单的失败，或当前订单已取消并释放后，才允许切换账号。
- 创建结果不确定时不得切换账号。
- 当前 V3 `JsonWandaAccountSource` 只读账号池，不负责 Token 刷新、失败次数持久化或 cooldown 持久化。

### 8.4 配额

当前 V3 只读取：

```text
remaining
```

并要求为正整数；未提供 `remaining` 时视为可用。

当前代码没有在 Probe 成功后原子扣减 quota，也没有持久化：

```text
失败次数
cooldown
最后一次失败原因
Token 失效时间
```

因此“配额耗尽”目前主要表现为账号过滤后的：

```text
wplus_account_unavailable
```

这不能视为完整的生产级配额迁移。

## 9. Probe 状态机（行为规格）

V3 当前没有 Durable `ProbeOrder` 表，但其行为应冻结为以下状态：

```text
CREATED
  → LOCKED
  → PRICE_READ
  → CANCEL_REQUESTED
  → CANCEL_CONFIRMED
  → RELEASE_CHECKING
  → RELEASE_VERIFIED
```

失败分支：

```text
CREATED + create result unknown
  → FAILED / temporary_lock_state_unknown

LOCKED + activity failure
  → CANCEL_REQUESTED

CANCEL_REQUESTED + cancel/status/release failure
  → RELEASE_UNVERIFIED / temporary_lock_release_unverified

RELEASE_UNVERIFIED
  → 后台 reconciliation
  → RELEASE_VERIFIED 或永久失败
```

强制规则：

```text
temporary_order_id 一旦存在，不能直接进入 FAILED 并结束任务。
必须完成 cancel 尝试和 release verification，或留下可恢复的 pending 状态。
```

## 10. Probe 与 Transaction 边界

V3 设计意图上，Probe 临时订单不是买家订单，且 direct client 没有以下能力：

```text
支付
退款
出票
发货
闲鱼订单改价
良票订单创建
```

但是 V3 当前代码没有独立的 Durable `ProbeOrder` 模型。V4 必须明确建立：

```text
ProbeOrder
├─ probe_id
├─ tenant_id
├─ show_id
├─ account_id（内部引用）
├─ seat_ids
├─ temporary_order_id（仅内部 Audit）
├─ created_at
├─ cancel_requested_at
├─ cancel_confirmed_at
├─ release_verified_at
├─ status
└─ error_code
```

Probe 不得写入：

```text
XianyuOrder
LiangpiaoOrder
TransactionState
```

ProbeResult 只能作为报价事实输入，不能触发：

```text
change_price
payment
fulfillment
ticketing
```

## 11. ProbeResult 行为结构

对 Agent 和 V4 Pricing 层暴露的安全结构：

```json
{
  "probe_id": "probe-opaque-id",
  "provider": "WANDA",
  "show_id": "show-opaque-id",
  "status": "SUCCESS",
  "seat_type_prices": [
    {
      "area_code": "opaque-area",
      "zone_type": "W+",
      "representative_seat_id": "opaque-seat",
      "original_price_cents": 5000,
      "member_price_cents": 3800
    }
  ],
  "temporary_order_created": true,
  "temporary_order_cancelled": true,
  "release_verified": true,
  "account_ref": "opaque-account-ref",
  "completed_at": "2026-01-01T00:00:00+08:00"
}
```

不得暴露给 Agent：

```text
手机号
Token
Cookie
Authorization
temporary_order_id
原始 MX-API
签名
设备标识
```

内部审计可以保存脱敏后的：

```text
account_ref
provider request id
temporary_order_id 的不可逆引用
状态转换
上游错误码
阶段耗时
```

## 12. Historical V3 Pricing Policy（仅历史记录）

> 本节只记录 V3 当时的商业报价策略，**不属于 V4 Active Probe Protocol，不得作为 V4 ProbeCoordinator、WandaActiveProbe、ProbeResult、Probe lifecycle 或 V4 QuoteEngine 的实现依据**。V4 Probe 只返回官方价格事实；最终售价必须由 V4 当前 Pricing/Quote 逻辑决定。

### 12.1 W+ 规则（V3 历史）

V3 默认配置：

```text
wplus_adjustment_cents = -290
wplus_member_price_threshold_cents = 6000
```

对于 W+ 座位：

```text
if member_price_cents <= 6000:
    raw_selling_price = max(original_price_cents - 290, member_price_cents)
else:
    raw_selling_price = member_price_cents
```

随后：

```text
按 0.1 元，即 10 cents，四舍五入
报价不低于会员成本
报价不高于实时原价
报价必须为正数
```

如果十分位取整后会员成本高于实时原价上限：

```text
quote_price_conflict
```

### 12.2 普通区等非 W+ 规则

V3 当前另有：

```text
regular_adjustment_cents = 100
```

非 W+ 分支：

```text
selling_price = member_price_cents + 100
```

仍必须经过：

```text
10 cents rounding
member cost floor
original price ceiling
```

### 12.3 多张与多类型

- 同一 `(area_code, zone_type)` 的座位复用该类型单座会员价。
- 不同座位类型分别探针、分别计算。
- 最终按座位逐项计算并求和。
- 不把一个混合订单的总优惠平均到所有座位。
- Area Probe 在数量已知时，将单价乘以买家确认数量；它不能宣称代表具体截图座位。

### 12.4 其他持久化报价策略

`PluginBridgeStore.DEFAULT_QUOTE_POLICY` 还包含：

```text
max_auto_order_amount_cents = 200000
```

它属于报价/交易策略边界，不是 W+ Probe 的会员价公式；迁移时必须与 Probe 逻辑分离。

## 13. V3 Probe 相关文件清单

### 核心 Probe 实现

```text
v3-backend-gateway-work/app/wanda_quote.py
v3-backend-gateway-work/app/wanda_direct_gateway.py
v3-backend-gateway-work/app/wanda_official_api.py
```

### 领域与选择

```text
v3-backend-gateway-work/app/wanda_quote_domain.py
v3-backend-gateway-work/app/wanda_showtime_matcher.py
v3-backend-gateway-work/app/local_catalog.py
v3-backend-gateway-work/app/schemas.py
```

### Gateway 与本地桥接

```text
v3-backend-gateway-work/app/wanda_quote_gateway.py
v3-backend-gateway-work/app/direct_gateway_preflight.py
v3-backend-gateway-work/app/main.py
```

### 错误、报价策略、回复和配置

```text
v3-backend-gateway-work/app/wanda_quote_diagnostics.py
v3-backend-gateway-work/app/plugin_bridge_store.py
v3-backend-gateway-work/app/wanda_quote_store.py
v3-backend-gateway-work/app/settings_store.py
v3-backend-gateway-work/app/quote_reply.py
```

### 识图和预览入口（Probe 输入来源）

```text
v3-backend-gateway-work/app/vision.py
v3-backend-gateway-work/app/vision_recognition_cache.py
v3-backend-gateway-work/app/quote_preview_support.py
v3-backend-gateway-work/app/quote_preview_store.py
v3-backend-gateway-work/app/routes/quote_preview.py
v3-backend-gateway-work/app/conversation_agent.py
```

### 测试文件

```text
v3-backend-gateway-work/tests/test_api.py
v3-backend-gateway-work/tests/test_wanda_direct_gateway_contract.py
v3-backend-gateway-work/tests/test_wanda_official_api.py
v3-backend-gateway-work/tests/test_wanda_quote_domain.py
v3-backend-gateway-work/tests/test_wanda_quote_gateway.py
v3-backend-gateway-work/tests/test_wanda_showtime_matcher.py
v3-backend-gateway-work/tests/test_wanda_quote_diagnostics.py
v3-backend-gateway-work/tests/test_direct_gateway_preflight.py
```

当前测试中，`test_api.py` 包含跨平台识图、实时座位、临时锁座、活动读取、取消和释放复核的大量行为测试；Direct 专项测试覆盖账号筛选、租约、同场次串行、0/2/5 秒复核、延迟复核、敏感信息隔离和官方 URL 边界。

但是当前还没有独立的、版本化的 `tests/fixtures/provider_replays/` V3 Golden Fixture 集合。

## 14. V3 配置项与常量

### 14.1 Direct official path

| 配置 | 默认值/来源 | 用途 |
|---|---|---|
| `WANDA_DIRECT_GATEWAY_ENABLED` | `false` | Direct Probe 总开关；默认关闭 |
| `WANDA_DIRECT_ACCOUNT_POOL_PATH` | `/var/lib/ticket-system/backend-data/accounts.json` | 账号池文件 |
| `WANDA_PRICING_ACCOUNT_REF_KEY` | 无默认值，至少 32 字节 | 生成不可逆报价账号引用 |
| `WANDA_DIRECT_APP_CLIENT_KEY` | 内置公开 App client key，可环境覆盖 | 官方签名协议材料，不是账号 Token |
| `WANDA_ALLOW_FRIDAY_MEMBER_DAY` | `false` | 是否接受周五会员日活动 |

### 14.2 Legacy local gateway path

| 配置 | 默认值/来源 | 用途 |
|---|---|---|
| `WANDA_QUOTE_GATEWAY_URL` | `http://127.0.0.1:8000` | 本地票务 gateway |
| `WANDA_QUOTE_GATEWAY_KEY` | 空 | V3 到本地 gateway 的桥接认证 |
| `WANDA_ACCOUNT_PHONE` | 空 | 固定核价账号手机号 |
| `WANDA_QUOTE_SETTINGS_PATH` | `backend/data/wanda_quote_config.json` | 持久化账号手机号 |

### 14.3 影院和应用配置

| 配置/路径 | 默认值/来源 | 用途 |
|---|---|---|
| `WANDA_CINEMA_CACHE_PATH` | `/var/lib/ticket-system/backend-data/cinema_cache.sqlite` | 影院缓存，只读匹配 |
| `WANDA_SETTINGS_PATH` | `backend/data/model_config.json` | 模型和应用配置 |
| `WANDA_QUOTE_PREVIEW_STORE_PATH` | `backend/data/quote_preview_queue.json` | 预览队列，不是 Probe 状态库 |
| `WANDA_VISION_RECOGNITION_CACHE_PATH` | `/var/lib/ticket-system/wanda-ai-v2-data/vision-recognition-cache.json` | 识图缓存 |

### 14.4 固定常量

```text
RELEASE_RECHECK_DELAYS_SECONDS = (0.0, 2.0, 5.0)
Direct delayed release recheck = (15.0, 15.0)
Direct default AccountLeaseRegistry TTL = 180 seconds
Direct default max_account_attempts = 3
Legacy showtime lock = process-local asyncio.Lock
W+ adjustment = -290 cents
W+ threshold = 6000 cents
Regular adjustment = +100 cents
max_auto_order_amount = 200000 cents
```

## 15. V3 Probe 错误码清单

### 15.1 DirectGatewayError

```text
account_pool_unavailable
account_pool_invalid
wplus_account_unavailable
account_lease_unavailable
official_credentials_unavailable
official_origin_forbidden
official_http_failed
official_response_invalid
pre_create_account_unavailable
invalid_direct_lock_request
temporary_lock_failed
temporary_lock_state_unknown
activity_offers_failed
temporary_lock_release_unverified
release_recheck_lease_lost
release_confirmed_after_failure
release_still_unverified_after_background_recheck
```

### 15.2 官方 API/账号相关

```text
official_http_failed
official_response_invalid
official_origin_forbidden
pre_create_account_unavailable
temporary_lock_state_unknown
```

其中：

- `pre_create_account_unavailable`：创建订单前确定性的账号鉴权失败，可以换账号。
- `temporary_lock_state_unknown`：创建结果、锁座状态或返回结构无法安全确认，不能盲目换账号。
- `official_http_failed`：官方 HTTP/JSON 访问失败。

### 15.3 Quote diagnostics 对外安全错误码

```text
cinema_catalog_not_unique
non_wanda_cinema
ticket_count_conflict
official_selection_unverifiable
showtime_not_unique
insufficient_available_seats
wplus_seats_unavailable
wplus_area_unavailable
wplus_price_unavailable
quote_price_conflict
wplus_account_unavailable
temporary_lock_failed
temporary_lock_release_unverified
wanda_gateway_unavailable
```

### 15.4 入口层和本地 gateway 错误

```text
万达核价账号尚未配置
万达账号池网关返回 HTTP <status>
万达账号池网关连接失败
万达账号池返回格式无效
线上账号池没有可用的 W+ 会员账号
万达核价网关返回 HTTP <status>
万达核价网关连接失败
万达核价网关返回格式无效
```

这些文本必须在 V4 通过稳定的结构化错误码表示，不能把原始上游响应、Token、手机号或订单号传给 Agent。

## 16. V3 与 V4 现状映射

### 16.1 V4 已有能力

| V3 能力 | V4 当前对应 | 状态 |
|---|---|---|
| Wanda 影院本地匹配 | `WandaDirectQuoteService._resolve_cinema` + SQLite | 已有，但不是 Probe 模块 |
| Wanda 官方场次查询 | `WandaDirectQuoteService._official_get` | 已有 |
| Wanda 实时座位查询 | `WandaDirectQuoteService._official_get` | 已有 |
| 选座与座位类型解析 | `WandaDirectQuoteService._seat_facts` / `_selected_seat_facts` | 已有 |
| quote.preview | `backend/app/agent/tools.py` | 已有 |
| QuoteRecord | `QuoteRecordStore` | 已有 |
| Agent Observation | `backend/app/agent/observations.py` | 已有 |
| ReplyValidator | `backend/app/agent/validators.py` | 已有 |
| Outbox send_message | RulesFirstRuntime / Node Runtime | 已有 |
| 交易写熔断 | `AGENT_HARNESS_READ_ONLY=true` | 已有，但不能替代 Probe kill switch |

### 16.2 V4 当前不等价或缺失

| V3 能力 | V4 状态 | 结论 |
|---|---|---|
| 独立 `ProbeOrder` | 缺失 | Blocker |
| ProbeCoordinator | 缺失 | Blocker |
| Probe policy | 缺失 | Blocker |
| `WANDA_ACTIVE_PROBE_ENABLED` | 缺失 | Blocker |
| 账号池多账号轮换 | 当前 `_fixed_account` 单账号 | Blocker |
| 账号 Token 健康/risk/quota/cooldown | 不完整 | Blocker |
| AccountLease | 当前无等价 Durable 实现 | Blocker |
| show 级锁 | 只有服务实例内 `_showtime_locks` | 不等价 |
| release pending show gate | 缺失 | Blocker |
| 0/2/5 秒释放验证 | 当前只在旧 `_probe_member_price` 路径部分存在 | 未统一 |
| orderStatus 60 / lockSeatTime -1 | 当前实现不形成独立 Probe 状态 | Blocker |
| 15/15 秒后台 release reconciliation | 缺失 | Blocker |
| Backend restart recovery | 缺失 | Blocker |
| Probe 审计 | Quote audit 有，但没有 Probe lifecycle audit | Blocker |
| ProbeResult → V4 Existing Pricing Engine | 当前报价服务内部直接计算 | 需要拆分，不能引入 V3 Pricing Policy |
| Active Probe 与 `AGENT_HARNESS_READ_ONLY` 联动 | 未建立专用门禁 | Blocker |
| Agent stale 与 Probe cleanup 分离 | 未建立 Probe 独立生命周期 | Blocker |
| endpoint guard | 现有 Wanda endpoint allow-list 有部分保护 | 需补充 Active Probe 开关 guard |

### 16.3 V4 当前最危险的行为

V4 `WandaDirectQuoteService.quote()` 在会员价缺失时可能进入 `_probe_member_price()`，该路径会直接调用：

```text
POST /order/create_order.api
POST /order/order_status.api
GET  /mkt/activity/secret/list.api
POST /order/cancel.api
GET  /order/real_time_seat.api
```

因此：

```text
AGENT_HARNESS_READ_ONLY=true
```

目前只读交易熔断不能单独证明“不会 Active Probe”。在 V4 Probe 迁移完成前，必须让 Active Probe 拥有独立的：

```text
WANDA_ACTIVE_PROBE_ENABLED=false
```

并在 Coordinator、Wanda provider client、quote.preview 三层 fail closed。

## 17. V4 Pricing Architecture（只读审计）

本节记录当前 V4 实际运行代码中的报价职责，不修改公式，不把 V3 商业规则带入 Probe。

### 17.1 当前主链路

```text
Wanda 官方 Provider
→ WandaDirectQuoteService._seat_facts()
→ Seat Facts
   ├─ original price / salesPrice
   ├─ regular settlePrice
   ├─ W+ activity price
   ├─ physical W+ eligibility
   ├─ area_id / area_name / seat_id
   └─ channel_fee
→ WandaDirectQuoteService._priced_unit()
   或 _vip_priced_unit()
→ RealSeatQuote.unit_quote_cents
→ RealQuote.total_quote_cents
→ PluginAutomation._record_quote()
→ QuoteRecordStore
→ ReplyValidator / Agent 回复
```

当前没有独立命名为 `QuoteEngine` 的 V4 类；实际最终售价计算集中在：

```text
backend/app/wanda_direct_quote.py
├─ WandaDirectQuoteService._priced_unit()
├─ WandaDirectQuoteService._vip_priced_unit()
├─ WandaDirectQuoteService._dynamic_wanda_adjustment()
├─ WandaDirectQuoteService._exact_quote()
└─ WandaDirectQuoteService._middle_wplus_quote()
```

### 17.2 V4 当前售价公式

当 `rules.enabled == false`：

```text
有有效 member_price → member_price
否则 → original_price
```

当 `rules.wanda_rules` 非空：

```text
discount_percent = member_price / original_price * 100
adjustment = 按 wanda_rules 匹配的 fixed_adjustment_cents
raw = member_price + adjustment
```

当前仓库 `data/pricing-rules.json` 的状态为：

```text
enabled = true
wanda_rules = 六个完整区间
每个区间 fixed_adjustment_cents = 290
rounding_increment_cents = 10
```

因此当前仓库配置下，Wanda 普通座和 W+ 座均优先进入 `wanda_rules` 动态区间路径，而不是自动使用 V3 历史的 `-290 / +100` 分支。

当 `wanda_rules` 为空时，才回退到代码中的兼容路径：

```text
W+ 且 member_price <= wplus_member_price_threshold_cents：
    max(original_price + wplus_adjustment_cents, member_price)

W+ 且 member_price > threshold：
    member_price

非 W+：
    member_price + regular_adjustment_cents
```

所有非 VIP 结果随后执行：

```text
向 rounding_increment_cents 取整
不低于 member_price
不高于 original_price
```

VIP 影厅由 `_vip_priced_unit()` 独立处理：

```text
original_price <= vip_discount_threshold_cents：
    original_price - vip_low_price_discount_cents

original_price > vip_discount_threshold_cents：
    original_price * vip_high_price_discount_percent / 100
```

随后按 `rounding_increment_cents` 取整，并限制在正数至实时原价之间。当前默认字段为：

```text
vip_fixed_cost_cents = 5000
vip_discount_threshold_cents = 6000
vip_high_price_discount_percent = 90
vip_low_price_discount_cents = 200
```

### 17.3 多张票与渠道/区域分支

- Exact seats：`_exact_quote()` 对每个实时座位生成 `RealSeatQuote`，最终 `total_quote_cents` 为每座 `unit_quote_cents` 求和。
- 同一 `(area_id, price, channel_fee)` 的座位共享已取得的会员价事实；不同区域/类型不平均价格。
- Area preview：`_middle_wplus_quote()` 先得到 W+ 参考单价；数量已知时使用 `unit_quote_cents * ticket_count`，未知时总价为空。
- Wanda 区域差异来自 `physical_wplus`、`area_id`、`area_name`、`seat_type`、官方原价、会员价和 channel fee。
- Liangpiao 使用独立的 `SelectedSeatQuoteService._apply_operator_pricing()`，受 `FIXED/LIMIT`、`liangpiao_rules`、`liangpiao_fixed_rules`、`marketAmount`、`estimateAmount` 和 `totalAmount` 影响。

### 17.4 当前 V4 价格边界与配置来源

当前存在的最终价格边界：

```text
动态 Wanda fixed_adjustment_cents
普通座 adjustment
VIP discount/fixed-cost 规则
rounding_increment_cents
member price floor
original price ceiling
```

安全边界：

```text
最终报价不得低于有效会员成本
最终报价不得高于实时官方原价
取整后若 floor > ceiling，则拒绝报价
```

默认配置入口：

```text
WANDA_PRICING_RULES_PATH=data/pricing-rules.json
```

当前仓库可确认：

```text
enabled=true
wanda_rules=六段 fixed_adjustment_cents=290
rounding_increment_cents=10
liangpiao_price_mode=LIMIT
```

本轮没有读取生产机进程环境，因此不能把仓库文件断言为生产最终生效值；生产环境若设置 `WANDA_PRICING_RULES_PATH` 覆盖，应单独脱敏核对。

`QuoteRecordStore` 负责持久化和审计，不负责重新计算最终售价；`PricingRulesStore.rule_version()` 为实际规则生成版本摘要。

### 17.5 WandaDirectQuoteService 的职责拆分结论

当前 `WandaDirectQuoteService` 同时承担：

```text
影院匹配
官方场次查询
实时座位查询
座位事实归一化
Active Probe 触发（_probe_member_price）
Probe 释放等待
最终商业报价计算
RealQuote 组装
```

后续应拆成：

```text
WandaReadClient / SeatFactMapper
→ WandaActiveProbe
→ ProbeResult（只含 original/member cost facts）
→ V4 Existing Pricing Engine
→ RealQuote / QuoteRecord
```

ProbeResult 禁止包含：

```text
selling_price
quote_price
markup
discount
adjustment
threshold
```

本节只用于 V4 报价审计，不改变当前公式。

## 18. Golden Tests 与 Shadow 迁移基线

### 18.1 Golden Tests 分层

#### A. Probe Golden Tests

只验证：

```text
代表座选择
create 是否成功
lock 是否确认
W+ 活动是否唯一
member_price_cents 是否正确读取
cancel 是否完成
release 是否确认
error code 是否一致
```

期望结果只允许是 Probe 事实，例如：

```json
{
  "status": "SUCCESS",
  "member_price_cents": 3800,
  "release_verified": true
}
```

不得验证或写入：

```text
selling_price
quote_price
markup
discount
adjustment
threshold
最终买家报价
```

#### B. V4 Pricing Tests

使用脱离 Probe 的纯 `ProbeResult` 作为输入，调用 V4 当前 Pricing/Quote 逻辑，验证：

```text
V4 当前配置
V4 当前 Wanda rules
V4 当前 VIP 分支
V4 当前 Liangpiao 分支
取整、成本下限、原价上限
多张求和
```

这些测试必须根据 V4 当前真实规则建立，不能复制 V3 Golden 的最终报价期望值。

### 18.2 V3 已有测试来源

现有测试已经覆盖以下行为类别：

```text
成功 Probe
create/bizCode 失败
orderId 缺失
锁座状态不为 40
活动不存在
活动价格缺失
活动接口异常
finally cancel
取消后状态未确认
0/2/5 秒释放
延迟释放复核
多 seat type
普通区 + W+ 区混合
同 show 串行
同账号 lease
账号失效前置重试
W+ 账号筛选
remaining 配额过滤
敏感信息不泄露
禁止 legacy ticket URL 旁路
```

### 18.3 当前未完成项

尚未建立独立版本化的：

```text
tests/fixtures/provider_replays/
```

也没有把每个场景统一保存为：

```json
{
  "status": "SUCCESS|FAILED",
  "code": "...",
  "seat_type_prices": [],
  "release_verified": true
}
```

所以当前不能宣称 V3 Golden Fixtures 已完成。

### 18.4 M2 必须建立的 Golden 场景

至少覆盖：

```text
1  正常 W+ 探针成功
2  create_order 失败
3  orderId 缺失
4  锁座状态不是 40
5  W+ 活动不存在
6  totalPayPrice 缺失
7  Probe 成功后 cancel 失败
8  orderStatus 未到 60
9  lockSeatTime 未到 -1
10 座位立即恢复
11 座位 2 秒恢复
12 座位 5 秒恢复
13 5 秒后仍未恢复
14 多 seat type
15 普通区 + W+ 区混合
16 同 show 并发
17 同账号并发
18 Account Token 失效
19 W+ 会员失效
20 Probe 额度耗尽
```

M2 的 Probe 结果必须与本协议和 Probe Golden Result 一致；V4 Pricing Tests 只需与 V4 当前 Pricing 规则一致。不能要求 V3 最终 quote 与 V4 最终 quote 相同。

Shadow Replay 只比较 Probe 层：

```text
V3 输入 → V3 Probe → member price fact
V4 输入 → V4 Probe → member price fact
```

比较：

```text
代表座
区域类型
member_price
create/lock/cancel/release 行为
error code
```

不比较：

```text
V3 最终 selling_price
V3 最终 quote
V3 与 V4 的最终 total_quote_cents
```

## 19. Migration Blockers

在以下事项完成前，V3 不得删除：

1. 当前 V3 工作树的核心文件快照和协议已冻结，但仍需生成可独立 replay 的 Golden Fixtures。
2. V4 缺少 `ProbeCoordinator`、`WandaActiveProbe` 和 `ProbeResult` 正式模块。
3. V4 缺少账号池迁移：在线状态、W+资格、Token 健康、risk、quota、失败次数、cooldown、选择顺序。
4. V4 缺少账号 Durable Lease 和同场次 Durable 串行/释放 pending 门禁。
5. V4 缺少独立 `ProbeOrder`，当前不能充分证明 Probe 与 TransactionState 隔离。
6. V4 缺少 Durable release state 和 Backend restart recovery。
7. V4 现有报价服务仍可能在只读模式下进入临时锁座探价路径。
8. V4 没有独立 `WANDA_ACTIVE_PROBE_ENABLED=false` 双层 Kill Switch。
9. V4 不能保证 Agent stale 后 Probe cleanup 独立继续完成。
10. V4 尚未把 Probe 原始会员价事实和 V4 Existing Pricing Engine 完全拆开；不得引入 V3 商业报价公式。
11. Legacy local gateway path 与 direct official path 的取消/状态确认语义尚未统一。
12. 尚未完成 Probe-only Golden Result 与 V4 Shadow Replay 一致性证明；不要求 V3/V4 最终商业报价一致。
13. 尚未完成真实 Provider Capture；本阶段不得用 mock 结果冒充真实验证。

## 20. M1 结论

M1 已完成：

```text
V3 Probe 核心实现定位
V3 核心依赖图
V3 输入与座位选择行为
V3 create/lock/activity/cancel/release 协议
V3 并发、账号、租约和配额盘点
V3 报价规则盘点
V3 文件、配置项和错误码清单
V4 已有能力与缺失能力映射
V4 Pricing Architecture 审计
V4 当前规则与生产配置来源审计
Migration Blockers
```

M1 未执行：

```text
真实 Probe
真实 create_order
真实 cancel
真实 Provider Capture
V4 Probe 代码迁移
V4 Golden Tests
V4 Shadow Replay
部署
V3 删除
```

下一步必须等待 M1 确认。确认后才可进入 M2；M2 仍应先实现离线协议、Golden Fixture 和 Kill Switch，默认保持：

```text
AGENT_HARNESS_READ_ONLY=true
WANDA_ACTIVE_PROBE_ENABLED=false
```
