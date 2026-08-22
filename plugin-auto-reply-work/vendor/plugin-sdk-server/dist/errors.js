"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.PluginApiError = void 0;
/**
 * 核心回调 API 返回非 2xx 时抛出。
 *
 * 携带 HTTP 状态码与核心的业务错误码（如 E_QUOTA_EXCEEDED / E_FORBIDDEN），
 * 便于插件按 code 分支处理（例如配额超限时降级、令牌失效时重新注册）。
 */
class PluginApiError extends Error {
    status;
    code;
    body;
    constructor(status, code, message, body) {
        super(message);
        this.status = status;
        this.code = code;
        this.body = body;
        this.name = 'PluginApiError';
    }
    /** 与文档/示例一致的别名（`e.errorCode`）。 */
    get errorCode() {
        return this.code;
    }
}
exports.PluginApiError = PluginApiError;
//# sourceMappingURL=errors.js.map