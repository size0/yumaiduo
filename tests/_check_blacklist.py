"""黑名单接线：查到了要拦，查不到绝不能拦。

★★ 店主原话：「这里也接入一下，鱼麦多有黑名单的接口」
★★ 实测结论（写死在注释里，免得以后忘）：
     接口 GET /im/blacklist/:peerUnb 要求 scope `im.buyer_blacklist.read`。
     把它加进 manifest 后【注册照样成功】，但查询返回
       403 E_FORBIDDEN「租户未授予或凭证无此 scope」
     ⇒ 还要租户去应用市场订阅页点同意。在那之前一直是"没查成"。

★ 所以这套测试的重点不是"能不能查到"，而是：
    ① 查到了、且在名单里      → 必须拦，回配置的话术
    ② 查到了、不在名单里      → 必须放行
    ③ ★★★ 查不到（没授权/插件不通）→ 必须放行（fail-open）
       这一条最要紧：写成"查不到就拦"的话，权限没配好的店会整个瘫痪
    ④ 缓存：查到的缓存、失败的不缓存、TTL 过期重查
    ⑤ ★ _identity 要认 peerUnb —— 不认的话黑名单永远查不了
       （官方 IM payload 里买家标识的字段名就是 peerUnb）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from application import blacklist as bl                          # noqa: E402
from application import plugin_runtime as pr                      # noqa: E402
from application.service import TicketService                     # noqa: E402
from domain.quote_rules import QuoteRuleEngine                    # noqa: E402
from domain.quote_rules import tianjin_default_rules              # noqa: E402
from integrations.liangpiao.api import LiangPiaoAPI               # noqa: E402
from integrations.liangpiao.client import LiangPiaoClient         # noqa: E402
from application import ledger as ledger_mod                      # noqa: E402
import settings as settings_mod                                   # noqa: E402

RES = []
SEC = "k" * 64


def check(label, ok, detail=""):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


class T:
    """假 transport：记调用次数，返回固定 (status, body)。"""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.calls = 0

    def send(self, url, body, headers, timeout):
        self.calls += 1
        return self.status, self.body


print("=" * 74)
print("一、查到了 ⇒ 该拦就拦、不该拦就放")
print("=" * 74)
bl.clear_cache()
t_in = T(200, '{"ok":true,"blacklisted":true,'
              '"reason":"恶意拍下不付款","peerNick":"闲鱼用户8777"}')
r = bl.check("S1", "P1", transport=t_in, now=1000, secret=SEC, tenant_id="107")
check("★ 在名单里 ⇒ ok 且 blacklisted", r["ok"] and r["blacklisted"],
      bl.status_text(r))
check("★ 带出了理由和昵称（排查要用）",
      "恶意拍下" in r["reason"] and "8777" in r["peer_nick"])
check("★★ is_blacklisted 为 True", bl.is_blacklisted(
    "S1", "P1", transport=t_in, now=1000, secret=SEC, tenant_id="107"))

t_out = T(200, '{"ok":true,"blacklisted":false}')
r2 = bl.check("S1", "P2", transport=t_out, now=1000, secret=SEC, tenant_id="107")
check("★ 不在名单 ⇒ ok=True 但 blacklisted=False",
      r2["ok"] and not r2["blacklisted"], bl.status_text(r2))
check("★★ 不误拦（is_blacklisted=False）",
      not bl.is_blacklisted("S1", "P2", transport=t_out, now=1000,
                            secret=SEC, tenant_id="107"))

print()
print("=" * 74)
print("二、★★★ 查不到 ⇒ 一律放行（fail-open，最要紧的一条）")
print("=" * 74)
cases = [
    ("租户没同意权限（403）", 403, '{"ok":false,"error":"x"}'),
    ("插件不通（502）", 502, "bad gateway"),
    ("返回不是 JSON", 200, "not-json"),
    ("返回形状不对", 200, "[1,2,3]"),
]
for label, status, body in cases:
    bl.clear_cache()
    tt = T(status, body)
    got = bl.check("S1", "PX", transport=tt, now=1000, secret=SEC,
                   tenant_id="107")
    check(f"★★ {label} ⇒ ok=False 且不拦",
          (not got["ok"]) and (not got["blacklisted"])
          and (not bl.is_blacklisted("S1", "PX", transport=tt, now=1000,
                                     secret=SEC, tenant_id="107")),
          f"ok={got['ok']} code={got['error_code']}")

check("★ 403 单独给一个「没授权」原因码（和真故障分开）",
      bl.check("S1", "PY", transport=T(403, "{}"), now=1, secret=SEC,
               tenant_id="107")["error_code"] == "BLACKLIST_SCOPE_REQUIRED")

r_nopeer = bl.check("S1", "", secret=SEC, tenant_id="107")
check("★ 没有买家标识 ⇒ 不查、不拦", (not r_nopeer["ok"])
      and r_nopeer["error_code"] == "BLACKLIST_NO_PEER")

print()
print("=" * 74)
print("三、缓存：查到的缓存、失败的【不】缓存、TTL 过期重查")
print("=" * 74)
bl.clear_cache()
tc = T(200, '{"ok":true,"blacklisted":false}')
bl.check("S1", "C1", transport=tc, now=1000, secret=SEC, tenant_id="107")
r_c = bl.check("S1", "C1", transport=tc, now=1001, secret=SEC, tenant_id="107")
check("★ 第二次命中缓存（没再打插件）", r_c["cached"] and tc.calls == 1,
      f"calls={tc.calls}")

bl.clear_cache()
tf = T(403, "{}")
bl.check("S1", "F1", transport=tf, now=1000, secret=SEC, tenant_id="107")
bl.check("S1", "F1", transport=tf, now=1001, secret=SEC, tenant_id="107")
check("★★★ 失败【不】缓存（否则一次抖动=十分钟误判）", tf.calls == 2,
      f"calls={tf.calls}")

bl.clear_cache()
ttl = T(200, '{"ok":true,"blacklisted":true}')
a = bl.check("S1", "T1", transport=ttl, now=1000, secret=SEC, tenant_id="107",
             ttl=5)
b = bl.check("S1", "T1", transport=ttl, now=1003, secret=SEC, tenant_id="107",
             ttl=5)
c = bl.check("S1", "T1", transport=ttl, now=1006, secret=SEC, tenant_id="107",
             ttl=5)
check("★ TTL 内命中、过期后重查",
      (not a["cached"]) and b["cached"] and (not c["cached"]) and ttl.calls == 2,
      f"calls={ttl.calls}")

check("★ 不同店铺各自缓存（不会串店）",
      bl.cache_key("S1", "P") != bl.cache_key("S2", "P"))

print()
print("=" * 74)
print("四、★★★ 闸门：在名单里回话术，不在/查不到照常走")
print("=" * 74)


def make_service():
    cl = LiangPiaoClient(SEC[:16], SEC[:32], base_url="https://x.test",
                         transport=T(500, "{}"), sleep=lambda: None)
    s = ledger_mod.stores()
    return TicketService(LiangPiaoAPI(cl),
                         QuoteRuleEngine(tianjin_default_rules()),
                         tel="13800000000", app_key=SEC[:16],
                         app_secret=SEC[:32], persist=False,
                         quote_store=s.quotes, order_store=s.orders)


cfg = settings_mod.load()
_reply = cfg.blacklist.reply
print(f"  配置的话术: {_reply!r}")

svc = make_service()
bl.clear_cache()

# ① 在名单里 ⇒ 拦
import application.blacklist as _bl                                  # noqa: E402
_orig_check = _bl.check
_bl.check = lambda shop, peer, **kw: {
    "ok": True, "blacklisted": True, "reason": "r", "peer_nick": "n",
    "cached": False, "error": "", "error_code": ""}
try:
    out = svc.on_message("BL-A", text="你好", shop_id="S1", peer_unb="P1")
    check("★★★ 在名单里 ⇒ 回配置的话术",
          any(_reply and _reply in m for m in out.messages),
          str(out.messages)[:90])
    check("★ 打了 BLACKLIST 标记（便于排查）",
          "BLACKLIST" in (out.template_keys or []))
    check("★ 不改会话状态（他被移出名单后还能接着走）",
          out.state == svc.session("BL-A").conversation.state,
          str(out.state))

    # ② 不在名单 / 查不到 ⇒ 不能拦
    for label, payload in (
            ("不在名单", {"ok": True, "blacklisted": False}),
            ("查不到（没授权）", {"ok": False, "blacklisted": False,
                                 "error_code": "BLACKLIST_SCOPE_REQUIRED"}),
            ("查不到（插件不通）", {"ok": False, "blacklisted": False,
                                   "error_code": "PLUGIN_UNREACHABLE"})):
        bl.clear_cache()
        _bl.check = lambda shop, peer, _p=payload, **kw: dict(
            _p, reason="", peer_nick="", cached=False, error="e",
            error_code=_p.get("error_code", ""))
        o = svc.on_message(f"BL-{label}", text="你好", shop_id="S1",
                           peer_unb="P2")
        blocked = any(_reply and _reply in m for m in o.messages)
        check(f"★★★ {label} ⇒ 【不】用黑名单话术挡人", not blocked,
              str(o.messages)[:80])

    # ④ 没传 peer_unb（内部调用/测试）⇒ 根本不查
    bl.clear_cache()
    _calls = {"n": 0}

    def _counting(shop, peer, **kw):
        _calls["n"] += 1
        return {"ok": True, "blacklisted": False, "reason": "", "peer_nick": "",
                "cached": False, "error": "", "error_code": ""}
    _bl.check = _counting
    svc.on_message("BL-NOPEER", text="你好")
    check("★★ 没传买家标识 ⇒ 不查黑名单（不会拿 chat_id 乱查）",
          _calls["n"] == 0, f"查了 {_calls['n']} 次")
finally:
    _bl.check = _orig_check

print()
print("=" * 74)
print("五、★★★ _identity 必须认 peerUnb（否则黑名单永远查不了）")
print("=" * 74)
ident = pr._identity({
    "tenantId": "107", "eventId": "e1",
    "payload": {"chatId": "chat-1", "accountUnb": "shop-1",
                "peerUnb": "2208999888777", "content": "你好"},
})
check("★★★ 从 payload.peerUnb 取到 buyer_id",
      ident["buyer_id"] == "2208999888777", str(ident))
check("★★★ 从 payload.accountUnb 取到 shop_id",
      ident["shop_id"] == "shop-1", str(ident["shop_id"]))
ident2 = pr._identity({"tenantId": "107",
                       "payload": {"chatId": "c", "peerUnb": "p",
                                   "shopId": "s"}})
check("★ shopId 在 payload 里也认", ident2["shop_id"] == "s", str(ident2))
ident4 = pr._identity({"tenantId": "107", "shopId": "explicit",
                       "payload": {"accountUnb": "fallback"}})
check("★ 显式 shopId 优先于 accountUnb",
      ident4["shop_id"] == "explicit", str(ident4))
ident3 = pr._identity({"tenantId": "107", "buyerId": "explicit",
                       "payload": {"chatId": "c", "peerUnb": "peer"}})
check("★ 显式 buyerId 优先于 peerUnb（有些路径会带）",
      ident3["buyer_id"] == "explicit", str(ident3))

image_payload = {"messageType": 2, "content": "https://example.test/image.jpg",
                 "imageUrls": ["https://example.test/image.jpg"]}
image_text, image_url = pr._extract_message(image_payload)
check("★★★ 图片消息从 imageUrls 识别图片",
      image_url == "https://example.test/image.jpg" and image_text == "",
      str((image_text, image_url)))
image_text, image_url = pr._extract_message(
    {"messageType": 2, "content": "https://example.test/fallback.jpg"})
check("★ 图片消息缺 imageUrls 时从 content 识别图片",
      image_url == "https://example.test/fallback.jpg" and image_text == "",
      str((image_text, image_url)))
image_text, image_url = pr._extract_message(
    {"messageType": 1, "content": "https://example.test/page"})
check("★ 普通文字中的链接不误判为图片",
      image_text == "https://example.test/page" and image_url == "",
      str((image_text, image_url)))

print()
print("=" * 74)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 74)
sys.exit(0 if all(RES) else 1)
