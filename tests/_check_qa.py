"""
_check_qa.py —— 智能问答表校验

它回答：
  1. 有没有重复的 ID / 重复的关键字
  2. 文案里 [xxx] 引用是否都能找到（找不到就是配置错误）
  3. 引用有没有成环
  4. [送花] [比心] 这类表情会不会被误当成 ID（正则边界）
"""
import re
import sys
from collections import Counter
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


results = []

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qa
from qa import BY_ID, QA_RULES, ReferenceError




def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


print("=" * 74)
print("一、ID 与关键字唯一性")
print("=" * 74)

ids = [r.qa_id for r in QA_RULES]
dup_ids = [k for k, v in Counter(ids).items() if v > 1]
check("ID 不重复", not dup_ids, f"★ 重复: {dup_ids}" if dup_ids else f"{len(ids)} 个唯一")

keywords = [r.keyword for r in QA_RULES]
dup_kw = [k for k, v in Counter(keywords).items() if v > 1]
check("触发关键字不重复", not dup_kw,
      f"★ 重复: {dup_kw}（同一个词会让后一条覆盖前一条）" if dup_kw else f"{len(keywords)} 个唯一")

print()
print("=" * 74)
print("二、ID 命名规范（决定正则边界）")
print("=" * 74)

# 用户的例子：[bx98yxna9z] → 小写字母+数字，10 位
# 表情：[送花] [比心] [举手] [ok] [号外] → 中文或含大写
id_shape = re.compile(r"^[a-z0-9]{6,16}$")
bad_shape = [i for i in ids if not id_shape.match(i)]
check("所有 ID 都是小写字母+数字，长度 6~16", not bad_shape,
      f"★ 例外: {bad_shape}" if bad_shape else f"样例长度: {sorted(set(len(i) for i in ids))}")

# 表情必须【不】符合 ID 形状，否则会被误展开
emojis = ["送花", "比心", "举手", "号外", "ok", "撇嘴", "尴尬", "拜"]
misread = [e for e in emojis if id_shape.match(e)]
check("表情不会被误判成 ID", not misread,
      f"★ 会被误判: {misread}" if misread else f"检查了 {len(emojis)} 个表情，都不会被展开")

print()
print("=" * 74)
print("三、引用可解析性")
print("=" * 74)

all_text = "\n".join(r.content for r in QA_RULES)
refs = qa.all_references(all_text)
unknown = sorted(set(r for r in refs if r not in BY_ID))
check("文案里引用的 ID 都能找到", not unknown,
      f"★ 找不到: {unknown}" if unknown else f"{len(refs)} 处引用，全部可解析")

# 环检测
cycles = []
for rule in QA_RULES:
    try:
        qa.resolve(rule.content)
    except ReferenceError as e:
        cycles.append(f"{rule.qa_id}: {e}")
check("引用没有成环", not cycles, f"★ {cycles}" if cycles else f"{len(QA_RULES)} 条全部可展开")

# 展开后再看是否还有未解析的 ID（说明漏了一层）
still = []
for rule in QA_RULES:
    text = qa.resolve(rule.content)
    for ref in qa.all_references(text):
        if ref in BY_ID:
            still.append(f"{rule.qa_id} -> {ref}")
check("展开后没有残留引用", not still, f"★ 残留: {still}" if still else "全部一层展开到纯文本")

print()
print("=" * 74)
print("四、配额与触发类型")
print("=" * 74)

once_rules = [r.qa_id for r in QA_RULES if r.trigger == qa.Trigger.ONCE]
check("单人限制的规则有每日限额", all(
    r.daily_limit != "不限制" for r in QA_RULES if r.trigger == qa.Trigger.ONCE),
    f"单人限制: {once_rules}")

limited = [(r.qa_id, r.daily_limit) for r in QA_RULES if r.daily_limit != "不限制"]
check("限额配置可读", True, f"有限额的: {limited}")

print()
print("=" * 74)
print("五、关键词命中测试")
print("=" * 74)

cases = [
    ("999", "3ewl2vw1zr3"),
    ("请问999是什么", "3ewl2vw1zr3"),      # 包含匹配
    ("取票码怎么用", "3zzomddqon4"),         # "取票" 命中
    ("谢谢老板", "kb72fy71j4m"),            # 长的优先
    ("随便说点啥", None),                   # 没命中 → 交给状态机
]
for message, expect_id in cases:
    hit = qa.match(message)
    got = hit.qa_id if hit else None
    check(f"「{message}」", got == expect_id, f"命中 {got}（期望 {expect_id}）")


# ==================================================================== 关键词页 UI
# ★ 店主：「把上下移动删除」+「回复内容和下方的四个下拉框的 UI 都优化一下」
print()
print("=" * 74)
print("关键词页 UI（2026-10-14 店主指定）")
print("=" * 74)
_WEB = Path(__file__).resolve().parent.parent / "web"
_qjs = (_WEB / "qa.js").read_text(encoding="utf-8")
_qcss = (_WEB / "qa.css").read_text(encoding="utf-8")

check("★★ 表格里没有 ↑↓ 上下移动按钮",
      "data-up" not in _qjs and "data-down" not in _qjs
      and ">↑<" not in _qjs and ">↓<" not in _qjs)
check("★★ move() 函数也删了（不留死代码）", "function move(" not in _qjs)
check("★ 编辑按钮还在（只删了箭头，别删错）", "data-edit" in _qjs)

# ★★ 控件撑满：根因是"从来没写过宽度"（<input> 浏览器默认 173px）
check("★★ 弹窗里的 input/textarea/select 设了 width:100%",
      "width: 100%" in _qcss
      and ".modal-body .qf-item > input" in _qcss)
# ★ 2026-10-14：输入框样式【搬到了共用规则】（style.css 的"弹窗共用件"），
#   和「回复文案」的文案输入框共用一条 —— 店主要求两处做成一样。
#   ⇒ 断言改查 style.css，并且【两个选择器都要在】。
_style = (Path(__file__).resolve().parent.parent / "web" / "style.css"
          ).read_text(encoding="utf-8")
check("★★ 两个弹窗的输入框共用一条样式（店主：做成一样）",
      ".modal-body .m-field textarea" in _style
      and ".modal-body .qf-item > textarea" in _style
      and "min-height: 170px" in _style)
check("★ 回复内容给了最小高度（够写字）",
      "min-height: 180px" in _qcss or "min-height: 170px" in _style)
# ★ 这行间距我漏过一次，被截图抓到（断言量不出"挤不挤"）
check("★ 四个下拉框那行留了行间距",
      ".qf-row { align-items: end; margin-bottom: 16px; }" in _qcss)
check("★ 窄屏改 2 列（4 列时下拉会被挤到看不出内容）",
      "@media (max-width: 620px)" in _qcss)
check("★ 弹窗外层宽度已加大", "min(860px, 92vw)" in _qcss)

print()
print("=" * 74)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 74)
sys.exit(0 if passed == total else 1)
