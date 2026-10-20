"""
_check_ai.py —— 对话 AI 自检（假模型，不需要 Key）

════════════════════════════════════════════════════════════════
★ 这个测试补的是哪一块

  之前买家问「你们家能开发票吗」「我昨天买的那个还能改吗」「在吗」，
  系统一律回「您提交的确认指令不存在」——
  因为除了 18 条写死的关键词，没有东西能生成回复。

  这个文件验证新加的那条通路，以及它的**边界**：
    · 关键词命中时【不走模型】（能逐字确定的别让模型发挥）
    · 模型答得了 → 发它的回复
    · 模型答不了 / 报错 / 没 Key → 转人工（而不是甩"指令不存在"）
    · ★ 模型说什么都【不改变会话状态】
    · ★ 模型出错【不告诉买家】

★ 用假模型而不是真模型

  真模型每次输出都不一样，没法断言。
  假模型能精确构造这几条路 —— 包括真模型很难复现的"返回空"、
  "HTTP 500"、"说 [转人工]"。
════════════════════════════════════════════════════════════════
"""

import json
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


RESULTS = []

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings as settings_module
from application.conversation_ai import HANDOFF_MARK, ConversationAI
from application.service import TicketService
from domain.conversation import ConvState
from domain.quote_rules import QuoteRuleEngine, seed_rules
from integrations.liangpiao.api import LiangPiaoAPI
from integrations.liangpiao.client import LiangPiaoClient
from integrations.model.client import ModelClient, ModelError




def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return bool(ok)


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


# ==================================================================== 假模型
class FakeModelTransport:
    """
    假模型传输。

    ★ 可以精确指定"这次返回什么" —— 包括真模型很难复现的异常路径。
    ★ 也能模拟"模型要调工具"：给 tool_calls 就返回一次工具调用请求。
    """

    def __init__(self, content="好的哈～", *, status=200, explode=False,
                 empty=False, tool_calls=None, tool_rounds=1):
        self.content = content
        self.status = status
        self.explode = explode
        self.empty = empty
        # tool_calls: [{"name": ..., "arguments": {...}}, ...] —— 前 N 轮返回它
        self.tool_calls = list(tool_calls or [])
        self.tool_rounds = tool_rounds
        self.calls = []

    def post(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers})
        if self.explode:
            raise ModelError("连不上模型服务：模拟网络故障", retryable=False)
        if self.status != 200:
            return self.status, json.dumps(
                {"error": {"message": "模拟错误"}}, ensure_ascii=False)

        # ★ 前 tool_rounds 次返回工具调用请求，之后返回文本
        if self.tool_calls and len(self.calls) <= self.tool_rounds:
            calls = [{"id": f"call_{i}", "type": "function",
                      "function": {"name": c["name"],
                                   "arguments": json.dumps(c.get("arguments") or {},
                                                           ensure_ascii=False)}}
                     for i, c in enumerate(self.tool_calls)]
            return 200, json.dumps({
                "choices": [{"message": {"role": "assistant", "content": None,
                                         "tool_calls": calls}}],
                "usage": {"total_tokens": 50},
            }, ensure_ascii=False)

        content = "" if self.empty else self.content
        return 200, json.dumps({
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"total_tokens": 42},
        }, ensure_ascii=False)

    def last_body(self):
        return self.calls[-1]["body"] if self.calls else None


class FakeLiangPiaoTransport:
    """良票的假传输（这个测试里只用来装配服务，不会被真正调到）。"""

    def send(self, url, body, headers, timeout):
        return json.dumps({"code": 0, "message": "ok", "requestId": "R",
                           "data": {}}, ensure_ascii=False)


def make_cfg(**over):
    """造一份配置，可覆盖任意字段。"""
    cfg = settings_module.Settings()
    for k, v in over.items():
        if "." in k:
            sec, field = k.split(".", 1)
            setattr(getattr(cfg, sec), field, v)
        else:
            setattr(cfg, k, v)
    return cfg


def _fake_pending():
    """造一个假的在途订单（测工具用）。"""
    from application.ticket_flow import PendingOrder
    return PendingOrder(
        order_no="LP-FAKE-001", out_order_no="OUT-001", chat_id="t1",
        cinema_name="万达影城", movie_name="测试影片", hall_name="6号厅",
        start_time="2099-07-28T19:30:00+08:00",
        seats=[{"seatNo": "4排1座"}], ticket_mode="STANDARD",
        quote_amount_fen=7020, freeze_amount_fen=7020)


def make_service(*, model_content="好的哈～", status=200, explode=False,
                 empty=False, api_key="sk-test", cfg=None,
                 tool_calls=None, tool_rounds=1, with_tools=True):
    cfg = cfg or settings_module.load()
    tr = FakeModelTransport(model_content, status=status, explode=explode,
                            empty=empty, tool_calls=tool_calls,
                            tool_rounds=tool_rounds)
    client = ModelClient(api_key, transport=tr)
    ai = ConversationAI(client, enabled=True)
    lp_tr = FakeLiangPiaoTransport()
    svc = TicketService(
        LiangPiaoAPI(LiangPiaoClient("K", "S", transport=lp_tr, sleep=lambda _: None)),
        QuoteRuleEngine(seed_rules(["万达影城"])),
        tel="13800000000", ai=ai, persist=False)
    return svc, tr


def main() -> int:                                   # noqa: C901
    # ------------------------------------------------------------------ 一
    section("一、★ 之前回不出来的 5 句话，现在能回了吗")
    cfg = make_cfg(**{"assistant.agent_enabled": True, "reply.intelligent_qa_enabled": True})
    svc, tr = make_service(model_content="可以的哈，我们支持开发票～", cfg=cfg)
    r = svc.on_message("c1", "你们家能开发票吗", config=cfg)
    check("「能开发票吗」现在有回复", bool(r.messages), str(r.template_keys))
    check("★ 走的是模型而不是「指令不存在」",
          "AI_REPLY" in r.template_keys, str(r.template_keys))
    check("回复内容来自模型",
          r.messages and "开发票" in r.messages[0], str(r.messages)[:60])
    check("不需要转人工", not r.need_human)

    # 模型不认识的问题也照样回
    for q in ["我昨天买的那个还能改吗", "帮我看看订单到哪了", "在吗"]:
        svc, tr = make_service(cfg=cfg)
        rr = svc.on_message("c2", q, config=cfg)
        check(f"「{q}」有回复", bool(rr.messages), str(rr.template_keys)[:40])

    section("一之二、首次问票价要引导发图")
    svc, tr = make_service(api_key="", cfg=cfg)
    r = svc.on_message("price-new", "你好啊，电影票多少钱哦", config=cfg)
    check("首次问价不转人工", not r.need_human, str(r.notes))
    check("明确请买家发选座图，不编价格",
          "PRICE_NEEDS_SCREENSHOT" in r.template_keys
          and "选座截图" in "".join(r.messages)
          and "多少钱" not in "".join(r.messages), str(r.messages))
    check("没有模型 Key 也能答首次询价", not tr.calls)

    svc, tr = make_service(cfg=cfg)
    r = svc.on_message("price-refund", "退票手续费多少钱", config=cfg)
    check("退票费用问题不误当新票询价",
          "PRICE_NEEDS_SCREENSHOT" not in r.template_keys, str(r.template_keys))

    svc, tr = make_service(cfg=cfg)
    svc.session("price-existing").conversation.state = ConvState.QUOTED
    r = svc.on_message("price-existing", "这单多少钱", config=cfg)
    check("已有报价时保留查价路径", "PRICE_NEEDS_SCREENSHOT" not in r.template_keys
          and bool(tr.calls), str(r.template_keys))

    cfg_greet = make_cfg(**{"assistant.agent_enabled": True,
                            "reply.greeting_configured": True,
                            "reply.greeting_interval_hours": 1})
    svc, tr = make_service(api_key="", cfg=cfg_greet)
    r = svc.on_message("price-greet", "电影票多少钱", config=cfg_greet)
    check("首次问价不重复追加打招呼", r.template_keys == ["PRICE_NEEDS_SCREENSHOT"],
          str(r.template_keys))
    r = svc.on_message("price-greet", "在吗", config=cfg_greet)
    check("问价后不会立刻再补一次打招呼", "GREETING" not in r.template_keys,
          str(r.template_keys))

    # ------------------------------------------------------------------ 二
    section("二、★ 关键词仍然优先（能逐字确定的别让模型发挥）")
    svc, tr = make_service(cfg=cfg)
    r = svc.on_message("c3", "999", config=cfg)
    check("命中关键词", any(k.startswith("QA:") for k in r.template_keys),
          str(r.template_keys))
    check("★★ 没有调用模型", not tr.calls,
          f"模型被调用 {len(tr.calls)} 次（应为 0）")

    svc, tr = make_service(cfg=cfg)
    r = svc.on_message("c3", "出票", config=cfg)
    check("「出票」也命中关键词", any(k.startswith("QA:") for k in r.template_keys))
    check("★ 也没调用模型", not tr.calls)

    # ------------------------------------------------------------------ 三
    section("三、★ 模型说「我搞不定」→ 必须转人工")
    svc, tr = make_service(
        model_content=f"{HANDOFF_MARK} 这个我帮您转人工看一下哈", cfg=cfg)
    r = svc.on_message("c4", "我要投诉你们", config=cfg)
    check("★ 标记为需要人工", r.need_human)
    check("★ 发了配置里的转人工话术",
          any("人工客服" in m for m in r.messages), str(r.messages)[:70])
    check("标记了 AI_HANDOFF", "AI_HANDOFF" in r.template_keys)
    check("★ 模型给的安抚话也发了（别让买家干等）",
          any("转人工" in m for m in r.messages), str(r.messages)[:70])
    check("★ 发出去的话里【不含】内部标记",
          all(HANDOFF_MARK not in m for m in r.messages), str(r.messages)[:70])

    # ------------------------------------------------------------------ 四
    section("四、★ 模型出错 → 转人工，且【不告诉买家出错了】")
    svc, tr = make_service(explode=True, cfg=cfg)
    r = svc.on_message("c5", "在吗", config=cfg)
    check("★ 转人工", r.need_human)
    check("★ 没有把错误信息发给买家",
          all("模拟网络故障" not in m and "ModelError" not in m for m in r.messages),
          str(r.messages)[:70])
    check("★ 错误记在 notes 里（给我们看）",
          any("模型调用失败" in n for n in r.notes), str(r.notes)[:80])
    check("有给买家的话（不是空手）", bool(r.messages), str(r.template_keys))

    svc, tr = make_service(status=500, cfg=cfg)
    r = svc.on_message("c6", "在吗", config=cfg)
    check("HTTP 500 也转人工", r.need_human)
    check("500 的错误也不外泄",
          all("500" not in m and "模拟错误" not in m for m in r.messages))

    svc, tr = make_service(empty=True, cfg=cfg)
    r = svc.on_message("c7", "在吗", config=cfg)
    check("模型返回空 → 转人工", r.need_human)
    check("空返回的说明记在 notes", any("空内容" in n for n in r.notes), str(r.notes))

    # ------------------------------------------------------------------ 五
    section("五、★ 没配 Key 也不能挂 —— 直接转人工")
    svc, tr = make_service(api_key="", cfg=cfg)
    r = svc.on_message("c8", "你们家能开发票吗", config=cfg)
    check("★ 没 Key 时转人工", r.need_human)
    check("★ 没 Key 时也没报错", bool(r.messages), str(r.template_keys))
    check("说明是缺 Key", any("API Key" in n for n in r.notes), str(r.notes)[:80])

    r2 = svc.on_message("c8", "999", config=cfg)
    check("★ 没 Key 时关键词仍然能用", any(k.startswith("QA:") for k in r2.template_keys))

    section("五之二、AI 总开关关掉时也是转人工")
    # ★ 必须用【关掉的那个】配置。早先这里传的是上面 agent_enabled=True 的 cfg，
    #   等于没测到"关掉"这条路。
    cfg_off = make_cfg(**{"assistant.agent_enabled": False})
    svc, tr = make_service(cfg=cfg_off)
    r = svc.on_message("c9", "在吗", config=cfg_off)
    check("★ 总开关关 → 转人工", r.need_human)
    check("★ 没有调用模型", not tr.calls)
    check("★ 关掉时说明原因", any("开关关闭" in n for n in r.notes), str(r.notes)[:70])
    check("★ 关掉时也关键词可用",
          any(k.startswith("QA:")
              for k in svc.on_message("c9", "999", config=cfg_off).template_keys))

    # ------------------------------------------------------------------ 六
    section("六、★★ 模型说了什么都不能改变会话状态")
    svc, tr = make_service(
        model_content="我已经帮您下单了，也帮您退款了，订单号 LP999",
        cfg=cfg)
    before = svc.session("c10").conversation.state
    r = svc.on_message("c10", "帮我把这单退了", config=cfg)
    after = svc.session("c10").conversation.state
    check("★ 状态没变（模型管说话，管不了钱）", before is after,
          f"{before.value} → {after.value}")
    check("状态仍是空闲（没有因为模型的话就进退款）",
          after is ConvState.IDLE, after.value)
    check("★ 模型编的订单号也只是文本，没有进任何状态",
          svc.session("c10").conversation.pending_order is None)

    # ------------------------------------------------------------------ 七
    section("七、★ system prompt 里必须带上本店经验")
    cfg = make_cfg(**{"assistant.agent_enabled": True,
                      "assistant.experience": "问能不能选座 → 说不能，截图为准\n"
                                              "要开发票 → 转人工"})
    svc, tr = make_service(cfg=cfg)
    svc.on_message("c11", "能选座吗", config=cfg)
    body = tr.last_body()
    check("调用了模型", body is not None)
    system = ""
    if body:
        for m in body.get("messages", []):
            if m.get("role") == "system":
                system = m.get("content", "")
                break
    check("★ 经验进了 system prompt", "问能不能选座" in system, "找到了经验段")
    check("★ 经验段有标题（模型知道这是要优先遵守的）",
          "本店经验" in system)
    check("★ HARD_RULES 也在（风控底线不能被经验覆盖）",
          "绝对不能做" in system or "不许承诺" in system)
    check("★ 本次任务约束也在（拿不准就转人工那条）",
          HANDOFF_MARK in system)
    check("★ 当前进度也在", "【当前进度】" in system)
    check("★ 只读事实段在，且明确说了暂无",
          "已知订单事实" in system and "暂无" in system)

    section("七之二、★ 模型不许看到内部信息（成本/毛利）")
    check("system prompt 里没有成本字样",
          "成本" not in system and "毛利" not in system,
          "确认没有把内部成本放进 prompt")
    # 检查参数
    check("★ thinking 显式关掉了（快、便宜）",
          body and body.get("thinking") == {"type": "disabled"},
          str(body.get("thinking") if body else None))
    check("Authorization 头带上了",
          tr.calls and "Authorization" in tr.calls[-1]["headers"])

    section("七之三、★★ 工具必须是【只读】的（这条是底线）")
    # ★ 这条断言改过。原来断言 "tools" not in body ——
    #   那测的是"模型一个工具都没有"（纯文本生成器）。
    #   现在给了只读工具，所以要验的是【工具是只读的】，不是"没有工具"。
    sent = body.get("tools") if body else None
    check("★ 确实带了工具（模型能自己查）", bool(sent), f"{len(sent or [])} 个")
    from application.tools import TOOL_NAMES
    names = [t["function"]["name"] for t in (sent or [])]
    check("★ 工具名与白名单一致", names == TOOL_NAMES, str(names))
    # ★★ 最要紧的一条：不许出现任何"会改状态"的工具
    bad = [n for n in names if not n.startswith("read_") and n != "search_faq"]
    check("★★ 没有非只读工具（必须 read_* 或 search_faq）", not bad,
          f"★ 发现可写工具：{bad}" if bad else str(names))
    for t in (sent or []):
        desc = t["function"].get("description", "")
        hit = [w for w in ("下单", "发起退票", "改价", "发送消息") if w in desc]
        check(f"★ {t['function']['name']} 的描述里没有可写动作",
              not hit, f"★ 描述含 {hit}" if hit else "只读")

    # ------------------------------------------------------------------ 八
    section("八、★ 对话记忆：模型每轮都要重新告诉它")
    svc, tr = make_service(model_content="好的", cfg=cfg)
    svc.on_message("c12", "第一句", config=cfg)
    svc.on_message("c12", "第二句", config=cfg)
    body = tr.last_body()
    roles = [m["role"] for m in body.get("messages", [])]
    check("★ 第二轮的请求里带了第一轮的历史",
          roles.count("user") >= 2, str(roles))
    check("system 只有一条（每次重新拼）", roles.count("system") == 1, str(roles))
    check("user/assistant 交替",
          roles.index("user") < len(roles) - 1, str(roles))

    section("八之二、★ 记忆窗口按配置截断")
    cfg_mem = make_cfg(**{"assistant.agent_enabled": True,
                          "assistant.memory_messages": 4})
    svc, tr = make_service(cfg=cfg_mem)
    s = svc.session("c13")
    for i in range(12):
        s.remember(f"第{i}句", "好")
    # ★ 用注入的那份配置，不是盘上的 —— 否则量的是线上配置的窗口
    hist = svc._history(s, cfg_mem)
    check("★ 按 memory_messages 截断", len(hist) <= 4, f"{len(hist)} 条（限 4）")
    # ★ 截断保留的是【最近】的几轮。最后一条是 assistant 的"好"，
    #   最后一条 user 才是"第11句" —— 断言要看对角色。
    last_user = next((m["content"] for m in reversed(hist)
                      if m["role"] == "user"), "")
    check("★ 留的是最近的", "第11句" in last_user, last_user)
    check("★ 最早的那句被丢掉了",
          all("第0句" not in m["content"] for m in hist), str(hist)[:80])

    # ------------------------------------------------------------------ 九
    section("九、★ 模型客户端自身")
    tr = FakeModelTransport()
    c = ModelClient("k", transport=tr)
    check("ready 为真", c.ready)
    check("no key 时 ready 为假", not ModelClient("").ready)
    r = c.chat([{"role": "user", "content": "hi"}], system="sys")
    check("解析出内容", r.content == "好的哈～", r.content)
    check("记账了 tokens", r.tokens == 42 and c.total_tokens == 42,
          f"{r.tokens}/{c.total_tokens}")
    check("模型名不在白名单时回退默认",
          ModelClient("k", model="乱写的").model == "deepseek-flash")

    # 鉴权失败不重试
    tr2 = FakeModelTransport(status=401)
    c2 = ModelClient("bad", transport=tr2, retries=3, sleep=lambda _: None)
    try:
        c2.chat([{"role": "user", "content": "x"}])
        check("401 应当抛错", False)
    except ModelError as exc:
        check("★ 401 抛错且不重试", len(tr2.calls) == 1,
              f"调了 {len(tr2.calls)} 次（应为 1）")
        check("错误信息说清是鉴权", "鉴权" in str(exc), str(exc)[:60])

    # 429 会重试
    class FlakyTransport:
        def __init__(self):
            self.n = 0

        def post(self, url, body, headers, timeout):
            self.n += 1
            if self.n == 1:
                return 429, json.dumps({"error": {"message": "限流"}})
            return 200, json.dumps({"choices": [{"message": {"content": "重试成功"}}]})

    fl = FlakyTransport()
    c3 = ModelClient("k", transport=fl, retries=2, sleep=lambda _: None)
    r3 = c3.chat([{"role": "user", "content": "x"}])
    check("★ 429 会重试并成功", r3.content == "重试成功" and fl.n == 2,
          f"调了 {fl.n} 次")

    section("十、★★ 工具循环：模型真的调工具了吗")
    cfg = make_cfg(**{"assistant.agent_enabled": True,
                      "reply.intelligent_qa_enabled": True})

    # ---- 模型调 read_order，然后给答复 ----
    svc, tr = make_service(
        model_content="您那单已经出票了（万达影城）",
        tool_calls=[{"name": "read_order"}], cfg=cfg)
    # 先造一个在途订单，否则工具会说"还没有订单"
    svc.session("t1").conversation.pending_order = _fake_pending()
    r = svc.on_message("t1", "我那单到哪了", config=cfg)
    check("★ 模型真的调了工具（不是直接答）", len(tr.calls) >= 2,
          f"模型被调用 {len(tr.calls)} 次")
    check("★ 答复来自工具查完之后", "出票" in (r.messages[0] if r.messages else ""),
          str(r.messages)[:50])
    check("★ note 里记了查过什么",
          any("查了" in n and "read_order" in n for n in r.notes),
          str(r.notes)[:80])

    # ---- 工具结果真的回灌给模型了吗 ----
    body2 = tr.calls[-1]["body"]
    roles = [m.get("role") for m in body2.get("messages", [])]
    check("★ 第二轮请求里有 role=tool 的消息", "tool" in roles, str(roles))
    tool_msg = next((m for m in body2["messages"] if m.get("role") == "tool"), {})
    check("★ 工具结果里带回了订单信息",
          "order" in (tool_msg.get("content") or ""),
          (tool_msg.get("content") or "")[:60])
    check("★ 第二轮仍带 tools（模型可以继续查）",
          bool(body2.get("tools")))

    # ---- 工具报"查不到"时，模型看到的是人话 ----
    svc, tr = make_service(tool_calls=[{"name": "read_order"}], cfg=cfg)
    # 没有 pending_order
    r = svc.on_message("t2", "我那单到哪了", config=cfg)
    body3 = tr.calls[-1]["body"]
    tm = next((m for m in body3["messages"] if m.get("role") == "tool"), {})
    check("★ 没订单时工具说人话（不是空对象）",
          "还没有订单" in (tm.get("content") or ""), (tm.get("content") or "")[:60])

    # ---- 模型编了个不存在的工具 ----
    svc, tr = make_service(tool_calls=[{"name": "delete_order"}], cfg=cfg)
    r = svc.on_message("t3", "把那单删了", config=cfg)
    body4 = tr.calls[-1]["body"]
    tm4 = next((m for m in body4["messages"] if m.get("role") == "tool"), {})
    check("★★ 不存在的工具被拒（模型不能以为它做成了）",
          "没有" in (tm4.get("content") or "") and "工具" in (tm4.get("content") or ""),
          (tm4.get("content") or "")[:60])

    # ---- 参数不是合法 JSON ----
    class BadJsonTransport(FakeModelTransport):
        def post(self, url, body, headers, timeout):
            self.calls.append({"url": url, "body": body, "headers": headers})
            if len(self.calls) == 1:
                return 200, json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "read_order",
                                                 "arguments": "{不是json"}}]}}],
                    "usage": {"total_tokens": 10}}, ensure_ascii=False)
            return 200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": "好的"}}],
                "usage": {"total_tokens": 10}}, ensure_ascii=False)
    from application.conversation_ai import ConversationAI as _CA
    bt = BadJsonTransport()
    ai_bad = _CA(ModelClient("k", transport=bt), enabled=True)
    svc_bad = TicketService(
        LiangPiaoAPI(LiangPiaoClient("K", "S", transport=FakeLiangPiaoTransport(),
                                      sleep=lambda _: None)),
        QuoteRuleEngine(seed_rules(["万达影城"])),
        tel="13800000000", ai=ai_bad, persist=False)
    svc_bad.on_message("t4", "查一下", config=cfg)
    tm5 = next((m for m in bt.calls[-1]["body"]["messages"]
                if m.get("role") == "tool"), {})
    check("★ 参数不是 JSON 时也回一句人话",
          "合法 JSON" in (tm5.get("content") or ""), (tm5.get("content") or "")[:60])

    section("十之二、★★ 轮数上限：模型不能一直调下去")
    from application.conversation_ai import MAX_TOOL_ROUNDS
    # 让模型【每一轮】都调工具 —— 正常模型不会，但要防住
    svc, tr = make_service(tool_calls=[{"name": "read_order"}],
                           tool_rounds=99, cfg=cfg)
    r = svc.on_message("t5", "查查查", config=cfg)
    check(f"★ 模型调用次数被 MAX_TOOL_ROUNDS({MAX_TOOL_ROUNDS}) 限制住",
          len(tr.calls) <= MAX_TOOL_ROUNDS + 1,
          f"调了 {len(tr.calls)} 次")
    check("★ 到上限后仍然给了买家一句回复（不静默）",
          bool(r.messages), str(r.template_keys))

    section("十之三、★★ 工具拿不到别的会话的东西")
    svc, tr = make_service(tool_calls=[{"name": "read_order"}], cfg=cfg)
    # A 会话有订单，B 会话没有
    svc.session("A").conversation.pending_order = _fake_pending()
    svc.on_message("A", "我那单呢", config=cfg)
    tm_a = next((m for m in tr.calls[-1]["body"]["messages"]
                 if m.get("role") == "tool"), {})
    _, tr_b = make_service(tool_calls=[{"name": "read_order"}], cfg=cfg)
    # B 会话（新服务）查 —— 拿不到 A 的订单
    check("★ A 会话能查到自己的订单",
          "LP-FAKE" in (tm_a.get("content") or ""), (tm_a.get("content") or "")[:60])
    check("★★ 工具 schema 里没有 chat_id（模型无法指定查谁的）",
          all("chat_id" not in json.dumps(t.get("function", {}).get("parameters", {}))
              for t in (tr.calls[-1]["body"].get("tools") or [])),
          "身份隔离是结构保证的")

    section("★ 缺口清单（quote_gaps）—— 实测需求：万达买家补文字的场景")
    from application.conversation_ai import (ConversationAI, context_from,
                                             quote_gaps)

    class _Rec:
        def __init__(self, **kw):
            self.cinema_name = ""
            self.movie_name = ""
            self.show_id = ""
            self.start_time = ""
            self.seats = []
            self.city_name = ""
            for k, v in kw.items():
                setattr(self, k, v)

    class _Conv:
        def __init__(self, rec=None, seats=None, channel=None, city=None):
            self.recognition = rec
            self.seats = seats or []
            self.channel = channel
            self.city_name = city
            self.quote = None
            self.pending_order = None

    check("没发图 → 缺「选座截图」",
          quote_gaps(_Conv()) == ["选座截图"], str(quote_gaps(_Conv())))
    # ★ 万达：图上有影院、城市也在图上 → 不该再问城市
    g = quote_gaps(_Conv(_Rec(cinema_name="万达影城（呼和浩特店）",
                              city_name="呼和浩特"), channel="wanda"))
    check("★ 万达 · 城市已从图上拿到 → 不问城市", "城市" not in g, str(g))
    # ★ 万达：图上有影院但没城市 → 要问（要靠城市收窄影院）
    g = quote_gaps(_Conv(_Rec(cinema_name="万达影城IMAX店"), channel="wanda"))
    check("★ 万达 · 缺城市 → 要问", "城市" in g, str(g))
    # 良票不靠城市收窄 → 不该问城市
    g = quote_gaps(_Conv(_Rec(cinema_name="济宁影城"), channel="liangpiao"))
    check("★ 良票 · 不问城市（不靠城市收窄）", "城市" not in g, str(g))
    # 全齐 → 无缺口
    g = quote_gaps(_Conv(_Rec(cinema_name="万达", movie_name="八仙",
                              show_id="S1", start_time="20:30",
                              city_name="呼和浩特"),
                         seats=[{"seatNo": "8排9座"}], channel="wanda"))
    check("全齐 → 无缺口", g == [], str(g))

    # ★ 缺口要真进 prompt
    conv = _Conv(_Rec(cinema_name="万达影城IMAX店"), channel="wanda")
    import settings as _st
    from domain.conversation import ConvState as _CS
    _cfg = _st.load()
    _ai = ConversationAI.__new__(ConversationAI)
    prompt = _ai._system_prompt(_cfg, context_from(conv), _CS.IDLE,
                                with_tools=True, conv=conv)
    check("★ 缺口真的进了 system prompt", "【还缺的信息" in prompt)
    check("★ prompt 里列了「城市」",
          "城市" in prompt.split("【还缺的信息")[1][:80],
          prompt.split("【还缺的信息")[1][:60].replace("\n", " "))
    check("★ prompt 说了「已经有的不要再问」", "不要再问" in prompt)

    section("⑨ ★★ 出票时效必须进模型上下文（否则模型照配置里写死的数答）")
    # ★★ 真缺口（2026-10-14 修）：时效是按【实际购入档位】变的
    #   （templates.TICKET_WINDOW，抄自良票文档 TicketMode 说明），
    #   而 conversation_ai 传给模型的事实块里【原本没有时效】——
    #   模型只能照 assistant.precautions 里运营写死的数字答。
    #   实测店主配置里那个数比 STANDARD 的窗口窄，
    #   而两张真图的场次（离开场 >40 分钟）实际都是 STANDARD
    #   ⇒ 买家问多久出票，模型说的和报价卡/TICKETING 的时效打架。
    from application.conversation_ai import _ticket_window_of, context_from
    from domain.conversation import Conversation as _C

    class _Q:
        total_price_fen = 3240; seat_count = 1; rule_name = "默认"

    class _R:
        cinema_name = "万达影城（娄底五江广场XLAND店）"
        movie_name = "什么意思夫妇"
        start_time = "2026-10-14T20:35:00+08:00"

    for mode, want in (("STANDARD", "1-60分钟"), ("FAST", "15分钟内"),
                       ("FLASH", "1-15分钟")):
        c = _C(chat_id="x"); c.quote = _Q(); c.recognition = _R()
        c.ticket_mode = mode
        check(f"★ {mode} → 模型看到 {want}", _ticket_window_of(c) == want,
              repr(_ticket_window_of(c)))
        check(f"★ {mode} 的时效写进了事实块",
              f"出票时效：{want}" in context_from(c), context_from(c)[:80])

    # ★★ 不知道档位时【必须返回空】，不许兜底成 STANDARD ——
    #   时效是对买家的承诺，不知道就别讲。
    #   （我第一版写成 or TICKET_STANDARD，空档位会返回 1-60分钟，
    #     正是这个函数要消灭的编一个数。）
    for bad in ("", None, "UNKNOWN"):
        c = _C(chat_id="y"); c.quote = _Q(); c.recognition = _R()
        if bad is not None:
            c.ticket_mode = bad
        check(f"★★ 档位为 {bad!r} 时不编时效", _ticket_window_of(c) == "",
              repr(_ticket_window_of(c)))

    # 没报过价就不该谈时效（还没下单，承诺什么服务）
    c0 = _C(chat_id="z"); c0.recognition = _R(); c0.ticket_mode = "STANDARD"
    check("★ 还没报价 → 不提时效", _ticket_window_of(c0) == "",
          repr(_ticket_window_of(c0)))

    section("⑩ ★★★ 意图判定 → 事件（店主指出的真需求）")
    # ★★★ 背景："买家的话 → 状态机事件"原先只有【逐字匹配】一层
    #   （_is_confirm 要求 text 逐字 ∈ 别名；外加 25 条关键词）。
    #   线上真实消息库里抓到的买家原话：「我已拍下，待付款」——
    #   不是关键词、不是确认别名 ⇒ 什么都不触发，单下不出去。
    #   店主明确说：万达识别 + 买家问话需要判意图，这才是要 agent 的原因。
    #
    # ★★ 设计：模型只判【意图名】，代码把意图名映射成事件，
    #   再拿事件去问【状态机自己】这个转移合不合法。
    #   所以模型能影响的只有状态机本来允许的那几个转移 ——
    #   猜错最多判成 NONE（什么都不发生），不会下错单。
    from application.service import INTENT_EVENTS
    from domain.conversation import Event as _Ev

    check("★★ 意图表只放【状态机认的】事件",
          all(isinstance(e, _Ev) for e in INTENT_EVENTS.values()),
          str({k: v.value for k, v in INTENT_EVENTS.items()}))

    class _IntentTransport:
        """按 prompt 分流：意图分类那一次回指定意图，其余回文本。"""

        def __init__(self, intent):
            self.intent = intent
            self.calls = []

        def post(self, url, body, headers, timeout):
            self.calls.append(body)
            blob = json.dumps(body, ensure_ascii=False)
            content = (json.dumps({"intent": self.intent, "why": "测试"})
                       if "意图分类器" in blob else "（不该被调用）")
            return 200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": content}}]},
                ensure_ascii=False)

    class _Conv:
        def __init__(self, state):
            self.state = state
            self.channel = "liangpiao"

    def _mk_intent_svc(intent):
        tr = _IntentTransport(intent)
        ai2 = ConversationAI(ModelClient("sk-test", transport=tr), enabled=True)
        svc2 = TicketService(
            LiangPiaoAPI(LiangPiaoClient("K", "S",
                                         transport=FakeLiangPiaoTransport(),
                                         sleep=lambda _: None)),
            QuoteRuleEngine(seed_rules(["万达影城"])),
            tel="13800000000", ai=ai2, persist=False)
        return svc2, tr

    cfg_on = make_cfg(**{"assistant.agent_enabled": True})
    svc_i, tr_i = _mk_intent_svc("CONFIRM")

    # ① 已报价状态：CONFIRM → buyer.confirm（这就是"放宽确认"）
    hit = svc_i._intent_to_event(_Conv(ConvState.QUOTED), "好的没问题", cfg_on)
    check("★★★ 已报价 + 模型判 CONFIRM → buyer.confirm",
          hit is not None and hit[0] is _Ev.BUYER_CONFIRM, repr(hit))

    # ② 没有合法意图的状态【不打模型】（省一次调用，也让计数不失真）
    for st in (ConvState.IDLE, ConvState.PICK_CINEMA, ConvState.AWAIT_TICKET,
               ConvState.DELIVERED, ConvState.MANUAL):
        tr_i.calls.clear()
        got = svc_i._intent_to_event(_Conv(st), "好的没问题", cfg_on)
        check(f"★★ {st.name} 下不打模型也不放行",
              got is None and not tr_i.calls,
              f"{got!r} / 模型调用 {len(tr_i.calls)} 次")

    # ③ 拿不准 / 表外意图一律 None —— 绝不放行
    for intent in ("NONE", "REPRICE", "CANCEL", "UNKNOWN", "confrim"):
        s3, _t3 = _mk_intent_svc(intent)
        got = s3._intent_to_event(_Conv(ConvState.QUOTED), "能便宜点吗", cfg_on)
        check(f"★★ 模型判 {intent} → 不放行", got is None, repr(got))

    # ④ 超长消息不打模型（买家发一长串通常是转述/投诉，不是短意图）
    s4, t4 = _mk_intent_svc("CONFIRM")
    got = s4._intent_to_event(_Conv(ConvState.QUOTED), "我" * 200, cfg_on)
    check("★ 超长消息不判意图（也不打模型）",
          got is None and not t4.calls, f"{got!r} / 调用 {len(t4.calls)}")

    # ⑤ 开关关掉时完全不判
    cfg_off2 = make_cfg(**{"assistant.agent_enabled": False})
    s5, t5 = _mk_intent_svc("CONFIRM")
    got = s5._intent_to_event(_Conv(ConvState.QUOTED), "好的", cfg_off2)
    check("★ agent_enabled=False 时完全不判意图",
          got is None and not t5.calls, f"{got!r} / 调用 {len(t5.calls)}")

    # ⑥ 解析容错（纯函数）：拼错/中文/两个词都不许猜
    for raw, want in (
        ('{"intent": "CONFIRM"}', "CONFIRM"),
        ('`json\n{"intent": "NONE"}\n`', "NONE"),
        ('{"intent": "CONFRIM"}', None),
        ('{"intent": "下单"}', None),
        ("CONFIRM CANCEL", None),
        ("", None),
    ):
        check(f"★ 解析 {raw[:26]!r} → {want!r}",
              ConversationAI._parse_intent(raw) == want,
              repr(ConversationAI._parse_intent(raw)))

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    for i, ok in enumerate(RESULTS):
        if not ok:
            print(f"  ✗ 第 {i + 1} 项失败")
    print(f"\n{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
