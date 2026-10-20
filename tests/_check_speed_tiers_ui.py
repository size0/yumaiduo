"""票速设置（复选框组）不许再把值存丢。

★★★ 这是一个真 bug 的回归测试，而且它【真的破坏过配置】：
   「票速设置」在后端是三个独立标量（speed_slow / speed_fast / speed_flash），
   但前端原来假设"整个复选框组 = 一个数组字段"，
   三个复选框全写进 speed_flash ⇒
     · speed_fast / speed_slow 永远改不了（没有控件指向它们）
     · 一保存，speed_flash 变成数组 []（空数组被当成 False）
   ⇒ 我在浏览器里点了一次保存，就把 speed_flash 从 true 弄成了 false，
     而 _check_ticket_flow 的"档位阈值"断言当场失败 —— 我才发现。

★ 所以这里验两件事：
   ① 前端 DOM 上每个复选框各指向自己的字段（data-key 不重复）
   ② 勾掉一个 → 保存 → 只有那一个字段变，其它不动
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RES = []


def check(label, ok, detail=""):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def page():
    from playwright.sync_api import sync_playwright
    return sync_playwright


# ★★ 浏览器那一段需要 playwright。服务器上没装（那是开发期工具，
#   不该为了一条测试去改生产环境的 Python 安装方式）——
#   ⇒ 没装就【跳过】，不判失败；有装（本机）就跑完整链路。
#   ★ 前三节不依赖浏览器，任何环境都会跑，所以"字段映射"这个核心
#     判据在任何地方都有人守。
try:
    _PW = page()
    _HAS_PW = True
except Exception:                                             # noqa: BLE001
    _PW = None
    _HAS_PW = False
print(f"  （playwright 可用: {_HAS_PW}）")


# ---------- ① 后端：keys 元数据与字段名对得上 ----------
import dataclasses                                              # noqa: E402
from settings import TicketRelease, Settings                    # noqa: E402

tr = TicketRelease()
field_names = {f.name for f in dataclasses.fields(tr)}
meta = {f.name: f.metadata for f in dataclasses.fields(tr)}
keys = list(meta["speed_flash"].get("keys") or [])
check("★★ speed_flash 的 metadata 里配了 keys（三项）",
      len(keys) == 3, str(keys))
check("★★ keys 每项都是真实存在的布尔字段",
      all(k in field_names for k in keys), str(keys))
check("★★ keys 不重复（否则又会有字段没人能改）",
      len(set(keys)) == len(keys), str(keys))
check("★ keys 与 options 数量一致",
      len(keys) == len(meta["speed_flash"].get("options") or []),
      f"{len(keys)} vs {len(meta['speed_flash'].get('options') or [])}")
check("★★ 三个档位字段都被 keys 覆盖（不会漏掉谁）",
      {"speed_slow", "speed_fast", "speed_flash"} <= set(keys), str(keys))

# ---------- ② 序列化：发出去的控件带 keys + value_by_key ----------
payload = Settings().to_payload() if hasattr(Settings(), "to_payload") else None
if payload is None:
    # 找序列化入口
    for name in ("to_dict", "control_payload", "ui_payload", "export_dict"):
        if hasattr(Settings(), name):
            payload = getattr(Settings(), name)()
            break
check("★ 能拿到序列化结果", payload is not None, type(payload).__name__)

# ---------- ③ 前端源码：渲染器/收集器的分流写对了 ----------
_js = (ROOT / "web" / "config.js").read_text(encoding="utf-8")
check("★★ 渲染器读 c.keys（按选项分配字段名）", "c.keys" in _js)
check("★★ 渲染器给独立字段打 data-own 标记", "data-own" in _js)
check("★★★ 收集器按 data-own 分流（不是一律聚合数组）",
      "dataset.own" in _js)
check("★ 老行为保留：没有 keys 的组仍然聚合成数组",
      "bucket[k].push(input.dataset.opt)" in _js)

# ---------- ④ 真浏览器：勾一下，只有一个字段变 ----------
_sf = ROOT / "data" / "settings.json"
_before = json.loads(_sf.read_text(encoding="utf-8")) if _sf.exists() else None
if not _HAS_PW:
    print()
    print("=" * 74)
    print("四、真浏览器验证 —— 跳过（这个环境没装 playwright）")
    print("=" * 74)
    print("   ★ 前三节已经把核心判据验了（字段映射 / keys / 收集器分流）")
    print("   ★ 要看真浏览器那一段，在有 Chrome+playwright 的机器上跑")
try:
    if _HAS_PW:
      with _PW() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page(viewport={"width": 1500, "height": 950})
        cap = {}
        pg.on("request", lambda r: cap.update({"b": r.post_data})
              if (r.method == "PUT" and "/api/settings" in r.url) else None)
        pg.goto("http://127.0.0.1:8801/features", wait_until="networkidle")
        pg.wait_for_timeout(1200)
        dom = pg.evaluate("""() => {
          const sec=[...document.querySelectorAll('[data-section]')]
            .find(s=>s.dataset.section==='ticket_release');
          if(!sec) return null;
          return [...sec.querySelectorAll('input[type=checkbox]')]
            .map(x=>({key:x.dataset.key, own:x.dataset.own, checked:x.checked}));
        }""")
        check("★★ DOM 里有三个复选框", bool(dom) and len(dom) == 3, str(dom))
        if dom:
            got = [x["key"] for x in dom]
            check("★★★ 三个复选框指向【三个不同】字段（这是 bug 的核心）",
                  len(set(got)) == 3, str(got))
            check("★★ 都带 data-own 标记", all(x["own"] == "1" for x in dom),
                  str([x["own"] for x in dom]))
            # 勾掉"急速票"那一项 → 保存
            pg.evaluate("""() => {
              const sec=[...document.querySelectorAll('[data-section]')]
                .find(s=>s.dataset.section==='ticket_release');
              const cb=[...sec.querySelectorAll('input[type=checkbox]')]
                .find(x=>x.dataset.key==='speed_flash');
              cb.click();
            }""")
            pg.wait_for_timeout(300)
            pg.evaluate("""() => { const b=document.getElementById('btn-save');
                                   if(b) b.click(); }""")
            pg.wait_for_timeout(2500)
            sent = json.loads(cap.get("b") or "{}").get("ticket_release") or {}
            check("★★★ 发回的是三个【独立布尔】、不是数组",
                  all(isinstance(sent.get(k), bool)
                      for k in ("speed_slow", "speed_fast", "speed_flash")),
                  json.dumps(sent, ensure_ascii=False))
            check("★★ 只有 speed_flash 变了（另外两个没被连累）",
                  sent.get("speed_flash") is False
                  and sent.get("speed_slow") is True
                  and sent.get("speed_fast") is True,
                  json.dumps(sent, ensure_ascii=False))
        b.close()
finally:
    # ★ 把配置还原（这个测试会动真实配置文件 —— 必须自己收尾）
    if _before is not None:
        _sf.write_text(json.dumps(_before, ensure_ascii=False, indent=2),
                       encoding="utf-8")

print()
print("=" * 74)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 74)
sys.exit(0 if all(RES) else 1)
