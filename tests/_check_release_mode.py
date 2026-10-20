# 良票链路夹具使用非万达影院；万达缺桥必须阻断，见 test_city_code_cache.py。
"""「出票及放单」两个开关真的接线了吗。

★★ 店主原话：「图二中没有接入」「后台不要硬编码 由这里控制」

  这两个原来是**静默失效**的开关：
    · 出票方式（手动/自动）：代码里永远自动建单，根本没读这个配置
    · 换座状态（允许/不允许）：api.ch5_create 的 payload 里一直有
      allowSeatChange 字段，但调用处【从来没传过】⇒ 永远走签名默认 True

  ⇒ 界面上填了没用，而且不报错。
  ⇒ 所以这里的判据是【行为】：手动模式真的不建单；不允许换座真的传 False。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from application import ledger as ledger_mod                       # noqa: E402
from application.ticket_flow import FlowResult                    # noqa: E402
from application import ticket_flow as tf                          # noqa: E402
from application.service import TicketService                      # noqa: E402
from domain.quote_rules import QuoteRuleEngine                     # noqa: E402
from domain.quote_rules import tianjin_default_rules               # noqa: E402
from integrations.liangpiao.api import LiangPiaoAPI               # noqa: E402
from integrations.liangpiao.client import LiangPiaoClient          # noqa: E402
import settings as settings_mod                                    # noqa: E402

RES = []


def check(label, ok, detail=""):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


KEY, SECRET = "k" * 16, "s" * 32


class T:
    """记录所有出站请求的假 transport。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def send(self, url, body, headers, timeout):
        p = url.split("/api/v1", 1)[-1]
        self.calls.append({"path": p, "body": json.loads(body or "{}")})
        h = self.routes.get(p)
        if h is None:
            raise AssertionError(f"没打桩的接口: {p}")
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


tmp = Path(tempfile.mkdtemp())
old_cfg = settings_mod.CONFIG_FILE
old_get = tf.get_local_service if hasattr(tf, "get_local_service") else None
settings_mod.CONFIG_FILE = tmp / "settings.json"
ledger_mod.set_paths(quotes=str(tmp / "q.sqlite3"), orders=str(tmp / "o.sqlite3"))

from application import store as store_mod                         # noqa: E402
_sess = store_mod.SESSIONS_FILE
_backup = _sess.read_bytes() if _sess.exists() else None
if _sess.exists():
    _sess.unlink()

try:
    print("=" * 74)
    print("一、★ 换座状态：不允许 ⇒ 必须传 allowSeatChange=False")
    print("=" * 74)
    s = settings_mod.load()
    s.ticket_release.allow_seat_change = False
    settings_mod.save(s)
    check("配置读回来 False", tf._allow_seat_change() is False)

    svc, tr = make()
    r = svc.on_message("RM-A", image_url="https://x/a.jpg", city_name="天津")
    check("报价成功", r.ok, str(r.error))
    r2 = svc.on_message("RM-A", text="正确")
    check("确认成功", r2.ok, str(r2.error))
    # 付款 → 这一步才会调 ch5_create
    from application import plugin_runtime as pr                   # noqa: E402
    pr.get_local_service = lambda: svc
    # ★ 这里【直接调编排层】（on_payment_confirmed），不走 order.paid 事件：
    #   那条事件路上还有"实付金额必须等于报价总额"的门，
    #   而我这一段要验的是【换座字段有没有传下去】，不是金额门。
    #   ★ 判据要盯住被测的那一件事，别让它被另一个正确行为挡住 ——
    #     我第一版就是被金额门挡住，得到"没走到 create"这种误导性失败。
    # ★ Conversation 上本来就存着 quote（字段 quote），不用重建 ——
    #   我第一版想调 _build_quote 重建，结果它要 RecognitionResult，
    #   而 Session 上根本没有那个字段。绕了一圈不如直接读现成的。
    conv_a = svc.session("RM-A").conversation
    conv_a.quote_channel = "LIANGPIAO"
    conv_a.pending_out_order_no = "OUT-A"
    out_a = svc.flow.on_payment_confirmed(
        conv_a, FlowResult(),
        paid_fen=int(getattr(conv_a.quote, "total_price_fen", 0) or 0))
    creates = [c for c in tr.calls if c["path"] == "/order/create"]
    if creates:
        body = creates[-1]["body"]
        check("★★★ allowSeatChange 真的传下去了，且是 False",
              body.get("allowSeatChange") is False,
              f"allowSeatChange={body.get('allowSeatChange')!r}")
    else:
        check("★★★ 走到了 /order/create（否则没法验换座）", False,
              f"实际调用: {[c['path'] for c in tr.calls]} | "
              f"notes={getattr(out_a, 'notes', None)}")

    print()
    print("=" * 74)
    print("二、★★★ 出票方式=手动 ⇒ 不许自动建单")
    print("=" * 74)
    s = settings_mod.load()
    s.ticket_release.mode = "手动"
    settings_mod.save(s)
    check("配置读回来 手动", tf._release_mode() == tf.RELEASE_MANUAL)

    svc2, tr2 = make()
    r3 = svc2.on_message("RM-B", image_url="https://x/b.jpg", city_name="天津")
    check("报价成功", r3.ok, str(r3.error))
    r4 = svc2.on_message("RM-B", text="正确")
    check("确认成功", r4.ok, str(r4.error))
    conv_b = svc2.session("RM-B").conversation
    conv_b.quote_channel = "LIANGPIAO"
    conv_b.pending_out_order_no = "OUT-B"
    out_b = svc2.flow.on_payment_confirmed(
        conv_b, FlowResult(),
        paid_fen=int(getattr(conv_b.quote, "total_price_fen", 0) or 0))
    paths2 = [c["path"] for c in tr2.calls]
    check("★★★ 手动模式【没有】调 /order/create",
          "/order/create" not in paths2,
          str(paths2) + " | notes=" + str(getattr(out_b, "notes", None)))
    check("★ 手动模式给出了明确说明（不是静默什么都不做）",
          any("手动" in str(n) for n in (getattr(out_b, "notes", None) or [])),
          str(getattr(out_b, "notes", None))[:160])

    print()
    print("=" * 74)
    print("三、再切回自动 ⇒ 恢复建单（别把功能关死）")
    print("=" * 74)
    s = settings_mod.load()
    s.ticket_release.mode = "自动"
    s.ticket_release.allow_seat_change = True
    settings_mod.save(s)
    check("配置读回来 自动", tf._release_mode() == tf.RELEASE_AUTO)
    svc3, tr3 = make()
    r5 = svc3.on_message("RM-C", image_url="https://x/c.jpg", city_name="天津")
    check("报价成功", r5.ok, str(r5.error))
    r6 = svc3.on_message("RM-C", text="正确")
    check("确认成功", r6.ok, str(r6.error))
    conv_c = svc3.session("RM-C").conversation
    conv_c.quote_channel = "LIANGPIAO"
    conv_c.pending_out_order_no = "OUT-C"
    out_c = svc3.flow.on_payment_confirmed(
        conv_c, FlowResult(),
        paid_fen=int(getattr(conv_c.quote, "total_price_fen", 0) or 0))
    creates3 = [c for c in tr3.calls if c["path"] == "/order/create"]
    check("★★ 自动模式又建单了", bool(creates3),
          str([c["path"] for c in tr3.calls]) + " | notes="
          + str(getattr(out_c, "notes", None))[:150])
    if creates3:
        check("★★ 换座允许 ⇒ allowSeatChange=True",
              creates3[-1]["body"].get("allowSeatChange") is True,
              str(creates3[-1]["body"].get("allowSeatChange")))

    print()
    print("=" * 74)
    print("四、★ 读不到配置时的兜底（不许把下单搞挂）")
    print("=" * 74)
    settings_mod.CONFIG_FILE = tmp / "nonexistent" / "settings.json"
    check("★★ 读不到 ⇒ 出票方式=自动（照旧建单）",
          tf._release_mode() == tf.RELEASE_AUTO, tf._release_mode())
    check("★★ 读不到 ⇒ 允许换座（= 签名默认，不失败）",
          tf._allow_seat_change() is True)

finally:
    settings_mod.CONFIG_FILE = old_cfg
    ledger_mod.set_paths(quotes=None, orders=None)
    if _backup is None:
        if _sess.exists():
            _sess.unlink()
    else:
        _sess.write_bytes(_backup)

print()
print("=" * 74)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 74)
sys.exit(0 if all(RES) else 1)
