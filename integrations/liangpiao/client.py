"""
integrations/liangpiao/client.py —— 良票 HTTP 客户端

════════════════════════════════════════════════════════════════
★ 三个设计决定，都是为了让"接入期最容易错的地方"变难

【1】Transport 可注入
    真正发 HTTP 的部分抽成 Transport 协议。
    测试时换成 FakeTransport，就能在【没有 AppKey、不联网】的情况下
    把全部逻辑（签名、错误码映射、重试、幂等）测一遍。

【2】错误按"该怎么办"分类，不是按数字分类
    良票有 60+ 错误码。调用方不该去记数字，该记的是处置方式：
      RetryableError     稍后重试就好
      BusinessError      业务结果（场次下架、无供应商），要改变行为
      AuthError          我们的问题，重试只会刷日志
      RateLimitError     退避
    ★ 最重要的一条：110002 签名失败【绝不重试】—— 重试一百次也是失败，
      而且会把配额刷光、把日志刷满。

【3】HTTP 状态码不能当成功判据
    文档：HTTP 状态码【恒为 200】，成败一律看响应体的 code。
    所以这里先解析 body 再看 code，而不是先看 HTTP status。
════════════════════════════════════════════════════════════════
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .signature import SignedRequest, build_request

DEFAULT_BASE_URL = "https://portal-web-v3.liangpiao.net.cn"
API_PREFIX = "/api/v1"

# 文档：每接口默认 50 次/秒（限流基数，不是超时）。
# ★★ 下面三个超时里，前两个【文档给了明确数字】，第三个是我选的：
#
#   SEAT_LIST_TIMEOUT  文档原文：「整条链路有 10 秒总预算，保证 10 秒内必定返回……
#                      请把该接口的客户端超时设为 **12 秒**」
#   RECOGNIZE_TIMEOUT  文档原文：「中位 3.7 秒返回，p95 5.2 秒，请按 5 秒设超时」
#
#   DEFAULT_TIMEOUT    ★ 这一条文档【没给数字】，是我给其余接口选的兜底值。
#                      选 10 秒的依据：文档里最长的一段是座位图的 10 秒预算，
#                      其余接口（下单/询价/详情）都该更快。
#                      ⇒ 如果上线后发现某个接口经常超时，先看是不是这个值不合适，
#                        而不是去查文档 —— 文档里没有这个数。
DEFAULT_TIMEOUT = 10.0
SEAT_LIST_TIMEOUT = 12.0
RECOGNIZE_TIMEOUT = 5.0


# ==================================================================== 错误
class LiangPiaoError(Exception):
    """所有良票错误的基类。"""
    def __init__(self, code: int, message: str, request_id: str = "",
                 payload: dict | None = None):
        self.code = code
        self.message = message
        self.request_id = request_id
        self.payload = payload or {}
        super().__init__(f"[{code}] {message}" + (f" requestId={request_id}" if request_id else ""))


class RetryableError(LiangPiaoError):
    """稍后重试就好（服务内部错误、供应商超时）。"""


class RateLimitError(RetryableError):
    """120001 太频繁 —— 要退避，不要立刻重试。"""


class AuthError(LiangPiaoError):
    """
    鉴权/签名问题。【绝不自动重试】

    ★ 为什么单列这一类：
      110002 签名失败重试一百次也是失败，只会刷光配额、刷满日志，
      还掩盖了真正的 bug。运维看到这个应该去查代码，不是等它自己好。
    """


class BusinessError(LiangPiaoError):
    """
    业务结果，不是故障。调用方要改变行为，不是重试。

    例：200004 场次已下架 → 重新拉排期
        300004 暂无供应商 → 换档位
        300005 超过限价 → 提高 maxPrice 或放弃
        300010 座位正在出票 → 【不要重试】，已有单在跑
    """


class TransportError(Exception):
    """网络层失败（连不上、超时、不是 JSON）。可以重试。"""


# 错误码 → 分类。文档第四节。
AUTH_CODES = {110001, 110002, 110003, 110004, 110005, 110006, 110008}
RATE_CODES = {120001}
RETRYABLE_CODES = {100005, 600001, 600002}
# 其余走 BusinessError

# ★ 这几条是"重试也没用、而且重试危险"的，明确列出来，避免有人顺手加重试
NEVER_RETRY_CODES = {
    110002,   # 签名校验失败 —— 我们的 bug
    200004,   # 场次已下架 —— 重新拉排期
    200005,   # 场次已停售 —— 已过截单
    300010,   # 该场次座位正在出票 —— 已有单在跑，重试会重复提交
    700009,   # 所选候选无效 —— 只能从候选里挑
}


def classify(code: int, message: str, request_id: str, payload: dict) -> LiangPiaoError:
    """把错误码翻译成"该怎么办"。"""
    if code in AUTH_CODES:
        return AuthError(code, message, request_id, payload)
    if code in RATE_CODES:
        return RateLimitError(code, message, request_id, payload)
    if code in RETRYABLE_CODES:
        return RetryableError(code, message, request_id, payload)
    return BusinessError(code, message, request_id, payload)


# ==================================================================== Transport
class Transport(Protocol):
    """真正发 HTTP 的东西。抽出来是为了能在测试里替换掉。"""

    def send(self, url: str, body: str, headers: dict, timeout: float) -> tuple[int, str]:
        """返回 (http_status, response_text)。"""
        ...


class UrllibTransport:
    """标准库实现。零依赖。"""

    def send(self, url: str, body: str, headers: dict, timeout: float) -> tuple[int, str]:
        data = body.encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # 平台说 HTTP 状态码恒为 200，但网关层仍可能返回 5xx/4xx。
            # 把 body 读出来，交给上层按 code 判断。
            try:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception:                       # noqa: BLE001
                return exc.code, ""
        except Exception as exc:                    # noqa: BLE001
            raise TransportError(f"网络失败: {exc}") from exc


# ==================================================================== 响应
@dataclass
class ApiResponse:
    code: int
    message: str
    request_id: str
    data: Any
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == 0


@dataclass
class CallRecord:
    """一次调用的记录。用于审计与排查（requestId 是关键）。"""
    path: str
    code: int
    request_id: str
    message: str
    attempt: int
    elapsed_ms: float


# ==================================================================== 客户端
class LiangPiaoClient:
    """
    良票 API 客户端。

    ★ 只做"一次调用"这件事：签名 → 发送 → 判 code → 分类错误。
      重试策略调用方定（send(..., retry=...)），因为不同接口该有不同的重试口径
      —— 截图识别可以重试，下单不能随便重试（要靠 outOrderNo 幂等）。
    """

    def __init__(self, app_key: str, app_secret: str,
                 base_url: str = DEFAULT_BASE_URL,
                 transport: Transport | None = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 sleep=time.sleep,
                 log: list | None = None):
        if not app_key or not app_secret:
            raise ValueError("必须提供 app_key 与 app_secret（AppSecret 平台只下发一次）")
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        self._sleep = sleep
        # 调用记录。排查时靠 requestId 定位，所以必须留。
        self.calls: list[CallRecord] = log if log is not None else []

    # ---------------------------------------------------------------- 核心
    def call(self, path: str, payload: dict | None = None, *,
             timeout: float | None = None,
             retries: int = 0,
             backoff: float = 1.0) -> ApiResponse:
        """
        发一次调用。

        retries 默认 0 —— ★ 默认不重试是刻意的：
        重试是否安全取决于接口（下单要靠 outOrderNo 幂等，询价可以随便重试）。
        想重试的调用方显式传 retries，并在注释里说明为什么安全。
        """
        # ★ 路径拼接：只有已经带了 /api/ 的才认为调用方写全了。
        #   写成 `path.startswith("/")` 是错的 —— 那样 "/ping" 会被当成完整路径，
        #   丢掉 /api/v1 前缀。（这个 bug 是自检抓出来的。）
        if path.startswith("/api/"):
            url = self.base_url + path
        else:
            url = self.base_url + API_PREFIX + (path if path.startswith("/") else "/" + path)
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            if attempt:
                # 退避。限流场景应该退得更久，由调用方传更大的 backoff。
                self._sleep(backoff * (2 ** (attempt - 1)))

            # ★ 每次尝试都重新签名 —— nonce 10 分钟内不可重复，
            #   复用 nonce 会被拒（110004）。这是文档明确要求的。
            signed: SignedRequest = build_request(self.app_key, self.app_secret, payload)
            started = time.monotonic()
            try:
                status, text = self.transport.send(
                    url, signed.body, signed.headers,
                    timeout if timeout is not None else self.timeout)
            except TransportError as exc:
                last_error = exc
                self._record(path, -1, "", str(exc), attempt, started)
                if attempt < retries:
                    continue
                raise

            elapsed = (time.monotonic() - started) * 1000
            response = self._parse(status, text, path)
            self._record(path, response.code, response.request_id,
                         response.message, attempt, started)

            if response.ok:
                return response

            # ★ 把完整原始响应挂到错误上 —— 700009 / 100001 这类错误
            #   会在 message 里列出可选值，排查时要能看到原文。
            error = classify(response.code, response.message,
                             response.request_id, response.raw)

            # ★ 不可重试的错误立刻抛出，绝不进重试循环
            if response.code in NEVER_RETRY_CODES or isinstance(error, AuthError):
                raise error

            last_error = error
            if isinstance(error, RateLimitError):
                # 限流退避要更久：文档说退避后重试
                self._sleep(backoff * 4)
                continue
            if isinstance(error, RetryableError) and attempt < retries:
                continue
            raise error

        if last_error:
            raise last_error
        raise TransportError("未知失败")            # 理论上到不了

    def _parse(self, status: int, text: str, path: str) -> ApiResponse:
        """
        ★ HTTP 状态码不能当成功判据 —— 文档说恒为 200，成败看 body 的 code。
        但网关层仍可能返回 5xx，所以这里两种情况都要处理。
        """
        if not text:
            raise TransportError(f"空响应 (HTTP {status}) path={path}")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TransportError(f"响应不是 JSON (HTTP {status}): {text[:200]}") from exc
        if not isinstance(raw, dict):
            raise TransportError(f"响应不是对象: {text[:200]}")
        if "code" not in raw:
            # 没有 code 说明不是平台标准响应（网关错误页之类）
            raise TransportError(f"响应缺 code 字段 (HTTP {status}): {text[:200]}")
        return ApiResponse(
            code=int(raw.get("code", -1)),
            message=str(raw.get("message", "")),
            request_id=str(raw.get("requestId", "")),
            data=raw.get("data"),
            raw=raw,
        )

    def _record(self, path: str, code: int, request_id: str,
                message: str, attempt: int, started: float) -> None:
        self.calls.append(CallRecord(
            path=path, code=code, request_id=request_id, message=message,
            attempt=attempt, elapsed_ms=(time.monotonic() - started) * 1000))

    # ---------------------------------------------------------------- 便捷
    def ping(self) -> ApiResponse:
        """连通性自检。联调第一步，不涉及业务数据。"""
        return self.call("/ping")

    def last_request_id(self) -> str:
        """最近一次的 requestId —— 报障时提供它才能定位。"""
        return self.calls[-1].request_id if self.calls else ""

    def stats(self) -> dict:
        """简单统计，便于观察限流与失败率。"""
        if not self.calls:
            return {"calls": 0}
        codes = {}
        for c in self.calls:
            codes[c.code] = codes.get(c.code, 0) + 1
        return {
            "calls": len(self.calls),
            "retries": sum(1 for c in self.calls if c.attempt > 0),
            "codes": codes,
            "avg_ms": round(sum(c.elapsed_ms for c in self.calls) / len(self.calls), 1),
        }
