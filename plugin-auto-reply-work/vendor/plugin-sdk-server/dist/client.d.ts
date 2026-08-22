import type { AddBlacklistRequest, AmapTipsQueryRequest, AmapTipsResponse, AnalyticsDateTypeQuery, DashboardCsOverviewResponse, DashboardFansInsightsResponse, DashboardFansSummaryResponse, DashboardOverviewResponse, PluginProductMetricsPage, ProductMetricsQuery, TenantDashboardOverviewResponse, CancelOrderRequest, CatalogCategoriesQuery, CatalogPropertyChildrenQuery, ChangeOrderPriceRequest, ChargeRequest, CaptureRequest, CheckBadwordsRequest, ConsumeQuotaRequest, CreateOrderRateRequest, DecideRefundRequest, EditOrderLogisticsRequest, EditProductRequest, EmojiLibraryView, ExtensionView, GrouponFreeShipRequest, GrouponFreeShipResult, KgraphRecommendRequest, ListImMessagesQuery, ListImSessionsQuery, ListOrderRatesQuery, ListProductsQuery, OpenProductCoinDeductionRequest, OpenProductGrouponRequest, OrderRechargeProcessRequest, PinSessionResult, PluginActionResult, PluginBizType, PluginBlacklistStatusView, PluginChargeResultView, PluginCaptureResultView, PluginFansPriceView, PluginImageUploadResult, PluginImImageUploadResult, PluginImMessagePage, PluginImSessionPage, PluginImSessionView, PluginMarketItemDetail, PluginMarketSearchPage, PluginOrderRateCreateResult, PluginOrderRatePage, PluginOrderRatesView, PluginOrderSensitiveInfoView, PluginOrderView, PluginNotificationRequest, PluginNotificationResult, PluginPolishAllRequest, PluginPolishAllResult, PluginPolishResult, PluginProductCoinDeductionView, PluginProductGrouponView, PluginProductPage, PluginProductPromotionResult, PluginProductPromotionState, PluginProductRechargeResult, PluginProductSkuListView, PluginProductView, PluginPublishResult, PluginRedFlowerResult, PluginRefundView, PluginRemindReceiptResult, PluginShopView, PluginSkuGrantView, PluginSubscriptionOrderPage, PluginSubscriptionView, SubscriptionOrdersQuery, PluginReleaseResultView, PluginReserveResultView, PluginWalletView, PluginXianyuBlacklistStatusView, ProductBadwordsCheckResponse, ProductCategoryNode, ProductIndustryCategory, ProductKgraphRecommendation, ProductPropertyOption, ProductPublishPhysicalRequest, ProductPublishVirtualRequest, ProductShelfRequest, PublishChannelCapabilityResponse, WebCategoryRecommendRequest, WebPropertyValueSearchRequest, WebPropertyValueSearchResult, WebRecommendResult, WebServiceCardsRequest, WebServiceCardsResponse, WebShopEditRequest, WebShopEditDetailView, QuotaState, RecordUsageRequest, ReleaseRequest, ReserveRequest, RecallImMessageRequest, RecallImMessageResult, RefundDecisionResult, SendImImageRequest, SendImMessageRequest, SendImMessageResult, SendOrderMessageRequest, SetFansPriceRequest, SetOrderMemoRequest, SetProductRechargeRequest, ShipOrderRequest, UploadImageByUrlRequest } from '@yumaiduo/plugin-contracts';
/** 写订单备注结果（PATCH /orders/:orderId/memo）。 */
export interface SetOrderMemoResult {
    ok: boolean;
    memo: string;
    memoGroup: number | null;
}
/** 同开发者广播发布入参（POST /broadcast/publish）。 */
export interface PublishBroadcastRequest {
    /** 广播主题（须在本插件 manifest.broadcast.publishes 中声明）。 */
    topic: string;
    /** 开发者自定义数据（受平台字节上限约束，不含 cookie/token 等凭证）。 */
    data: Record<string, unknown>;
    /** 业务幂等键（同 tenant+source+topic+idempotencyKey 重复发布会被去重）。 */
    idempotencyKey: string;
    /**
     * 链路深度（协作式防环）。基于收到的广播再次发布时，**必须透传** `payload.depth`，
     * 否则核心无法识别该调用来自广播回调。首次主动发布可省略（默认 0）。
     */
    depth?: number;
}
/** 广播发布结果：fanned 为真实落库（去重后）的目标数。 */
export interface PublishBroadcastResult {
    fanned: number;
}
/**
 * 最小 fetch 契约（避免强依赖 DOM/Node fetch 类型，便于测试注入）。
 *
 * body 为 unknown：JSON 请求传序列化字符串，图片直传（uploadImage）传 FormData。
 */
export type FetchLike = (input: string, init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: unknown;
}) => Promise<{
    ok: boolean;
    status: number;
    text(): Promise<string>;
}>;
/** 图片直传入参（Node 18+ / 浏览器均可用；data 为原始图片字节）。 */
export interface UploadImageFileArgs {
    /** 操作账号 unb（消耗该账号上游凭证，必须属于当前租户）。 */
    accountUnb: string;
    /** 文件名（含扩展名，如 cover.jpg）。 */
    filename: string;
    /** MIME 类型，仅支持 image/jpeg|png|webp|gif；缺省 image/jpeg。 */
    contentType?: string;
    /** 图片字节（单张 ≤ 5MB）。 */
    data: Uint8Array | ArrayBuffer;
}
/** IM 图片上传入参（im.uploadImage；data 为原始图片字节，单张 ≤ 5MB）。 */
export interface ImUploadImageArgs {
    /** 操作账号 unb（消耗该账号上游凭证，必须属于当前租户且已开启集成客服）。 */
    accountUnb: string;
    /** 文件名（含扩展名，如 reply.jpg）。 */
    filename: string;
    /** MIME 类型，仅支持 image/jpeg|png|webp|gif；缺省 image/jpeg。 */
    contentType?: string;
    /** 图片字节（单张 ≤ 5MB）。 */
    data: Uint8Array | ArrayBuffer;
}
/** 发布可选项。 */
export interface PublishOptions {
    /**
     * 幂等键（透传 X-Idempotency-Key，1~128 可见 ASCII 字符）。
     * 网络重试时带同一 key 可避免重复发布；强烈建议与请求体 outerId 搭配使用。
     */
    idempotencyKey?: string;
}
export interface PluginClientOptions {
    /** 核心基址，如 http://127.0.0.1:3000（尾部斜杠会被忽略）。 */
    coreUrl: string;
    /** 插件令牌（注册时核心下发），作为 X-Plugin-Token。 */
    pluginToken: string;
    /** 当前操作所属租户 id，作为 X-Yumaiduo-Tenant-Id（核心会校验「确已安装且启用」）。 */
    tenantId: string;
    /** 可注入 fetch（默认全局 fetch）；Node18+ 自带，老运行时可传 polyfill，测试可传桩。 */
    fetchImpl?: FetchLike;
}
/**
 * 插件回调核心的客户端句柄。所有方法自动带上 X-Plugin-Token + X-Yumaiduo-Tenant-Id，
 * 非 2xx 统一抛 {@link PluginApiError}。
 */
export interface PluginClient {
    quota: {
        /** 原子幂等消耗配额；idempotencyKey 通常用事件信封 id，保证重复投递不重复扣。 */
        consume(req: ConsumeQuotaRequest): Promise<QuotaState>;
    };
    broadcast: {
        /**
         * 向同开发者、订阅了该 topic 的兄弟插件广播一条事件（经核心中转）。
         * 仅投给本租户内已安装启用且可访问的兄弟插件；返回真实落库的目标数。
         */
        publish(req: PublishBroadcastRequest): Promise<PublishBroadcastResult>;
    };
    /**
     * 通过租户通知中心发送消息。需要 notify.send scope；网络重试必须复用 idempotencyKey。
     */
    sendNotification(req: PluginNotificationRequest): Promise<PluginNotificationResult>;
    usage: {
        /** 记一条计量流水（幂等，按 idempotencyKey 去重）。 */
        record(req: RecordUsageRequest): Promise<void>;
    };
    /** 按量计费资金包（阶段 3）：无需额外 scope，付费能力由 usage_based 定价承载。 */
    billing: {
        /**
         * 从租户资金包实时扣费（消费时确认收入）。meterKey 须带插件命名空间前缀，
         * 且对应在售 usage_based SKU（单价服务端权威），否则抛
         * PluginApiError(400, 'E_PLUGIN_METER_NOT_ACTIVE')。余额不足抛
         * PluginApiError(402, 'E_PLUGIN_BALANCE_INSUFFICIENT')；同 idempotencyKey 回放不重复扣减。
         */
        charge(req: ChargeRequest): Promise<PluginChargeResultView>;
        /**
         * 为 two_phase 计量项按最大单位数冻结资金；meterKey 无对应在售 SKU 时抛
         * PluginApiError(400, 'E_PLUGIN_METER_NOT_ACTIVE')。
         */
        reserve(req: ReserveRequest): Promise<PluginReserveResultView>;
        /**
         * 按实际单位数结算预授权，并释放未使用差额。预授权不存在时抛
         * PluginApiError(404, 'E_PLUGIN_RESERVATION_NOT_FOUND')；已结算、已释放或已过期时抛
         * PluginApiError(409, 'E_PLUGIN_RESERVATION_INVALID_STATE')；实际单位数超过预授权时抛
         * PluginApiError(400, 'E_PLUGIN_CAPTURE_EXCEEDS_RESERVATION')。
         */
        capture(req: CaptureRequest): Promise<PluginCaptureResultView>;
        /**
         * 主动释放尚未结算的预授权。预授权不存在时抛
         * PluginApiError(404, 'E_PLUGIN_RESERVATION_NOT_FOUND')；预授权已进入终态时抛
         * PluginApiError(409, 'E_PLUGIN_RESERVATION_INVALID_STATE')。
         */
        release(req: ReleaseRequest): Promise<PluginReleaseResultView>;
        /** 查询当前租户对本插件的资金包余额。 */
        balance(): Promise<PluginWalletView>;
        /**
         * freemium 内购：查询当前租户对本插件「已解锁的付费 SKU」。插件据此自门控高级功能
         * （返回列表中含某 skuCode 即已解锁）。无需额外 scope。
         */
        skus(): Promise<PluginSkuGrantView[]>;
        /**
         * 查询当前租户对本插件的订阅/购买详情（只读，无需额外 scope）。
         * 返回计费形态、订阅状态（active/in_trial/in_grace/…）、当前 SKU 与付费周期、
         * 本期起止（periodStart/periodEnd）与最近一笔已开通订单（orderId/paidAt），
         * 供续费提醒、周期差异化能力与对账锚点。免费/按量/freemium 插件恒 active，
         * 订单相关字段可能为 null。
         */
        subscription(): Promise<PluginSubscriptionView>;
        /**
         * 分页查询本租户对本插件的订阅/购买订单历史（仅已开通 applied 终态，支付时间倒序）。
         * 含首购与每次续费的订单号、SKU、周期、实付金额（分）与续期前后有效期。
         * 缺省 page=1 / pageSize=20（上限 100）。
         */
        subscriptionOrders(query?: SubscriptionOrdersQuery): Promise<PluginSubscriptionOrderPage>;
    };
    orders: {
        get(orderId: string): Promise<PluginOrderView>;
        /**
         * 读取单订单收件人姓名、手机号和地址。
         *
         * 需要 order.read + pii.read、开发者敏感信息认证及租户显式授权。
         * 仅返回核心镜像现值，不按需回源；尚未同步的字段为 null。
         * 开发者无归属、账号停用或认证未开启时抛
         * PluginApiError(403, 'E_PLUGIN_DEVELOPER_SENSITIVE_INFO_REQUIRED')。
         */
        getSensitiveInfo(orderId: string): Promise<PluginOrderSensitiveInfoView>;
        updateExtension(orderId: string, fields: Record<string, unknown>): Promise<ExtensionView>;
        /** 取消订单（scope: order.write.cancel）。 */
        cancel(orderId: string, req: CancelOrderRequest): Promise<PluginActionResult>;
        /** 实物发货（scope: order.write.ship）。 */
        ship(orderId: string, req: ShipOrderRequest): Promise<PluginActionResult>;
        /** 无物流发货（scope: order.write.ship）：虚拟商品/卡密/服务类订单，无需运单信息。 */
        fakeShip(orderId: string): Promise<PluginActionResult>;
        /**
         * 拼团免拼（scope: order.write.ship）：把「待刀成」订单推进到「待发货」。
         * req 可全省，核心会从拼团标记、订单 buyerUnb、IM 会话和受控 H5 回源补齐 buyerId。
         * 所有来源均缺失时返回 `failed + BUYER_ID_UNRESOLVED`，不会发送缺参上游请求。
         */
        grouponFreeShip(orderId: string, req?: GrouponFreeShipRequest): Promise<GrouponFreeShipResult>;
        /**
         * 直充订单充值状态推进（scope: order.write.recharge，高危）。
         *
         * - `state='start'`：开始充值（买家侧展示充值中）；
         * - `state='success'`：充值成功——闲鱼**直接完成发货**，无需再调 fakeShip；
         * - `state='fail'`：充值失败——闲鱼**自动向买家退款**（资金敏感，不可逆）。
         *
         * 是否直充单可用 `orders.get().rechargeOrder` 判断；买家充值账号经
         * `orders.getSensitiveInfo()` 读取（需另有 pii.read）。重复推进同一状态可能被
         * 闲鱼拒绝，插件应自行记录已推进到的状态。
         */
        rechargeProcess(orderId: string, req: OrderRechargeProcessRequest): Promise<PluginActionResult>;
        /** 修改物流单号（scope: order.write.ship）。 */
        editLogistics(orderId: string, req: EditOrderLogisticsRequest): Promise<PluginActionResult>;
        /** 改价，单位分（scope: order.write.price_change）。 */
        changePrice(orderId: string, req: ChangeOrderPriceRequest): Promise<PluginActionResult>;
        /** 写订单备注/分组色（scope: order.write.memo）。 */
        setMemo(orderId: string, req: SetOrderMemoRequest): Promise<SetOrderMemoResult>;
        /**
         * 求小红花（scope: order.rate.write，高危）：向订单买家发起闲鱼官方「小红花」求好评。
         * `idempotencyKey` **必传**（重试去重）；核心叠加订单维度状态位（30 天）+ 并发锁，
         * 重复请求返回 `alreadyRequested=true`（幂等短路，不二次触达买家）。
         */
        requestRedFlower(orderId: string, req: {
            idempotencyKey: string;
        }): Promise<PluginRedFlowerResult>;
        /**
         * 提醒买家收货（scope: order.write.ship）：给已发货订单的买家发一条闲鱼官方催收货提醒。
         * `idempotencyKey` **必传**（重试去重）；核心叠加订单维度 24 小时冷却 + 并发锁，
         * 冷却期内重复请求返回 `alreadyReminded=true`（不二次触达买家）。
         * 订单不是「已发货」状态时抛 `E_VALIDATION`。
         */
        remindReceipt(orderId: string, req: {
            idempotencyKey: string;
        }): Promise<PluginRemindReceiptResult>;
        /**
         * 评价列表（scope: order.read）：按**订单**分页，含买卖家均未评价的订单，
         * 每组分卖家 / 买家 / 角色未知三栏。缺省时间窗为最近 60 天。
         *
         * `sync=true` 会先触达闲鱼上游再读（重型、受服务端同步冷却约束，命中冷却回传
         * `cooldownActive`）；常规轮询请用缺省的 false 只读核心镜像。
         */
        listRates(query?: ListOrderRatesQuery): Promise<PluginOrderRatePage>;
        /**
         * 单订单评价（scope: order.read）：只读镜像不触发上游同步，附带本店主评 / 追评
         * 是否已发的状态位与求小红花状态位。发起评价前应先读此接口判重。
         */
        getRates(orderId: string): Promise<PluginOrderRatesView>;
        /**
         * 创建评价 / 追评（scope: order.rate.write，高危）：向买家发出闲鱼评价。
         *
         * 仅交易成功（`orderStatus=4`）的订单可评价；本店对同一订单同一 `rateType`
         * 只能发一次，重复提交会被核心拒绝（抛 PluginApiError，不是静默成功）——
         * 重试前先用 `getRates()` 读状态位。带 `imageUrls` 走 MTOP 图文通道，该通道失败
         * 时核心会丢弃图片回退中台文字通道。
         */
        createRate(orderId: string, req: CreateOrderRateRequest): Promise<PluginOrderRateCreateResult>;
        /**
         * 按订单号给买家发消息（scope: im.message.send）：走中台通道，用于无 IM 会话的冷订单触达。
         * 闲鱼限制每单 1~3 次；常规发消息请优先 im.getSessionByOrder + im.sendMessage。
         */
        sendMessage(orderId: string, req: SendOrderMessageRequest): Promise<PluginActionResult>;
        /** 读退款单（scope: order.refund.read）。 */
        getRefund(refundOrderId: string): Promise<PluginRefundView>;
        /**
         * 同意退款（scope: order.refund.decide）。
         *
         * `remark` **必填**：同意退款=资金流出（高危），核心强制留痕以便审计追责。
         * 省略 `remark` 会被服务端拒绝（400），故类型层要求必传，避免运行期报错。
         */
        agreeRefund(refundOrderId: string, req: DecideRefundRequest & {
            remark: string;
        }): Promise<RefundDecisionResult>;
        /**
         * 拒绝退款（scope: order.refund.decide）。
         *
         * `reason` **必填**（回写 `sellerRefuseReason`）：省略会被服务端拒绝（400），
         * 故类型层要求必传。
         */
        refuseRefund(refundOrderId: string, req: DecideRefundRequest & {
            reason: string;
        }): Promise<RefundDecisionResult>;
    };
    products: {
        /** 按店铺分页查询商品（scope: product.read）。accountUnb 必填。 */
        list(query: ListProductsQuery): Promise<PluginProductPage>;
        get(itemId: string): Promise<PluginProductView>;
        /**
         * 商品 SKU 镜像列表（scope: product.read）。全通道可用（含 middleware），
         * 只读镜像不回源；单品商品返回空 skus。金额为分字符串；新鲜度取决于最近一次
         * 商品同步（发布写骨架，MTOP 刷新补全价格/库存/粉丝价）。
         */
        getSkus(itemId: string): Promise<PluginProductSkuListView>;
        updateExtension(itemId: string, fields: Record<string, unknown>): Promise<ExtensionView>;
        /** 下架商品（scope: product.write.publish）。 */
        offShelf(itemId: string, req?: ProductShelfRequest): Promise<PluginActionResult>;
        /** 上架商品（scope: product.write.publish）。 */
        upShelf(itemId: string): Promise<PluginActionResult>;
        /**
         * 商品擦亮（scope: product.write.publish）：提升搜索/曝光排序，闲鱼限每天每商品一次。
         * 重复擦亮返回 `alreadyPolished=true`（闲鱼幂等回放，等价成功），插件可据此自然去重。
         */
        polish(itemId: string): Promise<PluginPolishResult>;
        /**
         * 全店一键擦亮（scope: product.write.publish）：一次调用擦亮该店铺全部在售商品，
         * 比逐个 `polish(itemId)` 省得多，也不占商品维度的每日额度。
         * 闲鱼限每店每天一次，服务端按天短路，重复调用返回 `alreadyPolished=true`。
         */
        polishAll(req: PluginPolishAllRequest): Promise<PluginPolishAllResult>;
        /** 编辑库存/价格（scope: product.write.update）。 */
        edit(itemId: string, req: EditProductRequest): Promise<PluginActionResult>;
        /**
         * 鱼小铺（web_shop）完整编辑回显（scope: product.write.update）。
         * web_shop 商品始终可用；web_personal 商品在所属店铺已开通鱼小铺时也可用；middleware 商品抛错。
         * 返回与 web 发布同构的回显视图，供回填后再 editWebShop 提交。
         */
        getWebEditDetail(itemId: string): Promise<WebShopEditDetailView>;
        /**
         * 鱼小铺（web_shop）完整编辑提交（scope: product.write.update）。
         * 适用 web_shop 商品，以及所属店铺已开通鱼小铺的 web_personal 商品（middleware 商品走中台 edit）。
         * 入参与 web 发布 DTO 同构（图片 / 类目 / CPV / 价格 / 库存 / 发货地 / 服务承诺）；
         * `labels[].transportData` 同发布：原样回灌推荐对象，核心自动补齐 `properties` / `text` /
         * `isUserClick`。核心组装 inputJson 走 MTOP 提交，不跨通道兜底。返回 publishChannel 为发布即固化的原通道（不翻转）。
         */
        editWebShop(itemId: string, req: WebShopEditRequest): Promise<PluginPublishResult>;
        /** 读粉丝价配置（scope: product.read）。 */
        getFansPrice(itemId: string): Promise<PluginFansPriceView>;
        /** 设置粉丝价（scope: product.write.update）。 */
        setFansPrice(itemId: string, req: SetFansPriceRequest): Promise<PluginFansPriceView>;
        /**
         * 商品直充打标（scope: product.write.recharge，高危）。
         *
         * 前置：卖家账号已开通闲鱼「放心充」资质且商品属卡券类目（由闲鱼中台裁决，不满足
         * 时上游报错）。打标后买家在下单页被强制按 `accountType`（1=微信 2=邮箱 3=QQ
         * 4=微博 5=陌陌 6=手机 7=平台账号）填写充值账号。核心即时写 `rechargeEnabled` 标记，
         * 并由中台 `templateExtraInfo` 在后续商品同步时权威收敛（闲鱼 App 侧改动会自动纠正）。
         */
        setRecharge(itemId: string, req: SetProductRechargeRequest): Promise<PluginProductRechargeResult>;
        /** 商品直充撤标（scope: product.write.recharge，高危）。 */
        removeRecharge(itemId: string): Promise<PluginProductRechargeResult>;
        /**
         * 商品营销开关（小刀活动 + 闲鱼币抵扣）。
         *
         * 整组能力经**闲鱼 App 网关**代发，除 scope 与租户显式授权外，
         * 还要求**插件所属开发者已通过平台的「App 网关能力认证」**，否则一律 403
         * `E_PLUGIN_DEVELOPER_APP_GATEWAY_REQUIRED`（与「订单敏感信息认证」相互独立）。
         *
         * 通道资源稀缺（全局并发默认 2、单店铺串行）：批量展示请直接读 `products.list` /
         * `products.get` 上的营销镜像字段（`grouponActive` / `grouponPriceFen` /
         * `coinDeductionRatio` / `promotionSyncedAt`），单品用 `promotion.get`；
         * 只在真正要操作前才调 `groupon.get` / `coinDeduction.get` 这两个实时读端点。
         */
        promotion: {
            /** 读营销状态镜像（scope: product.read；不打上游，可放心轮询）。 */
            get(itemId: string): Promise<PluginProductPromotionState>;
        };
        /** 小刀活动（团购）。 */
        groupon: {
            /**
             * 小刀活动实时详情（scope: product.read；打上游）。
             *
             * 用 `canGroupon` 判断能否开通；已开通商品该值为 false，展示逻辑请以 `active` 优先。
             */
            get(itemId: string): Promise<PluginProductGrouponView>;
            /**
             * 开通小刀活动（scope: product.write.promotion，高危）。
             *
             * `priceFen` 单位是**分**（核心负责换算成上游要的元字符串）。价格与份数的业务边界
             * （最低价、是否必须低于原价、份数上限、是否受库存约束）上游尚未确认，核心只做类型校验
             * 与 `canGroupon` 前置，闲鱼拒绝时透传原始失败原因——请勿在插件里写死未验证的规则。
             */
            open(itemId: string, req: OpenProductGrouponRequest): Promise<PluginProductPromotionResult>;
            /** 关闭小刀活动（scope: product.write.promotion，高危）。可逆，不是一次性操作。 */
            close(itemId: string): Promise<PluginProductPromotionResult>;
        };
        /** 闲鱼币抵扣。 */
        coinDeduction: {
            /**
             * 抵扣实时详情（scope: product.read；打上游）。
             *
             * `ratios` 是闲鱼动态下发的档位，**不要写死 20/30**；`agreementAgreed=false` 时开通会被
             * 拒绝，需引导商家到鱼麦多控制台由主账号 / 管理员确认协议（协议同意不开放给插件）。
             */
            get(itemId: string): Promise<PluginProductCoinDeductionView>;
            /**
             * 开通抵扣（scope: product.write.promotion，高危）。
             *
             * ⚠ 抵扣消耗卖家**真实闲鱼币余额**。`ratio` 必须取自 `get()` 返回的 `canOpen=true`
             * 档位，核心会在提交前实时复核。
             */
            open(itemId: string, req: OpenProductCoinDeductionRequest): Promise<PluginProductPromotionResult>;
            /** 关闭抵扣（scope: product.write.promotion，高危）。 */
            close(itemId: string): Promise<PluginProductPromotionResult>;
        };
        /**
         * 发布实物商品（scope: product.write.publish，高危）。
         * 金额单位为分；imageIds 来自 uploadImage / uploadImageByUrl 返回的 imageId。
         * web 通道（web_personal / web_shop）：`labels[].transportData` 原样传识图推荐返回的
         * 候选对象即可，核心在提交闲鱼前自动补齐官方 `itemLabelExtList` 的 `properties` / `text`
         * 并按规则覆盖 `isUserClick`（推荐接口不返回这三项），插件无需也不应自行拼这些字段。
         */
        publishPhysical(req: ProductPublishPhysicalRequest, opts?: PublishOptions): Promise<PluginPublishResult>;
        /** 发布虚拟/服务类商品（scope: product.write.publish，高危）。 */
        publishVirtual(req: ProductPublishVirtualRequest, opts?: PublishOptions): Promise<PluginPublishResult>;
        /** 公网图片 URL 转存为闲鱼 imageId（中台发布用；返回 mode='middleware'）。 */
        uploadImageByUrl(req: UploadImageByUrlRequest): Promise<PluginImageUploadResult>;
        /** 图片字节直传为闲鱼 imageId（中台发布用，multipart；返回 mode='middleware'）。 */
        uploadImage(args: UploadImageFileArgs): Promise<PluginImageUploadResult>;
        /**
         * 图片字节直传为闲鱼 CDN 直链 + 宽高（**web 个人 / 鱼小铺发布**用，multipart）。
         * 返回 mode='web'，无 imageId；web 发布 DTO 的图片项要的是 { url, width, height }。
         */
        uploadImageWeb(args: UploadImageFileArgs): Promise<PluginImageUploadResult>;
        /**
         * 探测账号在 middleware / web_personal / web_shop 三通道的可用性 + 推荐通道
         * （scope: product.read）。通道优先发布的第一步：据此选通道再发布。
         */
        publishChannels(accountUnb: string): Promise<PublishChannelCapabilityResponse>;
    };
    /** 发布前置查询（scope: product.read）。 */
    catalog: {
        /** 业务模式字典（实物发布 bizType）。 */
        bizTypes(): Promise<PluginBizType[]>;
        /** 虚拟商品行业类目（一选自动得 channelCatId/spBizType/categoryId 三件套）。 */
        industryCategories(): Promise<ProductIndustryCategory[]>;
        /** 类目树（不传 parentId = 行业根；下钻到 level=4 叶子才可发实物）。 */
        categories(query?: CatalogCategoriesQuery): Promise<ProductCategoryNode[]>;
        /** 单类目详情（不存在返回 null）。 */
        categoryInfo(categoryId: string): Promise<ProductCategoryNode | null>;
        /** 类目 PV 属性（一级）。 */
        properties(categoryId: string): Promise<ProductPropertyOption[]>;
        /** 类目 PV 子属性（categoryId/propertyId/valueId 三参必填）。 */
        propertyChildren(query: CatalogPropertyChildrenQuery): Promise<ProductPropertyOption[]>;
        /** 标题/图片智能推荐类目与 PV。 */
        kgraph(req: KgraphRecommendRequest): Promise<ProductKgraphRecommendation[]>;
        /** 标题/描述违规词预检（建议发布前调用，命中则阻断）。 */
        badwords(req: CheckBadwordsRequest): Promise<ProductBadwordsCheckResponse>;
        /**
         * web 通道类目 + CPV 推荐（web 无手动全量类目树，按图片 / 描述自动推荐）。
         * 仅 web 发布用；中台发布仍走 categories / properties 类目树。
         *
         * 切换类目刷新 CPV：首推后保存返回的 `rawCards`（上游原始卡片）；用户改选类目时，
         * 二次推荐回传 `priorCards: rawCards` + `selectedChannelCatId: 选中类目的 channelCatId`，
         * 由核心按官方结构重建上游 `currentCardList`（标记用户点选类目），拿到**该类目专属**的 CPV。
         * 旧的 `selectedCards` 已废弃（仅发单个分类卡，上游无法识别"用户改选类目"，CPV 不会刷新）。
         */
        webCategoryRecommend(req: WebCategoryRecommendRequest): Promise<WebRecommendResult>;
        /**
         * web 通道属性值搜索（「更多 / 搜索」）：按 channelCatId + propertyId 拉某属性的**全量**可选值。
         *
         * `webCategoryRecommend` 只返回各属性 TOP-N 候选；当目标值不在候选里时，用本方法按
         * `inputText` 搜索（留空拉首屏全量），选中后把返回值（含 transportData）作为该属性的
         * label 回灌发布，与推荐候选同结构、同发布链路。channelCatId 取已选类目，propertyId
         * 取推荐属性卡 `WebPropertyCard.propertyId`。
         */
        webPropertySearch(req: WebPropertyValueSearchRequest): Promise<WebPropertyValueSearchResult>;
        /** web 通道服务承诺卡片（选定类目后回传已选卡，拉取适配服务承诺；小刀活动等载体）。 */
        webServiceCards(req: WebServiceCardsRequest): Promise<WebServiceCardsResponse>;
    };
    /** 地区查询（scope: product.read）。 */
    locations: {
        /** 地址联想（命中项的 adcode 即发布入参 divisionId）。 */
        amapTips(query: AmapTipsQueryRequest): Promise<AmapTipsResponse>;
    };
    im: {
        /**
         * 按订单号反查最近会话（orderId = 订单卡消息写入会话的交易订单号）；无会话返回 null。
         * 返回的 accountUnb/chatId/peerUnb 即 sendMessage/sendImage 所需寻址三件套。
         */
        getSessionByOrder(orderId: string): Promise<PluginImSessionView | null>;
        /**
         * 按 (accountUnb, peerUnb) 精确反查最近买家单聊会话（scope: im.session.read，只读）。
         * 用于只有「账号 unb + 买家 unb」时拿 chatId 拼集成客服深链
         * `/console/im?accountUnb=&chatId=`（经 web SDK navigate 跳转）。
         * 同买家多条会话取 lastMessageAt 最新；无会话 / 非本租户账号统一返回 null。
         */
        getSessionByPeer(accountUnb: string, peerUnb: string): Promise<PluginImSessionView | null>;
        /**
         * 按店铺分页列会话（scope: im.session.read，只读）。
         * 供群发类插件按店铺枚举"历史聊过天的买家会话"作为收件人；仅含买家单聊会话。
         * pageSize 上限 100，需遍历全部时自行按 page 翻页直到取满 total。
         */
        listSessions(query: ListImSessionsQuery): Promise<PluginImSessionPage>;
        /**
         * 读会话消息历史（scope: im.message.read，独立高危权限，租户需显式勾选）。
         *
         * 含本店发出的消息（商家手机端 / 控制台人工 / 其它插件）。AI 客服在真正调
         * sendMessage 前应当用它复核最新一条的 direction——只靠「x 秒没收到事件」判断
         * 人工未介入会和商家手机端撞车。items 按时间倒序，最新一条在 items[0]。
         * 翻页：首次不传 beforeMs，之后回传上一页的 nextBeforeMs，直到它为 null。
         */
        listMessages(query: ListImMessagesQuery): Promise<PluginImMessagePage>;
        /** 给买家发文本消息（出站过核心敏感词审核 + leader WS 下发）。 */
        sendMessage(req: SendImMessageRequest): Promise<SendImMessageResult>;
        /**
         * 撤回本店发出的消息（scope: im.message.recall，高危）。messageId 取自发送结果 /
         * 消息历史条目；只能撤回 direction=outbound 的消息，闲鱼有撤回时间窗，超窗被拒返回
         * E_IM_MESSAGE_RECALL_FAILED。幂等：重复撤回返回首次撤回时间。
         */
        recallMessage(req: RecallImMessageRequest): Promise<RecallImMessageResult>;
        /**
         * 给买家发图片消息（经 leader WS 下发）。imageUrl 须为闲鱼 CDN 图片直链
         * （闲鱼仅渲染自家 CDN，外链不展示）+ 像素宽高，通常由 uploadImage 上传获得。
         */
        sendImage(req: SendImImageRequest): Promise<SendImMessageResult>;
        /**
         * 上传图片到闲鱼 CDN（scope: im.message.send），返回 { imageUrl, width, height } 不发消息。
         * 闲鱼发图只认自家 CDN 直链；上传一次后应按图片内容缓存复用,再用返回值调 sendImage,
         * 避免同一固定图片重复上传。需运行时支持 FormData/Blob（Node 18+）。
         */
        uploadImage(args: ImUploadImageArgs): Promise<PluginImImageUploadResult>;
        /** 置顶会话（scope: im.message.pin）。 */
        pinSession(accountUnb: string, chatId: string): Promise<PinSessionResult>;
        /** 取消置顶会话（scope: im.message.pin）。 */
        unpinSession(accountUnb: string, chatId: string): Promise<PinSessionResult>;
        /** 读表情库（scope: im.read）。 */
        getEmojiLibrary(): Promise<EmojiLibraryView>;
        /**
         * 买家黑名单（鱼麦多平台内部标记，按 (tenantId, peerUnb) 唯一）。
         *
         * 仅平台内部标记/提醒，不联动闲鱼平台侧真拉黑，也不阻止消息或自动回复。
         */
        blacklist: {
            /** 查询买家黑名单状态（scope: im.buyer_blacklist.read）；未拉黑返回 blacklisted=false。 */
            query(peerUnb: string): Promise<PluginBlacklistStatusView>;
            /**
             * 添加买家黑名单（scope: im.buyer_blacklist.write）。
             * 幂等：同一 peerUnb 重复添加不产生多条记录（upsert，保留首次拉黑时间）。
             */
            add(peerUnb: string, body?: AddBlacklistRequest): Promise<PluginBlacklistStatusView>;
            /** 移出买家黑名单（scope: im.buyer_blacklist.write）；幂等，移出不存在的买家也返回 ok。 */
            remove(peerUnb: string): Promise<{
                ok: true;
            }>;
        };
        /**
         * 闲鱼官方 IM 黑名单（**平台侧真拉黑**，与上面的内部标记完全不同）。
         *
         * 拉黑后买家无法再给该店铺发消息。寻址 accountUnb + chatId（会话归属核心前置校验）；
         * 闲鱼上游对重复拉黑/移除均幂等。高危 scope，需租户显式同意 + 人工评审。
         */
        xianyuBlacklist: {
            /** 查询会话对方是否在闲鱼官方黑名单（scope: im.xianyu_blacklist.read）。 */
            query(accountUnb: string, chatId: string): Promise<PluginXianyuBlacklistStatusView>;
            /** 闲鱼官方拉黑（scope: im.xianyu_blacklist.write，高危）。 */
            add(accountUnb: string, chatId: string): Promise<PluginXianyuBlacklistStatusView>;
            /** 取消闲鱼官方拉黑（scope: im.xianyu_blacklist.write，高危）。 */
            remove(accountUnb: string, chatId: string): Promise<PluginXianyuBlacklistStatusView>;
        };
    };
    /** 店铺只读（scope: account.read）。 */
    shops: {
        /** 当前租户全部店铺。 */
        list(): Promise<PluginShopView[]>;
        /** 单店铺详情（unb = 订单/商品/会话上的 accountUnb）。 */
        get(unb: string): Promise<PluginShopView>;
    };
    /**
     * 经营数据只读（T+1 离线快照，不触发实时拉取；响应透传 syncedAt）。
     *
     * 基础数据走 scope `report.read`；粉丝画像走 `report.advanced`（高危）且额外受租户
     * `feature.report.advanced` 套餐门控（不满足返回 403）。日粒度数据建议本地缓存当天结果、
     * 配 cron 在 09:30 后触发，避免读到旧快照或撞调用流控。
     */
    analytics: {
        /** 店铺交易/流量总览 KPI + trend（scope: report.read）。 */
        shopOverview(unb: string, query?: AnalyticsDateTypeQuery): Promise<DashboardOverviewResponse>;
        /** 粉丝总量/新增 + trend（scope: report.read）。 */
        fansSummary(unb: string, query?: AnalyticsDateTypeQuery): Promise<DashboardFansSummaryResponse>;
        /** 客服咨询/回复率总览（scope: report.read）。 */
        csOverview(unb: string, query?: AnalyticsDateTypeQuery): Promise<DashboardCsOverviewResponse>;
        /** 商品罗盘 item 运营指标，读落库快照（scope: report.read）。accountUnb 必填。 */
        productMetrics(query: ProductMetricsQuery): Promise<PluginProductMetricsPage>;
        /** 跨店聚合 KPI + 4 维 Top10 排行 + alerts（scope: report.read）。 */
        tenantOverview(query?: AnalyticsDateTypeQuery): Promise<TenantDashboardOverviewResponse>;
        /**
         * 粉丝画像（性别/省份/八大类/粉丝类型占比，仅昨日单日，无 dateType）。
         * scope: report.advanced（高危）+ 租户需具备 feature.report.advanced。
         */
        fansInsights(unb: string): Promise<DashboardFansInsightsResponse>;
    };
    /**
     * 全网市场数据：关键词搜索 + 竞品详情（scope: market.read，高危）。
     *
     * 与营销开关同走**闲鱼 App 网关**，同样要求**开发者已通过「App 网关能力认证」**，
     * 否则 403 `E_PLUGIN_DEVELOPER_APP_GATEWAY_REQUIRED`。接入前请注意：
     *
     * - **借的是商家的店铺账号**：请求带该账号的登录态、设备与定位特征，风控代价由商家承担。
     *   请在产品说明里建议商家配**专用账号**做选品，不要用主力店铺。
     * - **配额紧且两个方法共用**：按「租户 + 插件」计当日总量（默认 200 次），
     *   超出 429 `E_PLUGIN_RATE_LIMITED`。
     * - **同账号请求被强制拉开间隔**（默认 3 秒 + 抖动）：翻页请**顺序**发起，
     *   并发多页只会先撞排队上限被拒。
     * - **命中风控立即熔断该账号**：429 `E_XIANYU_APP_GATEWAY_RISK_CONTROL`，
     *   冷却期内该账号一律拒绝。收到就该停手换账号，**不要重试**。
     */
    market: {
        /**
         * 关键词搜索全网商品。
         *
         * @param accountUnb 借用哪个店铺的会话发起搜索，必须是本租户已接入且启用的店铺
         * @param query `page` 从 1 起；`pageSize` 默认 20、上限 30
         */
        search(accountUnb: string, query: {
            keyword: string;
            page?: number;
            pageSize?: number;
        }): Promise<PluginMarketSearchPage>;
        /** 竞品详情。商品不存在或已下架时 404 `E_XY_PRODUCT_NOT_FOUND`。 */
        getItem(accountUnb: string, itemId: string): Promise<PluginMarketItemDetail>;
    };
}
/** 创建插件回调客户端（无状态，可在事件 handler 内随用随建或复用）。 */
export declare function createPluginClient(opts: PluginClientOptions): PluginClient;
//# sourceMappingURL=client.d.ts.map