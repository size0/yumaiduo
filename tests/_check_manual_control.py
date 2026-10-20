"""人工介入检查：超时关闭 + 那个"永远返回 False"的真 bug。

★★ 为什么必须断：
   1. `_handed_off` 原来判的是中文显示值（"待人工" 里找 "HUMAN"），
      **永远 False** ⇒ "转人工后机器人不再插话"这个保护从没生效过。
      这类 bug 不报错、不抛异常，只是【保护静默失效】。
   2. `ConvState.MANUAL` 是终态、没有出口 ⇒ 不关接管的话
      会话永远无人应答。所以"关闭"这条出口必须可靠。
   3. 默认配置必须保证行为不变（这也是最容易被改坏的一条）。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings                                                         # noqa: E402
from application import manual_control as mc                            # noqa: E402
from application.service import Session                                 # noqa: E402
from domain.conversation import Conversation, ConvState                 # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


def mk(state=ConvState.MANUAL, status=""):
    s = Session(chat_id="c1",
                conversation=Conversation(chat_id="c1", state=state))
    if status:
        class _O:
            pass
        o = _O()
        o.status = status
        s.conversation.pending_order = o
    return s


NOW = datetime(2026, 10, 14, 12, 0, 0)
CFG = settings.Settings()

print("=" * 76)
print("一、★★★ 那个真 bug：转人工保护必须真的生效")
print("=" * 76)
from application import plugin_authorize as pa                          # noqa: E402
_src = (ROOT / "application" / "plugin_authorize.py").read_text(encoding="utf-8")
# ★ 判据必须是枚举名，不能是中文显示值
check("★★ _handed_off 判的是枚举名（不是中文 value）",
      'getattr(state, "name", "")' in _src and 'name != "MANUAL"' in _src)
# ★★ 剥掉注释/文档串再断言 ——
#   我第一版直接搜源码文本，结果匹配到【我自己写的解释注释】
#   （注释里写着 `"HUMAN" in name.upper()` 说明这个 bug）。
#   同类的坑我踩过好几次：断言的对象必须是【代码】，不是解释代码的文字。
import re as _re                                                        # noqa: E402
_code_only = _re.sub(r"#[^\n]*", "", _src)
_code_only = _re.sub(r'"""(?:.|\n)*?"""', "", _code_only)
check("★★ 不再用中文显示值当判据（\"HUMAN\" in value）",
      '"HUMAN"' not in _code_only)
# ★ 用真实枚举验证：不能靠"看到源码里有 MANUAL 四个字"就算过
from domain.conversation import ConvState as _CS                        # noqa: E402
check("★★ ConvState.MANUAL 的 value 里确实没有 HUMAN（证实原判据必错）",
      "HUMAN" not in str(_CS.MANUAL.value).upper(),
      f"value={_CS.MANUAL.value!r}")

print()
print("=" * 76)
print("二、★ 默认配置必须行为不变")
print("=" * 76)
d = settings.Settings()
check("超时默认 1 小时", mc.close_after(d) == 1, str(mc.close_after(d)))
check("关闭状态默认三项",
      mc.close_statuses(d) == ["已发货", "已退款", "交易关闭"],
      str(mc.close_statuses(d)))

print()
print("=" * 76)
print("三、接管标记（mark）")
print("=" * 76)
s = mk()
check("★ 第一次 mark 成功", mc.mark(s, now=NOW) is True)
check("★ 记下了时刻", s.manual_since == "2026-10-14T12:00:00", s.manual_since)
# ★★ 幂等：不刷新时间。否则买家一直说话就把超时无限往后推，永远不关
check("★★ 重复 mark 不刷新时间（否则永远不会超时）",
      mc.mark(s, now=NOW + timedelta(hours=5)) is False
      and s.manual_since == "2026-10-14T12:00:00")

print()
print("=" * 76)
print("四、超时判据")
print("=" * 76)
s2 = mk()
mc.mark(s2, now=NOW)
check("30 分钟：不关", mc.should_close(s2, now=NOW + timedelta(minutes=30), cfg=CFG) is False)
check("★ 59 分钟：不关", mc.should_close(s2, now=NOW + timedelta(minutes=59), cfg=CFG) is False)
check("★ 61 分钟：关", mc.should_close(s2, now=NOW + timedelta(minutes=61), cfg=CFG) is True)
# ★ 0 = 不自动关
c0 = settings.Settings()
c0.manual.close_after_hours = 0
check("★ 超时=0 ⇒ 不自动关闭", mc.should_close(s2, now=NOW + timedelta(days=99), cfg=c0) is False)
# ★ 没有时间依据时不许关（宁可多留，也不要机器人去抢话）
s_no = mk()
check("★★ 没有时间依据 ⇒ 不关（不许因为缺字段就去抢话）",
      mc.should_close(s_no, now=NOW, cfg=CFG) is False)

print()
print("=" * 76)
print("五、订单状态判据")
print("=" * 76)
check("已发货 ⇒ 关", mc.should_close(mk(status="已发货"), now=NOW, cfg=CFG) is True)
check("已退款 ⇒ 关", mc.should_close(mk(status="已退款"), now=NOW, cfg=CFG) is True)
check("待出票 ⇒ 不关", mc.should_close(mk(status="待出票"), now=NOW, cfg=CFG) is False)
# ★ 空列表 = 用默认，不是"一个都不关"
c_empty = settings.Settings()
c_empty.manual.close_on_status = []
check("★★ 空状态列表 ⇒ 用默认（不是「一个都不关」）",
      mc.close_statuses(c_empty) == ["已发货", "已退款", "交易关闭"])
c_custom = settings.Settings()
c_custom.manual.close_on_status = ["已发货"]
check("★ 自定义状态列表生效",
      mc.close_statuses(c_custom) == ["已发货"]
      and mc.should_close(mk(status="已退款"), now=NOW, cfg=c_custom) is False)

print()
print("=" * 76)
print("六、关闭只清标记、不动状态机")
print("=" * 76)
s3 = mk()
mc.mark(s3, now=NOW)
check("★ close 返回 True（真关了）", mc.close(s3) is True)
check("★ 标记已清空", s3.manual_since == "")
# ★★ 状态必须留在"待人工" —— 订单还在人工那边走（发货/退款是人在做）
check("★★ 状态仍在「待人工」（只让机器人能说话，不替人工做决定）",
      s3.conversation.state is ConvState.MANUAL)
check("★ 重复 close 返回 False", mc.close(s3) is False)

print()
print("=" * 76)
print("七、★ 接线形状")
print("=" * 76)
# ★ 不许 import plugin_runtime（那会成环）
_mod = (ROOT / "application" / "manual_control.py").read_text(encoding="utf-8")
check("★★ manual_control 不 import plugin_runtime（防循环依赖）",
      "from application.plugin_runtime import" not in _mod
      and "from .plugin_runtime import" not in _mod)
# ★ _finish 是唯一维护标记的地方（所有出口都过它）
_svc = (ROOT / "application" / "service.py").read_text(encoding="utf-8")
check("★★ 接管标记在 _finish 里统一维护（一处，不散落）",
      "manual_control" in _svc and "_mc.mark(session)" in _svc)
# ★ 挂到每秒的推进循环
_api = (ROOT / "application" / "plugin_api.py").read_text(encoding="utf-8")
check("★★ 挂到 handle_run_due（每秒推进）", "manual_control" in _api)
# ★ 标记要落盘
check("★★ 接管标记落盘并读回（重启后不忘）",
      '"manual_since"' in _svc and 'manual_since=row.get' in _svc)

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
