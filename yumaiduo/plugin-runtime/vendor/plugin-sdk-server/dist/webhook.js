"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.verifyWebhookSignature = verifyWebhookSignature;
const node_crypto_1 = require("node:crypto");
/**
 * 校验核心投递的 webhook 签名（与核心 Dispatcher 完全同算法）：
 *
 * 1. 时间戳须落在 ±toleranceSec 窗口内（防重放）；
 * 2. `HMAC-SHA256(secret, `${timestamp}.${rawBody}`)` 与 signature 恒定时间比较。
 *
 * 任一不过返回 false。插件应在校验通过后再处理事件，并按信封 id 自行幂等。
 */
function verifyWebhookSignature(opts) {
    const ts = Number(opts.timestamp);
    if (!Number.isFinite(ts))
        return false;
    const now = opts.now ?? Date.now();
    const tolerance = (opts.toleranceSec ?? 300) * 1000;
    if (Math.abs(now - ts) > tolerance)
        return false;
    const expected = (0, node_crypto_1.createHmac)('sha256', opts.secret)
        .update(`${ts}.${opts.rawBody}`)
        .digest('hex');
    return safeEqualHex(expected, opts.signature);
}
/** 恒定时间比较两个等长 hex 串；长度不等或非法直接 false。 */
function safeEqualHex(a, b) {
    if (a.length !== b.length)
        return false;
    try {
        return (0, node_crypto_1.timingSafeEqual)(Buffer.from(a, 'hex'), Buffer.from(b, 'hex'));
    }
    catch {
        return false;
    }
}
//# sourceMappingURL=webhook.js.map