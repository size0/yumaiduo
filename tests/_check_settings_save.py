"""保存配置：一个真 bug 的回归测试。

★★★ 这个 bug 的症状和店主的原话一模一样
   店主：「你不是设置了拍下后自动改价吗　这里怎么无法开启」

   真相：开关本身是好的（点得动、值也对），
        但点「保存配置」会被拦下 ——
        前端每次都发 "ticket_image": {"template": ""}（隐藏输入框的空值），
        而后端原来把【空字符串】也当成"新图片内容"去存
        ⇒ store_upload("") ⇒ "没有图片内容" ⇒ problems 非空 ⇒ 不落盘。

   ⇒ **只要没上传过取票图，功能设置页的「保存配置」就永远失败**，
     而界面上只显示一句"未保存：配置有问题"。
     改任何一个开关都存不进去 —— 但看起来是"那个开关有问题"。
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8801"
RES = []


def check(label, ok, detail=""):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode())


def put(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="PUT",
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:                                     # noqa: BLE001
            return e.code, {}


def flat(d):
    """把 /api/settings 摊成 {section.key: value}。"""
    out = {}
    for g in d.get("groups", []):
        for s in g.get("sections", []):
            for c in s.get("controls", []):
                out[f"{s['key']}.{c['key']}"] = c.get("value")
    return out


print("=" * 74)
print("一、★ 空 template 不许再把保存拦下来（这就是那个 bug）")
print("=" * 74)
before = get("/api/settings")
vals = flat(before)
# 取一个开关当前值，改成相反值再发回去（模拟"我改了一个开关然后保存"）
target = "fish_reprice.auto_reprice"
orig = bool(vals.get(target))
payload = {section: {} for section in
           {k.split(".")[0] for k in vals}}
for k, v in vals.items():
    sec, key = k.split(".", 1)
    payload[sec][key] = v
payload["fish_reprice"]["auto_reprice"] = (not orig)
# ★★ 关键：带上那个空 template（前端真实行为）
payload.setdefault("ticket_image", {})["template"] = ""

st, d = put("/api/settings", payload)
check("PUT 返回 200", st == 200, f"HTTP {st}")
problems = d.get("problems") or []
check("★★★ 保存成功（problems 为空）", not problems, str(problems))
check("★★ 不再报「取票图：没有图片内容」",
      not any("取票图" in str(x) for x in problems), str(problems))

after = flat(get("/api/settings"))
check("★★ 那次改动真的落盘了（读回来是反值）",
      bool(after.get(target)) == (not orig),
      f"原={orig} 现={after.get(target)}")

# 还原
payload["fish_reprice"]["auto_reprice"] = orig
payload["ticket_image"]["template"] = ""
st2, d2 = put("/api/settings", payload)
check("还原也成功", st2 == 200 and not (d2.get("problems") or []),
      str(d2.get("problems")))
check("还原后值对了", bool(flat(get("/api/settings")).get(target)) == orig)

print()
print("=" * 74)
print("二、★ 真的给了图片内容时，仍然要能存（别为了修 bug 把功能关掉）")
print("=" * 74)
# 1x1 透明 PNG
PNG_1PX = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
payload["ticket_image"]["template"] = PNG_1PX
st3, d3 = put("/api/settings", payload)
check("带真图时保存成功", st3 == 200 and not (d3.get("problems") or []),
      str(d3.get("problems") or d3.get("ticket_image_error")))

# ★ 收尾：把这张测试图清掉，不要留在生产配置里
from application import ticket_image as ti                              # noqa: E402
try:
    cur = ti.load() or {}
    cur.pop("data_b64", None)
    cur["present"] = bool(cur.get("url"))
    ti.save(cur)
except Exception:                                             # noqa: BLE001
    pass

print()
print("=" * 74)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 74)
sys.exit(0 if all(RES) else 1)
