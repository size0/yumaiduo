"""首次打招呼 + picker 控件检查。

★★ 为什么必须断：
   1. **picker 控件原来根本不保存** —— 它用 prompt() 取值，
      写进 `btn.dataset.value`，而 collect() 只读带 data-key 的 input
      ⇒ 改了什么都存不下去，但看起来能用（会弹窗、会回显）。
      影响两个设置：首次打招呼设置、退货信息。
   2. **hidden input 一律转布尔** —— return_info 是字典，
      会被写成 false，把配置覆盖坏。
   3. 打招呼必须"只在首次"（每句都回"你好"就不像人了），
      且默认关闭（默认行为不变）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings                                                         # noqa: E402
import templates as tpl                                                 # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


CFG_JS = (ROOT / "web" / "config.js").read_text(encoding="utf-8")

print("=" * 76)
print("一、★★★ picker 控件那个真 bug")
print("=" * 76)
# ★ 剥掉注释再断言（我踩过：注释里写着旧写法，直接搜源码会匹配到自己的注释）
CFG_CODE = re.sub(r"/\*[\s\S]*?\*/", "", CFG_JS)
CFG_CODE = re.sub(r"//[^\n]*", "", CFG_CODE)
check("★★ picker 渲染器现在生成【带 data-key 的 input】",
      "data-key" in CFG_CODE and "'data-key'" in CFG_CODE)
check("★★ 不再把值写进 btn.dataset（那没人读）",
      "btn.dataset.value" not in CFG_CODE)
check("★ 有当前值回显（用户才知道存了什么）",
      "picker-shown" in CFG_CODE)

print()
print("=" * 76)
print("二、★★ hidden input 不能一律转布尔")
print("=" * 76)
check("★★ 按原值类型转换（bool / dict / list / str）",
      "data-vtype" in CFG_CODE and "orig === 'bool'" in CFG_CODE)
check("★★ 字典型会 JSON.parse（return_info 是 {}，不能变 false）",
      "JSON.parse(input.value" in CFG_CODE)
# ★ 反向：不能再出现"hidden 一律转布尔"的旧写法
check("★★ 没有退回「hidden 一律 input.value === '1'」",
      not re.search(r"type === 'hidden'\)\s*\{\s*bucket\[k\] = input\.value === '1'",
                    CFG_CODE))

print()
print("=" * 76)
print("三、★ 打招呼文案是店主确认过的口径（不许自己改写）")
print("=" * 76)
g = next((t for t in tpl.TEMPLATES if t["key"] == "GREETING"), None)
check("★★ GREETING 模板在", g is not None)
check("★★ 文案与店主口径一致",
      g is not None and "发我想看的场次或选座截图" in g["text"],
      (g or {}).get("text", "")[:40])
check("★ 模板里标注了「不许改写」",
      g is not None and any("不要自己改写" in n for n in (g.get("notes") or [])))

print()
print("=" * 76)
print("四、★ 默认行为不变 + 三条判据")
print("=" * 76)
d = settings.Settings()
check("★★ 默认【关闭】打招呼（行为不变）",
      d.reply.greeting_configured is False)
check("★ 默认类型含 文字+图片",
      list(d.reply.greeting_message_types) == ["文字", "图片"])
check("★ 默认间隔 0（不限制）", d.reply.greeting_interval_hours == 0)
# ★ 源码层面：三条判据都在
SVC = (ROOT / "application" / "service.py").read_text(encoding="utf-8")
check("★★ 判据① 开关", "greeting_configured" in SVC)
check("★★ 判据② 消息类型", "greeting_message_types" in SVC)
check("★★ 判据③ 间隔时长", "greeting_interval_hours" in SVC)
# ★ 追加而不是替换；且所有出口都过 _finish
check("★★ 打招呼是【追加】到 messages（不拦住正常回复）",
      "result.messages.append(_gtext)" in SVC)
check("★★ 只有真发出去才记时刻（否则一次异常就永久不再打）",
      "session.last_greeting_at = datetime.now()" in SVC)
check("★ 记的时刻要落盘（重启不忘）",
      '"last_greeting_at"' in SVC and "last_greeting_at=row.get" in SVC)

print()
print("=" * 76)
print("五、★★ 兜底不许静默（这条是我自己踩出来的）")
print("=" * 76)
# ★ 我在 _finish 里写了 tpl.text_for(...) 但【没 import templates】——
#   NameError 被 `except: pass` 吞掉，打招呼永远发不出去且看不到报错。
check("★★ service.py 真的 import 了 templates（否则 tpl 是未定义名）",
      "import templates as tpl" in SVC)
check("★★ 打招呼失败会【留痕】（不是静默 pass）",
      "打招呼没发出去" in SVC)
# ★ 直接验证 tpl 真的可用（不是"看源码里有 import"就算过）
check("★★ tpl.text_for('GREETING') 真的能渲染",
      bool(tpl.text_for("GREETING")))

print()
print("=" * 76)
print("六、★ 契约：GREETING 登记为「外部触发」，不算死模板")
print("=" * 76)
CON = (ROOT / "tests" / "_check_contract.py").read_text(encoding="utf-8")
check("★ GREETING 进了 EXTERNAL_TRIGGERED",
      '"GREETING"' in CON)

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
