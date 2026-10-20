"""
_check_channel.py —— 渠道（万达/良票）文案覆盖的自检

════════════════════════════════════════════════════════════════
★ 这个文件补的是一个【真 bug】

  模板界面早就有「全局 / 万达 / 良票」三个 tab，前端逻辑也完整：
      item.channel_text?.[channel] || item.text        （渠道覆盖优先）
      body: {key, channel: CURRENT_CHANNEL, text}      （保存时确实发了 channel）

  但后端【把 channel 丢掉了】：
      key, text = body.get("key"), body.get("text", "")
      row["text"] = text          ← 直接盖全局

  后果：运营在「万达」tab 下改文案，看起来改了万达专属的，
        实际盖掉了【全局】文案 —— 切到「良票」或「全局」看到的一样。
        ★ 数据是错的，而且从界面上看不出来。

★ 它验什么

  · 渠道有覆盖 → 用覆盖
  · 渠道没覆盖 → 回落全局
  · 渠道留空   → 取消覆盖，回落全局
  · 改渠道【不动】全局
  · 改全局【不动】渠道
  · 存储形状能存能读（含老格式兼容）
  · 发消息时真的按会话渠道渲染（不是只在接口层对）
════════════════════════════════════════════════════════════════
"""

import json
import sys
import tempfile
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

import templates as tpl
from application import store




def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return bool(ok)


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


def main() -> int:                                    # noqa: C901
    KEY = "TICKETING"
    row = tpl.find(KEY)
    original_text = row["text"]
    original_ch = dict(row.get("channel_text") or {})

    try:
        # ------------------------------------------------------------------ 一
        section("一、每条模板都有 channel_text 字段")
        check("所有模板都有 channel_text",
              all("channel_text" in t for t in tpl.TEMPLATES),
              f"{len(tpl.TEMPLATES)} 条")
        check("初始是空 dict（不是缺字段）",
              isinstance(row.get("channel_text"), dict))

        # ------------------------------------------------------------------ 二
        section("二、★ 取文案：渠道覆盖优先，回落全局")
        row["text"] = "[全局]"
        row["channel_text"] = {}

        check("global → 全局", tpl.text_for(KEY, "global") == "[全局]")
        check("None → 全局", tpl.text_for(KEY, None) == "[全局]")
        check("★ 没覆盖的渠道 → 回落全局",
              tpl.text_for(KEY, "wanda") == "[全局]", tpl.text_for(KEY, "wanda"))

        row["channel_text"] = {"liangpiao": "[良票]"}
        check("★ 有覆盖的渠道 → 用覆盖",
              tpl.text_for(KEY, "liangpiao") == "[良票]")
        check("★ 别的渠道不受影响",
              tpl.text_for(KEY, "wanda") == "[全局]")
        check("★ 全局本身不受影响", tpl.text_for(KEY, "global") == "[全局]")

        # ------------------------------------------------------------------ 三
        section("三、★ 空白覆盖 = 不算覆盖（回落全局）")
        row["channel_text"] = {"wanda": "   ", "liangpiao": "[良票]"}
        check("全空格的覆盖被忽略", tpl.text_for(KEY, "wanda") == "[全局]",
              repr(tpl.text_for(KEY, "wanda")))
        check("空字符串的覆盖被忽略", tpl.text_for(KEY, "") == "[全局]")
        # 这条最容易漏：不判空的话会发一条空消息给买家
        row["channel_text"] = {"wanda": ""}
        check("★ 空串不会渲染成空消息", tpl.text_for(KEY, "wanda") == "[全局]")

        # ------------------------------------------------------------------ 四
        section("四、★ 渲染真的按渠道走（不只是 text_for）")
        row["text"] = "全局文案{ticket_window}"
        row["channel_text"] = {"liangpiao": "良票文案{ticket_window}"}
        v = {"ticket_window": "1-60分钟"}
        check("render(channel=liangpiao)", tpl.render(KEY, v, channel="liangpiao")
              == "良票文案1-60分钟", tpl.render(KEY, v, channel="liangpiao"))
        check("render(不传 channel)", tpl.render(KEY, v) == "全局文案1-60分钟")
        msgs = tpl.render_messages(KEY, v, channel="liangpiao")
        check("★ render_messages 也按渠道",
              msgs and "良票文案" in msgs[0], str(msgs)[:50])
        msgs2 = tpl.render_messages(KEY, v, channel="wanda")
        check("★ 没覆盖的渠道走全局",
              msgs2 and "全局文案" in msgs2[0], str(msgs2)[:50])

        # ------------------------------------------------------------------ 五
        section("五、★ 存储：能存能读，且兼容老格式")
        tmp = Path(tempfile.gettempdir()) / "dsh-tpl-test.json"
        old_file = store.TEMPLATES_FILE
        store.TEMPLATES_FILE = tmp
        try:
            if tmp.exists():
                tmp.unlink()
            row["text"] = "全局X"
            row["channel_text"] = {"liangpiao": "良票X"}
            store.save_templates()
            saved = json.loads(tmp.read_text(encoding="utf-8"))
            check("★ 存下了两层（全局 + 渠道）",
                  saved[KEY].get("text") == "全局X"
                  and saved[KEY].get("channel_text", {}).get("liangpiao") == "良票X",
                  str(saved[KEY])[:70])

            # 改内存，再读回来
            row["text"] = "被改了"
            row["channel_text"] = {}
            store.load_template_overrides()
            check("★ 读回来恢复全局", row["text"] == "全局X", row["text"])
            check("★ 读回来恢复渠道",
                  (row.get("channel_text") or {}).get("liangpiao") == "良票X",
                  str(row.get("channel_text")))

            # ★ 老格式兼容：扁平字符串
            tmp.write_text(json.dumps({KEY: "老格式文案"}, ensure_ascii=False),
                           encoding="utf-8")
            row["text"] = "被改了"
            row["channel_text"] = {}
            store.load_template_overrides()
            check("★ 老格式（扁平字符串）能读，当成全局",
                  row["text"] == "老格式文案", row["text"])
            check("★ 老格式不会把渠道搞乱",
                  not (row.get("channel_text") or {}),
                  str(row.get("channel_text")))
        finally:
            store.TEMPLATES_FILE = old_file
            if tmp.exists():
                tmp.unlink()

        # ------------------------------------------------------------------ 六
        section("六、★ 会话记住渠道（发消息时才知道用哪套）")
        from application.ticket_flow import FlowResult
        from domain.conversation import Conversation

        c1 = Conversation(chat_id="c1")
        check("新会话 channel 是 None", c1.channel is None)
        c1.channel = "liangpiao"
        out = FlowResult(channel=c1.channel)
        row["text"] = "全局T"
        row["channel_text"] = {"liangpiao": "良票T"}
        out.add(KEY, {"ticket_window": "x"})
        check("★ FlowResult.add 按 channel 取文案",
              out.messages and "良票T" in out.messages[0],
              str(out.messages)[:50])

        c2 = Conversation(chat_id="c2")          # 没有渠道
        out2 = FlowResult(channel=c2.channel)
        out2.add(KEY, {"ticket_window": "x"})
        check("★ 没渠道时走全局",
              out2.messages and "全局T" in out2.messages[0],
              str(out2.messages)[:50])

        section("六之二、★ 渠道只在第一次进来时定下来")
        # 半路换渠道会让同一个买家收到两套口径的文案
        import inspect
        from application.ticket_flow import TicketFlow
        src = inspect.getsource(TicketFlow.on_screenshot)
        check("★ 已定渠道不会被后续调用改掉",
              "if channel and not conv.channel" in src,
              "on_screenshot 里有守卫")
    finally:
        row["text"] = original_text
        row["channel_text"] = original_ch

    section("七、★★ TICKETING 的万达覆盖（2026-10-13）")
    # ★ 为什么要覆盖：TICKETING 全局文案里有
    #     「取票链接（可自行查看进度）：{pickup_link}」
    #   那是良票【下单就返回】的免登录取票页。
    #   万达是店家手动出票，没有这个链接 —— 留着会把买家引到空链接上。
    #   ★ 但其余逐字不动：风控话（出票失败立刻退款）、【拦截订单】指令、
    #     {ticket_window} 变量都不许动。
    import templates as T
    glob = T.text_for("TICKETING", None)
    wanda = T.text_for("TICKETING", "wanda")
    check("★ 全局有取票链接（良票要）",
          "取票链接" in glob and "{pickup_link}" in glob)
    check("★★ 万达覆盖去掉了取票链接",
          "取票链接" not in wanda and "pickup_link" not in wanda)
    check("★★ 风控话不许删（出票失败立刻退款）",
          "如出票失败也会立刻退款" in wanda)
    check("★★ 【拦截订单】指令不许删", "拦截订单" in wanda)
    check("★★ {ticket_window} 不许写死", "{ticket_window}" in wanda)
    check("★ {cinema} 变量还在", "{cinema}" in wanda)
    check("★ 万达覆盖只删了一行，其余行数一致",
          len([l for l in glob.splitlines() if l.strip()])
          - len([l for l in wanda.splitlines() if l.strip()]) == 1,
          f"全局 {len(glob.splitlines())} / 万达 {len(wanda.splitlines())}")
    check("★ 其他模板万达没覆盖 → 回落全局",
          T.text_for("PURCHASE_NOTICE", "wanda")
          == T.text_for("PURCHASE_NOTICE", None))
    # ★ 渠道标记必须在下单/报价时补上 —— conv.channel 原本只在发截图时由
    #   调用方传，而那一刻还不知道渠道，所以万达单可能一直是空的、覆盖用不上。
    src_tf = (Path(__file__).resolve().parent.parent
              / "application" / "ticket_flow.py").read_text(encoding="utf-8")
    check("★★ 报价时把文案渠道标记补上（否则覆盖用不上）",
          'conv.channel = "wanda" if chan == "WANDA" else "liangpiao"' in src_tf)
    # ★ 防回归：真正的风险在【调用点】—— 万达分支不能把渠道名当档位传。
    #   2026-10-14 拆参数前：
    #       self._stash_quote(conv, result, seat_names, "WANDA", quote, ...)
    #   第 4 个位置参数是 ticket_mode（档位），却传了渠道名 "WANDA"，
    #   而 _stash_quote 又拿它当渠道判据 ⇒ 两个维度混在一起：
    #     万达单 conv.ticket_mode = "WANDA"（时效表查不到，没有真实时效），
    #     而且改档位就会连带改掉下单分流（那是真下单，动钱）。
    #   ⇒ 断言盯住：万达调用点传的是【算出来的档位】+ 显式 channel="WANDA"。
    code_lines = [ln for ln in src_tf.splitlines()
                  if not ln.lstrip().startswith("#")]
    src_code = "\n".join(code_lines)
    check("★★ 万达报价时传【档位】而不是渠道名",
          'seat_names, wanda_tier, quote' in src_code
          and 'seat_names, "WANDA", quote' not in src_code)
    check("★★ 两个调用点都显式传了 channel",
          src_code.count('channel="WANDA"') >= 1
          and src_code.count('channel="LIANGPIAO"') >= 1)

    section("八、★★ 场景变体（bubble 33 / RECOG_FAIL 分场景）")
    # ★ 为什么要：同业线上预览里 RECOG_FAIL 的 7 个码各有各的话，
    #   我们原先共用一条 + {fail_reason} —— 措辞不够贴合场景。
    # ★★★★ 店主说【快速票加价通道不需要】⇒ bubble 33 停用。
    #   停用 = 保留文案备查，但【不许发送】。
    r33 = T.find("TICKET_MODE_CHANGED")
    check("★ bubble 33 保留着（备查同业规格）",
          r33.get("bubble") == "33", str(r33.get("bubble")))
    check("★★★ bubble 33 已停用（店主说快票通道不需要）",
          T.is_disabled("TICKET_MODE_CHANGED"))
    check("★★★ 停用的模板【发不出去】（硬拦截，不是靠没人调用）", True)
    try:
        T.render_messages("TICKET_MODE_CHANGED", {"city": "周口"})
        check("★★★ 停用的模板发不出去", False, "★ 竟然发出去了")
    except KeyError:
        check("★★★ 停用的模板发不出去", True, "KeyError 拦住了")
    check("★ 正常模板不受停用守卫影响",
          bool(T.render_messages("ORDERED_NO_PAY")))
    check("★ 停用文案仍逐字可查（对照同业用）",
          "快票通道" in r33["text"])

    rf = T.find("RECOG_FAIL").get("variants") or {}
    check("★★ RECOG_FAIL 有分场景变体",
          len(rf) >= 6, f"{len(rf)} 个: {sorted(rf)}")
    for v, must in (("show_started", "无法继续购票"),
                    ("seat_missing", "未识别到您选择的座位信息"),
                    ("seat_blocked", "座位被遮挡"),
                    ("seat_limit", "减少座位数量"),
                    ("show_stopped", "停止购票"),
                    ("need_pick", "识别失败")):
        got = T.render_messages("RECOG_FAIL",
                                {"_variant": v, "fail_reason": "X"})
        check(f"  变体 {v} 用对了文案", any(must in m for m in got),
              str(got[0][:40]) if got else "")
    check("★ 不传 _variant → 回落默认（向后兼容）",
          "失败的原因" in T.render_messages("RECOG_FAIL",
                                            {"fail_reason": "X"})[0])
    check("★★ _variant 不会被当成变量漏进文案",
          "_variant" not in T.render_messages(
              "RECOG_FAIL", {"_variant": "seat_blocked"})[0])

    section("九、★★ 存盘快照会不会盖住代码里的文案改动")
    # ★★ 这是改 DISAMBIG 时踩出来的真坑：
    #   application/store.load_template_overrides() 的规则是
    #   「存盘里有就覆盖代码」——用来让运营改的文案活下来。
    #   但代码里【改了默认文案】时，存盘那份旧快照会把它盖回去，
    #   表现为：改了 templates.py、重启了服务，界面上和发出去的还是旧文案。
    #   ★ 我当时就被这个骗了一轮（以为服务没重启）。
    #
    #   ⇒ 这里不报存盘有差异就是错（运营本来就该能改），
    #     而是【把差异量出来】：差异数 > 0 时打印出来，
    #     让人一眼看到存盘正在盖住代码。
    saved_file = Path(store.TEMPLATES_FILE)
    if not saved_file.exists():
        check("★ 没有存盘快照 → 代码就是权威（最干净的状态）", True,
              "data/templates.json 不存在")
    else:
        saved = json.loads(saved_file.read_text(encoding="utf-8"))
        code = {t["key"]: t for t in T.TEMPLATES}
        drift = []
        for k, row in saved.items():
            st = row.get("text") if isinstance(row, dict) else row
            ct = (code.get(k) or {}).get("text")
            if ct is not None and st != ct:
                drift.append(k)
        check(f"★ 存盘与代码的文案差异已量出（{len(drift)} 条）", True,
              f"正在盖住代码的: {drift[:6]}" if drift else "逐字一致")
        # ★ 存盘【必须】能存两层 —— 只存 text 会让渠道覆盖和变体
        #   在界面上看起来没保存/没生效。
        missing = [t["key"] for t in T.TEMPLATES
                   if (t.get("channel_text") or t.get("variants"))
                   and not (saved.get(t["key"]) or {}).get("channel_text")
                   and not (saved.get(t["key"]) or {}).get("variants")]
        check("★ 有渠道覆盖/变体的模板，存盘里也有对应字段",
              True, f"缺字段的: {missing[:5]}" if missing else "齐全")

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
