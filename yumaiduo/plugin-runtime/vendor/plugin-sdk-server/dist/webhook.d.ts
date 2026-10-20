export interface VerifyWebhookOptions {
    /** 注册时核心下发的 webhookSecret。 */
    secret: string;
    /** X-Yumaiduo-Timestamp 头（毫秒时间戳）。 */
    timestamp: string | number;
    /** X-Yumaiduo-Signature 头（hex）。 */
    signature: string;
    /** 原始请求体字符串——务必用 raw body，不要先 parse 再 stringify（字节须一致）。 */
    rawBody: string;
    /** 允许的时钟偏移（秒），默认 300（5min 防重放窗口）。 */
    toleranceSec?: number;
    /** 当前时间（毫秒），默认 Date.now()，便于测试。 */
    now?: number;
}
/**
 * 校验核心投递的 webhook 签名（与核心 Dispatcher 完全同算法）：
 *
 * 1. 时间戳须落在 ±toleranceSec 窗口内（防重放）；
 * 2. `HMAC-SHA256(secret, `${timestamp}.${rawBody}`)` 与 signature 恒定时间比较。
 *
 * 任一不过返回 false。插件应在校验通过后再处理事件，并按信封 id 自行幂等。
 */
export declare function verifyWebhookSignature(opts: VerifyWebhookOptions): boolean;
//# sourceMappingURL=webhook.d.ts.map