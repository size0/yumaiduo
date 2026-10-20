"""
_selftest_ui.py —— 端到端自检：HTTP 服务 + 配置持久化 + 模板保存

★ 这个文件本身踩过一个坑，值得记住：
  第一版把中文直接写在脚本里，通过 PowerShell -c 传给 Python 时
  被终端编码转成了字面量 '???'，于是"改动没持久化"的项目失败了 ——
  但服务其实是对的。**测试脚本的假警报比不测试更危险，因为它会把你引向错误的排查方向。**
  现在所有中文都用 \\uXXXX 转义写，绕开终端编码问题。
"""
import json
import sys
import urllib.request
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass



BASE = "http://127.0.0.1:8801"      # ★ UI 自检要打的服务地址

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 中文一律用 unicode 转义，避免终端/编码层把它变成 ???
CN_TEST = "\u3010\u6d4b\u8bd5\u3011{cinema}"           # 【测试】{cinema}
CN_WANDA = "\u4e07\u8fbe\u4e13\u5c5e\u62db\u547c\u8bed"  # 万达专属招呼语

results = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    results.append(ok)
    return ok


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def put(path, payload):
    """★ 显式用 UTF-8 编码，并且确保 ASCII 之外的内容不会被中途改写。"""
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, method="PUT", data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def find(key):
    return next(i for g in get("/api/templates")["catalog"] for i in g["items"] if i["key"] == key)


print("=" * 68)
print("1. 静态资源")
print("=" * 68)
for path, needle in [("/", "AI"), ("/style.css", ".card"), ("/app.js", "loadSettings")]:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        body = r.read().decode("utf-8")
    check(f"GET {path}", r.status == 200 and needle in body, f"{len(body)} 字节")

print()
print("=" * 68)
print("2. 配置读写往返（最容易假成功的地方）")
print("=" * 68)
before = get("/api/settings")["takeover_seconds"]
target = 37 if before != 37 else 41
put("/api/settings", {"takeover_seconds": target})
check("PUT 后重新 GET 读到新值", get("/api/settings")["takeover_seconds"] == target,
      f"{before} -> {target}")
disk = json.loads((ROOT / "data" / "settings.json").read_text(encoding="utf-8"))
check("磁盘文件同步落盘", disk["takeover_seconds"] == target, f"文件 = {disk['takeover_seconds']}")
put("/api/settings", {"takeover_seconds": 9999})
check("越界值被夹到上限", get("/api/settings")["takeover_seconds"] == 300, "传 9999 -> 300")
put("/api/settings", {"takeover_seconds": before})
check("还原成功", get("/api/settings")["takeover_seconds"] == before, f"回到 {before}")

print()
print("=" * 68)
print("3. 模板读写（含中文，验证编码没被破坏）")
print("=" * 68)
cat = get("/api/templates")
total = sum(len(g["items"]) for g in cat["catalog"])
check("模板总数", total == 27, f"{total} 条 / {len(cat['stages'])} 阶段")

row = find("QUOTE_CARD")
check("报价卡变量数", len(row["variables"]) == 10, f"{len(row['variables'])} 个")
preview = get("/api/preview/QUOTE_CARD")["text"]
check("变量被渲染替换", "{cinema}" not in preview, preview.splitlines()[2][:28] + "...")

original = row["text"]
put("/api/templates", {"key": "QUOTE_CARD", "channel": "global", "text": CN_TEST})
saved = find("QUOTE_CARD")["text"]
check("中文模板改动被持久化且未损坏", saved == CN_TEST, f"实读 {saved!r}")
disk2 = json.loads((ROOT / "data" / "templates.json").read_text(encoding="utf-8"))
check("模板落盘", disk2["QUOTE_CARD"]["global"] == CN_TEST, "文件里一致")

put("/api/templates", {"key": "QUOTE_CARD", "channel": "global", "text": original})
check("模板还原", find("QUOTE_CARD")["text"] == original, "已回到原文案")
disk3 = json.loads((ROOT / "data" / "templates.json").read_text(encoding="utf-8"))
check("还原也落盘（不留脏数据）", disk3["QUOTE_CARD"]["global"] == original, "文件已还原")

print()
print("=" * 68)
print("4. 渠道覆盖：留空必须回落全局")
print("=" * 68)
put("/api/templates", {"key": "GREETING", "channel": "wanda", "text": CN_WANDA})
check("渠道文案已保存", find("GREETING")["channel_text"]["wanda"] == CN_WANDA, CN_WANDA)
put("/api/templates", {"key": "GREETING", "channel": "wanda", "text": ""})
check("留空回落全局", find("GREETING")["channel_text"]["wanda"] == "", "空字符串 = 不覆盖")
disk4 = json.loads((ROOT / "data" / "templates.json").read_text(encoding="utf-8"))
check("渠道层无残留", disk4["GREETING"].get("wanda", "") == "", "文件里也是空")

print()
print("=" * 68)
print(f"结果: {sum(results)}/{len(results)} 项通过")
print("=" * 68)
sys.exit(0 if all(results) else 1)
