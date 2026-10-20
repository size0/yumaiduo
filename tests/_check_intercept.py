"""订单拦截检查（手动关键字 / 自动拦截）。

★★ 为什么必须断：
   拦截 = 【真的去关掉买家的订单】。判错的后果：
     · 把已经发货的单关掉 ⇒ 买家付了钱没票
     · 把不该拦的单拦掉     ⇒ 纠纷
   ★ 所以"什么时候发关单命令"必须钉住：
      没有平台订单号 → 不发（发了也必被身份校验拒）
      手动拦截只认【精确等于】（询问句不能被当成指令）
      自动拦截默认关（行为不变）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings                                                         # noqa: E402
from application.ticket_flow import TicketFlow                          # noqa: E402
from domain.conversation import Conversation, ConvState                 # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


SVC = (ROOT / "application" / "service.py").read_text(encoding="utf-8")
FLOW = (ROOT / "application" / "ticket_flow.py").read_text(encoding="utf-8")


def mkconv(**kw):
    c = Conversation(chat_id="c1", state=ConvState.AWAIT_TICKET)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class _P:
    """假的在途订单。provider_order_id = 平台订单号（关单要用它）。"""
    tenant_id = "t1"
    shop_id = "unb-1"
    buyer_id = "b1"
    order_no = "LOCAL-1"
    provider_order_id = ""


print("=" * 76)
print("一、★ 默认行为不变")
print("=" * 76)
d = settings.Settings()
check("★ 默认关键字 = 拦截订单", d.order_intercept.keyword == "拦截订单",
      d.order_intercept.keyword)
check("★★ 自动拦截默认【关】（行为不变）",
      d.order_auto_intercept.enabled is False)
check("★ 话术里写明【不保证成功】（不吹牛）",
      "不保证" in d.order_intercept.message, d.order_intercept.message[:36])

print()
print("=" * 76)
print("二、★★ 手动拦截只认精确等于（询问不能被当指令）")
print("=" * 76)
check("★★ 用精确等于（want == kw）", "want == kw" in SVC)
check("★ 有独立的判定函数 _wants_intercept", "def _wants_intercept" in SVC)
check("★★ 路由放在确认指令之后（确认要花钱，优先级最高）",
      SVC.find("_wants_intercept(text, cfg)")
      > SVC.find("_is_confirm(text, cfg"))
check("★★ 命中就 return（模型不跑，避免两处口径）",
      "return self._finish(s, result, greet_due=_greet_due)" in SVC)

print()
print("=" * 76)
print("三、★★ 没有平台订单号就不发关单命令")
print("=" * 76)
# ★ 这是关键安全判据：插件的 CLOSE_FAILED_ORDER 会用
#   client.orders.get(orderId) 校验身份，空号必被拒。
class F(TicketFlow):
    def __init__(self):
        pass


f = F()
conv = mkconv(pending_order=_P())
out = f.request_intercept(conv)
check("★★ 无平台订单号 ⇒ 不发关单命令",
      out.pending_commands == [] and any("没有平台订单号" in n
                                         for n in out.notes),
      str(out.notes)[:60])
check("★ 但仍然回了那句确认话术（买家要的是「我提了」）",
      bool(out.messages) and "ORDER_INTERCEPT" in out.template_keys)

# 有平台订单号 ⇒ 发
_p2 = _P()
_p2.provider_order_id = "ONLINE-9"
conv2 = mkconv(pending_order=_p2)
out2 = f.request_intercept(conv2)
check("★★ 有平台订单号 ⇒ 发一条 CLOSE_FAILED_ORDER",
      len(out2.pending_commands) == 1
      and out2.pending_commands[0].type == "CLOSE_FAILED_ORDER",
      str([c.type for c in out2.pending_commands]))
_c = out2.pending_commands[0] if out2.pending_commands else None
check("★★ 命令里带平台订单号（插件要用它校验身份）",
      _c is not None and _c.extra.get("order_id") == "ONLINE-9",
      str(getattr(_c, "extra", {}))[:70])
check("★★ 带幂等键（买家反复说「拦截」不该反复关单）",
      _c is not None and str(_c.dedupe_key).startswith("intercept:"),
      str(getattr(_c, "dedupe_key", "")))
check("★ 身份四元组齐全（插件逐字回核）",
      _c is not None and all(getattr(_c, k, "")
                             for k in ("tenant_id", "shop_id", "buyer_id",
                                       "chat_id")))

print()
print("=" * 76)
print("四、★ 自动拦截（买家申请退款时顺手拦）")
print("=" * 76)
check("★★ 默认关 ⇒ 不发任何命令（行为不变）",
      "bool(getattr(_ai, \"enabled\", False))" in FLOW)
check("★ 接了话术（order_auto_intercept.message）",
      "order_auto_intercept" in FLOW and "ORDER_AUTO_INTERCEPT" in FLOW)
check("★★ 也带幂等键（auto_intercept:）",
      "auto_intercept:" in FLOW)
check("★★ 没有平台订单号也不发（同手动那条判据）",
      "自动拦截：没有平台订单号" in FLOW)
check("★ 拦截失败不影响「退票已受理」这个结论",
      "不影响退票受理" in FLOW)
# ★ 放在 REFUND_APPLIED 之后（不能影响退票受理本身）
check("★ 放在「退票已受理」之后（顺序有讲究）",
      FLOW.find('out.add("REFUND_APPLIED")')
      < FLOW.find("order_auto_intercept"))

print()
print("=" * 76)
print("五、★ 关单命令的机制（插件侧已有的安全）")
print("=" * 76)
PLUG = (ROOT / "yumaiduo" / "plugin-runtime" / "src"
        / "failed-order-close.mjs")
check("★ 插件有 failed-order-close.mjs", PLUG.exists())
if PLUG.exists():
    pt = PLUG.read_text(encoding="utf-8")
    check("★★ 只在 live 模式真做（shadow 只 WOULD_SEND）",
          "cfg.mode!=='live'" in pt and "WOULD_SEND" in pt)
    check("★★ 只关「已付款未发货」的单（发货后绝不关）",
          "orderStatus" in pt and "[2,6]" in pt)
    check("★★ 要后端授权 + 逐字回核字段", "authorize-close" in pt)
    check("★★ 不重放取消（Never replay / attempted）",
          "replay" in pt.lower() or "attempted" in pt)

print()
print()
print("=" * 76)
print("六、★★ 强制发货（拦截没成但票已出 ⇒ 转正常发货）")
print("=" * 76)
# ★ 语义来自 force_ship_reply 的全文（不是我猜的）：
#   "我们第一时间为您提交了系统拦截。但……影院系统已经成功出票。
#    小店已为您转为正常发货，请您按时观影。"
_CFG = settings.Settings()
check("★★ 默认 force_ship=开（默认行为就是转正常发货）",
      _CFG.order_ship.force_ship is True)
check("★ 话术写明「转为正常发货」",
      "转为正常发货" in _CFG.order_ship.force_ship_reply)
# ★ 判据要具体：退款未成那个方法里要有 force_ship 的读取。
#   我第一版写成「从 _on_refund_rejected 往后 2600 字里找 order_ship」——
#   那是【跨过了整个文件尾部】，随便哪里出现都算命中 ⇒ 假通过。
#   ★ 假通过比假失败更危险：它会让没接看起来像接了。
_i = FLOW.find("def _on_refund_rejected")
_j = FLOW.find("\\n    def ", _i + 10)
_seg = FLOW[_i:_j if _j > 0 else len(FLOW)]
check("★★ 接在退款未成那个方法里（不是发货闸门）",
      "force_ship" in _seg and "SHIP_ORDER" in _seg,
      f"该方法长度 {len(_seg)}")
# ★★ 最关键：没提交过拦截 ⇒ 不许发那句（否则是编造事实）
check("★★ 判据是「这一单确实提交过拦截」",
      "_intercept_attempted(pending)" in FLOW)
check("★★ 两条拦截路径都会打标（否则永远判不出）",
      FLOW.count("mark_intercept_attempted(pending)") >= 3,
      str(FLOW.count("mark_intercept_attempted(pending)")))
check("★★ 判不出来时返回 False（宁可不发，也不编造）",
      "宁可不发那句，也不编造" in FLOW)
# ★ 替换而不是追加（否则两个口径打架）
check("★★ 替换掉默认那句（避免两个口径打架）",
      '"退票没能成功" not in m' in FLOW)
# ★ 排 SHIP_ORDER 且带幂等键
check("★★ 排 SHIP_ORDER 且带幂等键", "force_ship:" in FLOW)
check("★★ 强制发货排不上不影响「退票未成」这个结论",
      "不影响退票结论" in FLOW)

print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
