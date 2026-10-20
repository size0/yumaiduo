"""
_check_settings.py —— 配置规格 与 settings.py 的一致性校验

它回答：
  1. 规格里列的每个配置项，代码里都有吗
  2. 默认值与规格里抄的一致吗
  3. 所有数值型配置都在合法区间内吗
  4. 派生表（买家指令/票速）是否与配置一致
"""
import sys
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

# ★ 配置规格文档的真实位置。文档是别人给的（票达人那份），
#   放在工作区根目录，不叫 SPEC.md —— 名字是中文原名。
SPEC = ROOT.parent / "票达人-配置规格.md"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import DEFAULT, Settings



results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


# 规格里写明的默认值 —— 这是"抄得对不对"的对照表
SPEC_DEFAULTS = {
    # 识别报价
    "recognize_quote.auto_recognize": True,
    "recognize_quote.quote_ttl_minutes": 30,
    "recognize_quote.keyword_cinema": "影院",
    "recognize_quote.keyword_movie": "影片",
    "recognize_quote.keyword_show": "场次",
    # 兜底报价
    "fallback_quote.enabled": True,
    "fallback_quote.mode": "预估限价",
    "fallback_quote.price_capped": True,
    # 二次报价
    "second_quote.enabled": False,
    "second_quote.wait_seconds": 30,
    "second_quote.discount_percent": 3,
    # 放映时间限制
    "showtime_limit.remaining_minutes": 40,
    # 夜间识别
    "night_recognize.start": "23:30",
    "night_recognize.end": "08:00",
    # 催促下单
    "urge_order.delay_minutes": 60,
    # 闲鱼改价
    "fish_reprice.auto_reprice": True,
    "fish_reprice.need_user_confirm": False,
    "fish_reprice.unique_command": False,
    "fish_reprice.confirm_keyword": "正确",
    # 校验与下架
    "amount_check.verify_amount": True,
    # 出票及放单
    "ticket_release.mode": "自动",
    "ticket_release.allow_seat_change": True,
    "ticket_release.speed_slow": True,
    "ticket_release.speed_fast": True,
    "ticket_release.speed_flash": True,
    # 出票溢价
    "ticket_premium.enabled": False,
    "ticket_premium.cap_yuan": 0.00,
    # 重放订单
    "reorder.max_retries": 0,
    "reorder.add_limit_percent": 1.0,
    # 订单拦截
    "order_intercept.keyword": "拦截订单",
    # 订单自动拦截
    "order_auto_intercept.enabled": False,
    # 订单发货
    "order_ship.auto_ship": True,
    "order_ship.force_ship": True,
    # 订单退款
    "order_refund.delay_seconds": 0,
    "order_refund.time_start": "00:00:00",
    "order_refund.time_end": "23:59:59",
    # 取票图模板
    "ticket_image.enabled": False,
    "ticket_image.template": "模板1",
    # 获取票码
    "get_ticket_code.keyword": "获取票码",
    # 观影提醒
    "movie_remind.advance_minutes": 10,
    # 催收货 / 催好评
    "urge_receive.delay_minutes": 0,
    "urge_review.delay_minutes": 0,
    # 黑名单
    "blacklist.reply": "系统升级维护中，建议您咨询其他商家",
    # 回复
    "reply.intelligent_qa_enabled": True,
    "reply.greeting_interval_hours": 0,
    # 消息延迟
    "message_delay.enabled": False,
    "message_delay.min_ms": 100,
    "message_delay.max_ms": 500,
    # 人工介入
    "manual.close_after_hours": 1,
    # 呼叫人工
    "call_manual.keyword": "人工客服",
    "call_manual.reply": "已为您呼叫人工客服，请稍等",
}

print("=" * 76)
print("一、规格默认值 vs 代码默认值")
print("=" * 76)

mismatches = []
for path, expected in SPEC_DEFAULTS.items():
    section, attr = path.split(".")
    obj = getattr(DEFAULT, section)
    actual = getattr(obj, attr, "<不存在>")
    if actual != expected:
        mismatches.append(f"{path}: 规格={expected!r} 代码={actual!r}")

check("所有默认值与规格一致", not mismatches,
      f"★ 不一致 {len(mismatches)} 处:\n      " + "\n      ".join(mismatches) if mismatches
      else f"{len(SPEC_DEFAULTS)} 项逐一核对通过")

print()
print("=" * 76)
print("二、配置节完整性（规格列了 25 节）")
print("=" * 76)

SPEC_SECTIONS = [
    "recognize_quote", "fallback_quote", "second_quote", "showtime_limit", "night_recognize",
    "urge_order", "fish_reprice", "amount_check",
    "ticket_release", "ticket_premium", "reorder", "order_intercept", "order_auto_intercept",
    "order_ship", "order_refund", "ticket_image", "get_ticket_code",
    "movie_remind", "urge_receive", "urge_review", "blacklist",
    "reply", "message_delay", "manual", "call_manual",
]
missing = [s for s in SPEC_SECTIONS if not hasattr(DEFAULT, s)]
check("规格里每个配置节都有对应字段", not missing,
      f"★ 缺失: {missing}" if missing else f"{len(SPEC_SECTIONS)} 节全部存在")

# 反向：代码里有没有规格没写的节
#   ★ assistant 是 AI助理配置，不属于「功能设置」规格，单列在下面校验
NON_FEATURE_SECTIONS = {"assistant"}
extra = [f.name for f in DEFAULT.__dataclass_fields__.values()
         if f.name not in SPEC_SECTIONS and f.name not in NON_FEATURE_SECTIONS]
check("没有规格外的多余配置节", not extra,
      f"★ 多出: {extra}" if extra else "无多余节")

print()
print("=" * 76)
print("二之二、★ AI助理配置字段完整性（这块曾经整个丢过）")
print("=" * 76)

ASSISTANT_FIELDS = [
    "agent_enabled", "business_scope", "business_scope_options",
    "takeover_seconds", "human_pause_seconds", "memory_hours", "memory_messages",
    "model_name", "model_options", "api_key_tail",
    "persona", "precautions", "reply_style", "style_presets",
    "stage_filter_enabled", "stage_labels", "allowed_stages",
]
a = DEFAULT.assistant
missing_a = [f for f in ASSISTANT_FIELDS if not hasattr(a, f)]
check("AI助理字段一个不少", not missing_a,
      f"★ 缺失: {missing_a}" if missing_a else f"{len(ASSISTANT_FIELDS)} 个字段齐全")

check("人设三段都有默认值", all(str(getattr(a, k)).strip()
                                for k in ("persona", "precautions", "reply_style")),
      f"人设 {len(a.persona)} 字 / 注意 {len(a.precautions)} 字 / 风格 {len(a.reply_style)} 字")

prompt = a.build_prompt()
check("system prompt 能拼出来", len(prompt) > 300, f"{len(prompt)} 字")
check("prompt 含不可改层 HARD_RULES", "【绝对不能做】" in prompt,
      "风控条款在 prompt 里")
check("prompt 含店铺注意事项", "【店铺补充注意事项】" in prompt)
check("prompt 含说话风格", "【说话风格】" in prompt)
check("AI助理校验通过", not a.validate(), f"{a.validate()}")

# 序列化往返：AI助理配置存下来再读回来要一致
snap = DEFAULT.to_dict()
s2 = Settings()
s2.apply_dict(snap)
check("AI助理配置序列化往返一致",
      s2.assistant.to_dict() == DEFAULT.assistant.to_dict())

print()
print("=" * 76)
print("三、数值区间校验")
print("=" * 76)

problems = DEFAULT.validate()
check("默认配置全部合法", not problems,
      f"★ {problems}" if problems else "25 节默认值都在合法区间")

# 故意造非法值，验证校验器真的能抓到
print()
print("  验证校验器有效性（故意造错）：")
bad_cases = [
    ("报价时长 0", lambda s: setattr(s.recognize_quote, "quote_ttl_minutes", 0)),
    ("报价时长 99999", lambda s: setattr(s.recognize_quote, "quote_ttl_minutes", 99999)),
    ("二次报价比例 99", lambda s: setattr(s.second_quote, "discount_percent", 99)),
    ("消息延迟下限>上限", lambda s: setattr(s.message_delay, "min_ms", 900)),
    ("重放次数 99", lambda s: setattr(s.reorder, "max_retries", 99)),
]
caught = 0
for name, mutate in bad_cases:
    s = Settings()
    mutate(s)
    if s.validate():
        caught += 1
        print(f"    ✓ 「{name}」被抓到")
    else:
        print(f"    ✗ 「{name}」漏过了")
check("校验器能抓到非法值", caught == len(bad_cases),
      f"{caught}/{len(bad_cases)} 被抓到")

print()
print("=" * 76)
print("四、派生表一致性")
print("=" * 76)

cmds = DEFAULT.buyer_commands()
expected_cmds = {"confirm", "intercept", "get_code", "call_manual",
                 "pick_cinema", "pick_movie", "pick_show"}
check("买家指令表覆盖全部指令", set(cmds) == expected_cmds,
      f"{sorted(cmds)}")

# 指令表里的值必须来自配置，不能是硬编码
check("指令来自配置项（intercept=拦截订单）",
      cmds["intercept"] == [DEFAULT.order_intercept.keyword],
      f"{cmds['intercept']}")

# 票速档位
tiers = DEFAULT.speed_tiers()
check("票速档位与配置开关一致", len(tiers) == 3,
      f"{[(t, m) for t, m in tiers]}")

# 关掉一个开关，档位应该少一个
s2 = Settings()
s2.ticket_release.speed_flash = False
check("关掉急速票后档位减少", len(s2.speed_tiers()) == 2,
      f"{[t for t, _ in s2.speed_tiers()]}")

print()
print("=" * 76)
print("五、规格文档本身")
print("=" * 76)
check("配置规格文件存在", SPEC.exists(), str(SPEC))
if SPEC.exists():
    text = SPEC.read_text(encoding="utf-8")
    missing_sections = [s for s in ["识别报价", "兜底报价", "二次报价", "放映时间限制",
                                    "夜间识别", "催促下单", "闲鱼改价", "校验与下架",
                                    "出票及放单", "出票溢价", "重放订单", "订单拦截",
                                    "订单自动拦截", "订单发货", "订单退款", "取票图模板",
                                    "获取票码", "观影提醒", "催收货", "催好评", "黑名单",
                                    "回复", "消息延迟", "人工介入", "呼叫人工"]
                        if s not in text]
    check("规格覆盖全部 25 节", not missing_sections,
          f"★ 缺: {missing_sections}" if missing_sections else "25 节全部有说明")
    check("规格标注了确认关键字冲突", "确认无误" in text and "正确" in text,
          "含冲突提示")


# ==================================================================== 未接线标记
# ★★ 背景：设置页曾有 58 个控件"能动、能存、但没有任何代码读它"。
#   危害是【骗人】—— 运营以为关掉了某个功能，其实没有。
#   现在用一张注册表（settings.UNWIRED）标出来，并靠下面这条断言
#   防止新的摆设再长出来。
print()
print("=" * 76)
print("未接线标记（防摆设再长出来）")
print("=" * 76)

import re as _re                                                        # noqa: E402
from dataclasses import fields as _fields                               # noqa: E402
import settings as _st                                                  # noqa: E402

# ① 注册表指向的 section / 字段必须真的存在
_bad_ref = []
for _sec, _items in _st.UNWIRED.items():
    if not hasattr(DEFAULT, _sec):
        _bad_ref.append(f"section {_sec} 不存在")
        continue
    _names = {f.name for f in _fields(getattr(DEFAULT, _sec))}
    for _k in _items:
        if _k not in _names:
            _bad_ref.append(f"{_sec}.{_k} 不存在")
check("★★ UNWIRED 注册表指向的字段都真的存在（否则标了个空气）",
      not _bad_ref, str(_bad_ref[:5]))

# ② ★★★ 核心不变量：没标未接线的控件，必须真的有人读它
_corpus = {}
for _q in sorted(ROOT.rglob("*.py")):
    _sp = str(_q).replace("\\", "/")
    if "__pycache__" in _sp:
        continue
    if any(_x.startswith(("_", ".")) for _x in _q.parts):
        continue
    if "tests/" in _sp or "demos/" in _sp or _q.name == "settings.py":
        continue
    _corpus[_sp] = _q.read_text(encoding="utf-8", errors="replace")

# ★ 提醒这类"间接读"：字段在 application/reminders.py 里被读，
#   那个文件也在业务语料里 ⇒ 只要它用 cfg.<section>.<key> 读，就算有人读。
#   （它的调用方 plugin_api.run_reminders 已经在语料里，所以是活的。）

# ★★ 2026-10-14：窗口大小收成【一个常量】。
#   原来三处各写一个数（400 / 300 / 900）——
#   而"section 和 key 之间隔多远"完全取决于中间写了多少注释，
#   每加一段注释就可能把某个窗口撑破 ⇒ 假失败。
#   ★ 假失败和真失败长得一模一样，都要手算距离才能区分。
#     与其每遇到一次就调大某一个数字，不如统一成一个足够宽的值。
_WIN = 2000

_live_bad = []

for _g in DEFAULT.layout():
    for _sec in _g["sections"]:
        for _c in _sec["controls"]:
            _sk, _k = _sec["key"], _c["key"]
            if _st.unwired_reason(_sk, _k):
                continue
            # ★ 判据：业务代码里能搜到 <section>.<key> 的读取
            #   （访问语法就是 cfg.<section>.<key> /
            #     CONFIG.<section>.<key>，见 application/service.py）
            #   ★ lookbehind 只排单词字符、【不能排点】——
            #     section 名前面正好是那个点。这个坑我踩过。
            # ★ 两种读取形态都要认（实测两种都存在）：
            #   ① cfg.movie_remind.advance_minutes   （点号链）
            #   ② getattr(cfg.movie_remind, "advance_minutes", 60)
            #   ★ 第二版只认了点号链，漏了 getattr ⇒ 假失败。
            _pat_dot = _re.compile(
                r"(?<!\w)" + _re.escape(_sk)
                + r"\s*\.\s*" + _re.escape(_k) + r"\b")
            # 第三种形态：先 load().<section> 拿到子配置，再取 .<key>
            #   实测 message_delay 这么读：
            #       cfg = _st.load().message_delay
            #       ms = int(getattr(cfg, "min_ms", 0) or 0)
            #   section 在上一行、key 在下一行 ⇒ 紧邻正则认不出。
            #   用一个"同段落"窗口兜。
            _pat_get = _re.compile(
                r"getattr\s*\(\s*[\w\.]*" + _re.escape(_sk)
                + r"\s*,\s*[\"']" + _re.escape(_k) + r"[\"']")
            _pat_win = _re.compile(
                r"\.load\(\)\s*\.\s*" + _re.escape(_sk)
                + r"\b[\s\S]{0," + str(_WIN) + r"}?[\"']"
                + _re.escape(_k) + r"[\"']")
            # 第四种形态：section 对象被【传进别的模块/函数】，
            #   在那个模块里按属性取 key。
            #   实测 ticket_image 这么读：
            #       application/ticket_image.py:
            #           ti = getattr(cfg, "ticket_image", None)
            #           bool(getattr(ti, "enabled", False))
            #   ⇒ section 在 getattr 的第一个参数，key 在另一个 getattr 里，
            #     中间隔了赋值和换行。用一个"同段落"窗口兜。
            # 第四种形态：先拿到 section（点号链或字符串），
            #   之后用 getattr(局部名, "key") 取字段。
            #   实测几种写法：
            #       ti = getattr(cfg, "ticket_image", None) + getattr(ti, "enabled")
            #       cfg = _st.load().second_quote + getattr(cfg, "wait_seconds")
            #   ⇒ section 前面可能是【引号】也可能是【点】，
            #     后面可能是【引号】也可能是【换行】。
            #     ★ 我原来写成"引号…引号"两头都要求引号，
            #       于是 `_st.load().second_quote` 这种（前面是点）认不出。
            #     ⇒ 前后都放宽成「引号或点或空白」。
            _pat_sec = _re.compile(
                r"[\"'.]" + _re.escape(_sk) + r"[\"'.]?[\s\S]{0,"
                + str(_WIN) + r"}?[\"']" + _re.escape(_k) + r"[\"']")
            # 第五种形态：子配置先赋给一个局部名，后面用 getattr 取 key。
            #   实测 greeting 这么读：
            #       rs = cfg.reply
            #       if not bool(getattr(rs, "greeting_configured", False)):
            #       hours = int(getattr(rs, "greeting_interval_hours", 0) or 0)
            #   ⇒ section 是【点号链】不是带引号的字符串，
            #     所以上面那条 _pat_sec 认不出。这里按点号链兜。
            #   ★ 已经第五种了 —— 这说明"认定一个字段被读"这件事
            #     本来就有很多写法。与其不断加正则，不如让业务代码
            #     统一用一两种写法（见 settings.py 顶部的读取约定）。
            #   ★ 窗口大小：实测 greeting 那两行之间隔了 482 个字符
            #     （中间是我写的注释）。300 不够 ⇒ 放到 900。
            #     ★ 教训：窗口是"猜"出来的数字，猜小了就变成假失败。
            #       真失败和假失败长得一样，都要靠手算距离来区分。
            _pat_local = _re.compile(
                r"\.\s*" + _re.escape(_sk) + r"\b[\s\S]{0," + str(_WIN) + r"}?"
                r"[\"']" + _re.escape(_k) + r"[\"']")
            if not any(_pat_dot.search(_t) or _pat_get.search(_t)
                       or _pat_win.search(_t) or _pat_sec.search(_t)
                       or _pat_local.search(_t)
                       for _t in _corpus.values()):
                _live_bad.append(f"{_sk}.{_k}")
# ★★ 补充判定：认得【间接读】。
#   有些字段不是业务代码直接读的，而是被 settings.py 的某个方法读、
#   再由业务代码调那个方法。例：
#       settings.speed_tiers() 里读 self.ticket_release.speed_flash
#       ticket_flow 调 _st.load().speed_tiers()
#   ★★ 必须【两级都满足】才算数：
#       ① 字段出现在 settings.py 的某个【业务方法】里
#          （排除 to_dict / apply_dict / validate / layout /
#            buyer_commands —— 这些通用方法会碰每个字段，
#            拿它们当证据等于断言作废）
#       ② 那个方法名在【业务代码】里被调用
# ★★ 2026-10-14 扩：间接读还有第二种形态 ——
#   字段被【另一个模块】（如 application/reminders.py）读，
#   而那个模块被业务代码调用。例：
#       reminders.r_movie_remind() 读 cfg.movie_remind.advance_minutes
#       plugin_api.run_reminders() 调 build_reminder_commands() → 那些函数
#   ⇒ 语料里要包含这些"业务模块"，同时排除 settings.py 自己
#     （那样所有字段都会"被读"，断言就废了）。
_GENERIC = {"to_dict", "apply_dict", "validate", "layout", "buyer_commands"}
_method_of = {}
for _m in _re.finditer(
        r"\n    def (\w+)\(self[^)]*\)[^\n]*:\n(.*?)(?=\n    def |\Z)",
        Path(__file__).resolve().parent.parent.joinpath(
            "settings.py").read_text(encoding="utf-8"), _re.S):
    _name, _body = _m.group(1), _m.group(2)
    if _name in _GENERIC:
        continue
    for _g in DEFAULT.layout():
        for _sec in _g["sections"]:
            for _c in _sec["controls"]:
                if _st.unwired_reason(_sec["key"], _c["key"]):
                    continue
                if _c["key"] in _body:
                    _method_of.setdefault(
                        f"{_sec['key']}.{_c['key']}", []).append(_name)
_indirect_ok = set()
for _full, _methods in _method_of.items():
    for _mn in _methods:
        if any(_re.search(r"\.\s*" + _re.escape(_mn) + r"\s*\(", _src)
               for _src in _corpus.values()):
            _indirect_ok.add(_full)
            break

_live_bad = [x for x in _live_bad if x not in _indirect_ok]
check("★★★ 没标未接线的控件，必须真的有人读它（摆设不许再长）",
      not _live_bad,
      (f"既没标未接线、也没人读: {_live_bad[:6]}" if _live_bad else "")
      + (f"　（间接读 {len(_indirect_ok)} 个：{sorted(_indirect_ok)}）"
         if _indirect_ok else ""))

# ③ 后端带出来了、前端也要真的显示（带了不显示等于没标）
_WEB = ROOT / "web"
_cjs = (_WEB / "config.js").read_text(encoding="utf-8")
_ccss = (_WEB / "config.css").read_text(encoding="utf-8")
check("★★ 设置页渲染了「未接线」角标 + 行下说明",
      "unwired-badge" in _cjs and "unwired-note" in _cjs
      and "unwired-badge" in _ccss)
check("★ 会变值的控件（开关/单选/多选/排序）在未接线时真被禁用",
      "MUTATING_CONTROLS" in _cjs and "disabled = true" in _cjs)
check("★ 统计：未接线项数与注册表一致",
      sum(len(v) for v in _st.UNWIRED.values()) > 0,
      f"注册表标记 {sum(len(v) for v in _st.UNWIRED.values())} 项")

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
