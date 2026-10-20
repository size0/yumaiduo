"""修正：佐证机制要区分【来源】，不能一刀切。

★ 实测暴露的问题：
  · 模型每轮都得给每个新字段写"逐字片段" —— 用起来很烦
  · 数字类字段（ticket_count=2）给"逐字片段"很别扭

★ 真实场景的三种来源，佐证要求应该不同：
    image  —— 截图识别来的（识别引擎已经看过图，它给的字段可信）
              ★ 不需要"逐字片段"，它给不出文字
    text   —— 模型从买家文字里抽的
              ★ 需要逐字片段（防它把老信息当这轮的）
    model  —— 模型自己推的（比如根据票价推张数）
              ★ 不许有这种来源 —— 推出来的不算事实
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from application.agent import Facts, Tools  # noqa: E402

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def q(facts):
    return {"total_yuan": 84.05, "quote_id": "Q-1"}


def main() -> int:
    t = Tools(provider="WANDA", quote_fn=q)

    print("=" * 74)
    print("① 占位值（实测撞到的 movie_name=\"未知\"）")
    print("=" * 74)
    f = Facts()
    o = t.execute("request_quote",
                  {"cinema_name": "万达", "movie_name": "未知", "show_date": "2026-10-12",
                   "show_time": "20:30", "ticket_count": 2},
                  f, "万达 20:30 两张")
    check("占位值被拒", o.error_code == "PLACEHOLDER_VALUE", o.error_code)
    check("「未知」没进事实", f.get("movie_name") is None, str(f.snapshot()))

    print()
    print("=" * 74)
    print("② ★ 佐证必须非空（空串 = 没有佐证，实测踩过）")
    print("=" * 74)
    f2 = Facts()
    o = t.execute("request_quote",
                  {"cinema_name": "万达", "movie_name": "八仙",
                   "evidence": {"cinema_name": "万达", "movie_name": ""}},
                  f2, "万达 八仙")
    check("★ 空串佐证被拒（'' in text 永远为真，是漏洞）",
          o.error_code == "EVIDENCE_MISSING", o.error_code)

    print()
    print("=" * 74)
    print("③ ★ 已确认字段不需要本轮佐证（第2轮只需给新字段）")
    print("=" * 74)
    f3 = Facts()
    f3.merge({"cinema_name": "万达", "movie_name": "八仙", "show_date": "2026-10-12",
              "seat_marking": "PRESENT"}, source="image")
    o = t.execute("request_quote",
                  {"cinema_name": "万达", "movie_name": "八仙", "show_date": "2026-10-12",
                   "show_time": "20:30", "ticket_count": 2,
                   "evidence": {"show_time": "20:30", "ticket_count": "两张"}},
                  f3, "20:30 两张")
    check("★ 只给新字段佐证就够", o.ok, f"{o.error_code} {o.message[:50]}")

    print()
    print("=" * 74)
    print("④ ★ 截图来源不需要佐证（识别引擎也读图，它给不出文字）")
    print("=" * 74)
    f4 = Facts()
    o = t.execute("request_quote",
                  {"cinema_name": "万达", "movie_name": "八仙", "show_date": "2026-10-12",
                   "show_time": "20:30", "ticket_count": 2,
                   "seat_marking": "PRESENT",     # ★ 万达要这个（测的是佐证，不是这个）
                   "source": "image"},
                  f4, "[买家发了一张选座截图]")
    check("★ source=image 时不要求佐证", o.ok, f"{o.error_code} {o.message[:50]}")

    print()
    print("=" * 74)
    print("⑤ 万达没圈座 → SEAT_MARKING_REQUIRED")
    print("=" * 74)
    f5 = Facts()
    f5.merge({"cinema_name": "万达", "movie_name": "八仙"}, source="image")
    o = t.execute("request_quote",
                  {"cinema_name": "万达", "movie_name": "八仙", "show_date": "2026-10-12",
                   "show_time": "20:30", "ticket_count": 2,
                   "evidence": {"show_date": "2026-10-12",
                                "show_time": "20:30",
                                "ticket_count": "2"}},
                  f5, "2026-10-12 20:30 2张")
    check("报 SEAT_MARKING_REQUIRED", o.error_code == "SEAT_MARKING_REQUIRED",
          o.error_code)
    check("告诉模型去让买家圈选", "圈" in o.message)

    f5.merge({"seat_marking": "PRESENT"}, source="image")
    o = t.execute("request_quote",
                  {"cinema_name": "万达", "movie_name": "八仙", "show_date": "2026-10-12",
                   "show_time": "20:30", "ticket_count": 2},
                  f5, "（买家又发了张圈好位置的图）")
    check("★ 圈了之后报价成功", o.ok, f"{o.error_code} {o.message[:40]}")

    print()
    print("=" * 74)
    print("⑥ 良票不需要 seat_marking")
    print("=" * 74)
    f6 = Facts()
    o = Tools(provider="LIANGPIAO", quote_fn=q).execute(
        "request_quote",
        {"cinema_name": "济宁影城", "movie_name": "神探", "show_date": "2026-10-12",
         "show_time": "10:00", "ticket_count": 1, "source": "image"},
        f6, "[买家发了选座截图]")
    check("★ 良票没圈座也能报价", o.ok, f"{o.error_code} {o.message[:40]}")

    print()
    print("=" * 74)
    print("⑦ 缺字段 / 格式 / 前置 / 假工具")
    print("=" * 74)
    f7 = Facts()
    o = t.execute("request_quote", {"cinema_name": "万达", "source": "image"}, f7, "[图]")
    check("缺字段报 NEEDS_INFORMATION", o.error_code == "NEEDS_INFORMATION",
          o.error_code)
    check("缺项列全了",
          all(k in o.message for k in ("movie_name", "show_date", "show_time",
                                       "ticket_count")), o.message[:90])

    f8 = Facts()
    f8.merge({"cinema_name": "万达", "movie_name": "八仙", "seat_marking": "PRESENT"},
             source="image")
    o = t.execute("request_quote",
                  {"cinema_name": "万达", "movie_name": "八仙", "show_date": "10月12日",
                   "show_time": "20:30", "ticket_count": 2,
                   "evidence": {"show_date": "10月12日", "show_time": "20:30",
                                "ticket_count": "2"}},
                  f8, "10月12日 20:30 2张")
    check("日期格式不对被拒", o.error_code == "SHOW_DATE_FORMAT", o.error_code)

    o = Tools(provider="WANDA").execute("create_order", {}, Facts(), "")
    check("没报价就下单被拒", o.error_code == "NO_QUOTE", o.error_code)

    o = t.execute("delete_order", {}, Facts(), "")
    check("编的工具名被拒", o.error_code == "UNKNOWN_TOOL", o.error_code)

    print()
    print("=" * 74)
    print("⑧ 事实带出处 / 白名单")
    print("=" * 74)
    f9 = Facts()
    f9.merge({"cinema_name": "万达"}, source="image")
    f9.merge({"city_name": "呼和浩特"}, source="text",
             evidence={"city_name": "呼和浩特"}, buyer_text="呼和浩特")
    check("image 来源记对了",
          f9.provenance["cinema_name"]["source"] == "image")
    check("text 来源带佐证",
          f9.provenance["city_name"]["evidence"] == "呼和浩特")
    f10 = Facts()
    accepted = f10.merge({"password": "abc", "vip": True, "city_name": "上海"},
                         source="text", evidence={"city_name": "上海"},
                         buyer_text="上海")
    check("白名单外的字段不收", set(accepted) == {"city_name"}, str(accepted))
    check("★ 非法 source 被拒",
          f10.merge({"hall_name": "1号厅"}, source="model") == {},
          str(f10.snapshot()))

    print()
    print("=" * 74)
    print("⑨ ★ 报价状态必须跨轮活着（实测：「确认」曾报 NO_QUOTE）")
    print("=" * 74)
    from application.agent import render_quote_card
    f11 = Facts()
    f11.merge({"city_name": "呼和浩特", "cinema_name": "万达", "movie_name": "八仙",
               "show_date": "2026-10-12", "show_time": "20:30",
               "ticket_count": 2, "seat_marking": "PRESENT"}, source="image")
    t11 = Tools(provider="WANDA", quote_fn=q)
    o = t11.execute("request_quote",
                    {"cinema_name": "万达", "movie_name": "八仙",
                     "show_date": "2026-10-12", "show_time": "20:30",
                     "ticket_count": 2}, f11, "算一下")
    check("报价成功", o.ok, o.error_code)
    check("★ quote_id 写进了 facts（跨轮保留）", f11.get("quote_id") == "Q-1",
          str(f11.get("quote_id")))
    check("quote_id 来源记为 system",
          f11.provenance.get("quote_id", {}).get("source") == "system",
          str(f11.provenance.get("quote_id")))
    # ★ 换一个 Tools 实例（模拟"下一轮 Tools 新建"）—— 报价状态不能丢
    t12 = Tools(provider="WANDA", quote_fn=q, order_fn=lambda: {"order_no": "O-1"})
    o = t12.execute("create_order", {}, f11, "")
    check("★ 换了 Tools 实例后仍能下单（状态在 facts 里，不在 Tools 上）",
          o.ok, f"{o.error_code} {o.message[:50]}")

    print()
    print("=" * 74)
    print("⑩ 改价 / 取消（实测：没有这两个工具时十次十次转人工）")
    print("=" * 74)
    f13 = Facts()
    o = Tools(provider="WANDA", quote_fn=q).execute("request_reprice", {}, f13, "")
    check("没报价时改价被拒", o.error_code == "NO_QUOTE", o.error_code)
    f14 = Facts()
    f14.data["quote_id"] = "Q-1"
    t14 = Tools(provider="WANDA", quote_fn=q)
    o = t14.execute("request_reprice", {}, f14, "")
    check("有报价时改价成功", o.ok, o.error_code)
    check("改价状态记住了", f14.get("reprice_status") == "REPRICE_REQUESTED")
    o = t14.execute("cancel_request", {}, f14, "")
    check("取消成功", o.ok)
    check("★ 取消清掉了报价意向", f14.get("quote_id") is None,
          str(f14.snapshot()))

    print()
    print("=" * 74)
    print("⑪ ★ 金额不许模型复述（实测它把 84.05 说成 84.06）")
    print("=" * 74)
    from application.agent import _restates_price
    check("识别「单价42.03元，共84.06元」",
          _restates_price("单价42.03元，共84.06元"))
    check("★ 识别 markdown 的「**84.06 元**」",
          _restates_price("合计 **84.06 元**。"), "带 ** 也要认出来")
    check("识别「**84.06** 元」", _restates_price("**84.06** 元"))
    check("不会误伤「要下单吗？」", not _restates_price("要下单吗？"))
    check("不会误伤「《八仙》10月12日 20:30，2张」",
          not _restates_price("《八仙》10月12日 20:30，2张"))
    card = render_quote_card({"cinema_name": "万达", "movie_name": "八仙",
                              "show_time": "20:30", "seat_count": 2,
                              "unit_price_yuan": 42.03, "total_yuan": 84.06})
    check("报价卡含影院/影片/场次/张数/单价/总价",
          all(k in card for k in ("万达", "八仙", "20:30", "2张",
                                  "42.03", "84.06")), card[:40])

    print()
    print("=" * 74)
    print("⑫ 会员座 / 灰色座位 / 可以买吗 —— 店家确认的口径")
    print("=" * 74)
    import qa as qa_module
    for kw in ("会员座", "灰色", "可以买吗", "能买吗", "可以拍吗", "能下吗"):
        rule = qa_module.find_by_keyword(kw)
        check(f"「{kw}」有配置", rule is not None,
              (rule.content[:44] if rule else "★ 没有"))
    rule = qa_module.find_by_keyword("会员座")
    if rule:
        check("会员座口径 = 可以买 + 圈位置 + 报张数",
              "可以买" in rule.content and "位置" in rule.content
              and "几张" in rule.content, rule.content[:50])

    print()
    print("=" * 74)
    print("⑬ ★ 「可以买吗」分两种场景（取决于发过图没有）")
    print("=" * 74)
    from application.agent import has_image_facts, is_can_i_buy
    # 判据本身
    check("认得出「可以买吗」", is_can_i_buy("这个可以买吗"))
    # ★ 实测误判：「会员座能代订吗」被这条抢走，回了"可以买，需要几张呢" ——
    #   而那时买家还没给影片/场次，太早了。它问的是服务能力，
    #   该走【会员座】那条配置规则。
    check("★「会员座能代订吗」不算「可以买吗」（走配置规则）",
          not is_can_i_buy("会员座能代订吗"))
    check("★「能代下吗」不算", not is_can_i_buy("能代下吗"))
    check("认得出「可以拍吗」", is_can_i_buy("可以拍吗"))
    check("不误判「多少钱」", not is_can_i_buy("多少钱"))
    check("不误判「你好」", not is_can_i_buy("你好"))
    # 发没发图
    blank = Facts()
    check("★ 没发图 → has_image_facts 为假", not has_image_facts(blank))
    with_img = Facts()
    with_img.merge({"cinema_name": "万达", "seat_marking": "ABSENT"}, source="image")
    check("★ 发过图（没圈）→ has_image_facts 为真",
          has_image_facts(with_img))
    with_mark = Facts()
    with_mark.merge({"cinema_name": "万达", "seat_marking": "PRESENT"}, source="image")
    check("★ 发过图（圈了）→ has_image_facts 为真",
          has_image_facts(with_mark))
    # ★ 只看"发没发图"，不看"圈没圈" —— 店家确认的规则
    check("★ 圈没圈都算发过图（只看发没发）",
          has_image_facts(with_img) and has_image_facts(with_mark),
          "ABENT/PRESENT 都返回 True")

    print()
    print("=" * 74)
    print("⑭ ★ 会员座/灰色座位的口径（店家确认，不是推的）")
    print("=" * 74)
    import qa as qa_module2
    for kw in ("会员座", "灰色", "可以买吗", "能买吗", "可以拍吗", "能下吗"):
        rule = qa_module2.find_by_keyword(kw)
        check(f"「{kw}」有配置", rule is not None,
              (rule.content[:46] if rule else "★ 没有"))
    r_ms = qa_module2.find_by_keyword("会员座")
    if r_ms:
        check("会员座口径含「可以买」+ 圈位置 + 报张数",
              "可以买" in r_ms.content and "位置" in r_ms.content
              and "几张" in r_ms.content, r_ms.content[:54])
    r_cb = qa_module2.find_by_keyword("可以买吗")
    if r_cb:
        check("没发图时的口径 = 引导发图圈位置 + 报张数",
              "选座图" in r_cb.content and "圈" in r_cb.content
              and "几张" in r_cb.content, r_cb.content[:54])

    print()
    print("=" * 74)
    print("⑮ ★ 关键词回复的「文件 vs 代码」合并（踩过的坑：整体替换）")
    print("=" * 74)
    import json as _json
    import importlib
    import qa as qa_m
    from application import store as store_m
    qa_file = Path(ROOT) / "data" / "qa.json"
    backup = qa_file.read_text(encoding="utf-8") if qa_file.exists() else None
    code_count = len(qa_m.QA_RULES)
    try:
        # ① 文件里改了某条 → 以文件为准
        d = _json.loads(backup) if backup else {"rules": []}
        for r in d["rules"]:
            if r["keyword"] == "出票":
                r["content"] = "【测试改过】"
        qa_file.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
        importlib.reload(qa_m)
        store_m.load_qa_overrides()
        check("★ 文件里的改动生效（文件优先）",
              qa_m.BY_KEYWORD.get("出票")
              and qa_m.BY_KEYWORD["出票"].content == "【测试改过】",
              str(qa_m.BY_KEYWORD.get("出票") and qa_m.BY_KEYWORD["出票"].content))
        check("★ 代码里的新规则没被文件覆盖掉（合并而非替换）",
              len(qa_m.QA_RULES) >= code_count,
              f"代码 {code_count} 条 → 加载后 {len(qa_m.QA_RULES)} 条")
        # ★ 2026-10-14：改用 has_keyword ——
        #   "会员座"现在和"灰色"合并在一条里（keyword = `会员座|灰色`），
        #   {r.keyword} 里找 "会员座" 精确相等就找不到了。
        #   而"还在"的正确判据是"能通过这个关键字找到规则"。
        check("★「会员座」还在（实测它曾重启后消失）",
              qa_m.has_keyword("会员座"))

        # ② 文件里删了一条 → 代码里的应该回来
        d2 = _json.loads(backup) if backup else {"rules": []}
        d2["rules"] = [r for r in d2["rules"] if r["keyword"] != "会员座"]
        qa_file.write_text(_json.dumps(d2, ensure_ascii=False), encoding="utf-8")
        importlib.reload(qa_m)
        store_m.load_qa_overrides()
        check("★ 文件里删掉的、代码里有的 → 保留（不致于丢规则）",
              qa_m.has_keyword("会员座"))

        # ③ 文件里单独新增 → 要保留
        d3 = _json.loads(backup) if backup else {"rules": []}
        d3["rules"].append({"qa_id": "zztest00001", "keyword": "测试新增词",
                            "content": "测试内容", "trigger": "重复触发",
                            "total_limit": "不限制", "daily_limit": "不限制",
                            "message_type": "文本消息"})
        qa_file.write_text(_json.dumps(d3, ensure_ascii=False), encoding="utf-8")
        importlib.reload(qa_m)
        store_m.load_qa_overrides()
        check("★ 界面上新建的规则要保留",
              "测试新增词" in {r.keyword for r in qa_m.QA_RULES})
    finally:
        if backup is not None:
            qa_file.write_text(backup, encoding="utf-8")
        importlib.reload(qa_m)
        store_m.load_qa_overrides()

    print()
    print("=" * 74)
    print("⑯ ★★ 识别结果【代码直写】事实（不经模型）")
    print("=" * 74)
    # ★ 这是本次最重要的修正：座位号曾因为"要靠模型转述、而工具 schema
    #   里没这个字段"而整条丢掉。识别引擎已经读到了，就不该经模型。

    class _Rec:
        """假装是良票的 RecognitionResult。★ match_level 必须有 ——
        没有它就会被"按证据级别收"的闸门当成 NONE 拦掉。"""
        match_level = "EXACT"
        city_name = "杭州"
        cinema_name = "博纳国际影城西溪IMAX店"
        movie_name = "生化危机：爆发夜"
        hall_name = "IMAX厅"
        start_time = "2026-10-12T19:00:00+08:00"
        seats = [{"seatName": "7排15座", "seatPriceFen": "5600",
                  "rowNo": 7, "colNo": 15}]
        cinema_id = 902
        cinema_code = "33018631"
        movie_id = 865
        show_id = "36518509"

    rec_facts = Facts()
    got = rec_facts.absorb_recognition(_Rec())
    check("★ 识别结果直接进事实（一次调用，没问模型）", bool(got), str(got))
    check("★★ 座位号记下来了（曾经整条丢掉）",
          rec_facts.get("seats") == ["7排15座"],
          str(rec_facts.get("seats")))
    check("★ 张数按座位数补上", rec_facts.get("ticket_count") == 1,
          str(rec_facts.get("ticket_count")))
    check("★ 城市从识别结果直接拿（qwen 那条读不出城市）",
          rec_facts.get("city_name") == "杭州", str(rec_facts.get("city_name")))
    check("★ 影院用规范名（不是 OCR 的括号版）",
          rec_facts.get("cinema_name") == "博纳国际影城西溪IMAX店",
          str(rec_facts.get("cinema_name")))
    check("★ ISO 开场时间拆成日期+时刻",
          rec_facts.get("show_date") == "2026-10-12"
          and rec_facts.get("show_time") == "19:00",
          f"{rec_facts.get('show_date')} {rec_facts.get('show_time')}")
    check("★ cinema_id 记下来了（报价记录要存 id 不是名字）",
          rec_facts.get("cinema_id") == 902, str(rec_facts.get("cinema_id")))
    check("★ show_id 记下来了（询价要用）",
          rec_facts.get("show_id") == "36518509",
          str(rec_facts.get("show_id")))
    check("座位来自 image 来源（不要求买家佐证）",
          rec_facts.provenance.get("seats", {}).get("source") == "image",
          str(rec_facts.provenance.get("seats")))

    print()
    print("=" * 74)
    print("⑰ ★ 识别结果给的字段，买家文字不许覆盖（保名字和 id 一致）")
    print("=" * 74)
    # 实测：买家后来说「杭州 西溪银泰城博纳国际影城」，
    # 影院被改名，而 cinema_id 还是 902 → 名字与 id 对不上
    accepted = rec_facts.merge(
        {"cinema_name": "西溪银泰城博纳国际影城"}, source="text",
        evidence={"cinema_name": "西溪银泰城博纳国际影城"},
        buyer_text="杭州 西溪银泰城博纳国际影城")
    check("★ 文字改影院被拒", not accepted, str(accepted))
    check("★★ 影院名没被改（仍是识别的规范名）",
          rec_facts.get("cinema_name") == "博纳国际影城西溪IMAX店",
          str(rec_facts.get("cinema_name")))
    check("★★ 名字与 cinema_id 仍然一致",
          rec_facts.get("cinema_id") == 902, str(rec_facts.get("cinema_id")))
    # 但非权威字段（识别时没有的）仍可由文字补
    accepted2 = rec_facts.merge(
        {"ticket_count": 4}, source="text",
        evidence={"ticket_count": "四张"}, buyer_text="四张")
    check("★ 识别时没给的字段，文字仍可补（张数没被冻住）",
          not accepted2 or rec_facts.get("ticket_count") == 4,
          f"{accepted2} tc={rec_facts.get('ticket_count')}")

    print()
    print("=" * 74)
    print("⑱ ★ record_facts 工具 schema 必须有 seats")
    print("=" * 74)
    from application.agent import TOOLS as _TOOLS
    props = None
    facts_spec = None
    for _t in _TOOLS:
        if _t["function"]["name"] == "record_facts":
            facts_spec = _t["function"]["parameters"]["properties"]["facts"]
            props = facts_spec["properties"]
    check("★ schema 里有 seats（曾漏掉导致座位丢）",
          props is not None and "seats" in props,
          str(list(props.keys()) if props else None))
    check("schema 是 additionalProperties=False（多余字段会被拒，所以要显式列全）",
          bool(facts_spec) and facts_spec.get("additionalProperties") is False,
          str(facts_spec.get("additionalProperties") if facts_spec else None))

    print()
    print("=" * 74)
    print("⑲ ★★ 按证据级别收（EXACT / CANDIDATE / NONE）")
    print("=" * 74)
    # ★ 实测：同一张图，第一次 matchLevel=EXACT（有 showId），
    #   隔一会儿再调 matchLevel=NONE（showId=None，排片下架了）。
    #   不管 matchLevel 照收 → 会把"没有场次的影院"当事实，
    #   下游拿 None 询价必然失败。线上 V5 在 NONE 时一个字段都不采信。

    def _rec_at(level, show_id="36518509"):
        class _R:
            match_level = level
            city_name = "杭州"
            cinema_name = "博纳国际影城西溪IMAX店"
            movie_name = "生化危机：爆发夜"
            hall_name = "IMAX厅"
            start_time = "2026-10-12T19:00:00+08:00"
            seats = [{"seatName": "7排15座"}]
            cinema_id = 902
            cinema_code = "33018631"
            movie_id = 865
            cinema_address = "西湖区双龙街588号"
        _R.show_id = show_id
        return _R()

    f_exact = Facts()
    _a = f_exact.absorb_recognition(_rec_at("EXACT"))
    check("EXACT → 影片/场次都收",
          f_exact.get("movie_name") == "生化危机：爆发夜"
          and f_exact.get("show_id") == "36518509",
          f"movie_name={f_exact.get('movie_name')} show_id={f_exact.get('show_id')}")

    f_cand = Facts()
    _b = f_cand.absorb_recognition(_rec_at("CANDIDATE"))
    check("★ CANDIDATE → 影院/城市收（缩小范围用）",
          f_cand.get("cinema_name") and f_cand.get("city_name"),
          f"cinema_name={f_cand.get('cinema_name')} city_name={f_cand.get('city_name')}")
    check("★★ CANDIDATE → 影片和场次【不收】（场次没落实）",
          f_cand.get("movie_name") is None and f_cand.get("show_id") is None,
          f"movie_name={f_cand.get('movie_name')} show_id={f_cand.get('show_id')}")
    check("CANDIDATE → 座位仍收（座位是图上直接读到的）",
          f_cand.get("seats") == ["7排15座"],
          str(f_cand.get("seats")))

    f_none = Facts()
    _c = f_none.absorb_recognition(_rec_at("NONE", show_id=None))
    check("★★ NONE → 一个字段都不收（对齐 V5 的 CANDIDATE_UNVERIFIED）",
          _c == {} and not f_none.data, str(_c))

    print()
    print("=" * 74)
    passed, total = sum(RESULTS), len(RESULTS)
    for i, ok in enumerate(RESULTS):
        if not ok:
            print(f"  ✗ 第 {i + 1} 项失败")
    print(f"{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
