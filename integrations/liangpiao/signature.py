"""
integrations/liangpiao/signature.py —— 签名与请求头

════════════════════════════════════════════════════════════════
★ 这个文件只有一件事，但它是整个接入的命门

  待签名串 = appKey + timestamp + nonce + 请求体原始 JSON 字符串
  X-Sign  = HMAC-SHA256(待签名串, appSecret) 的十六进制

★ 文档原文（必须照做）：
  「签名针对【原始字节】计算，服务端不做任何 JSON 重排。因此必须先把请求体
    序列化成字符串，用这个字符串算签名，再把【同一个字符串】作为 body 发出去。
    不要先算签名再重新序列化一次对象 —— 键顺序或空格的任何差异都会导致验签失败，
    这是接入阶段最常见的问题。」

  所以这个模块的接口设计成：
      body = encode_body(payload)      # 只序列化一次
      sign = sign_body(body)           # 拿这个字符串签名
      send(body, sign)                 # 把同一个字符串发出去
  ★ 而不是 sign(payload) —— 那样每次调用都可能重新序列化，早晚出错。

★ 另外两条同样致命：
  · X-Timestamp 是【秒】不是毫秒（13 位毫秒值直接被判过期）
  · nonce 10 分钟内不可重复（重试必须换新的）
════════════════════════════════════════════════════════════════
"""

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

# 文档：Content-Type 固定这个值
CONTENT_TYPE = "application/json; charset=utf-8"

# 空请求体参与签名时用这个
EMPTY_BODY = "{}"

# 时间戳允许的偏差（文档：超过 5 分钟拒绝）。我们本地不判，只用来说明。
TIMESTAMP_TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    """签名前的准备出了问题（不是验签失败，那是服务端返回 110002）。"""


def encode_body(payload: dict | None) -> str:
    """
    把请求体序列化成字符串。**整个请求生命周期只调用这一次。**

    ★ 为什么不用 sort_keys：
      文档说服务端不做重排，所以怎么序列化都行 —— 只要【算签名】和【发出去】
      用的是同一个字符串。加 sort_keys 只是让输出稳定、便于比对，
      不影响正确性。但既然文档的示例用的是 JSON.stringify（保持插入顺序），
      我们也保持插入顺序，减少"看起来不一样"的困惑。

    ★ ensure_ascii=False：
      中文按 UTF-8 原样输出。签名是对 UTF-8 字节做的
      （Node 的 .update(str,'utf8')、Java 的 getBytes("UTF-8")），
      Python 的 .encode('utf-8') 一致。
    """
    if payload is None:
        return EMPTY_BODY
    if not payload:
        return EMPTY_BODY
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def make_nonce(nbytes: int = 16) -> str:
    """
    32 位以内的随机串。文档示例用 crypto.randomBytes(16).toString('hex') → 32 位。

    ★ 用 secrets 而不是 random：
      nonce 的防重放价值依赖不可预测性，random 是可预测的伪随机。
    """
    return secrets.token_hex(nbytes)


def now_seconds() -> int:
    """★ 秒级时间戳。传毫秒会被直接判为过期（110003）。"""
    return int(time.time())


def sign_body(app_key: str, app_secret: str, timestamp: str | int,
              nonce: str, body: str) -> str:
    """
    算签名。参数就是文档给的四段拼接。

    这里特意要求传入已经序列化好的 body 字符串，
    而不是 payload 对象 —— 从类型上就杜绝"重复序列化"这个错误。
    """
    for name, value in (("app_key", app_key), ("app_secret", app_secret),
                        ("nonce", nonce)):
        if not value:
            raise SignatureError(f"{name} 为空，无法签名")
    signing_string = f"{app_key}{timestamp}{nonce}{body}"
    return hmac.new(
        app_secret.encode("utf-8"),
        signing_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class SignedRequest:
    """
    一次签名好的请求。

    ★ 设计和存在意义：body 和 headers 绑在一起，不可能拿 A 的签名配 B 的 body。
    """
    body: str
    headers: dict
    timestamp: str
    nonce: str
    sign: str


def build_request(app_key: str, app_secret: str, payload: dict | None = None,
                  *, timestamp: str | int | None = None,
                  nonce: str | None = None) -> SignedRequest:
    """
    一次做完：序列化 → 算签名 → 组装请求头。

    ★ timestamp / nonce 可以外部注入 —— 这是为了测试能构造确定性输入，
      不是为了生产环境省事。生产必须每次新生成（重试也要换新的）。
    """
    body = encode_body(payload)
    ts = str(timestamp if timestamp is not None else now_seconds())
    nc = nonce or make_nonce()
    sign = sign_body(app_key, app_secret, ts, nc, body)
    return SignedRequest(
        body=body,
        headers={
            "Content-Type": CONTENT_TYPE,
            "X-App-Key": app_key,
            "X-Timestamp": ts,
            "X-Nonce": nc,
            "X-Sign": sign,
        },
        timestamp=ts, nonce=nc, sign=sign,
    )


# ==================================================================== 自检
def _selftest() -> int:
    """
    签名自检。

    ★ 用文档给的 Node/Java 示例做交叉验证：
      文档的算法是 HMAC-SHA256(appKey+ts+nonce+body, appSecret) 十六进制。
      我们用一个固定输入，手算一遍，确认实现与文档一致。
    """
    fails = []

    def check(label, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
        if not ok:
            fails.append(label)

    # ---- 1. 拼接顺序与无分隔符 ----
    sig = sign_body("KEY", "SECRET", "1234567890", "abc", '{"a":1}')
    manual = hmac.new(b"SECRET", b'KEY1234567890abc{"a":1}', hashlib.sha256).hexdigest()
    check("待签名串是四段直接拼接、无分隔符", sig == manual, sig[:16] + "…")

    # ---- 2. 空请求体用 {} ----
    check("空 payload 序列化为 {}", encode_body(None) == "{}")
    check("空 dict 序列化为 {}", encode_body({}) == "{}")

    # ---- 3. 时间戳是秒级（10 位）----
    ts = str(now_seconds())
    check("时间戳是 10 位秒级", len(ts) == 10, ts)

    # ---- 4. nonce 长度 ≤32 且不重复 ----
    nonces = {make_nonce() for _ in range(200)}
    check("nonce 长度 ≤32", all(len(n) <= 32 for n in nonces),
          f"长度 {len(next(iter(nonces)))}")
    check("nonce 不重复（200 次）", len(nonces) == 200)

    # ---- 5. ★ 同一个 body 字符串必须算出同一个签名 ----
    payload = {"showId": "889900", "seats": [{"rowNo": 4, "colNo": 1}], "tel": "13800000000"}
    body1 = encode_body(payload)
    req1 = build_request("K", "S", payload, timestamp="1700000000", nonce="n1")
    req2 = build_request("K", "S", payload, timestamp="1700000000", nonce="n1")
    check("★ 同 body 同签名（可复现）", req1.sign == req2.sign, req1.sign[:16] + "…")
    check("★ 签名用的就是发出去的 body", req1.body == body1)

    # ---- 6. ★ body 改一个字节，签名必须变 ----
    payload2 = dict(payload)
    payload2["tel"] = "13800000001"
    req3 = build_request("K", "S", payload2, timestamp="1700000000", nonce="n1")
    check("★ body 变则签名变", req3.sign != req1.sign)

    # ---- 7. ★ 键顺序不同 → 字符串不同 → 签名不同（这正是文档警告的坑）----
    body_a = encode_body({"a": 1, "b": 2})
    body_b = encode_body({"b": 2, "a": 1})
    check("★ 键顺序不同则序列化结果不同（服务端不重排）", body_a != body_b,
          f"{body_a} vs {body_b}")

    # ---- 8. 中文按 UTF-8 签名 ----
    req_cn = build_request("K", "S", {"cinema": "万达影城"}, timestamp="1700000000", nonce="n")
    manual_cn = hmac.new(
        b"S", 'K1700000000n{"cinema":"万达影城"}'.encode("utf-8"),
        hashlib.sha256).hexdigest()
    check("中文按 UTF-8 签名", req_cn.sign == manual_cn)

    # ---- 9. 请求头齐全 ----
    need = {"Content-Type", "X-App-Key", "X-Timestamp", "X-Nonce", "X-Sign"}
    check("五个请求头齐全", need <= set(req1.headers), str(sorted(req1.headers)))
    check("Content-Type 是文档要求的值",
          req1.headers["Content-Type"] == "application/json; charset=utf-8")

    # ---- 10. 缺密钥要报错，不能静默算出错签名 ----
    for kwargs, label in [({"app_key": "", "app_secret": "S"}, "appKey 为空"),
                          ({"app_key": "K", "app_secret": ""}, "appSecret 为空")]:
        try:
            build_request(kwargs["app_key"], kwargs["app_secret"], {}, nonce="n")
            check(f"{label}时应当报错", False, "★ 没报错")
        except SignatureError:
            check(f"{label}时报错", True)

    print()
    passed = 10 + 5 - len(fails) if False else None
    print(f"结果: {len(fails) == 0 and '全部通过' or '有失败: ' + str(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
