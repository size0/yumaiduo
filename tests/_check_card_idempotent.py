# 良票链路夹具使用非万达影院；万达缺桥必须阻断，见 test_city_code_cache.py。
"""同一张闲鱼订单卡片重投两次 —— 会不会重复落单？

★ 为什么单测这一条：
   平台 webhook 是【至少投递一次】（会重试）。而我在 _from_order_card 里
   加了"没有单就补落一张"的逻辑 —— 补落单本身不是幂等的，
   如果重投时前面那步（找已有单）没找到，就会落第二张。

   ⇒ 这比"买家先回正确"那个场景重要得多：
     重复落单的后果是买家收到两条改价、订单管理页出现两张一样的单。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from domain.quote_rules import QuoteRuleEngine                     # noqa: E402
from domain.quote_rules import tianjin_default_rules               # noqa: E402
from integrations.liangpiao.api import LiangPiaoAPI                # noqa: E402
from integrations.liangpiao.client import LiangPiaoClient          # noqa: E402
from application.service import TicketService                      # noqa: E402
from application import ledger as ledger_mod                       # noqa: E402
from application import plugin_runtime as pr                       # noqa: E402

KEY, SECRET = "k" * 16, "s" * 32
RES = []


def check(label, ok, detail=""):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


class T:
    def __init__(self, routes):
        self.routes = routes

    def send(self, url, body, headers, timeout):
        p = url.split("/api/v1", 1)[-1]
        h = self.routes.get(p)
        if h is None:
            raise AssertionError(p)
        return h(json.loads(body or "{}")) if callable(h) else h


def env(d=None):
    return json.dumps({"code": 0, "message": "ok", "data": d}, ensure_ascii=False)


EXACT = {"recognizeId": "1", "cached": False, "rawResults": {},
         "finalResults": {
             "matchLevel": "EXACT", "cinemaId": 1, "cinemaName": "CGV影城",
             "cityName": "天津", "brandName": "CGV影城", "movieId": 1,
             "movieName": "片", "hallName": "1厅",
             "startTime": "2099-07-28T19:30:00+08:00", "showId": "S1",
             "cinemaHitNums": 1,
             "seat": [{"rowNo": 1, "colNo": 1, "seatNo": "1排1座",
                       "originalPriceFen": "3890", "areaId": "1"}],
             "seatMatched": True, "prices": [],
             "candidates": {"cinemas": [], "movies": [], "shows": []}}}

ROUTES = {"/recognize/seat-shot": (200, env(EXACT)),
          "/order/preflight": (200, env({"available": True,
                                         "estimateAmount": "3310",
                                         "totalAmount": "3510"})),
          "/order/create": (200, env({"orderNo": "L1", "status": "SUBMITTING",
                                      "freezeAmount": "3510", "seatCount": 1})),
          "/order/detail": (200, env({"orderNo": "L1", "status": "TICKETED"}))}


def make():
    tr = T(ROUTES)
    cl = LiangPiaoClient(KEY, SECRET, base_url="https://example.test",
                         transport=tr, sleep=lambda: None)
    s = ledger_mod.stores()
    return TicketService(LiangPiaoAPI(cl),
                         QuoteRuleEngine(tianjin_default_rules()),
                         tel="13800000000", app_key=KEY, app_secret=SECRET,
                         persist=True, quote_store=s.quotes,
                         order_store=s.orders), tr


def card(chat, oid, eid=None):
    return {"event": "im.message.received", "eventId": eid or f"c-{oid}",
            "tenantId": "107",
            "payload": {"messageType": "26", "orderId": oid, "chatId": chat,
                        "shopId": "1"}}


def mine(chat):
    return [o for o in (ledger_mod.orders_payload().get("orders") or [])
            if o.get("chat_id") == chat]


tmp = Path(tempfile.mkdtemp())
old = pr.get_local_service
backup = None
sess = None
try:
    ledger_mod.set_paths(quotes=str(tmp / "q.sqlite3"),
                         orders=str(tmp / "o.sqlite3"))
    from application import store as sm
    sess = sm.SESSIONS_FILE
    if sess.exists():
        backup = sess.read_bytes()
        sess.unlink()

    svc, tr = make()
    pr.get_local_service = lambda: svc
    chat = "IDEMPOTENT-CHAT"
    r = svc.on_message(chat, image_url="https://x/a.jpg", city_name="天津")
    print(f"  报价 ok={r.ok} state={r.state.value}")

    print()
    print("=== ① 同一张卡片，同样的 eventId，投两次 ===")
    pr.events_to_commands(card(chat, "ONLINE-Z", "evt-1"))
    n1 = len(mine(chat))
    pr.events_to_commands(card(chat, "ONLINE-Z", "evt-1"))
    n2 = len(mine(chat))
    check("重投同一事件不重复落单", n2 == n1 and n1 == 1, f"第一次={n1} 第二次={n2}")

    print()
    print("=== ② 同一张卡片，但 eventId 不同（平台重试时 id 可能变）===")
    pr.events_to_commands(card(chat, "ONLINE-Z", "evt-2"))
    n3 = len(mine(chat))
    check("订单号相同也不重复落单（按 provider_order_id 找到已有单）",
          n3 == n1, f"第一次={n1} 换 id 后={n3}")

    print()
    print("=== ③ 改价命令的幂等键（同一订单只该有一条）===")
    cmds = pr.events_to_commands(card(chat, "ONLINE-Z", "evt-3"))
    keys = [getattr(c, "dedupe_key", "") for c in cmds
            if getattr(c, "type", "") == "CHANGE_ORDER_PRICE"]
    check("改价命令带稳定幂等键（同一订单号）",
          all(k == "reprice:ONLINE-Z" for k in keys), str(keys))
finally:
    pr.get_local_service = old
    ledger_mod.set_paths(quotes=None, orders=None)
    if sess is not None:
        if backup is None:
            if sess.exists():
                sess.unlink()
        else:
            sess.write_bytes(backup)

print()
print("=" * 74)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 74)
sys.exit(0 if all(RES) else 1)
