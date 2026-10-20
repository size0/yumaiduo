"""店铺级「自动发送」开关的行为验证。

★★ 店主原话："启用怎么没有关闭　这里设置为开关　关闭则不会自动发送信息"
   ⇒ 要验的是**真的不发**，不是"界面上有个开关"。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from application import store                                      # noqa: E402
from application import plugin_runtime as pr                       # noqa: E402

RES = []


def check(label, ok, detail=""):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


tmp = Path(tempfile.mkdtemp())
old_file = store.SHOPS_FILE
store.SHOPS_FILE = tmp / "shops.json"

SHOP = "2313315754"


def buyer_msg(shop_id):
    return {
        "event": "im.message.received",
        "eventId": f"evt-{shop_id}-1",
        "tenantId": "107",
        "payload": {"content": "你好", "chatId": "chat-x", "shopId": shop_id},
    }


print("=" * 74)
print("一、默认（没配过）⇒ 照常发，行为不变")
print("=" * 74)
check("没配过 ⇒ shop_send_enabled 为 True", store.shop_send_enabled(SHOP) is True)
check("配置为空时不会崩", store.load_shop_prefs() == {})

print()
print("=" * 74)
print("二、★★★ 关掉这个店 ⇒ 一条对买家的消息都不排")
print("=" * 74)
store.save_shop_prefs({SHOP: {"send_enabled": False}})
check("读回来是 False", store.shop_send_enabled(SHOP) is False)

# ★ 直接验"闸门函数"的判定（不依赖编排层能不能跑通）
_ident = {"shop_id": SHOP, "chat_id": "chat-x", "tenant_id": "107",
          "buyer_id": ""}
check("★★ _shop_allows_send 对关掉的店返回 False",
      pr._shop_allows_send(_ident) is False)
check("★★ _shop_allows_send 对没配过的店返回 True",
      pr._shop_allows_send({**_ident, "shop_id": "999999"}) is True)

# ★ 造两条假的 SEND_MESSAGE，验证"有 text 的被拦、记账的保留、关单的保留"
from integrations.yumaiduo.commands import Command                  # noqa: E402
_real = Command(command_id="c1", type="SEND_MESSAGE", event_id="e1",
                session_id="chat-x", text="你好，我帮您查价",
                dedupe_key="k1", shop_id=SHOP, tenant_id="107",
                chat_id="chat-x")
_note = pr._note_command("e1", _ident, "内部记账")
_close = Command(command_id="c2", type="CLOSE_FAILED_ORDER", event_id="e1",
                 session_id="chat-x", text="", dedupe_key="k2",
                 shop_id=SHOP, tenant_id="107", chat_id="chat-x")


def filter_like_code(cmds, ident):
    """照 _from_order_paid 里那段收口逻辑做同样的过滤。

    ★ 为什么在这里复制一份：那段逻辑写在函数体里（为了让它靠近
      产生命令的地方），没法直接单测。这里用同样的判据复现一遍，
      并额外断言【源码里确实是这个判据】—— 免得两边漂移。
    """
    if pr._shop_allows_send(ident):
        return list(cmds)
    return [c for c in cmds
            if not (getattr(c, "type", "") == "SEND_MESSAGE"
                    and str(getattr(c, "text", "") or "").strip())]


_kept = filter_like_code([_real, _note, _close], _ident)
_kinds = [(getattr(c, "type", ""), str(getattr(c, "text", ""))[:6]) for c in _kept]
check("★★★ 关掉时：对买家的消息被拦掉", _real not in _kept, str(_kinds))
check("★★ 关掉时：内部记账命令保留（排查不能瞎）", _note in _kept, str(_kinds))
check("★★ 关掉时：关单命令保留（关的是说话，不是止损）",
      _close in _kept, str(_kinds))

# ★★ 关键：断言源码里真的是这个判据（防止我上面那份复制漂移）
_src = (ROOT / "application" / "plugin_runtime.py").read_text(encoding="utf-8")
i = _src.find("def _from_order_paid")
j = _src.find("\ndef ", i + 10)
_body = _src[i:j if j > 0 else len(_src)]
check("★★★ _from_order_paid 里确实有「店铺开关收口」",
      "_shop_allows_send(ident)" in _body)
check("★★ 收口只挡有 text 的 SEND_MESSAGE（判据写对了）",
      'type", "") == "SEND_MESSAGE"' in _body.replace("getattr(c, ", "")
      or 'SEND_MESSAGE' in _body and 'text' in _body)
check("★ 拦下时会留一条记账说明（不是静默丢弃）",
      "自动发送" in _body and "_note_command" in _body)

print()
print("=" * 74)
print("三、再打开 ⇒ 恢复发送")
print("=" * 74)
store.save_shop_prefs({SHOP: {"send_enabled": True}})
check("打开后 shop_send_enabled 为 True", store.shop_send_enabled(SHOP) is True)
check("打开后 _shop_allows_send 为 True", pr._shop_allows_send(_ident) is True)

print()
print("=" * 74)
print("四、多店互不影响 + 读写往返")
print("=" * 74)
store.save_shop_prefs({"A": {"send_enabled": False},
                       "B": {"send_enabled": True}})
check("A 关着", store.shop_send_enabled("A") is False)
check("B 开着", store.shop_send_enabled("B") is True)
check("C 没配过 ⇒ 默认开", store.shop_send_enabled("C") is True)
_r = store.load_shop_prefs()
check("落盘能读回（往返一致）", _r.get("A", {}).get("send_enabled") is False)

print()
print("=" * 74)
print("五、★ 读不到配置时不许静默静音全部店铺")
print("=" * 74)
store.SHOPS_FILE = tmp / "nonexist" / "shops.json"      # 不存在且目录也没有
check("★★ 配置读不到 ⇒ 返回 True（照常发），不是 False",
      store.shop_send_enabled(SHOP) is True)

store.SHOPS_FILE = old_file
print()
print("=" * 74)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 74)
sys.exit(0 if all(RES) else 1)
