/**
 * 核心回调 API 返回非 2xx 时抛出。
 *
 * 携带 HTTP 状态码与核心的业务错误码（如 E_QUOTA_EXCEEDED / E_FORBIDDEN），
 * 便于插件按 code 分支处理（例如配额超限时降级、令牌失效时重新注册）。
 */
export declare class PluginApiError extends Error {
    readonly status: number;
    readonly code: string | undefined;
    readonly body?: unknown;
    constructor(status: number, code: string | undefined, message: string, body?: unknown);
    /** 与文档/示例一致的别名（`e.errorCode`）。 */
    get errorCode(): string | undefined;
}
//# sourceMappingURL=errors.d.ts.map