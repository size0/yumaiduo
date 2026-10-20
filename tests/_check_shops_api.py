"""直接验 PUT /api/shops：开关能写、能读回、非法输入被拦。

★ 为什么单独测接口：界面上那一下点击最终就是打这个 PUT。
  界面测试受"本机没有插件进程"限制（/api/shops 的 GET 读不到店铺），
  但 PUT 不依赖插件 —— 它只改我们自己的开关文件。
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


def put(body):
    req = urllib.request.Request(
        BASE + "/api/shops", data=json.dumps(body).encode(),
        method="PUT", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:                                     # noqa: BLE001
            return e.code, {}


# ★ 先备份真实的 shops.json，测完恢复（不污染运行中的配置）
shops_file = ROOT / "data" / "shops.json"
backup = shops_file.read_bytes() if shops_file.exists() else None
try:
    print("=" * 74)
    print("一、关掉一个店")
    print("=" * 74)
    st, d = put({"shop_id": "TEST-SHOP-1", "send_enabled": False})
    check("PUT 返回 200", st == 200, f"HTTP {st}")
    check("ok=True", d.get("ok") is True, str(d)[:90])
    check("★ 回的就是刚设的值（False）", d.get("send_enabled") is False,
          str(d.get("send_enabled")))
    check("★ 带 scope 说明（免得以为是改平台状态）",
          "鱼麦多" in str(d.get("scope", "")), str(d.get("scope"))[:60])

    print()
    print("=" * 74)
    print("二、再打开")
    print("=" * 74)
    st2, d2 = put({"shop_id": "TEST-SHOP-1", "send_enabled": True})
    check("PUT 返回 200", st2 == 200, f"HTTP {st2}")
    check("★ 回 True", d2.get("send_enabled") is True, str(d2.get("send_enabled")))

    print()
    print("=" * 74)
    print("三、非法输入要被拦（400，不是 500 也不是静默成功）")
    print("=" * 74)
    st3, d3 = put({"send_enabled": True})
    check("缺 shop_id ⇒ 400", st3 == 400, f"HTTP {st3} {str(d3)[:60]}")
    st4, d4 = put({"shop_id": "X"})
    check("缺 send_enabled ⇒ 400", st4 == 400, f"HTTP {st4} {str(d4)[:60]}")

    print()
    print("=" * 74)
    print("四、落盘了吗（真的写进 data/shops.json）")
    print("=" * 74)
    on_disk = json.loads(shops_file.read_text(encoding="utf-8")) \
        if shops_file.exists() else {}
    check("★ 文件里有这个店", "TEST-SHOP-1" in on_disk, str(list(on_disk))[:80])
    check("★ 值写对了",
          on_disk.get("TEST-SHOP-1", {}).get("send_enabled") is True,
          str(on_disk.get("TEST-SHOP-1")))
finally:
    # 恢复原状
    if backup is None:
        if shops_file.exists():
            shops_file.unlink()
    else:
        shops_file.write_bytes(backup)

print()
print("=" * 74)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 74)
sys.exit(0 if all(RES) else 1)
