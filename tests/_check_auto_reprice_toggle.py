"""验证「自动改价开关」真的能开/能关（店主怀疑它无法开启）。

★ 店主的问题：「你不是设置了拍下后自动改价吗　这里怎么无法开启」
  所以判据是：**关掉之后真的不排改价命令；打开之后真的排。**
  不是"界面上能点"。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from application import ledger as ledger_mod                      # noqa: E402
from application import plugin_runtime as pr                      # noqa: E402
import settings as settings_mod                                   # noqa: E402

RES = []


def check(label, ok, detail=""):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


tmp = Path(tempfile.mkdtemp())
old_paths = (ledger_mod.quotes_file(), ledger_mod.orders_file())
old_cfg = settings_mod.CONFIG_FILE
ledger_mod.set_paths(quotes=str(tmp / "q.sqlite3"), orders=str(tmp / "o.sqlite3"))
settings_mod.CONFIG_FILE = tmp / "settings.json"


def set_auto(v):
    s = settings_mod.load()
    s.fish_reprice.auto_reprice = v
    settings_mod.save(s)


# 造一张"待付款单"，这样卡片事件才拿得到目标价
from domain.order.models import Order                              # noqa: E402
st = ledger_mod.stores()
# ★★ 关键：把闲鱼订单号【预先绑上】。
#   为什么必须这样造：_from_order_card 的第一步是
#   find_by_provider_order(闲鱼订单号)。如果没绑，
#   它会走补落单那条路（那要求会话里有有效报价），
#   于是我测的就变成了补落单，不是自动改价开关。
#   ⇒ 一个不干净的输入会让断言看起来在测 A，其实在测 B。
# ★★★ 必须用 create（INSERT），不能用 put ——
#   put 是 UPDATE orders SET ... WHERE order_id=?：
#   订单不存在时它改 0 行、【而且不报错】，是典型的静默失败。
#   我第一版就用了 put，于是订单根本没建出来，
#   断言看起来在测改价开关，其实在测找不到订单。
#   ⇒ 造数据要用对方法，否则测的东西和你以为的不是一回事。
st.orders.create(Order(order_id="OID-1", quote_id="QID-1",
                       idempotency_key="IDEM-1",
                       chat_id="chat-1",
                       tenant_id="107", shop_id="S1", buyer_id="",
                       provider="liangpiao", amount_fen=3310,
                       provider_order_id="ONLINE-1",
                       state="AWAITING_PAYMENT", payment_state="UNPAID"))


def card():
    return {"event": "im.message.received", "eventId": "card-1", "tenantId": "107",
            "payload": {"messageType": "26", "orderId": "ONLINE-1",
                        "chatId": "chat-1", "shopId": "S1"}}


print("=" * 74)
print("一、打开（默认）⇒ 应该排 CHANGE_ORDER_PRICE")
print("=" * 74)
set_auto(True)
check("配置读回来是 True", settings_mod.load().fish_reprice.auto_reprice is True)
cmds = pr.events_to_commands(card())
kinds = [getattr(c, "type", "") for c in cmds]
check("★★ 打开时排了 CHANGE_ORDER_PRICE", "CHANGE_ORDER_PRICE" in kinds,
      str(kinds))

print()
print("=" * 74)
print("二、★★★ 关掉 ⇒ 不许排改价命令（店主问的就是这个）")
print("=" * 74)
set_auto(False)
check("配置读回来是 False", settings_mod.load().fish_reprice.auto_reprice is False)
cmds2 = pr.events_to_commands(card())
kinds2 = [getattr(c, "type", "") for c in cmds2]
check("★★★ 关掉时【没有】CHANGE_ORDER_PRICE", "CHANGE_ORDER_PRICE" not in kinds2,
      str(kinds2))
check("★ 但有记账说明（不是静默什么都不做）",
      any("自动改价开关=关" in str(getattr(c, "extra", {}).get("note", ""))
          for c in cmds2),
      str([str(getattr(c, "extra", {}).get("note", ""))[:40] for c in cmds2]))

print()
print("=" * 74)
print("三、再打开 ⇒ 恢复")
print("=" * 74)
set_auto(True)
cmds3 = pr.events_to_commands(card())
check("★★ 打开后又能排改价命令了",
      "CHANGE_ORDER_PRICE" in [getattr(c, "type", "") for c in cmds3])

# 收尾
settings_mod.CONFIG_FILE = old_cfg
ledger_mod.set_paths(quotes=None, orders=None)
print()
print("=" * 74)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 74)
sys.exit(0 if all(RES) else 1)
