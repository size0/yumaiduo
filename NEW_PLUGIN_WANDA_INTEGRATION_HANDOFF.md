# 新插件接入万达报价能力：AI 开发交接书

> 用途：把本文件直接交给另一个 AI。先完成本地架构、契约和测试，不得直接部署生产，不得操作真实订单，不得重放历史事件。

## 一、目标

在一个新的鱼麦多插件中接入现有 V4 的万达识别和权威报价能力，同时解决以下缺口：

1. 万达账号池尚未接入新主程序。
2. W+ 会员价探针尚未依赖注入。
3. 万达认证 Header、签名和最终下单端口缺少真实但脱敏的正式契约。
4. 本地修复尚未部署，生产仍可能运行旧构建。

插件只负责平台接入、验签、事件持久化和动作执行。万达账号、签名、场次匹配、实时座位、W+ 探针、报价规则和订单安全必须留在受控后端，不得把万达 Token、签名密钥或原始账号对象放进插件、浏览器或业务事件载荷。

## 二、必须先阅读的现有实现

### V4 后端

- `E:/鱼麦多/v4/app/service.py`
  - 图片校验、Qwen 视觉调用、二次解释、`MovieImageInfo` 严格校验。
- `E:/鱼麦多/v4/app/models.py`
  - `MovieImageInfo`、`RealQuote`、`PricingRulesUpdate`。
- `E:/鱼麦多/v4/app/wanda_direct_quote.py`
  - 账号筛选、影院匹配、官方场次、实时座位、W+ 探针、取消和释放确认、确定性报价。
- `E:/鱼麦多/v4/app/pricing_store.py`
  - 报价规则持久化和版本化。
- `E:/鱼麦多/v4/app/plugin_automation.py`
  - 报价决策、报价记录、订单绑定和动作生成。
- `E:/鱼麦多/v4/app/rules_first_runtime.py`
  - 持久 Event Inbox / Command Outbox。

### 现有插件边界

- `E:/票务系统/plugins/wanda-seat-autoquote/docs/V2_ARCHITECTURE.md`
- `E:/票务系统/plugins/wanda-seat-autoquote/src/platform/runtime.mjs`
- `E:/票务系统/plugins/wanda-seat-autoquote/src/backend/client.mjs`
- `E:/票务系统/plugins/wanda-seat-autoquote/src/runtime/event-processor.mjs`
- `E:/票务系统/plugins/wanda-seat-autoquote/src/actions/contracts.mjs`
- `E:/票务系统/plugins/wanda-seat-autoquote/src/actions/change-order-price.mjs`

现有生产插件 ID 是 `wanda-seat-autoquote`。新插件必须使用自己的唯一 ID、独立端口、独立数据目录、独立开发者 Token、独立 Webhook Secret、独立加密密钥和独立幂等命名空间，不得复用该插件的数据目录或事件库。

## 当前生产核验补充（只读）

- `WANDA_ACCOUNT_POOL_PATH` 当前指向：`/var/lib/ticket-system/backend-data/accounts.json`，这是出票系统后端账号池，不是插件自己的账号池。
- 当前账号池共 15 个账号，其中 14 个 `online`、1 个 `offline`；可用账号均有 Token、W+ 状态和 `userIdentifier`。
- `WANDA_FIXED_ACCOUNT_PHONE` 当前未配置，因此 V4 会按账号池文件顺序选择第一个满足以下条件的账号：`online`、Token 非空、手机号非空、W+ 有效。不是轮询，也不是按租户自动分配。
- `WANDA_CINEMA_CACHE_PATH` 指向官方影院缓存 SQLite；`WANDA_QUOTE_RECORDS_PATH` 指向加密报价记录。
- V4 当前没有“最终万达出票/购票”接口。`/order/create_order.api` 仅用于 W+ 会员价临时探针，随后必须取消并验证释放；不能当作最终出票接口。
- 当前同一出票账号会用于万达官方场次、实时座位、W+ 活动和临时探针请求。鱼麦多平台订单、消息和改价则走插件 SDK 的平台账号/会话，不使用万达账号池 Token。
- 当前生产价格规则运行时为：普通调整 `+1.00元`、W+阈值 `60.00元`、W+调整 `-2.90元`、按 `0.10元`取整；持久文件中的正 `290` 会在运行时规范化为 `-290`。`wplus_friday_member_day_enabled` 因旧配置缺省按 `true` 读取，当前代码以 W+活动价优先路径处理，并未在代码中额外判断自然日是否为周五，接入新插件时必须单独确认这一语义。

## 三、推荐架构

```text
鱼麦多平台
  -> 新插件（注册、Webhook验签、事件持久化、SDK执行器）
  -> 内部报价网关（HMAC/共享密钥、租户绑定、幂等）
  -> WandaQuoteService
       -> AccountProvider
       -> WandaOfficialClient
       -> WPlusProbe
       -> PricingPolicy
       -> QuoteRepository
  -> Command Outbox
  -> 新插件领取命令并执行平台动作
```

### 禁止架构

- 禁止在插件 UI 或浏览器中保存万达 Token。
- 禁止插件直接读取主程序账号 JSON 文件。
- 禁止把签名密钥写入 manifest、前端代码、日志或测试 fixture。
- 禁止让模型直接调用万达下单接口。
- 禁止把“报价、临时探针、最终下单”合并成一个通用工具。
- 禁止插件根据模型金额直接改价。
- 禁止收到 HTTP 超时后盲目重试创建订单、取消订单或最终下单。

## 四、必须定义的依赖注入接口

建议采用 TypeScript interface、Python Protocol 或等价的窄接口。

### 1. `AccountProvider`

```ts
interface AccountProvider {
  getFixedWplusAccount(input: {
    tenantId: string;
    purpose: 'showtime' | 'realtime_seat' | 'member_probe' | 'final_order';
  }): Promise<AccountLease>;
}

interface AccountLease {
  accountRef: string;
  phoneMasked: string;
  expiresAt: string | null;
  withCredentials<T>(fn: (credential: WandaCredential) => Promise<T>): Promise<T>;
  release(): Promise<void>;
}
```

要求：

- 只选择 `online`、Token 非空、固定手机号匹配、W+ 有效的账号。
- 调用者通常只能看到 `accountRef` 和掩码手机号。
- `WandaCredential` 不得序列化、不得日志输出、不得越过报价后端边界。
- 账号租约必须 `finally` 释放。
- 多账号时必须有确定性选择和并发上限，不能随机切换导致价格证据漂移。

### 2. `WandaAuthSigner`

```ts
interface WandaAuthSigner {
  sign(input: {
    channel: 'h5' | 'app';
    method: 'GET' | 'POST';
    origin: string;
    path: string;
    queryPairs: Array<[string, string]>;
    bodyPairs: Array<[string, string]>;
    timestampMs: number;
    credential: WandaCredential;
    signEncodedBody?: boolean;
  }): Promise<{
    url: string;
    headers: Record<string, string>;
    body?: string;
  }>;
}
```

契约必须包括：

- host/path 白名单；
- HTTPS 强制；
- 参数顺序；
- GET query 与 POST body 的签名串区别；
- URL 编码规则及百分号大小写；
- 时间戳单位；
- H5/APP channel 区别；
- `MX-API` 和 `X-RY-*` Header 的字段集合；
- Token、用户标识、设备指纹字段的脱敏规则。

使用 `app/wanda_direct_quote.py:_official_get()` 和 `_official_app_request()` 作为当前实现依据，但不要把生产密钥复制进文档或测试。新增固定时间戳＋假 Token＋假 client key 的 golden test vector。

### 3. `WandaOfficialClient`

拆分为窄方法，禁止一个 `requestAnything()`：

```ts
interface WandaOfficialClient {
  getShowtimes(input: {
    account: AccountLease;
    cinemaId: string;
    showDate: string; // YYYYMMDD
  }): Promise<OfficialShowtimeResponse>;

  getRealtimeSeats(input: {
    account: AccountLease;
    showtimeId: string;
  }): Promise<OfficialSeatResponse>;

  createProbeOrder(input: ProbeCreateInput): Promise<ProbeOrderReceipt>;
  getOrderStatus(input: OfficialOrderStatusInput): Promise<OfficialOrderStatus>;
  getMemberOffers(input: MemberOfferInput): Promise<MemberOfferResponse>;
  cancelOrder(input: OfficialCancelInput): Promise<OfficialCancelReceipt>;
}
```

当前已验证的官方能力路径：

- `GET /showtime/by_cinema.api`
- `GET /order/real_time_seat.api`
- `POST /order/create_order.api`
- `POST /order/order_status.api`
- `GET /mkt/activity/secret/list.api`
- `POST /order/cancel.api`

不能在没有脱敏抓包契约、字段说明和测试的情况下猜测新增接口。

### 4. `WPlusProbe`

```ts
interface WPlusProbe {
  probeMemberPrice(input: {
    tenantId: string;
    cinemaId: string;
    showtimeId: string;
    seatId: string;
    areaId: string;
    originalPriceCents: number;
    channelFeeCents: number;
    idempotencyKey: string;
  }): Promise<ToolResult<MemberPriceEvidence>>;
}
```

探针状态机必须是：

```text
获取同场次互斥锁
  -> 创建临时订单
  -> 回读 order_status，确认锁座状态
  -> 查询唯一 W+会员专享价格
  -> finally 取消临时订单
  -> 回读订单已取消
  -> 回读实时座位已恢复可售
  -> 两项都确认后才返回价格
```

硬性要求：

- 每个 `showtimeId` 串行探针。
- `finally` 中取消，取消过程使用受控 shield/不可被普通取消中断。
- 创建结果未知时先按幂等证据对账，不得重复创建。
- 取消成功但座位释放未确认时返回 `wanda_temporary_lock_release_unverified`，不得返回报价。
- 只能接受唯一且 `able=true`、名称为 W+会员专享、金额为正数的活动价。
- 探针订单和最终客户订单使用完全不同的类型、端口、权限和幂等命名空间。

### 5. `FinalOrderPort`

最终下单属于高风险写操作，必须与探针分离：

```ts
interface FinalOrderPort {
  createFinalOrder(input: FinalOrderCommand): Promise<ToolResult<FinalOrderReceipt>>;
}
```

在真实脱敏契约未齐全前：

```text
WANDA_FINAL_ORDER_ENABLED=false
```

实现只能返回：

```json
{
  "status": "warning",
  "summary": "final order port is not configured",
  "next_actions": ["provide sanitized official request/response contract"],
  "artifacts": [],
  "code": "final_order_port_unconfigured"
}
```

不得根据现有临时 `create_order.api` 自动推断最终下单参数。最终下单至少需要：请求字段、价格组成、优惠选择、联系人/手机号边界、幂等方式、成功状态、结果未知对账、取消边界和脱敏响应样本。

## 五、插件到报价网关的正式契约

### 请求

```json
{
  "schema_version": "wanda.quote.request.v1",
  "request_id": "stable-event-or-job-id",
  "tenant_id": "107",
  "shop_id": "2313315754",
  "buyer_id": "optional-bound-buyer-id",
  "chat_id": "optional-bound-chat-id",
  "recognition": {
    "city": "佛山",
    "cinema_name": "南海万达广场店",
    "movie_name": "奥德赛",
    "date": "2026-08-27",
    "showtime_start": "23:00",
    "showtime_end": null,
    "hall_name": null,
    "selected_seats": ["9排20座"],
    "ticket_count": 1
  }
}
```

规则：

- 模型只能提供截图事实，不能提供目标金额。
- `request_id + tenant_id` 必须幂等。
- tenant/shop/buyer/chat 绑定不能由模型填写。
- 所有金额使用整数分。

### 响应

```json
{
  "status": "success",
  "summary": "authoritative exact-seat quote completed",
  "next_actions": [],
  "artifacts": ["quote:q-..."],
  "data": {
    "schema_version": "wanda.quote.result.v1",
    "quote_id": "q-...",
    "quote_scope": "exact_seats",
    "matched_cinema_id": "...",
    "matched_cinema_name": "...",
    "matched_movie_name": "...",
    "matched_showtime_id": "...",
    "matched_showtime_start": "23:00",
    "seat_quotes": [
      {
        "seat_number": "9排20座",
        "area_name": "按摩椅区",
        "available": true,
        "original_price_cents": 8890,
        "member_price_cents": 7676,
        "unit_quote_cents": 7680
      }
    ],
    "ticket_count": 1,
    "total_quote_cents": 7680,
    "pricing_rule_version": "pricing-...",
    "expires_at": "ISO-8601"
  }
}
```

失败返回也保持固定观察格式：

```json
{
  "status": "warning",
  "summary": "showtime match is not unique",
  "next_actions": ["ask buyer for the exact hall"],
  "artifacts": [],
  "code": "wanda_showtime_not_unique"
}
```

不要把官方原始响应、Token、Header、收件人姓名、电话、地址或买家昵称返回给插件。

## 六、报价规则

以当前 V4 为准：

```text
普通座：会员价 + regular_adjustment_cents
W+且会员价 <= 阈值：max(原价 + wplus_adjustment_cents, 会员价)
W+且会员价 > 阈值：会员价
最后按 rounding_increment_cents 轮整
最终不得低于会员成本，不得高于官方原价
channelFee 记录为证据，但不加入当前客户报价
```

当前规则示例：

```json
{
  "regular_adjustment_cents": 100,
  "wplus_member_price_threshold_cents": 6000,
  "wplus_adjustment_cents": -290,
  "rounding_increment_cents": 10
}
```

不得把这些值硬编码进插件；由后端版本化策略提供，并在报价记录保存 `pricing_rule_version`。

## 七、分阶段实现顺序

### Phase 0：只读盘点

- 找出新插件目录、manifest、入口、SDK版本、事件存储和现有测试。
- 输出数据流和威胁模型。
- 不修改生产配置，不部署。

### Phase 1：契约和 mock

- 先写 `AccountProvider`、`WandaAuthSigner`、`WandaOfficialClient`、`WPlusProbe`、`FinalOrderPort` 接口。
- 建立脱敏 fixture 和 golden signature vectors。
- 所有真实能力开关默认关闭。

### Phase 2：只读官方报价

- 接入账号 provider。
- 只开放场次和实时座位 GET。
- 完成影院/场次唯一匹配、实时座位、`settlePrice` 和 `wPlusActivity.price`。
- 歧义全部 fail closed。

### Phase 3：W+ 探针

- 使用 mock 和沙箱先完成探针状态机。
- 验证每条失败路径都会取消并核验释放。
- 单独显式授权后才能连接真实账号探针。

### Phase 4：插件动作链路

- Webhook 验签后先持久化，再返回 `202`。
- 后端写 Command Outbox，插件使用租约 claim。
- 执行前重新读取会话和订单。
- 结果未知先对账，不盲目重写。

### Phase 5：最终下单

- 只有收到真实脱敏请求/响应契约、权限说明和联调账号后才能开始。
- 默认关闭；不得与报价部署捆绑启用。

### Phase 6：部署

- 本地单测、集成测试、安全测试、故障注入和包检查全部通过。
- 取得新的明确生产部署授权。
- 备份数据和环境文件，原子切换，双服务健康检查，保留回滚目标。

## 八、必须通过的测试

1. 账号离线、Token缺失、W+过期时失败关闭。
2. Header 和签名固定向量测试；参数顺序变化能被检测。
3. host/path 白名单测试，SSRF 测试。
4. 影院唯一、影院歧义、影片缺失但官方唯一补全。
5. 同时间多场次无影厅时返回 `wanda_showtime_not_unique`。
6. 明确座位精确匹配，不用其他 W+区域覆盖。
7. 普通区拿 `settlePrice`；有 `wPlusActivity.price` 时按W+资格计价但保留真实物理区域名。
8. 不可售座位不得替换；同类型参考价不可形成可确认报价。
9. 探针创建成功后的每条异常路径都会取消并验证释放。
10. 探针超时/未知结果不重复创建。
11. 日志、错误、持久化文件不含 Token、Authorization、完整手机号、地址和原始订单对象。
12. 插件重启后事件、命令和长任务可恢复。
13. tenant/shop/buyer/chat/order/quote 任一绑定不一致时禁止写操作。
14. 最终下单开关关闭时没有任何真实下单调用。
15. 至少包含一次全链路 mock E2E：事件 -> 报价 -> 命令 -> 插件回执。

## 九、功能开关

新插件初始必须保持：

```text
WANDA_QUOTE_ENABLED=false
WANDA_PROBE_ENABLED=false
WANDA_FINAL_ORDER_ENABLED=false
EXTERNAL_WRITES_ENABLED=false
```

建议按顺序开启：

```text
只读报价 -> 单测试租户 -> W+探针 -> 平台消息 -> 改价 -> 最终下单
```

每一步单独授权，不得一次性全部开启。

## 十、交付物

另一个 AI 完成后必须提交：

1. 架构图和边界说明。
2. 五个窄接口及 JSON schema。
3. 脱敏官方请求/响应契约。
4. golden signature vectors。
5. mock 官方服务。
6. 单元、集成、安全和恢复测试报告。
7. 未完成项列表，尤其是最终下单契约。
8. 部署清单和回滚方案，但不得自行部署。
9. 明确声明是否触碰过真实账号、真实探针、真实订单或历史事件。

## 十一、给开发 AI 的最终指令

先读取上述文件和新插件实际代码，再输出实施计划与风险清单；计划确认前不要改代码。实现时优先复用 V4 的已验证规则和失败关闭语义，不复制生产秘密，不推测未知接口。任何真实 W+ 探针、最终下单、生产配置保存、生产部署、历史事件回放或真实订单操作，都必须停下并请求新的明确授权。
