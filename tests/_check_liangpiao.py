"""
_check_liangpiao.py —— 良票接入层自检（不需要 AppKey，不联网）

════════════════════════════════════════════════════════════════
★ 为什么能在没有凭证的情况下测完

  真正发 HTTP 的部分抽成了 Transport，测试里换成 FakeTransport。
  于是签名、错误码分类、重试策略、nonce 不复用、requestId 记录
  —— 这些"接入期最容易错"的逻辑全都能测。

  ★ 换来的价值：等真的拿到 AppKey 时，这里应该是全绿的。
    如果那时 `ping` 还失败，问题就只可能在凭证或网络，不在我们的代码。
════════════════════════════════════════════════════════════════
"""

import json
import sys
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


RESULTS = []

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations.liangpiao import signature as sig
from integrations.liangpiao.client import (NEVER_RETRY_CODES,
                                            ApiResponse, AuthError, BusinessError,
                                            LiangPiaoClient, RateLimitError,
                                            RetryableError, TransportError)




def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


# ==================================================================== 假传输
class FakeTransport:
    """
    按脚本回应的假传输。

    ★ 它记录下每次收到的 body 和 headers —— 这正是验证
      "签名用的字符串 == 发出去的字符串" 的唯一办法。
    """

    def __init__(self, responses):
        # responses: [(http_status, body_text), ...]，耗尽后重复最后一个
        self.responses = list(responses)
        self.sent: list[dict] = []

    def send(self, url, body, headers, timeout):
        self.sent.append({"url": url, "body": body, "headers": dict(headers),
                          "timeout": timeout})
        idx = min(len(self.sent) - 1, len(self.responses) - 1)
        resp = self.responses[idx]
        if isinstance(resp, Exception):
            raise resp
        return resp


def envelope(code=0, message="success", request_id="REQ123", data=None) -> str:
    body = {"code": code, "message": message, "requestId": request_id}
    if data is not None:
        body["data"] = data
    return json.dumps(body, ensure_ascii=False)


def make_client(responses, **kw):
    fake = FakeTransport(responses)
    client = LiangPiaoClient("TESTKEY", "TESTSECRET", base_url="https://example.test",
                             transport=fake, sleep=lambda _: None, **kw)
    return client, fake


# ==================================================================== 测试
def main() -> int:
    section("一、成功路径")
    client, fake = make_client([(200, envelope(0, data={"channelId": 7, "env": "PROD"}))])
    resp = client.ping()
    check("ping 成功", resp.ok and resp.data["channelId"] == 7)
    check("记录了 requestId", resp.request_id == "REQ123")
    check("path 拼成了 /api/v1/ping", fake.sent[0]["url"].endswith("/api/v1/ping"),
          fake.sent[0]["url"])
    check("是 POST 且带 body", fake.sent[0]["body"] == "{}", fake.sent[0]["body"])

    section("二、★ 签名与发出的 body 必须是同一个字符串")
    payload = {"showId": "889900", "seats": [{"rowNo": 4, "colNo": 1}], "tel": "13800000000"}
    client, fake = make_client([(200, envelope(0, data={}))])
    client.call("/order/preflight", payload)
    sent = fake.sent[0]
    # 用发出的 body 和 headers 重算签名，必须一模一样
    recomputed = sig.sign_body("TESTKEY", "TESTSECRET",
                               sent["headers"]["X-Timestamp"],
                               sent["headers"]["X-Nonce"],
                               sent["body"])
    check("★ 用发出去的 body 重算签名，与服务端收到的一致",
          recomputed == sent["headers"]["X-Sign"],
          f"{recomputed[:16]}…")

    # 如果实现是"先算签名再重新序列化"，键顺序一变就会对不上。
    reserialized = json.dumps(payload, ensure_ascii=False)      # 带空格
    wrong = sig.sign_body("TESTKEY", "TESTSECRET", sent["headers"]["X-Timestamp"],
                          sent["headers"]["X-Nonce"], reserialized)
    check("★ 换成另一种序列化方式签名就会对不上（所以只能序列化一次）",
          wrong != sent["headers"]["X-Sign"],
          "证明必须复用同一个字符串")

    section("三、★ nonce 不复用（重试必须换新的）")
    client, fake = make_client([
        (200, envelope(100005, "服务内部错误")),
        (200, envelope(0, data={"ok": True})),
    ])
    client.call("/city/list", {}, retries=2)
    nonces = [s["headers"]["X-Nonce"] for s in fake.sent]
    check("重试了 2 次", len(fake.sent) == 2, f"{len(fake.sent)} 次")
    check("★ 两次 nonce 不同", len(set(nonces)) == 2, str(nonces))
    ts = [s["headers"]["X-Timestamp"] for s in fake.sent]
    check("时间戳是 10 位秒级", all(len(t) == 10 for t in ts), str(ts))

    section("四、★ 110002 签名失败绝不重试")
    client, fake = make_client([(200, envelope(110002, "签名校验失败"))])
    try:
        client.call("/ping", {}, retries=3)
        check("应当抛 AuthError", False, "★ 没抛")
    except AuthError as exc:
        check("抛出 AuthError", True, str(exc))
    check("★ 只发了 1 次，没有重试", len(fake.sent) == 1, f"{len(fake.sent)} 次")
    check("110002 在 NEVER_RETRY 名单里", 110002 in NEVER_RETRY_CODES)

    section("五、★ 300010 座位正在出票也不重试（重试会重复提交）")
    client, fake = make_client([
        (200, envelope(300010, "该场次座位已有提交的订单正在出票")),
    ])
    try:
        client.call("/order/create", {"outOrderNo": "X"}, retries=3)
        check("应当抛 BusinessError", False, "★ 没抛")
    except BusinessError:
        check("抛出 BusinessError", True)
        check("★ 只发了 1 次", len(fake.sent) == 1, f"{len(fake.sent)} 次")

    section("六、错误码分类")
    cases = [
        (110002, "签名校验失败", AuthError),
        (110003, "请求时间戳已过期", AuthError),
        (120001, "请求过于频繁", RateLimitError),
        (100005, "服务内部错误", RetryableError),
        (600002, "供应商响应超时", RetryableError),
        (200004, "场次不存在或已下架", BusinessError),
        (300004, "暂无可用供应商", BusinessError),
        (300005, "实际价格超过限价", BusinessError),
        (400002, "账户可用余额不足", BusinessError),
        (700009, "所选候选无效", BusinessError),
    ]
    for code, msg, expected in cases:
        client, _ = make_client([(200, envelope(code, msg))])
        try:
            client.call("/ping", {})
        except Exception as exc:                    # noqa: BLE001
            check(f"{code} {msg[:12]} → {expected.__name__}",
                  isinstance(exc, expected),
                  f"实际 {type(exc).__name__}")
            continue
        check(f"{code} 应当抛错", False)

    section("七、可重试错误会退避重试")
    client, fake = make_client([
        (200, envelope(600002, "供应商响应超时")),
        (200, envelope(600002, "供应商响应超时")),
        (200, envelope(0, data={"list": []})),
    ])
    waits = []
    client._sleep = lambda s: waits.append(s)
    resp = client.call("/seat/list", {"showId": "1"}, retries=3, backoff=1.0)
    check("最终成功", resp.ok)
    check("重试到第 3 次才成功", len(fake.sent) == 3, f"{len(fake.sent)} 次")
    check("退避时间递增", len(waits) >= 2 and waits[0] < waits[1], str(waits))

    section("八、限流退避更久")
    client, fake = make_client([
        (200, envelope(120001, "请求过于频繁")),
        (200, envelope(0, data={})),
    ])
    waits = []
    client._sleep = lambda s: waits.append(s)
    client.call("/city/list", {}, retries=2, backoff=1.0)
    check("限流后重试成功", len(fake.sent) == 2)
    check("限流退避 ≥ 普通退避（1.0 → 4.0）", waits and waits[0] >= 4.0, str(waits))

    section("九、HTTP 状态码不是成功判据")
    # 平台说恒为 200，但网关可能回 502；只要 body 里有合法 code 就按 code 处理
    client, _ = make_client([(502, envelope(0, data={"ok": 1}))])
    check("HTTP 502 但 code=0 → 视为成功", client.ping().ok)
    client, _ = make_client([(200, envelope(200004, "场次不存在或已下架"))])
    try:
        client.ping()
        check("HTTP 200 但 code≠0 → 视为失败", False)
    except BusinessError:
        check("HTTP 200 但 code≠0 → 视为失败", True)

    section("十、响应异常处理")
    # 空响应
    client, _ = make_client([(200, "")])
    try:
        client.ping()
        check("空响应应报 TransportError", False)
    except TransportError:
        check("空响应报 TransportError", True)
    # 不是 JSON
    client, _ = make_client([(200, "<html>502 Bad Gateway</html>")])
    try:
        client.ping()
        check("非 JSON 应报 TransportError", False)
    except TransportError:
        check("非 JSON 报 TransportError", True)
    # 缺 code
    client, _ = make_client([(200, '{"msg":"weird"}')])
    try:
        client.ping()
        check("缺 code 应报 TransportError", False)
    except TransportError:
        check("缺 code 报 TransportError", True)

    section("十一、网络异常可重试")
    client, fake = make_client([
        TransportError("连接被重置"),
        (200, envelope(0, data={"ok": 1})),
    ])
    resp = client.call("/ping", {}, retries=2)
    check("网络失败后重试成功", resp.ok and len(fake.sent) == 2)

    section("十二、审计记录")
    client, _ = make_client([(200, envelope(0, request_id="RID-42", data={}))])
    client.ping()
    check("last_request_id 可用", client.last_request_id() == "RID-42")
    st = client.stats()
    check("stats 统计调用", st["calls"] == 1 and st["codes"] == {0: 1}, str(st))

    section("十三、默认不重试（安全性默认值）")
    client, fake = make_client([(200, envelope(600002, "供应商响应超时"))])
    try:
        client.call("/ping", {})
    except RetryableError:
        pass
    check("★ retries 默认 0 —— 只发一次", len(fake.sent) == 1, f"{len(fake.sent)} 次")

    section("十四、超时按接口设置")
    client, fake = make_client([(200, envelope(0, data={}))])
    client.call("/seat/list", {}, timeout=12.0)
    check("座位图超时 12 秒（文档要求 ≥10 秒）", fake.sent[0]["timeout"] == 12.0)
    client, fake = make_client([(200, envelope(0, data={}))])
    client.call("/recognize/seat-shot", {}, timeout=5.0)
    check("识别超时 5 秒（文档 p95 5.2s）", fake.sent[0]["timeout"] == 5.0)

    section("十五、凭证校验")
    try:
        LiangPiaoClient("", "")
        check("空凭证应报错", False)
    except ValueError:
        check("空凭证报 ValueError", True)

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    for i, ok in enumerate(RESULTS):
        if not ok:
            print(f"  ✗ 第 {i+1} 项失败")
    print(f"\n{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
