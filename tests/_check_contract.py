"""
_check_contract.py —— 状态机 与 文案表 的契约校验

★ 为什么必须有这个：
  早先出过一次真 bug —— 状态机引用了 PLACED_NO_PAY，
  而文案表里实际叫 ORDER_PLACED_NO_PAY。
  两个文件各自看都对，连起来发消息时直接 KeyError 崩。

  这类"跨文件对不上"的问题，人工检查永远会漏。
  所以让代码自己查。
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

bad_vars = []

import templates as tpl
from domain.conversation import (GUARD_RULES, IDEMPOTENT, REPLY_OF,
                                 TRANSITIONS, ConvState, Event)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


print("=" * 72)
print("一、状态机引用的模板，文案表里都有吗")
print("=" * 72)

defined = {t["key"] for t in tpl.TEMPLATES}
# ★ 两个来源都要算：流转表 + 守卫规则
#   （我第一版只扫 TRANSITIONS，把刚加的 CONFIRM_INVALID 误报成死模板）
needed = set(REPLY_OF.values()) | {r["reply"] for r in GUARD_RULES.values() if r.get("reply")}
missing = sorted(needed - defined)
check("状态机引用的模板全部存在", not missing,
      f"★ 缺失: {missing}" if missing else f"{len(needed)} 个模板全部有定义")

print()
print("=" * 72)
print("二、文案表里的模板，状态机用得上吗（反向检查）")
print("=" * 72)

unreachable = sorted(defined - needed)
# 有些模板由外部流程触发（定时、人工、其他状态机），不算死代码
#
# ★ 判据是【谁触发它】：
#   · 编排层 ticket_flow.py 直接 add() 的 → 由链路触发
#   · settings 里配的定时/人工话术        → 由定时任务触发
EXTERNAL_TRIGGERED = {
    # 由 settings 里的配置驱动（定时提醒、人工介入、售后话术）
    "PURCHASE_NOTICE", "TICKET_CODE_UPDATED", "SUBSIDY_REDO",
    "TICKET_CODE_MANUAL", "FLASH_OFFER",
    # 由编排层 application/ticket_flow.py 触发（状态机表里看不到）
    "SHOWTIME_LIMIT",       # 距开场太近，在询价前拦下
    "ORDER_DUPLICATED",     # 平台返回 300010，同场次同座位已有单在出票
    # ★★ 建单失败（店主 2026-10-14 给的文案表里叫「创建订单失败文案」）：
    #   货位价格/状态与报价对不上 → 「其中有一个位置价格不对哦，
    #   辛苦取消分别重新截图下单哈」。由 on_payment_confirmed 在建单
    #   被拒（200006/200007/300005）时直接发。
    "CREATE_FAILED",
    # ★ 退票资格：订单详情 refundable=false 时发出。
    #   用权威字段判断，而不是提交后被错误码拒。
    "REFUND_NOT_ALLOWED",
    # ★★ 买家要求改价：店主选 C（先拍下、拍下后改价）。
    #   由 service 的意图层直接发（INTENT_MESSAGES 表），不走状态机 ——
    #   因为它【只回一句话、什么都不动】。
    "REPRICE_REQUEST",
    # ★★ 首次打招呼：由 service._finish 在【任何出口】追加一条，
    #   不走状态机流转（它不改变会话状态，只是打个招呼）。
    #   ★ 这条文案是店主确认过的口径。
    #   ★ 开关是「设置 → 回复 → 首次打招呼设置」。
    "GREETING",
    # 无选座图时的首次问价由 service.on_message 直接回复。
    "PRICE_NEEDS_SCREENSHOT",
    # ★★ 二次报价：由「到点推进」（handle_run_due → second_quote_due）发出，
    #   不走状态机流转（它不改变会话状态，只是再报一次价）。
    "SECOND_QUOTE",
}
# ★★★★ 停用的模板（templates.DISABLED）不算死代码 ——
#   它们是【店主明确说不做的功能】，保留文案备查。
#   ★ 但这不等于可以随便调：templates.render_messages 有硬拦截，
#     调了就 KeyError。两处一起才成立：
#       死模板检查  = 发现没人调用（维护视角）
#       停用守卫    = 保证调了也发不出去（安全视角）
real_dead = [k for k in unreachable if k not in EXTERNAL_TRIGGERED]
# ★★★★ 停用的模板（templates.DISABLED）不算死代码 ——
#   它们是【店主明确说不做的功能】，保留文案备查。
#   ★ 但这不等于"可以随便调"：templates.render_messages 有硬拦截，
#     调了就 KeyError。两处一起才成立：
#       死模板检查  = 发现"没人调用"（维护视角）
#       停用守卫    = 保证"调了也发不出去"（安全视角）
real_dead = [k for k in real_dead if k not in tpl.DISABLED]
check("没有意外的死模板", not real_dead,
      f"★ 未被任何流转触发: {real_dead}" if real_dead
      else f"无（{len(unreachable)} 条由外部流程触发: {unreachable}）")
# ★ 停用的模板必须【真的存在】—— 防止 DISABLED 里写了错别字，
#   结果既没停用到该停的、又让死代码检查误放行。
ghost = sorted(k for k in tpl.DISABLED if k not in defined)
check("停用清单里的模板都真实存在（防错别字）", not ghost,
      f"★ DISABLED 里有不存在的模板: {ghost}" if ghost else
      f"DISABLED = {sorted(tpl.DISABLED)}")
# ★★ 停用的模板必须【发不出去】—— 这才是"停用"的实际含义。
_guard_blocked = False
try:
    tpl.render_messages(sorted(tpl.DISABLED)[0], {}) if tpl.DISABLED else None
except KeyError:
    _guard_blocked = True
check("停用的模板调 render 会被硬拦截", (not tpl.DISABLED) or _guard_blocked,
      "KeyError 拦住了" if _guard_blocked else "★ 竟然让它发出去了")

print()
print("=" * 72)
print("三、可达性：从空闲出发能走到哪些状态")
print("=" * 72)

edges = {}
for (src, ev), dst in TRANSITIONS.items():
    edges.setdefault(src, set()).add(dst)
for (src, ev), rule in GUARD_RULES.items():
    edges.setdefault(src, set()).add(rule["target"])

# ★ 关键：任何状态都可能因"没预料到的事件"被兜底送到 MANUAL
#   （见 Conversation.apply 的非法流转分支）—— 这条隐式边必须算上，
#   否则 MANUAL 会被误报成死代码。
for state in ConvState:
    edges.setdefault(state, set()).add(ConvState.MANUAL)

seen, queue = {ConvState.IDLE}, [ConvState.IDLE]
while queue:
    for dst in edges.get(queue.pop(), ()):
        if dst not in seen:
            seen.add(dst)
            queue.append(dst)

unreachable_states = [s.value for s in ConvState if s not in seen]
check("所有状态都可达（无死代码）", not unreachable_states,
      f"★ 不可达: {unreachable_states}" if unreachable_states else f"{len(seen)} 个状态全部可达")

print()
print("=" * 72)
print("四、事件覆盖：每个事件都有处理吗")
print("=" * 72)

handled = ({ev for _, ev in TRANSITIONS} | {ev for _, ev in GUARD_RULES}
           | {ev for _, ev in IDEMPOTENT})
unhandled = [ev.value for ev in Event if ev not in handled]
check("所有事件都有处理路径", not unhandled,
      f"★ 无处理: {unhandled}" if unhandled else f"{len(Event)} 个事件全部有处理")

print()
print("=" * 72)
print("五、模板元数据")
print("=" * 72)

locked = sorted(t["key"] for t in tpl.TEMPLATES if t["locked"])
check("风控话术已标记", len(locked) == 4, f"{len(locked)} 条: {locked}")

multi = sorted(t["key"] for t in tpl.TEMPLATES if t["multi"])
check("多条发送已标记", len(multi) == 3, f"{len(multi)} 条: {multi}")

# 风控话术不该含变量 —— 但"事实变量"（城市/影院/座位）是代码填的、不是模型填的，
# 所以只禁"可自由发挥的文本变量"是更准确的口径。这里放宽为：
# 风控话术里出现的变量必须都是【纯事实】，不能有可自由发挥的字段。
FACT_VARS = {"city", "cinema", "cinema_addr", "hall", "movie", "show_time",
             "seats", "origin_unit", "origin_total", "unit_price", "total_price",
             "ticket_code", "ticket_link", "confirm_cmd", "fail_reason",
             "candidates", "pick_kind", "discount", "limit_minutes",
             # ★ ticket_window 来自 TICKET_WINDOW 档位表，是代码填的事实，
             #   不是模型能自由发挥的文本 —— 风控话术里注入它是安全的。
             "ticket_window",
             "fee_amount", "refund_amount"}
free_vars = []
for t in tpl.TEMPLATES:
    if not t["locked"]:
        continue
    bad = [v for v in t["variables"] if v not in FACT_VARS]
    if bad:
        free_vars.append(f"{t['key']}:{bad}")
check("风控话术里的变量都是纯事实（可安全注入）", not free_vars,
      f"★ 含可自由发挥的变量: {free_vars}" if free_vars
      else "4 条风控话术的变量都是事实字段，注入不改变固定措辞")

# 多条模板必须真的能拆开
for key in multi:
    parts = tpl.render_parts(key)
    check(f"{key} 能拆成多条", len(parts) >= 2, f"{len(parts)} 条")

# 变量占位符都能被替换（没有拼错的变量名）
import re


for t in tpl.TEMPLATES:
    for name in re.findall(r"\{(\w+)\}", t["text"]):
        if name not in tpl.VARIABLES:
            bad_vars.append(f"{t['key']}:{{{name}}}")
check("模板里的变量名都合法", not bad_vars,
      f"★ 未定义: {bad_vars}" if bad_vars else "全部合法")

print()
print("=" * 72)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 72)
sys.exit(0 if passed == total else 1)
