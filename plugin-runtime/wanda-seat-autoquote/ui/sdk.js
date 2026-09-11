// 注意：本文件为 @yumaiduo/plugin-sdk-web 的浏览器 ESM 构建产物，请勿手改。
// 更新方式：运行 pnpm sync:plugin-sdk-web-examples。
/**
 * 鱼麦多插件平台前端 SDK（M3 S3，浏览器端 / iframe 内）。
 *
 * 插件页面被核心以 iframe 内嵌（src 指向核心同源网关 `/api/v1/plugin/:id/gateway/ui`）。
 * 本 SDK 负责：
 * - 与核心宿主 postMessage 握手，拿到短命 session token（并随核心续签自动更新）；
 * - `authedFetch`：自动带 token，经同源网关访问插件后端（浏览器永不直连插件地址）；
 * - 自适应高度上报、内部导航、toast 透传。
 *
 * 用法：
 * ```ts
 * import { createPluginSdk } from '@yumaiduo/plugin-sdk-web'
 * const sdk = createPluginSdk()   // 创建即自动上报高度（无需再手动 autoResize）
 * await sdk.ready()               // 完成握手（内置重试）
 * const res = await sdk.authedFetch('api/orders')
 * // autoResize() 为可选高级用法：仅当需要观察自定义元素或手动停止自适应时调用
 * ```
 */
/** 桥两端身份标识（与核心 PluginHostPage 对称）。 */
const HOST_SOURCE = 'yumaiduo-host';
const PLUGIN_SOURCE = 'yumaiduo-plugin';
// 浏览器静态护栏；数值对应 shared 的 PLUGIN_WALLET_TOPUP_MIN_FEN/MAX_FEN。Web SDK
// 是独立发布包，刻意不依赖 shared，因此在此保留字面量并由测试锁定一致性。100 分只是深链
// 地板，运行时权威下限仍由宿主弹窗 minAmountFen 与服务端 schema 决定，平台配置可以更高。
const WALLET_TOPUP_STATIC_MIN_FEN = 100;
const WALLET_TOPUP_STATIC_MAX_FEN = 10000000;
/**
 * 高度上报合并：优先 rAF，环境无 rAF（测试等）时回落到近似一帧的 setTimeout；
 * 两者都不可用（极简/降级环境）时返回 null 表示未排帧——保证 createPluginSdk() 创建即上报的
 * 副作用在任何环境都不抛错（真实浏览器 setTimeout 恒在，此分支仅为鲁棒兜底）。
 */
function requestResizeFrame(cb) {
    if (typeof window.requestAnimationFrame === 'function') {
        return window.requestAnimationFrame(cb);
    }
    if (typeof window.setTimeout === 'function') {
        return window.setTimeout(() => cb(Date.now()), 16);
    }
    return null;
}
function cancelResizeFrame(id) {
    if (typeof window.cancelAnimationFrame === 'function') {
        window.cancelAnimationFrame(id);
        return;
    }
    window.clearTimeout(id);
}
/**
 * 累加 offsetTop 链，得到元素相对文档顶部的偏移。
 * `el.style.top` 是相对 offsetParent（而非文档），故 absolute 定位换算时需扣除本偏移，
 * 使浮层挂在 `position: relative` 父容器内也不偏位。缺 offset 属性（降级环境）按 0 处理。
 */
function offsetTopToDocument(el) {
    let top = 0;
    let node = el;
    while (node) {
        top += node.offsetTop || 0;
        node = node.offsetParent;
    }
    return top;
}
/** 拼接网关 URL：`gatewayBase` + 相对 `path`，规整重复斜杠。 */
export function buildGatewayUrl(gatewayBase, path) {
    const base = gatewayBase.replace(/\/+$/, '');
    const rel = path.replace(/^\/+/, '');
    return rel ? `${base}/${rel}` : base;
}
/** 创建插件 SDK 实例（在插件页面入口调用一次）。 */
export function createPluginSdk(options = {}) {
    // 双模握手（同源 / 跨源通用）：
    // - 显式传入 hostOrigin → 锁定该源，全程严格校验（保持旧行为）；
    // - 未传入 → hostOrigin 先为 null，`ready` 以 targetOrigin '*' 广播（不含敏感信息），
    //   收到首条来自 window.parent 的合法 init/session 消息后，以其 ev.origin 锁定宿主源，
    //   此后所有 postMessage 与入站 origin 校验都用锁定值。安全性由核心网关 frame-ancestors
    //   限定嵌入方兜底（插件页只能被白名单控制台源嵌入）。
    let hostOrigin = options.hostOrigin ?? null;
    const readyTimeoutMs = options.readyTimeoutMs ?? 10000;
    const readyRetryIntervalMs = options.readyRetryIntervalMs ?? 1000;
    const autoResizeOnCreate = options.autoResizeOnCreate ?? true;
    const hasResizeObserver = typeof ResizeObserver !== 'undefined';
    let session = null;
    let disposed = false;
    let standaloneMode = false;
    // 高度节流：记录上次已上报高度与在飞 rAF 句柄，重复高度不再上报，避免消息风暴打满宿主。
    let lastReportedHeight = null;
    let resizeFrame = null;
    // 自动高度观察器（单例）：createPluginSdk 时启动一个，autoResize() 只重定向它，绝不并存两个。
    let heightObserver = null;
    // 观察器代际：每次启动/重定向 +1，stop() 只停自己那一代，防旧闭包误停新观察器。
    let observerGeneration = 0;
    const sessionListeners = new Set();
    // 宿主可视区域：最近一次宿主推送值与订阅者集合（用于弹窗定位）。
    let viewport = null;
    const viewportListeners = new Set();
    // 活跃的 centerInViewport 停止函数集合：dispose() 统一停止，防调用方漏调 stop() 后
    // 本地 ResizeObserver 在销毁后仍触发回调。
    const centerStops = new Set();
    // ready() 单飞：SDK 实例内共享（非模块级，避免多实例互相污染）。
    let readyPromise = null;
    // 当前在飞 ready() 的中止句柄：dispose() 用它 reject 未完成的握手。
    let pendingReadyReject = null;
    function post(type, extra = {}) {
        // 宿主源未锁定（首个 ready 之前）用 '*' 广播；锁定后收敛到具体 origin。
        window.parent?.postMessage({ source: PLUGIN_SOURCE, type, ...extra }, hostOrigin ?? '*');
    }
    function onHostMessage(ev) {
        // 只接受来自父窗口的消息（跨源下 identity 比较仍有效）。
        if (ev.source !== window.parent)
            return;
        const data = ev.data;
        if (!data || data.source !== HOST_SOURCE)
            return;
        // 锁定宿主源：未锁定则以首条合法消息的 origin 为准；已锁定则拒绝其它源。
        if (hostOrigin === null) {
            hostOrigin = ev.origin;
        }
        else if (ev.origin !== hostOrigin) {
            return;
        }
        if (data.type === 'viewport') {
            const visibleTop = Number(data.visibleTop);
            const visibleHeight = Number(data.visibleHeight);
            if (!Number.isFinite(visibleTop) || !Number.isFinite(visibleHeight))
                return;
            // 值未变化不通知（宿主已按帧合并去重，这里再兜一层）。
            if (viewport &&
                viewport.visibleTop === visibleTop &&
                viewport.visibleHeight === visibleHeight) {
                return;
            }
            viewport = { visibleTop, visibleHeight };
            const snapshot = viewport;
            viewportListeners.forEach((cb) => cb(snapshot));
            return;
        }
        if (data.type === 'init' || data.type === 'session') {
            const token = typeof data.token === 'string' ? data.token : session?.token;
            if (!token)
                return;
            const firstSession = session === null;
            session = {
                pluginId: typeof data.pluginId === 'string'
                    ? data.pluginId
                    : (session?.pluginId ?? ''),
                token,
                gatewayBase: typeof data.gatewayBase === 'string'
                    ? data.gatewayBase
                    : (session?.gatewayBase ?? ''),
            };
            // 首次握手成功后强制重报一次高度：创建即上报的那条初始 resize 可能早于宿主 message
            // 监听器就绪而丢失；若靠同高度去重，后续高度不变将永不再发，宿主只能停在兜底高度。
            // 握手成功意味着宿主监听器此刻必已就绪，故清空去重基线、绕过去重再报一次，稳态兜底。
            if (firstSession) {
                lastReportedHeight = null;
                scheduleSizeReport();
            }
            const snapshot = session;
            sessionListeners.forEach((cb) => cb(snapshot));
        }
    }
    window.addEventListener('message', onHostMessage);
    function onSession(cb) {
        sessionListeners.add(cb);
        return () => {
            sessionListeners.delete(cb);
        };
    }
    function ready() {
        if (session)
            return Promise.resolve(session);
        if (readyPromise)
            return readyPromise;
        readyPromise = new Promise((resolve, reject) => {
            let timer = null;
            let retry = null;
            const cleanup = () => {
                if (timer !== null) {
                    clearTimeout(timer);
                    timer = null;
                }
                if (retry !== null) {
                    clearInterval(retry);
                    retry = null;
                }
                off();
            };
            // dispose() 时主动结束未完成的握手，避免业务 await sdk.ready() 永久悬挂。
            pendingReadyReject = (err) => {
                cleanup();
                readyPromise = null;
                pendingReadyReject = null;
                reject(err);
            };
            const off = onSession((s) => {
                cleanup();
                pendingReadyReject = null;
                resolve(s);
            });
            timer = setTimeout(() => {
                cleanup();
                standaloneMode = true;
                // 超时后重置单飞句柄，允许业务再次 ready() 重试。
                readyPromise = null;
                pendingReadyReject = null;
                resolve(null);
            }, readyTimeoutMs);
            // 首发 + 按间隔重发 ready：抵御 ready 早于宿主监听器就绪而丢失的时序竞态。
            post('ready');
            if (readyRetryIntervalMs > 0) {
                retry = setInterval(() => post('ready'), readyRetryIntervalMs);
            }
        });
        return readyPromise;
    }
    function refreshSession(expiredToken) {
        if (disposed) {
            return Promise.reject(new Error('插件 SDK 已销毁：无法续签会话'));
        }
        return new Promise((resolve, reject) => {
            let timer = null;
            let retry = null;
            const cleanup = () => {
                if (timer !== null) {
                    clearTimeout(timer);
                    timer = null;
                }
                if (retry !== null) {
                    clearInterval(retry);
                    retry = null;
                }
                off();
            };
            const off = onSession((nextSession) => {
                if (!nextSession.token || nextSession.token === expiredToken)
                    return;
                cleanup();
                resolve(nextSession);
            });
            timer = setTimeout(() => {
                cleanup();
                reject(new Error('插件会话已过期，续签未完成，请刷新页面后重试'));
            }, readyTimeoutMs);
            post('ready');
            if (readyRetryIntervalMs > 0) {
                retry = setInterval(() => post('ready'), readyRetryIntervalMs);
            }
        });
    }
    async function isExpiredPluginSession(response) {
        if (response.status !== 401)
            return false;
        const body = await response.clone().text().catch(() => '');
        return /plugin_session_expired|plugin session expired|session expired/iu.test(body);
    }
    async function authedFetch(path, init = {}) {
        if (!session) {
            if (!standaloneMode) {
                throw new Error('SDK 尚未握手，请先 await sdk.ready()');
            }
            const normalized = String(path).replace(/^\/+/, '');
            const directPath = normalized.startsWith('ui/v4/')
                ? `/${normalized.slice(6)}`
                : normalized.startsWith('ui/api/')
                    ? `/${normalized.slice(2)}`
                    : `/${normalized}`;
            return fetch(directPath, { ...init, credentials: 'include' });
        }
        const fetchWithSession = (activeSession) => {
            const url = buildGatewayUrl(activeSession.gatewayBase, path);
            const headers = new Headers(init.headers);
            headers.set('X-Plugin-Session', activeSession.token);
            return fetch(url, { ...init, headers, credentials: 'omit' });
        };
        const activeSession = session;
        const response = await fetchWithSession(activeSession);
        if (!(await isExpiredPluginSession(response)))
            return response;
        // 仅对网关明确声明的会话过期重试一次；其他 401 原样返回，避免把业务错误重放为写入请求。
        const refreshedSession = await refreshSession(activeSession.token);
        return fetchWithSession(refreshedSession);
    }
    function reportSize(height) {
        if (disposed)
            return;
        // 缺省取文档滚动高度；无 document（极简/降级环境）且未显式传高度时直接跳过，不抛错。
        const measured = height ??
            (typeof document !== 'undefined'
                ? document.documentElement.scrollHeight
                : undefined);
        if (measured === undefined)
            return;
        const h = Math.ceil(measured);
        // 无效值或与上次相同则跳过：宿主已按帧合并，这里再去重从源头削减消息量。
        if (!Number.isFinite(h) || h <= 0 || h === lastReportedHeight)
            return;
        lastReportedHeight = h;
        post('resize', { height: h });
    }
    function scheduleSizeReport() {
        if (disposed || resizeFrame !== null)
            return;
        // 返回 null 表示无可用调度器（极简/降级环境）：不记录在飞句柄，避免 dispose 误调 cancel。
        resizeFrame = requestResizeFrame(() => {
            resizeFrame = null;
            reportSize();
        });
    }
    /**
     * 启动/重定向单例高度观察器到 target。返回停止本代观察器的函数。
     * - 先停掉当前观察器再 observe 新 target，绝不并存两个；
     * - 用代际号隔离：返回的 stop() 只在自己那一代仍是最新时才停止，防旧闭包误停新观察器；
     * - 无 ResizeObserver 环境降级为仅上报一次当前高度，不抛错。
     */
    function startHeightObserver(target) {
        if (heightObserver) {
            heightObserver.disconnect();
            heightObserver = null;
        }
        const generation = ++observerGeneration;
        scheduleSizeReport();
        if (hasResizeObserver) {
            heightObserver = new ResizeObserver(() => scheduleSizeReport());
            heightObserver.observe(target);
        }
        return () => {
            // 仅当本代仍是最新观察器时才停止：后续 autoResize(newTarget) 已推进代际则本 stop no-op。
            if (generation !== observerGeneration)
                return;
            if (heightObserver) {
                heightObserver.disconnect();
                heightObserver = null;
            }
            if (resizeFrame !== null) {
                cancelResizeFrame(resizeFrame);
                resizeFrame = null;
            }
        };
    }
    function autoResize(target = document.documentElement) {
        return startHeightObserver(target);
    }
    function navigate(to) {
        post('navigate', { to });
    }
    function navigateToImSession(args) {
        const qs = new URLSearchParams({
            accountUnb: args.accountUnb,
            chatId: args.chatId,
        });
        navigate(`/console/im?${qs.toString()}`);
    }
    function navigateToWalletTopup(pluginId, amountFen) {
        const targetPluginId = pluginId ?? session?.pluginId;
        if (!targetPluginId) {
            throw new Error('插件会话尚未就绪，无法打开资金包充值页');
        }
        if (amountFen !== undefined &&
            (!Number.isSafeInteger(amountFen) ||
                amountFen < WALLET_TOPUP_STATIC_MIN_FEN ||
                amountFen > WALLET_TOPUP_STATIC_MAX_FEN)) {
            throw new Error(`充值预填金额 amountFen 必须为 ${WALLET_TOPUP_STATIC_MIN_FEN}～${WALLET_TOPUP_STATIC_MAX_FEN} 的安全整数（分）`);
        }
        const query = new URLSearchParams({ action: 'wallet-topup' });
        if (amountFen !== undefined)
            query.set('amountFen', String(amountFen));
        navigate(`/console/marketplace/${encodeURIComponent(targetPluginId)}?${query.toString()}`);
    }
    function navigateToPluginPurchase(pluginId, skuCode) {
        const targetPluginId = pluginId ?? session?.pluginId;
        if (!targetPluginId) {
            throw new Error('插件会话尚未就绪，无法打开购买页');
        }
        const query = new URLSearchParams({ action: 'purchase' });
        if (skuCode)
            query.set('skuCode', skuCode);
        navigate(`/console/marketplace/${encodeURIComponent(targetPluginId)}?${query.toString()}`);
    }
    function toast(message, level = 'info') {
        post('toast', { message, level });
    }
    function onViewport(cb) {
        viewportListeners.add(cb);
        return () => {
            viewportListeners.delete(cb);
        };
    }
    function requestViewport() {
        post('requestViewport');
    }
    /**
     * 把浮层元素持续居中到宿主可视区域，返回停止函数。
     * 仅适用于 position:absolute 的弹窗面板（非全屏遮罩）；换算已扣除 offsetParent 偏移。
     */
    function centerInViewport(el) {
        // 定位上下文校验：非 absolute 时只改 top 与开发者预期不符（尤其全屏遮罩），仅告警不改写。
        if (typeof getComputedStyle === 'function') {
            try {
                const pos = getComputedStyle(el).position;
                if (pos !== 'absolute') {
                    // eslint-disable-next-line no-console
                    console.warn(`[plugin-sdk-web] centerInViewport 仅适用于 position:absolute 的浮层面板，当前为 "${pos}"，定位可能不符预期`);
                }
            }
            catch {
                /* getComputedStyle 在降级环境可能抛错，忽略即可 */
            }
        }
        let stopped = false;
        let observer = null;
        let frame = null;
        function reposition() {
            if (stopped || !viewport)
                return;
            const elHeight = el.offsetHeight || 0;
            // 目标在文档坐标系下的顶部：可见区顶部 + 居中留白；弹窗高于可见区则贴可见区顶部。
            const targetDocTop = viewport.visibleTop +
                Math.max(0, (viewport.visibleHeight - elHeight) / 2);
            // 换算为相对 offsetParent 的 top（扣除 offsetParent 相对文档顶部的偏移）。
            // 不钳到 ≥0：offsetParent 顶部低于目标文档坐标时正确的 top 就是负数，钳 0 会把弹窗
            // 顶到父容器顶部而非可见区中央，破坏"relative 父容器下不偏移"的承诺。
            const parentDocTop = offsetTopToDocument(el.offsetParent);
            el.style.top = `${Math.round(targetDocTop - parentDocTop)}px`;
        }
        function scheduleReposition() {
            if (stopped || frame !== null)
                return;
            frame = requestResizeFrame(() => {
                frame = null;
                reposition();
            });
        }
        // 首帧延迟到下一帧：规避元素刚从 display:none 切换、布局尚未稳定时按错误高度定位。
        scheduleReposition();
        // 跟随宿主可视区域变化（滚动 / 缩放）。
        const offViewport = onViewport(() => scheduleReposition());
        // 跟随元素自身高度变化（异步内容 / 表单错误展开 / 图片加载）。
        if (hasResizeObserver) {
            observer = new ResizeObserver(() => scheduleReposition());
            observer.observe(el);
        }
        function stop() {
            centerStops.delete(stop);
            stopped = true;
            offViewport();
            if (observer) {
                observer.disconnect();
                observer = null;
            }
            if (frame !== null) {
                cancelResizeFrame(frame);
                frame = null;
            }
        }
        // 登记到实例集合：dispose() 会统一停止未手动 stop 的 center 观察器。
        centerStops.add(stop);
        return stop;
    }
    function dispose() {
        disposed = true;
        window.removeEventListener('message', onHostMessage);
        if (heightObserver) {
            heightObserver.disconnect();
            heightObserver = null;
        }
        if (resizeFrame !== null) {
            cancelResizeFrame(resizeFrame);
            resizeFrame = null;
        }
        // 统一停止仍活跃的 centerInViewport 观察器（调用方漏调 stop() 时兜底）。
        for (const stop of Array.from(centerStops))
            stop();
        centerStops.clear();
        // 结束未完成的握手，避免业务 await sdk.ready() 永久悬挂。
        if (pendingReadyReject) {
            pendingReadyReject(new Error('插件 SDK 已销毁：握手中止'));
        }
        sessionListeners.clear();
        viewportListeners.clear();
    }
    // 创建即自动启动高度上报：不等 ready() 成功，规避「握手输了竞态 → 从不上报高度 → iframe 停 0/兜底高度」死循环。
    // 高度信息不敏感，hostOrigin 未锁定时以 '*' 广播；无 ResizeObserver / 无 document 环境降级（不上报，
    // 不影响 ready()/authedFetch()），绝不因缺少浏览器 API 在创建时抛错。
    if (autoResizeOnCreate && typeof document !== 'undefined') {
        startHeightObserver(document.documentElement);
    }
    return {
        ready,
        getSession: () => session,
        authedFetch,
        reportSize,
        autoResize,
        navigate,
        navigateToWalletTopup,
        navigateToPluginPurchase,
        navigateToImSession,
        toast,
        getViewport: () => viewport,
        onViewport,
        requestViewport,
        centerInViewport,
        onSession,
        dispose,
    };
}
//# sourceMappingURL=index.js.map
