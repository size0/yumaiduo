"""「优先报价模式」接线检查（2026-10-14 接的第 15 个摆设控件）。

★★ 这个控件的语义是店主一句话定的，所以断言要把那句话钉住：
     "现在我们设计的报价规则就是简易报价规则
      专业报价规则更加复杂，我没有设计"
   ⇒ 只有「简易」有实现；其余三个是规划过、未实现。

★ 为什么这个接线特别容易"看着对但其实是摆设"：
   因为只有一种模式，所以【不管这个控件怎么设，报价结果都一样】。
   也就是说：我完全可以把 quote_priority 读出来、什么都不做，
   而所有"价格应该是对的"的测试照样全过。
   ⇒ 所以断言必须打在"它真的参与了规则选择"上，
     而不是"价格算得对"上（后者证明不了接线）。
"""
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings as settings_module                      # noqa: E402
from domain import quote_rules as qr                    # noqa: E402
from application import store                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


def strip_comments(src: str) -> str:
    """去掉注释和字符串，避免断言扫到我自己写的解释文字。"""
    out = re.sub(r"#[^\n]*", "", src)
    out = re.sub(r'"""[\s\S]*?"""', "", out)
    out = re.sub(r"'''[\s\S]*?'''", "", out)
    return out


# ─────────────────────────────────────────────────────────── 一、语义
print("=" * 76)
print("一、★ 只有「简易报价规则」是实现了的（店主确认）")
print("=" * 76)
check("★ 已实现的模式只有「简易报价规则」",
      qr.implemented_modes() == [qr.MODE_SIMPLE],
      str(qr.implemented_modes()))
check("★ 另外三个都在 MODE_RULES 里【查不到】（＝未实现）",
      all(m not in qr.MODE_RULES
          for m in (qr.MODE_PRO, qr.MODE_API, qr.MODE_COMPARE)),
      str(list(qr.MODE_RULES)))
check("★ 「简易」对应的规则集是 None（＝全部规则，即我们现在这套）",
      qr.MODE_RULES.get(qr.MODE_SIMPLE) is None)

# settings 的元数据要和 domain 对得上（否则界面和代码会各说各话）
_rq = settings_module.RecognizeQuote()
_meta = _rq.__dataclass_fields__["quote_priority"].metadata
check("★★ settings 声明的 implemented 与 domain 的 implemented_modes 一致",
      list(_meta.get("implemented") or []) == qr.implemented_modes(),
      f"settings={_meta.get('implemented')} domain={qr.implemented_modes()}")
check("★ 四个模式都还在 all_options 里（没被删掉，只是标了未实现）",
      list(_meta.get("all_options") or []) ==
      [qr.MODE_SIMPLE, qr.MODE_PRO, qr.MODE_API, qr.MODE_COMPARE],
      str(_meta.get("all_options")))

# ─────────────────────────────────────────────────────────── 二、筛选逻辑
print()
print("=" * 76)
print("二、★★ 筛选逻辑：不能返回空规则集（那会让报价全掉进兜底）")
print("=" * 76)
RULES = store.load_rules()
check("前提：种子规则有 6 条", len(RULES) == 6, str(len(RULES)))

out, mode, note = qr.select_rules_by_priority(RULES, _meta["all_options"])
check("★★ 默认顺序（简易在最前）⇒ 选中简易且规则一条不少",
      mode == qr.MODE_SIMPLE and len(out) == len(RULES),
      f"mode={mode!r} n={len(out)}")

out2, mode2, _ = qr.select_rules_by_priority(
    RULES, [qr.MODE_PRO, qr.MODE_SIMPLE])
check("★★ 未实现的模式排在第一 ⇒ 被跳过，轮不到它冒充可用",
      mode2 == qr.MODE_SIMPLE, f"mode={mode2!r}")

out3, mode3, note3 = qr.select_rules_by_priority(RULES, [qr.MODE_PRO])
check("★★ 只有未实现的模式 ⇒ 回退到全部规则（不是空）",
      len(out3) == len(RULES) and mode3 == "",
      f"n={len(out3)} mode={mode3!r}")
check("★★ 回退时必须给说明（否则运营不知道自己的设置为什么没效果）",
      bool(note3), repr(note3))

out4, mode4, note4 = qr.select_rules_by_priority(RULES, [])
check("★★ 清单为空 ⇒ 同样回退到全部规则 + 说明",
      len(out4) == len(RULES) and bool(note4), f"n={len(out4)}")

out5, _, _ = qr.select_rules_by_priority(RULES, ["不存在的模式"])
check("★ 未知模式名 ⇒ 也回退到全部规则（不静默按别的模式算）",
      len(out5) == len(RULES), str(len(out5)))

# ─────────────────────────────────────────────────────────── 三、真接上了
print()
print("=" * 76)
print("三、★★★ 真的接进报价链路（不是只把值读出来扔掉）")
print("=" * 76)
_svc = strip_comments((ROOT / "application" / "service.py").read_text(
    encoding="utf-8"))
_srv = strip_comments((ROOT / "server.py").read_text(encoding="utf-8"))
check("★★★ service.py 用它筛规则（build_service 那条路）",
      "select_rules_by_priority" in _svc and "QuoteRuleEngine(_rules" in _svc)
check("★★★ server.py 也用它筛规则（另一条构造路）",
      "select_rules_by_priority" in _srv)
check("★★ 两处都用同一个辅助函数（逻辑只写一遍）",
      _svc.count("def _quote_priority(") == 1
      and "service_mod._quote_priority(" in _srv)
check("★★ 未命中模式时会打印说明（不是静默）",
      "[报价]" in _svc and "_note" in _svc)

# ★ 审计要看到"有人在读这个字段"
_pq = strip_comments((ROOT / "application" / "service.py").read_text(
    encoding="utf-8"))
check("★ _quote_priority 里能看出读的是 quote_priority",
      re.search(r'getattr\(\s*section,\s*"quote_priority"', _pq) is not None)

# ─────────────────────────────────────────────────────────── 四、行为不变
print()
print("=" * 76)
print("四、★★ 因为只有一种模式，筛选前后必须是【同一批规则】（价格不变）")
print("=" * 76)
same = ([r.name for r in out] == [r.name for r in RULES]
        and [r.weight for r in out] == [r.weight for r in RULES])
check("★★ 筛出来的规则与原始规则顺序/权重完全一致", same)

eng = qr.QuoteRuleEngine(out)
eng.mode = mode
r = eng.quote(now=dt.time(12, 0), brand="万达影城", cinema="万达影城(娄底)",
              total_amount_fen=6000, estimate_total_fen=6000,
              seat_count=2, original_total_fen=10000)
check("★ 命中渠道规则（不是掉进兜底）",
      r.rule_name != "（无命中规则，按成本报）", r.rule_name)
check("★ 报价高于成本（配额规则还在起作用）",
      r.total_price_fen > 6000, f"{r.total_price_fen}")

print()
print("=" * 76)
print("五、★★ 界面：未实现的要显示成「未实现」且不能启用")
print("=" * 76)
_js = (ROOT / "web" / "config.js").read_text(encoding="utf-8")
_css = (ROOT / "web" / "config.css").read_text(encoding="utf-8")
check("★★ 渲染器读 implemented 元数据", "c.implemented" in _js)
# ★★★ 这一条我第一版写错了：只断言源码里有 "draggable: ok ? 'true' : 'false'"
#   这行【字】。而实际上 el() 对非白名单键做属性赋值，
#   字符串 'false' 是真值 ⇒ 浏览器里四项全能拖。
#   源码里有那行字【不代表行为正确】—— 这类扫源码的断言就是假通过的来源。
#   ⇒ 改成断言【实现方式】：只有 ok 时才 setAttribute('draggable','true')，
#     并且 el() 把 draggable 当布尔属性处理（不许再走属性赋值那条路）。
check("★★ 未实现项不设 draggable 属性（用 setAttribute 而不是传值）",
      "if (ok) item.setAttribute('draggable', 'true');" in _js)
check("★★ el() 把 draggable 当布尔属性处理（堵住 'false' 是真值这个坑）",
      "k === 'draggable'" in _js and "node.removeAttribute('draggable')" in _js)
check("★★ 未实现项【不绑定】点击/拖动事件（否则还能被启用）",
      "if (ok) {" in _js and "addEventListener('click'" in _js)
check("★★ 未实现项带「未实现」标签", "not-impl-tag" in _js and "未实现" in _js)
check("★★ collect 时排掉未实现项（存下去的永远是能用的）",
      "it.dataset.impl !== '0'" in _js)
check("★ CSS 有 not-impl 样式（与「停用」视觉不同）",
      ".sortable-item.not-impl" in _css)
check("★ CSS 里未实现是虚线、停用只是透明（两种含义不同）",
      "border-style: dashed" in _css)

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
