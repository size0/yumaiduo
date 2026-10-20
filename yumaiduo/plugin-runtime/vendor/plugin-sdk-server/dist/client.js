"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.createPluginClient = createPluginClient;
const json_bigint_1 = __importDefault(require("json-bigint"));
const errors_1 = require("./errors");
// 大整数安全：核心 { code, data } 信封里的 orderId / itemId 可能是 19 位 Long，
// 原生 JSON.parse 会截断；storeAsString 保证超大整数解析为字符串。
const jsonBigParser = (0, json_bigint_1.default)({ storeAsString: true, useNativeBigInt: false });
/** 创建插件回调客户端（无状态，可在事件 handler 内随用随建或复用）。 */
function createPluginClient(opts) {
    const base = opts.coreUrl.replace(/\/+$/, '');
    const doFetch = opts.fetchImpl ?? globalThis.fetch;
    if (typeof doFetch !== 'function') {
        throw new Error('未找到可用的 fetch；请在 Node 18+ 运行，或通过 fetchImpl 传入实现');
    }
    const fetchFn = doFetch;
    async function request(method, path, body, extraHeaders) {
        // FormData（图片直传）由 fetch 自动设置 multipart 边界，不可手动指定 content-type
        const isForm = typeof FormData !== 'undefined' && body instanceof FormData;
        const res = await fetchFn(`${base}/api/v1/plugin${path}`, {
            method,
            headers: {
                ...(isForm ? {} : { 'content-type': 'application/json' }),
                'x-plugin-token': opts.pluginToken,
                'x-yumaiduo-tenant-id': opts.tenantId,
                ...(extraHeaders ?? {}),
            },
            body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
        });
        const text = await res.text();
        const parsed = text ? safeJson(text) : undefined;
        // 核心统一响应外壳：成功 { code:0, message:'ok', data }；失败 { code, message, data:null, errorCode }
        const envelope = (parsed ?? {});
        if (!res.ok) {
            throw new errors_1.PluginApiError(res.status, envelope.errorCode, envelope.message ?? `核心返回 ${res.status}`, parsed);
        }
        return envelope.data;
    }
    return {
        quota: {
            consume: (req) => request('POST', '/quota/consume', req),
        },
        broadcast: {
            publish: (req) => request('POST', '/broadcast/publish', req),
        },
        sendNotification: (req) => request('POST', '/notify', req),
        usage: {
            record: async (req) => {
                await request('POST', '/usage', req);
            },
        },
        billing: {
            charge: (req) => request('POST', '/billing/charge', req),
            reserve: (req) => request('POST', '/billing/reserve', req),
            capture: (req) => request('POST', '/billing/capture', req),
            release: (req) => request('POST', '/billing/release', req),
            balance: () => request('GET', '/billing/balance'),
            skus: () => request('GET', '/billing/skus'),
            subscription: () => request('GET', '/billing/subscription'),
            subscriptionOrders: (query) => request('GET', `/billing/subscription/orders${toQueryString(query)}`),
        },
        orders: {
            get: (orderId) => request('GET', `/orders/${encodeURIComponent(orderId)}`),
            getSensitiveInfo: (orderId) => request('GET', `/orders/${encodeURIComponent(orderId)}/sensitive-info`),
            updateExtension: (orderId, fields) => request('PUT', `/orders/${encodeURIComponent(orderId)}/extension`, { fields }),
            cancel: (orderId, req) => request('POST', `/orders/${encodeURIComponent(orderId)}/cancel`, req),
            ship: (orderId, req) => request('POST', `/orders/${encodeURIComponent(orderId)}/ship`, req),
            fakeShip: (orderId) => request('POST', `/orders/${encodeURIComponent(orderId)}/fake-ship`, {}),
            grouponFreeShip: (orderId, req) => request('POST', `/orders/${encodeURIComponent(orderId)}/groupon/free-ship`, req ?? {}),
            rechargeProcess: (orderId, req) => request('POST', `/orders/${encodeURIComponent(orderId)}/recharge/process`, req),
            editLogistics: (orderId, req) => request('PATCH', `/orders/${encodeURIComponent(orderId)}/logistics`, req),
            changePrice: (orderId, req) => request('PATCH', `/orders/${encodeURIComponent(orderId)}/price`, req),
            setMemo: (orderId, req) => request('PATCH', `/orders/${encodeURIComponent(orderId)}/memo`, req),
            requestRedFlower: (orderId, req) => request('POST', `/orders/${encodeURIComponent(orderId)}/red-flower`, req),
            remindReceipt: (orderId, req) => request('POST', `/orders/${encodeURIComponent(orderId)}/remind-receipt`, req),
            listRates: (query) => request('GET', `/orders/rates${toQueryString(query)}`),
            getRates: (orderId) => request('GET', `/orders/${encodeURIComponent(orderId)}/rate`),
            createRate: (orderId, req) => request('POST', `/orders/${encodeURIComponent(orderId)}/rate`, req),
            sendMessage: (orderId, req) => request('POST', `/orders/${encodeURIComponent(orderId)}/messages`, req),
            getRefund: (refundOrderId) => request('GET', `/orders/refunds/${encodeURIComponent(refundOrderId)}`),
            agreeRefund: (refundOrderId, req) => request('POST', `/orders/refunds/${encodeURIComponent(refundOrderId)}/agree`, req),
            refuseRefund: (refundOrderId, req) => request('POST', `/orders/refunds/${encodeURIComponent(refundOrderId)}/refuse`, req),
        },
        products: {
            list: (query) => request('GET', `/products${toQueryString(query)}`),
            get: (itemId) => request('GET', `/products/${encodeURIComponent(itemId)}`),
            getSkus: (itemId) => request('GET', `/products/${encodeURIComponent(itemId)}/skus`),
            updateExtension: (itemId, fields) => request('PUT', `/products/${encodeURIComponent(itemId)}/extension`, { fields }),
            offShelf: (itemId, req) => request('POST', `/products/${encodeURIComponent(itemId)}/off-shelf`, req ?? {}),
            upShelf: (itemId) => request('POST', `/products/${encodeURIComponent(itemId)}/up-shelf`),
            polish: (itemId) => request('POST', `/products/${encodeURIComponent(itemId)}/polish`),
            polishAll: (req) => request('POST', '/products/polish-all', req),
            edit: (itemId, req) => request('PATCH', `/products/${encodeURIComponent(itemId)}`, req),
            getWebEditDetail: (itemId) => request('GET', `/products/${encodeURIComponent(itemId)}/web-edit-detail`),
            editWebShop: (itemId, req) => request('PATCH', `/products/${encodeURIComponent(itemId)}/web-edit`, req),
            getFansPrice: (itemId) => request('GET', `/products/${encodeURIComponent(itemId)}/fans-price`),
            setFansPrice: (itemId, req) => request('POST', `/products/${encodeURIComponent(itemId)}/fans-price`, req),
            setRecharge: (itemId, req) => request('POST', `/products/${encodeURIComponent(itemId)}/recharge`, req),
            removeRecharge: (itemId) => request('DELETE', `/products/${encodeURIComponent(itemId)}/recharge`),
            promotion: {
                get: (itemId) => request('GET', `/products/${encodeURIComponent(itemId)}/promotion`),
            },
            groupon: {
                get: (itemId) => request('GET', `/products/${encodeURIComponent(itemId)}/groupon`),
                open: (itemId, req) => request('POST', `/products/${encodeURIComponent(itemId)}/groupon`, req),
                close: (itemId) => request('DELETE', `/products/${encodeURIComponent(itemId)}/groupon`),
            },
            coinDeduction: {
                get: (itemId) => request('GET', `/products/${encodeURIComponent(itemId)}/coin-deduction`),
                open: (itemId, req) => request('POST', `/products/${encodeURIComponent(itemId)}/coin-deduction`, req),
                close: (itemId) => request('DELETE', `/products/${encodeURIComponent(itemId)}/coin-deduction`),
            },
            publishPhysical: (req, publishOpts) => request('POST', '/products/publish/physical', req, idempotencyHeaders(publishOpts)),
            publishVirtual: (req, publishOpts) => request('POST', '/products/publish/virtual', req, idempotencyHeaders(publishOpts)),
            uploadImageByUrl: (req) => request('POST', '/products/images/by-url', req),
            uploadImage: (args) => {
                if (typeof FormData === 'undefined' || typeof Blob === 'undefined') {
                    throw new Error('当前运行时缺少 FormData/Blob（需 Node 18+），请改用 uploadImageByUrl');
                }
                const form = new FormData();
                form.set('accountUnb', args.accountUnb);
                const bytes = args.data instanceof ArrayBuffer
                    ? new Uint8Array(args.data)
                    : args.data;
                form.set('file', new Blob([bytes], { type: args.contentType ?? 'image/jpeg' }), args.filename);
                return request('POST', '/products/images', form);
            },
            uploadImageWeb: (args) => {
                if (typeof FormData === 'undefined' || typeof Blob === 'undefined') {
                    throw new Error('当前运行时缺少 FormData/Blob（需 Node 18+），无法上传 web 通道图片');
                }
                const form = new FormData();
                form.set('accountUnb', args.accountUnb);
                const bytes = args.data instanceof ArrayBuffer
                    ? new Uint8Array(args.data)
                    : args.data;
                form.set('file', new Blob([bytes], { type: args.contentType ?? 'image/jpeg' }), args.filename);
                return request('POST', '/products/images/web', form);
            },
            publishChannels: (accountUnb) => request('GET', `/products/publish-channels${toQueryString({ accountUnb })}`),
        },
        catalog: {
            bizTypes: () => request('GET', '/products/catalog/biz-types'),
            industryCategories: () => request('GET', '/products/catalog/industry-categories'),
            categories: (query) => request('GET', `/products/catalog/categories${toQueryString(query)}`),
            categoryInfo: (categoryId) => request('GET', `/products/catalog/categories/${encodeURIComponent(categoryId)}`),
            properties: (categoryId) => request('GET', `/products/catalog/properties${toQueryString({ categoryId })}`),
            propertyChildren: (query) => request('GET', `/products/catalog/property-children${toQueryString(query)}`),
            kgraph: (req) => request('POST', '/products/catalog/kgraph', req),
            badwords: (req) => request('POST', '/products/catalog/badwords', req),
            webCategoryRecommend: (req) => request('POST', '/products/catalog/web/category-recommend', req),
            webPropertySearch: (req) => request('POST', '/products/catalog/web/property-search', req),
            webServiceCards: (req) => request('POST', '/products/catalog/web/service-cards', req),
        },
        locations: {
            amapTips: (query) => request('GET', `/locations/amap-tips${toQueryString(query)}`),
        },
        im: {
            getSessionByOrder: (orderId) => request('GET', `/im/sessions/by-order?orderId=${encodeURIComponent(orderId)}`),
            getSessionByPeer: (accountUnb, peerUnb) => request('GET', `/im/sessions/by-peer?accountUnb=${encodeURIComponent(accountUnb)}&peerUnb=${encodeURIComponent(peerUnb)}`),
            listSessions: (query) => request('GET', `/im/sessions${toQueryString(query)}`),
            listMessages: (query) => request('GET', `/im/messages${toQueryString(query)}`),
            sendMessage: (req) => request('POST', '/im/messages', req),
            recallMessage: (req) => request('POST', '/im/messages/recall', req),
            sendImage: (req) => request('POST', '/im/messages/image', req),
            uploadImage: (args) => {
                if (typeof FormData === 'undefined' || typeof Blob === 'undefined') {
                    throw new Error('当前运行时缺少 FormData/Blob（需 Node 18+），无法上传 IM 图片');
                }
                const form = new FormData();
                form.set('accountUnb', args.accountUnb);
                const bytes = args.data instanceof ArrayBuffer
                    ? new Uint8Array(args.data)
                    : args.data;
                form.set('file', new Blob([bytes], { type: args.contentType ?? 'image/jpeg' }), args.filename);
                return request('POST', '/im/images', form);
            },
            pinSession: (accountUnb, chatId) => request('POST', `/im/sessions/${encodeURIComponent(accountUnb)}/${encodeURIComponent(chatId)}/pin`),
            unpinSession: (accountUnb, chatId) => request('POST', `/im/sessions/${encodeURIComponent(accountUnb)}/${encodeURIComponent(chatId)}/unpin`),
            getEmojiLibrary: () => request('GET', '/im/emojis'),
            blacklist: {
                query: (peerUnb) => request('GET', `/im/blacklist/${encodeURIComponent(peerUnb)}`),
                add: (peerUnb, body) => request('POST', `/im/blacklist/${encodeURIComponent(peerUnb)}`, body ?? {}),
                remove: (peerUnb) => request('DELETE', `/im/blacklist/${encodeURIComponent(peerUnb)}`),
            },
            xianyuBlacklist: {
                query: (accountUnb, chatId) => request('GET', `/im/sessions/${encodeURIComponent(accountUnb)}/${encodeURIComponent(chatId)}/xianyu-blacklist`),
                add: (accountUnb, chatId) => request('POST', `/im/sessions/${encodeURIComponent(accountUnb)}/${encodeURIComponent(chatId)}/xianyu-blacklist`),
                remove: (accountUnb, chatId) => request('DELETE', `/im/sessions/${encodeURIComponent(accountUnb)}/${encodeURIComponent(chatId)}/xianyu-blacklist`),
            },
        },
        shops: {
            list: () => request('GET', '/shops'),
            get: (unb) => request('GET', `/shops/${encodeURIComponent(unb)}`),
        },
        analytics: {
            shopOverview: (unb, query) => request('GET', `/analytics/shops/${encodeURIComponent(unb)}/overview${toQueryString(query)}`),
            fansSummary: (unb, query) => request('GET', `/analytics/shops/${encodeURIComponent(unb)}/fans-summary${toQueryString(query)}`),
            csOverview: (unb, query) => request('GET', `/analytics/shops/${encodeURIComponent(unb)}/cs-overview${toQueryString(query)}`),
            // accountUnb 只进路径，不进 query string（避免冗余参数）
            productMetrics: ({ accountUnb, ...query }) => request('GET', `/analytics/shops/${encodeURIComponent(accountUnb)}/product-metrics${toQueryString(query)}`),
            tenantOverview: (query) => request('GET', `/analytics/tenant/overview${toQueryString(query)}`),
            fansInsights: (unb) => request('GET', `/analytics/shops/${encodeURIComponent(unb)}/fans-insights`),
        },
        market: {
            search: (accountUnb, query) => request('GET', `/market/search${toQueryString({ accountUnb, ...query })}`),
            getItem: (accountUnb, itemId) => request('GET', `/market/items/${encodeURIComponent(itemId)}${toQueryString({ accountUnb })}`),
        },
    };
}
function safeJson(text) {
    try {
        return jsonBigParser.parse(text);
    }
    catch {
        return text;
    }
}
/** 对象 → 查询串（跳过 undefined/null；空对象返回空串）。 */
function toQueryString(query) {
    if (!query)
        return '';
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
        if (v === undefined || v === null)
            continue;
        qs.set(k, String(v));
    }
    const s = qs.toString();
    return s ? `?${s}` : '';
}
/** 发布幂等键 → 请求头（未传则不带头）。 */
function idempotencyHeaders(opts) {
    return opts?.idempotencyKey
        ? { 'x-idempotency-key': opts.idempotencyKey }
        : undefined;
}
//# sourceMappingURL=client.js.map