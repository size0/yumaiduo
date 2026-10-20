"""保存配置：布尔字段落盘后是 bool 还是字符串？

★ 为什么单独查这一条：
  前端开关用的是 <input type="hidden" value="1">，
  收集时可能变成字符串 "1" / ""。如果 apply_dict 不做类型转换，
  存进 settings.json 的就是 "1"，而 bool("") 是 False ——
  ⇒ **开关看着是开的，代码读出来却是关的**（或反过来），
    而且不报错。这类"类型悄悄变了"的坑最难查。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings as st                                              # noqa: E402

RES = []


def check(label, ok, detail=""):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


f = st.CONFIG_FILE
before = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

print("=" * 74)
print("一、★ 前端那种形态（字符串 '1' / ''）喂进去，落盘必须还是 bool")
print("=" * 74)
d = st.load()
d.apply_dict({"fish_reprice": {"auto_reprice": "1"}})
raw1 = d.fish_reprice.auto_reprice
check("★ 字符串 '1' → 变成 bool True（不是字符串）",
      raw1 is True, f"{raw1!r} ({type(raw1).__name__})")

d2 = st.load()
d2.apply_dict({"fish_reprice": {"auto_reprice": ""}})
raw2 = d2.fish_reprice.auto_reprice
check("★ 空字符串 '' → 变成 bool False",
      raw2 is False, f"{raw2!r} ({type(raw2).__name__})")

d3 = st.load()
d3.apply_dict({"ticket_release": {"allow_seat_change": "False"}})
raw3 = d3.ticket_release.allow_seat_change
check("★ 字符串 'False' → bool False（不能因为非空就当 True）",
      raw3 is False, f"{raw3!r} ({type(raw3).__name__})")

print()
print("=" * 74)
print("二、★ 存盘再读回来，类型仍然是 bool（round-trip）")
print("=" * 74)
d4 = st.load()
d4.fish_reprice.auto_reprice = True
d4.ticket_premium.enabled = True
st.save(d4)
on_disk = json.loads(f.read_text(encoding="utf-8"))
check("★ 落盘的是 JSON true（不是 \"1\"）",
      on_disk["fish_reprice"]["auto_reprice"] is True,
      repr(on_disk["fish_reprice"]["auto_reprice"]))
check("★ 落盘的是 JSON true（ticket_premium.enabled）",
      on_disk["ticket_premium"]["enabled"] is True,
      repr(on_disk["ticket_premium"]["enabled"]))
back = st.load()
check("★ 读回来还是 bool True",
      back.fish_reprice.auto_reprice is True,
      f"{back.fish_reprice.auto_reprice!r}")

print()
print("=" * 74)
print("三、★ 这几项真的被接线读了（不是摆设）")
print("=" * 74)
from application import ticket_flow as tf                          # noqa: E402
from application import plugin_runtime as pr                       # noqa: E402
_c = st.load()
_c.ticket_release.mode = "手动"
_c.ticket_release.allow_seat_change = False
_c.ticket_premium.enabled = True
_c.ticket_premium.cap_yuan = 4.0
check("★★ 出票方式被读（手动）", tf._release_mode(_c) == tf.RELEASE_MANUAL)
check("★★ 换座状态被读（不允许）", tf._allow_seat_change(_c) is False)
check("★★ 出票溢价被读（4 元/张）",
      tf._fallback_quote_kwargs(_c).get("fallback_limit_yuan") == 4.0,
      str(tf._fallback_quote_kwargs(_c).get("fallback_limit_yuan")))
check("★★ 自动改价开关被读（plugin_runtime 里真的有这一行）",
      "auto_reprice" in (ROOT / "application" / "plugin_runtime.py")
      .read_text(encoding="utf-8"))

# 还原配置
if before:
    f.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")

print()
print("=" * 74)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 74)
sys.exit(0 if all(RES) else 1)
