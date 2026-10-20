"""
integrations/yumaiduo/signature.py —— 鱼麦多插件签名校验

════════════════════════════════════════════════════════════════
★ 协议（抄自插件源码 backend-request.mjs:9，不是猜的）

    签名 = HMAC-SHA256(secret, f"{timestamp}.{rawBody}")  十六进制
    请求头：x-yumaiduo-timestamp（毫秒字符串）
            x-yumaiduo-signature（上面那个签名）

    插件那边：
        const signature = createHmac('sha256', secret())
            .update(`${timestamp}.${raw}`).digest('hex');

★★ 两个必须守住的点

  ① 签名必须对【原始字节】算，不能对"解析后再序列化"的内容算。
     JSON 的键顺序、空格、转义在往返之后都可能变 ——
     用 json.dumps(body) 去算签名，合法的请求会被判成伪造。
     ⇒ 所以这一层拿 rawBody（bytes/str），调用方不许先 json.loads。

  ② 时间戳要卡窗口。
     没有窗口的话，一个抓到的旧请求可以永远重放。
     ★ 但窗口不能太窄：插件的请求可能因为网络慢而延迟到达，
       太窄会把正常请求判成过期。取 5 分钟。
════════════════════════════════════════════════════════════════
"""

import hashlib
import hmac
import time

# ★ 时间戳允许的偏差（秒）。见模块头 ② 的说明。
DEFAULT_WINDOW_SECONDS = 300


def sign(secret: str, timestamp: str, raw_body: str) -> str:
    """
    算签名。★ 这个函数测试和"当客户端"时都要用 ——
    只有一份实现，才不会出现"自己签的自己验不过"。
    """
    message = f"{timestamp}.{raw_body}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str | None, signature: str | None,
           raw_body: str, *, now_ms: int | None = None,
           window_seconds: int = DEFAULT_WINDOW_SECONDS) -> tuple:
    """
    校验签名。返回 (通过?, 原因)。

    ★ 返回原因而不是布尔：出问题时能直接看出来是哪一步挂的
      （没配密钥 / 少头 / 时间戳过期 / 签名不对），
      否则线上只会看到一个 401，什么都不知道。

    ★★ 为什么用 hmac.compare_digest 而不是 == ：
      字符串 == 在第一个不同字符处就返回，
      攻击者能靠响应时间差逐字节猜出签名（时序攻击）。
    """
    if not secret:
        return False, "NOT_CONFIGURED"
    if not timestamp or not signature:
        return False, "MISSING_HEADER"
    timestamp = str(timestamp).strip()
    if not timestamp.isdigit():
        return False, "BAD_TIMESTAMP"

    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        delta_ms = abs(now_ms - int(timestamp))
    except ValueError:
        return False, "BAD_TIMESTAMP"
    if delta_ms > window_seconds * 1000:
        return False, "TIMESTAMP_OUT_OF_WINDOW"

    expected = sign(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected, str(signature).strip().lower()):
        return False, "BAD_SIGNATURE"
    return True, ""
