"""
_demo_flow.py —— 完整链路模拟：状态机 + 文案表 一起跑

目的：让你直观看到「买家说话 → 状态变化 → 发出哪条话」的完整过程。
每一步都打印出对应哪个气泡码，可直接和票达人对照。
"""
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


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import templates as tpl
from domain.conversation import Conversation, Event


def emit(decision, indent="    "):
    if not decision.reply_key:
        return
    row = tpl.find(decision.reply_key)
    bubble = row["bubble"]
    locked = " ★风控" if row["locked"] else ""
    parts = tpl.render_parts(decision.reply_key) if row["multi"] else [tpl.render(decision.reply_key)]
    print(f"{indent}发出 bubble {bubble}{locked}  [{decision.reply_key}]")
    for i, part in enumerate(parts, 1):
        head = f"第{i}条 " if len(parts) > 1 else ""
        first = part.splitlines()[0] if part.splitlines() else part
        print(f"{indent}  {head}{first[:56]}{'…' if len(first) > 56 else ''}")
    if decision.side_effect:
        print(f"{indent}  ⚙ 执行动作: {decision.side_effect}")


def run(title, steps, chat_id="demo"):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    conv = Conversation(chat_id=chat_id)
    for desc, event in steps:
        before = conv.state.value
        decision = conv.apply(event)
        arrow = f"{before} → {decision.state.value}"
        print(f"\n  买家: {desc}")
        print(f"    状态: {arrow}" + ("   ★转人工" if decision.needs_manual else ""))
        emit(decision)
    return conv


# ==================================================================== 场景 1
run("场景 1｜一次成功的代订", [
    ("[发选座截图]", Event.BUYER_IMAGE),
    ("（系统识别成功，算出价格）", Event.RECOGNIZE_OK),
    ("正确", Event.BUYER_CONFIRM),
    ("（系统改价成功）", Event.ORDER_REPRICED),
    ("（买家付款）", Event.ORDER_PAID),
    ("（上游出票成功）", Event.TICKET_OK),
])

# ==================================================================== 场景 2
run("场景 2｜识别失败后重来", [
    ("[发选座截图]", Event.BUYER_IMAGE),
    ("（识别失败：影院场次暂不可售）", Event.RECOGNIZE_FAIL),
    ("[重新发截图]", Event.BUYER_IMAGE),
    ("（这次识别成功）", Event.RECOGNIZE_OK),
])

# ==================================================================== 场景 3
run("场景 3｜多影院消歧", [
    ("[发选座截图]", Event.BUYER_IMAGE),
    ("（识别到两个影院）", Event.RECOGNIZE_MULTI_CINEMA),
    ("影院1", Event.BUYER_PICK),
])

# ==================================================================== 场景 4
run("场景 4｜没报价就付款（自动退款）", [
    # ★ 注意事件选的是 PAID_WITHOUT_QUOTE，不是 ORDER_PAID。
    #   "买家付了钱但我们没报价" 是一个【独立事件】，
    #   由后端核验订单时判定，不能拿正常的 ORDER_PAID 来糊弄。
    ("[直接拍下并付款]", Event.PAID_WITHOUT_QUOTE),
])

# ==================================================================== 场景 5
run("场景 5｜付款太快，金额没对上", [
    ("[发选座截图]", Event.BUYER_IMAGE),
    ("（识别成功）", Event.RECOGNIZE_OK),
    ("正确", Event.BUYER_CONFIRM),
    ("（买家没等改价就付款）", Event.AMOUNT_MISMATCH),
])

# ==================================================================== 场景 6
run("场景 6｜出票失败自动退款", [
    ("[发选座截图]", Event.BUYER_IMAGE),
    ("（识别成功）", Event.RECOGNIZE_OK),
    ("正确", Event.BUYER_CONFIRM),
    ("（改价成功）", Event.ORDER_REPRICED),
    ("（付款）", Event.ORDER_PAID),
    ("（上游无供应商接单）", Event.TICKET_FAIL),
])

# ==================================================================== 场景 7
run("场景 7｜重复收到付款事件（平台重投）", [
    ("[发选座截图]", Event.BUYER_IMAGE),
    ("（识别成功）", Event.RECOGNIZE_OK),
    ("正确", Event.BUYER_CONFIRM),
    ("（改价成功）", Event.ORDER_REPRICED),
    ("（付款）", Event.ORDER_PAID),
    ("（平台又推了一次同一个付款事件）", Event.ORDER_PAID),
])

print()
print("=" * 78)
print("统计")
print("=" * 78)
print(f"  状态数: {len(list(__import__('domain.conversation', fromlist=['x']).ConvState))}")
print(f"  事件数: {len(list(__import__('domain.conversation', fromlist=['x']).Event))}")
print(f"  模板数: {len(tpl.TEMPLATES)}")
locked = [t['key'] for t in tpl.TEMPLATES if t['locked']]
print(f"  风控话术: {len(locked)} 条 → {locked}")
print(f"  多条发送: {len([t for t in tpl.TEMPLATES if t['multi']])} 条")
