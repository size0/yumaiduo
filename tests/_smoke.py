"""
_smoke.py —— 端到端冒烟测试（一条命令，全站体检）

════════════════════════════════════════════════════════════════
★ 为什么必须有这个文件

  之前的教训：改配置系统时，把 AI 助理整页弄坏了 ——
  字段没了、接口变了、连 system prompt 都拼不出来。

  根因不是"手滑"，是**验证方式不对**：
  我每次只验证"我改的那个东西对不对"，
  没有任何东西检查"我没改的东西还是不是好的"。

  这个文件就是那个东西。它回答一个问题：
  **从用户视角看，整个站还能不能用。**

  所以它必须：
    · 真的起服务（不是 import 一下就完事）
    · 真的用浏览器打开每个页面（不是只查文件存不存在）
    · 真的点每个接口并检查返回内容里的关键字段
    · 检查页面【渲染后】的 DOM，而不是源文件

  改动任何东西之前之后各跑一次，就不需要靠记性了。
════════════════════════════════════════════════════════════════
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
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



ROOT = Path(__file__).resolve().parent.parent

# ★ 测试脚本在 tests/ 子目录里，必须把项目根加进 sys.path，
#   否则 `import settings` 找不到 —— 这是移动目录后必然要改的一处。
sys.path.insert(0, str(ROOT))
PORT = 8899                      # 用独立端口，不干扰你自己开着的 8801
BASE = f"http://127.0.0.1:{PORT}"

RESULTS = []


def _linked(text: str, href: str) -> bool:
    """
    页面里有没有指向 href 的链接（或脚本/样式）。

    ★ 为什么两种写法都认：界面可以挂在 /ta/ 前缀下，那时 HTML 里是
      相对路径（href="qa"），靠服务端注入的 <base href="/ta/"> 解析；
      本机开发时是根路径。两种都合法，断言不该只认一种。
    """
    rel = href.lstrip("/")
    return (f'href="{href}"' in text or f'href="{rel}"' in text
            or f'src="{href}"' in text or f'src="{rel}"' in text)


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ==================================================================== HTTP 工具
def get(path, timeout=6):
    """返回 (状态码, 文本)。失败返回 (0, 错误信息)。"""
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:                       # noqa: BLE001
        return 0, str(exc)


def put(path, payload, timeout=6):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, method="PUT",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception as exc:                       # noqa: BLE001
        return 0, {"error": str(exc)}


def get_json(path):
    status, text = get(path)
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, {}


# ==================================================================== 浏览器
def find_browser() -> str | None:
    """找一个能用的无头浏览器。找不到就跳过渲染检查（但会明说）。"""
    for cand in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("google-chrome"), shutil.which("chromium"),
    ]:
        if cand and Path(cand).exists():
            return cand
    return None


def rendered_dom(browser: str, url: str, budget_ms: int = 6000) -> str:
    """用无头浏览器打开页面并返回【渲染后】的 DOM。"""
    profile = Path(tempfile.gettempdir()) / f"smoke-{os.getpid()}-{time.time_ns()}"
    try:
        out = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={profile}", f"--virtual-time-budget={budget_ms}",
             "--dump-dom", url],
            capture_output=True, timeout=90)
        return out.stdout.decode("utf-8", "replace")
    except Exception as exc:                       # noqa: BLE001
        return f"<!-- browser failed: {exc} -->"
    finally:
        shutil.rmtree(profile, ignore_errors=True)


# ==================================================================== 启动服务
def port_free(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def start_server() -> subprocess.Popen | None:
    if not port_free(PORT):
        print(f"  ✗ 端口 {PORT} 已被占用，无法启动测试服务")
        return None
    env = dict(os.environ, PYTHONUTF8="1", SMOKE_PORT=str(PORT))
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys;sys.path.insert(0,'.');"
         "import server;server.PORT=int(os.environ['SMOKE_PORT']);server.HOST='127.0.0.1';"
         "server.main()"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if not port_free(PORT):
            return proc
        time.sleep(0.1)
    proc.kill()
    return None


# ==================================================================== 主流程
def main() -> int:
    print("=" * 74)
    print("全站冒烟测试")
    print("=" * 74)

    section("零、模块能不能导入（import 期就报错的话，后面全白搭）")
    # ★ 只查【当前系统】的模块。
    #   （曾经这里排除过几个从 V5 搬来的模块，它们已按
    #     「V5 只作参考、不得引用」的规则删除。）
    for name in ["settings", "templates", "qa", "server",
                 "domain.conversation", "domain.quote_rules",
                 "application.ticket_flow",
                 "integrations.liangpiao.api", "integrations.liangpiao.callbacks",
                 "integrations.liangpiao.client", "integrations.liangpiao.signature",
                 "prompts.persona"]:
        try:
            __import__(name)
            check(f"import {name}", True)
        except Exception as exc:                   # noqa: BLE001
            check(f"import {name}", False, f"★ {type(exc).__name__}: {exc}")

    section("一、AI助理配置（这块曾经整个丢过，重点查）")
    try:
        from settings import DEFAULT as cfg
        a = cfg.assistant
        need = ["agent_enabled", "business_scope", "takeover_seconds",
                "human_pause_seconds", "memory_hours", "memory_messages",
                "model_name", "persona", "precautions", "reply_style",
                "stage_filter_enabled", "allowed_stages"]
        miss = [k for k in need if not hasattr(a, k)]
        check("AI助理字段齐全", not miss, f"★ 缺 {miss}" if miss else f"{len(need)} 个")
        prompt = a.build_prompt()
        check("system prompt 可生成", len(prompt) > 300, f"{len(prompt)} 字")
        check("prompt 含不可改层风控条款", "【绝对不能做】" in prompt)
    except Exception as exc:                       # noqa: BLE001
        check("AI助理配置可用", False, f"★ {type(exc).__name__}: {exc}")

    section("二、状态机 / 文案 / 配置 三者的一致性")
    try:
        import templates as tpl
        from domain.conversation import GUARD_RULES, REPLY_OF, ConvState, Conversation, Event
        defined = {t["key"] for t in tpl.TEMPLATES}
        needed = set(REPLY_OF.values()) | {r["reply"] for r in GUARD_RULES.values()
                                           if r.get("reply")}
        miss = sorted(needed - defined)
        check("状态机引用的模板都存在", not miss, f"★ 缺 {miss}" if miss else f"{len(needed)} 个")

        conv = Conversation(chat_id="smoke")
        for ev in [Event.BUYER_IMAGE, Event.RECOGNIZE_OK, Event.BUYER_CONFIRM,
                   Event.ORDER_REPRICED, Event.ORDER_PAID, Event.TICKET_OK]:
            conv.apply(ev)
        check("主链路能走完（发图→报价→确认→改价→付款→出票）",
              conv.state is ConvState.DELIVERED, conv.state.value)
    except Exception as exc:                       # noqa: BLE001
        check("状态机可用", False, f"★ {type(exc).__name__}: {exc}")

    section("三、接口（每个都要 200 且关键字段存在）")
    proc = start_server()
    if proc is None:
        check("测试服务启动", False, "端口被占用")
    else:
        check("测试服务启动", True, f"端口 {PORT}")
        try:
            eps = ["/api/assistant", "/api/settings", "/api/templates",
                   "/api/qa", "/api/rules"]
            for ep in eps:
                status, data = get_json(ep)
                ok = status == 200 and isinstance(data, dict) and data
                check(f"GET {ep}", ok, f"HTTP {status}" if ok else f"★ HTTP {status}")

            # AI助理接口的关键字段 —— 页面就靠这些活
            _, a = get_json("/api/assistant")
            for key in ["agent_enabled", "takeover_seconds", "human_pause_seconds",
                        "memory_hours", "memory_messages", "model_name",
                        "model_options", "persona", "precautions", "reply_style",
                        "effective_prompt", "stage_labels", "allowed_stages"]:
                if key not in a:
                    check(f"AI助理接口含 {key}", False, "★ 缺字段")
                    break
            else:
                check("AI助理接口字段完整", True, f"{len(a)} 个字段")

            _, s = get_json("/api/settings")
            nsec = len([x for g in s.get("groups", []) for x in g["sections"]])
            nctl = len([c for g in s.get("groups", []) for x in g["sections"]
                        for c in x["controls"]])
            check("功能设置接口有配置节", nsec >= 25, f"{nsec} 节 / {nctl} 控件")
            check("功能设置接口有派生数据",
                  {"buyer_commands", "speed_tiers", "quote_ttl_minutes", "confirm_keyword"}
                  <= set(s.get("derived", {})), str(list(s.get("derived", {}))))

            # 报价规则接口
            _, rl = get_json("/api/rules")
            # ★ 阈值从 6 改成 3：初始规则是按【良票真实品牌名】生成的，
            #   首屏会拿到万达/连锁/兜底三条。不是"功能变少了"——
            #   早先那 6 条是票达人页面的复印件，院线名良票里没有，
            #   看着热闹但一条都匹配不上。
            check("报价规则接口返回规则", len(rl.get("rules", [])) >= 3,
                  f"{len(rl.get('rules', []))} 条")
            check("报价规则接口有院线可选列表", len(rl.get("brands", [])) >= 2,
                  f"{len(rl.get('brands', []))} 条院线")
            # ★ 断言改过两次，记一下为什么要改：
            #   ① 早先要求 >= 20 条院线 —— 但那时界面上是票达人的【分级名】，
            #      良票 brand/list 里没有，看着热闹却一条都匹配不上。
            #   ② 后来写成"良票品牌名与票达人分级名【不能有交集】"——
            #      当时 known_brands 只有硬编码的两个名字，所以碰巧通过。
            #   ③ ★ 现在（2026-10-14）院线缓存真的接上了（140 个真实品牌名），
            #      实测两边【有 10 个交集】：票达人的分级名里本身就混着
            #      真实品牌名（'UME影城' '万象影城' '中影国际影城'
            #      '卢米埃影城' '金逸影城' …）。
            #      ⇒ "不能有交集"这个前提本身就是错的，不能再拿它当断言。
            #   ★ 真正该验的是：
            #      · 两个列表都存在且非空（页面要能分别显示）
            #      · 良票品牌名是【动态拉到的全集】，不是一个手写的小清单
            #        —— 这正是 sync_brands_from_api 曾经没人调、
            #           导致 138 个院线名进不来的那个缺口
            _kb = rl.get("known_brands") or []
            _tn = rl.get("tier_names_from_piaodaren") or []
            check("★ 院线名分得清良票品牌名 vs 票达人分级名",
                  isinstance(_kb, list) and isinstance(_tn, list)
                  and bool(_kb) and bool(_tn),
                  f"良票品牌 {len(_kb)} 个 / 票达人分级 {len(_tn)} 个")
            check("★★ 良票院线名是动态拉到的全集（不是硬编码的两个）",
                  len(_kb) >= 20,
                  f"{len(_kb)} 个 —— 少于 20 说明品牌缓存没同步上"
                  f"（sync_brands_from_api 没人调的老问题）")
            check("★ 两个列表不是同一份",
                  set(_kb) != set(_tn),
                  f"交集 {len(set(_kb) & set(_tn))} 个")
            check("报价规则接口给出模式→priceMode 映射",
                  rl.get("options", {}).get("price_mode_of_quote", {}).get("渠道一口价") == "FIXED",
                  str(rl.get("options", {}).get("price_mode_of_quote")))

            # ★★★ 2026-10-14 改（店主定的新流程）：
            #   报价卡里【不再】出现「回复'{confirm_cmd}'开始购票」——
            #   下单改由「买家直接拍下」触发（订单卡片事件会自动落本地单）。
            #   ⇒ 原来那条断言（"确认指令读自配置"必须出现在报价卡里）
            #     已经过时，改成断新文案；同时保留"配置本身还在"这一条，
            #     因为 confirm_keyword 仍然用来识别买家的确认回复。
            _, prev = get_json("/api/preview/QUOTE_CARD")
            parts = prev.get("parts", [])
            ck = s.get("derived", {}).get("confirm_keyword", "")
            check("报价卡能渲染出多条消息", len(parts) >= 2, f"{len(parts)} 条")
            _joined = "\n".join(parts)
            check("★★ 报价卡教买家「直接拍下」（不再是回复指令）",
                  "直接拍下" in _joined, _joined[-60:])
            check("★★ 报价卡里【不再】出现「回复'{confirm_cmd}'开始购票」",
                  "开始购票" not in _joined, _joined[-60:])
            check("★ 确认关键字配置仍然有效（用于识别买家的确认回复）",
                  bool(ck), f"确认关键字={ck!r}")

            section("四、写接口（保存要能往返，非法值要被拦下）")
            _, before = get_json("/api/assistant")
            status, out = put("/api/assistant",
                              {"takeover_seconds": before["takeover_seconds"],
                               "persona": before["persona"]})
            check("PUT /api/assistant 成功", status == 200 and out.get("ok") is True,
                  f"HTTP {status} / {out.get('problems')}")
            _, after = get_json("/api/assistant")
            check("AI助理保存后能读回",
                  after.get("persona") == before.get("persona"),
                  "人设内容一致")

            status, out = put("/api/assistant", {"takeover_seconds": 99999})
            check("非法值被拦截", out.get("ok") is False and out.get("problems"),
                  f"{out.get('problems')}")
            _, still = get_json("/api/assistant")
            check("非法值没有落盘", still.get("takeover_seconds") == before["takeover_seconds"],
                  f"仍是 {still.get('takeover_seconds')}")

            section("五、页面渲染（真的用浏览器打开，查渲染后的 DOM）")
            browser = find_browser()
            if not browser:
                check("找到无头浏览器", False, "跳过渲染检查 —— 没装 Chrome/Edge")
            else:
                check("找到无头浏览器", True, Path(browser).name)

                # AI助理页
                dom = rendered_dom(browser, BASE + "/")
                for label, pattern, why in [
                    ("人设文本框", 'id="persona"', "AI助理的核心"),
                    ("注意事项", 'id="precautions"', ""),
                    ("回复风格", 'id="reply_style"', ""),
                    ("模型下拉", 'id="model"', ""),
                    ("停止时机滑块", 'id="stage-slider"', ""),
                    ("system prompt 预览", 'id="effective-prompt"', ""),
                ]:
                    check(f"AI助理页有{label}", pattern in dom, why)

                # 功能设置页
                dom2 = rendered_dom(browser, BASE + "/features")
                n_row = len(re.findall(r'class="rowBox', dom2))
                n_nav = len(re.findall(r'class="nav-sec"', dom2))
                n_chk = len(re.findall(r'type="checkbox"', dom2))
                n_sw = len(re.findall(r'class="switch', dom2))
                check("功能设置页渲染出配置行", n_row >= 60, f"{n_row} 行")
                check("功能设置页渲染出左侧导航", n_nav >= 24, f"{n_nav} 项")
                check("功能设置页渲染出复选框", n_chk >= 15,
                      f"{n_chk} 个（==0 说明 el() 把 false 跳过了）")
                check("功能设置页渲染出开关", n_sw >= 10, f"{n_sw} 个")

                # 回复文案页
                dom3 = rendered_dom(browser, BASE + "/replies")
                check("回复文案页渲染出阶段导航", 'id="stage-nav"' in dom3 and
                      len(re.findall(r'class="stage-btn', dom3)) >= 6,
                      f"{len(re.findall(r'class=.stage-btn', dom3))} 个阶段")
                check("回复文案页渲染出模板卡片",
                      len(re.findall(r'class="rc-card"', dom3)) >= 1,
                      f"{len(re.findall(r'class=.rc-card.', dom3))} 张卡片")

                # 报价规则页
                dom4 = rendered_dom(browser, BASE + "/rules")
                n_rows = len(re.findall(r'<tr class="', dom4))
                n_cells = len(re.findall(r"<td", dom4))
                check("报价规则页渲染出规则行", n_rows >= 3, f"{n_rows} 行")
                # ★ 断言改成"每行 8 列"，而不是写死单元格总数。
                #   早先写 >= 54（= 6 行 × 9 列），规则数一变就误报 ——
                #   那验的是"恰好 6 条规则"，不是"表格结构对"。
                # ★★ 2026-10-14 从 9 列改成 8 列：
                #   去掉了「生效时间段」列。原因：实测 4 条规则的时段
                #   全是 00:00:00-23:59:59（全天），那一列固定 148px
                #   却把整表推出视口（列宽合计 1272 > 容器 1180），
                #   最右的「编辑/删除」被截断。现在非全天规则
                #   在「启用状态」列里多显示一行小字，信息不丢。
                check("报价规则页每行 8 列",
                      n_rows > 0 and n_cells >= n_rows * 8,
                      f"{n_rows} 行 × 8 = {n_rows * 8}，实际 {n_cells} 个单元格")
                check("报价规则页有校验告警区", 'id="problems"' in dom4)

                # ★ 弹窗必须真的能看见 —— 这是踩过的坑：
                #   .modal-panel 的入场动画从 opacity:0 开始，
                #   动画没跑时弹窗会永久不可见（盖住页面、点不动的空壳）。
                dom5 = rendered_dom(browser, BASE + "/rules?open=new")
                # 注意：不能匹配 class="rf-item"（带收尾引号）——
                # 那样会漏掉 class="rf-item rf-span2"。
                n_rf = len(re.findall(r'class="rf-item', dom5))
                check("规则弹窗渲染出字段", n_rf >= 13, f"{n_rf} 个字段")
                # ★ 按 ID 逐个核对，比数个数更可靠：
                #   数个数时少一个字段看不出来，点名核对能发现具体缺哪个。
                need_fields = ["f-name", "f-weight", "f-brands", "f-excluded",
                               "f-time-start", "f-time-end", "f-quote-mode",
                               "f-price-left", "f-price-right", "f-method",
                               "f-markup", "f-limit", "f-adjust", "f-capped",
                               "f-enabled"]
                miss = [f for f in need_fields if f'id="{f}"' not in dom5]
                check("规则弹窗字段一个不少（按 ID 核对）", not miss,
                      f"★ 缺 {miss}" if miss else f"{len(need_fields)} 个字段齐全")
                check("规则弹窗渲染出院线可选", len(re.findall(r"data-brand=", dom5)) >= 2,
                      f"{len(re.findall(r'data-brand=', dom5))} 个院线")
                check("规则弹窗渲染出实时预览", 'id="rule-preview"' in dom5)
                check("★ 弹窗面板不是 opacity:0（动画没跑也必须可见）",
                      "opacity: 0;" not in dom5 and 'opacity:0;' not in dom5,
                      "入场动画的起始 opacity 不能是 0")

                # ★★ 关键词回复页
                #    这一页是【补上来的】：/api/qa 之前只有 GET，
                #    界面上任何地方都看不到、也改不了那 18 条。
                _, qa = get_json("/api/qa")
                check("关键词接口返回规则", len(qa.get("rules", [])) >= 10,
                      f"{len(qa.get('rules', []))} 条")
                check("★ 关键词字段齐全（缺字段界面就编不了）",
                      all(k in (qa.get("rules") or [{}])[0]
                          for k in ("qa_id", "keyword", "content", "trigger",
                                    "total_limit", "daily_limit", "message_type")),
                      str(sorted((qa.get("rules") or [{}])[0].keys())))
                check("接口给出触发器/限额/消息类型选项",
                      bool(qa.get("triggers")) and bool(qa.get("limits"))
                      and bool(qa.get("message_types")),
                      f"{qa.get('triggers')} / {len(qa.get('limits') or [])} 档 / "
                      f"{qa.get('message_types')}")
                check("接口给出引用格式（内容里能写 [ID]）",
                      bool(qa.get("ref_pattern")), str(qa.get("ref_pattern")))

                dom6 = rendered_dom(browser, BASE + "/qa")
                n_qrow = len(re.findall(r'data-edit="\d+"', dom6))
                check("关键词页渲染出规则行", n_qrow >= 10, f"{n_qrow} 行")
                check("关键词页有新增按钮", 'id="btn-new"' in dom6)
                check("关键词页有编辑弹窗", 'id="modal"' in dom6)
                n_qf = len(re.findall(r'class="qf-item', dom6))
                check("关键词弹窗渲染出字段", n_qf >= 5, f"{n_qf} 个字段")
                need_qf = ["qf-keyword", "qf-id", "qf-content", "qf-trigger",
                           "qf-total", "qf-daily", "qf-type", "qf-preview"]
                miss_qf = [f for f in need_qf if f'id="{f}"' not in dom6]
                check("关键词弹窗字段一个不少（按 ID 核对）", not miss_qf,
                      f"★ 缺 {miss_qf}" if miss_qf else f"{len(need_qf)} 个字段齐全")
                check("关键词页有引用展开预览", 'id="qf-preview"' in dom6)
                check("★ 导航里有「关键词回复」入口",
                      all(_linked(rendered_dom(browser, BASE + p), "/qa")
                          for p in ("/", "/features", "/rules")),
                      "四个页面都能点进去")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    section("结果")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for label, ok, detail in RESULTS:
        if not ok:
            print(f"  ✗ {label}  {detail}")
    print(f"\n{passed}/{total} 项通过")
    if passed != total:
        print("★ 有失败项 —— 在说'做好了'之前必须先修完")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
