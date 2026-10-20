"""
chatex.py —— 标准 agent 的本地对话测试（工具选择式）

★ 跟 chat.py 的区别

  chat.py  走的是现有主流程（意图分类式：状态机 + 关键词 + 模型兜底）
  chatex.py 走的是 application/agent.py（工具选择式：模型选工具 + 校验层）

  ★ 两个可以对照着跑同一段对话，看差别。

★ 它不需要真接口
  报价/下单是假的（写成函数注入），所以：
    · 不花钱、不下单、不连云票
    · 但工具定义、校验规则、循环、事实累积都是真的

★ 用法
    python chatex.py                     # 假报价，只用模型抽事实
    python chatex.py --provider LIANGPIAO # 换成良票规则（不要 seat_marking）
    python chatex.py --show-facts         # 每轮后打印已确认事实

  对话里：
    /img <URL>   当作买家发了这张截图（会走识别：真调良票要 --live）
    /facts       看已确认事实
    /calls       看这一轮调了哪些工具、报了什么错
    /quit
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from application import store                                  # noqa: E402
from application.agent import (                                # noqa: E402
    TOOL_NAMES, Facts, Tools, build_client, run_turn,
)


# ==================================================================== 假接口
def fake_quote(facts: dict) -> dict:
    """
    假日报价。★ 用固定的成本算，不调任何接口。

    ★ 钱的算法（这里踩过坑）：
      第一版是 unit = 4203/100 = 42.03，再 total = 42.03*2 = 84.06。
      ⇒ 【先舍入单价再乘】，多出 1 分。
      正确做法：全程用【分】算整数，只在最后展示时除 100。
        总价 = 4203 * 2 = 8406 分
        单价 = 8406 // 2，余数单独处理
    """
    count = int(facts.get("ticket_count") or 1)
    unit_fen = 4203                      # 每张 42.03 元，用分存
    total_fen = unit_fen * count         # ★ 先算总价（分）
    # ★ 单价从总价反推，保证 单价×张数 == 总价（不给买家留算术矛盾）
    display_unit_fen = total_fen // count
    return {
        "quote_id": "Q-FAKE",
        "cinema_name": facts.get("cinema_name"),
        "movie_name": facts.get("movie_name"),
        "show_time": facts.get("show_time"),
        "seat_count": count,
        "unit_price_yuan": round(display_unit_fen / 100, 2),
        "total_yuan": round(total_fen / 100, 2),
        "provider": "WANDA(假)",
    }


def fake_order() -> dict:
    """假下单。★ 不用 facts —— 真下单才需要，假的不需要。"""
    return {"order_no": "LP-FAKE-001", "status": "SUBMITTING",
            "tip": "这单需要店家处理出票，请勿重复付款。"}


def search_faq(query: str) -> list:
    """
    查配置里的规则话术（★ 用真的 qa.py + templates.py，不是假的）。

    ★ 为什么这个要用真的（实测教训）：
      买家问「多久出票」，agent 编了句"很快就能出票"。
      而配置里【明明配了】出票时效 —— 就在 qa.py 的「出票」那条里。
      ⇒ 规则库是真配置的一部分，假了测不出真相。

    ★ 匹配方式（也是实测逼出来的）：
      第一版要求「买家的话」是配置文案的连续子串 ——
      「退」查不到（文案里写的是"退票""退改签"），
      「会员座」查不到（文案里写的是"会员价"）。
      ⇒ 改成：把查询按词切开，任一词命中就算；并做同义词扩展。
    """
    import re

    import qa as qa_module
    import templates as templates_module

    q = (query or "").strip()
    if not q:
        return []

    # ★ 同义词扩展 —— 买家说的词和配置里写的词常常不一样
    #   实测：「怎么买」「怎么拍」查不到，而配置里写的是
    #        「如何购票」「立即购买」「拍下」。买家不会用配置的词。
    SYNONYMS = {
        "退": ["退票", "退改", "退款"],
        "退款": ["退票", "退改"],
        "会员座": ["会员价", "W+", "会员"],
        "灰色": ["会员座", "会员价"],
        "出票": ["出票", "支付"],
        "支付": ["支付", "付款"],
        "付款": ["付款", "支付"],
        "取票": ["取票"],
        "发票": ["发票"],
        "改价": ["改价"],
        "改签": ["改签", "退改"],
        "二维码": ["取票码", "二维码"],
        "怎么买": ["购票", "购买", "拍下"],
        "怎么拍": ["拍下", "购买", "购票"],
        "怎么下单": ["拍下", "购买"],
        "在哪拍": ["拍下", "购买"],
        "如何购票": ["购票", "购买"],
        "怎么购票": ["购票", "购买"],
    }
    terms = {q}
    for key, alts in SYNONYMS.items():
        if key in q or q in key:
            terms.update(alts)
    # 再把长词切成 2 字词干（「多久出票」→「出票」）
    for t in list(terms):
        if len(t) >= 4:
            terms.add(t[-2:])
            terms.add(t[:2])
    terms = {t for t in terms if len(t) >= 2}

    def hit(text: str) -> bool:
        return any(t in text for t in terms)

    # ★★ 问法 → 规则 的映射（覆盖常见但字面不含关键词的问法）
    #
    #   实测撞出来的：查「能退吗」「怎么买」「票出错了怎么办」，
    #   第 1 条返回的都是「999」FAQ 菜单 —— 因为菜单的【内容】里
    #   列了"可以改签和退票吗""如何购票""票出错了咋办"。
    #   而真正的答案在带编号的规则里（003 / 001 / 004），
    #   关键词是数字，字面上和问法毫无重叠。
    #
    #   ★ 这是【店家口径】的一部分（哪个问题对应哪条规则），
    #     所以写死在代码里，不用模型猜。
    PREFER = {
        "退": "003", "退款": "003", "退票": "003", "改签": "003",
        "怎么买": "111", "怎么购票": "001", "如何购票": "001",
        "怎么拍": "001", "在哪拍": "111", "在哪里拍": "111",
        "出票错": "004", "票出错": "004", "票错": "004",
        "座位不一致": "004", "座位不对": "004", "城市错": "004",
        "扫不出": "005", "扫不了": "005", "用不了": "005",
        "取不出": "005", "取票码": "取票",
        "出票失败": "出票失败111", "出票慢": "出票",
        "多久出票": "出票", "支付后多久": "002",
    }
    preferred: set = set()
    for key, want in PREFER.items():
        if key in q:
            preferred.add(want)

    # ★★ 排序权重 —— 实测撞出来的：
    #   查「多久出票」时，第 1 条返回的是「999」FAQ 菜单
    #   （因为它的内容里含"出票"），而真正的答案「出票」那条排后面。
    #   模型拿到无关的第 1 条，就得自己挑 —— 挑错就编。
    #   ⇒ ① 问法映射命中的排最前 ② 关键词完全命中次之
    def weight(rule_kw: str, query: str) -> int:
        """越小越靠前。"""
        q = query.strip()
        if rule_kw in preferred:
            return 0                      # ★ 问法映射点名了这条
        if rule_kw == q:
            return 1                      # 关键词和买家问的一模一样
        if q in rule_kw or rule_kw in q:
            return 2                      # 互相包含（「出票」vs「多久出票」）
        if any(t in rule_kw for t in terms):
            return 3                      # 同义词命中的关键词
        return 4                          # 只在内容里出现 → 最次

    hits = []
    # ① 关键词回复表（qa.py）
    for rule in qa_module.QA_RULES:
        kw = getattr(rule, "keyword", "") or ""
        text = (getattr(rule, "content", "")
                or getattr(rule, "text", "") or "")
        if not kw or not text:
            continue
        if hit(kw) or hit(text) or kw in preferred:
            hits.append({"来源": f"关键词回复「{kw}」", "内容": text,
                         "_w": weight(kw, query)})
    # ② 模板（templates.py）—— ★ 只搜【文案】，不搜 key
    #    搜 key 会撞出一堆无关的（key 是 CINEMA_REQUIRED 这种英文名）
    for row in templates_module.catalog():
        key = row.get("key") or ""
        try:
            text = templates_module.text_for(key, "wanda") or ""
        except KeyError:
            # ★ catalog() 里有些 key（如 intake）不是可渲染的模板，
            #   find() 会抛 KeyError。跳过就行，别让搜索整个挂掉。
            continue
        if not text:
            continue
        if q in text or hit(text):
            hits.append({"来源": f"模板 {key}", "内容": text, "_w": 2})
    # ★ 权重小的在前；同权重保持原顺序（关键词类天然在模板前）
    hits.sort(key=lambda h: h["_w"])
    for h in hits:
        h.pop("_w", None)
    # 去重（同一条文案可能既在 qa 又在模板里）
    seen, uniq = set(), []
    for h in hits:
        k = h["内容"][:40]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)
    return uniq[:6]


# ==================================================================== 展示
def show(turn, *, show_facts: bool, facts: Facts) -> None:
    print()
    for i, msg in enumerate(turn.messages, 1):
        tag = f"[{i}]" if len(turn.messages) > 1 else "  "
        # 报价卡单独打好看点
        if "total_yuan" in str(msg):
            print(f"  它会发 {tag} （报价卡）")
            print(f"        {msg}")
        else:
            print(f"  它会发 {tag} {msg}")
    if not turn.messages:
        print("  它会发：（这一轮没给买家发话）")

    print(f"  调用    {[c['name'] for c in turn.calls] or '（没调工具）'}")
    for c in turn.calls:
        mark = "✓" if c["ok"] else "✗"
        args = {k: v for k, v in c["args"].items() if k != "evidence"}
        print(f"          {mark} {c['name']}  {json.dumps(args, ensure_ascii=False)[:88]}")
        if not c["ok"]:
            print(f"              → {c['error_code']}")
    if turn.handoff:
        print("  ★ 转人工")
    if turn.note:
        print(f"  note    {turn.note}")
    if show_facts:
        print(f"  事实    {json.dumps(facts.snapshot(), ensure_ascii=False)}")


HELP = """
本地命令：
  /img <URL>   当作买家发了截图
  /facts       看已确认事实（含出处）
  /calls       重看上一轮的工具调用
  /quit        退出
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="标准 agent 对话测试")
    ap.add_argument("--provider", default="WANDA",
                    choices=["WANDA", "LIANGPIAO"])
    ap.add_argument("--show-facts", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="★ /img 时真调良票识别（会花钱）")
    args = ap.parse_args()

    cred = store.load_credentials()
    key = cred.get("model_api_key") or ""
    client = build_client(key)
    tools = Tools(provider=args.provider, quote_fn=fake_quote,
                  order_fn=fake_order, faq_fn=search_faq)
    facts = Facts()

    print("=" * 74)
    print("标准 agent（工具选择式）")
    print("=" * 74)
    print(f"  模型 Key   {'已配' if client.ready else '★ 没配 —— 会失败'}")
    print(f"  供货商     {args.provider}"
          + ("   ← 要 seat_marking（圈选）" if args.provider == "WANDA"
             else "   ← 要精确座位"))
    print(f"  工具       {', '.join(TOOL_NAMES)}")
    print("  ★ 没有意图枚举、没有状态机 —— 模型直接选工具")
    print("  ★ 报价/下单是假的（不花钱、不下单）")
    print(HELP)

    if not client.ready:
        print("✗ 没有模型 Key，无法跑。先在界面「配置」页填 model_api_key。")
        return 1

    last_turn = None
    while True:
        try:
            line = input("买家> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/facts":
            print("  已确认事实：")
            if not facts.data:
                print("    （空）")
            for k, v in facts.snapshot().items():
                p = facts.provenance.get(k, {})
                print(f"    {k:<14}{str(v):<22}"
                      f"来自 {p.get('source','?')}"
                      + (f"  佐证「{p.get('evidence')}」"
                         if p.get("evidence") else ""))
            continue
        if line == "/calls":
            if last_turn is None:
                print("  （还没对话）")
            else:
                show(last_turn, show_facts=args.show_facts, facts=facts)
            continue

        buyer_text = line
        evidence_source = "text"
        if line.startswith("/"):
            parts = line.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            if cmd in ("/img", "/image", "/pic"):
                if not arg.startswith(("http://", "https://")):
                    print("  ★ /img 后面要跟公网图片 URL")
                    continue
                if not args.live:
                    # ★ 不真调识别，但【明确标成本轮来源是图】——
                    #   否则模型会去这句话里抽字段（而这句话是我写的描述，
                    #   不是买家说的），结果一条事实都进不去。
                    #   踩过这个坑：/facts 显示（空），模型还重复问已确认的字段。
                    buyer_text = ("【买家发来一张选座截图，以下是识别结果】\n"
                                  "影院：万达影城（呼和浩特IMAX店）\n"
                                  "城市：呼和浩特\n"
                                  "影片：（图上没有）\n"
                                  "日期时间：（图上没有）\n"
                                  "影厅：（图上没有）\n"
                                  "座位：没有选座，也没有圈位置")
                    evidence_source = "image"
                    print("  （假的图：不真调识别。要真识别加 --live）")
                else:
                    print("  （真调良票识别…）")
                    buyer_text, evidence_source = _recognize(arg, cred), "image"
            else:
                print(f"  ★ 不认识的命令：{cmd}")
                print("    可用：/img <URL>  /facts  /calls  /quit")
                continue

        try:
            turn = run_turn(client, tools, facts, buyer_text,
                            evidence_source=evidence_source)
        except Exception as exc:                            # noqa: BLE001
            print(f"  ✗ 抛异常（★ 不该抛给调用方）："
                  f"{type(exc).__name__}: {exc}")
            import traceback
            for ln in traceback.format_exc().splitlines()[-5:]:
                print(f"     {ln}")
            continue
        show(turn, show_facts=args.show_facts, facts=facts)
        last_turn = turn

    print("\n结束。")
    return 0


def _recognize(image_url: str, cred: dict) -> str:
    """真调良票识别，把结果拼成给 agent 看的文字。"""
    from integrations.liangpiao.api import LiangPiaoAPI
    from integrations.liangpiao.client import LiangPiaoClient
    client = LiangPiaoClient(cred["app_key"], cred["app_secret"])
    r = LiangPiaoAPI(client).ch7_recognize(image_url)
    raw = r.raw_results or {}
    parts = ["[买家发了一张选座截图]"]
    parts.append(f"识别结果 matchLevel={r.match_level}")
    for label, value in (("影院", raw.get("cinema_name") or r.cinema_name),
                         ("城市", raw.get("city_name") or r.city_name),
                         ("影片", raw.get("film") or r.movie_name),
                         ("日期时间", raw.get("showtime") or r.start_time),
                         ("影厅", raw.get("hall_name") or r.hall_name),
                         ("座位", raw.get("seat") or r.seats)):
        if value:
            parts.append(f"{label}：{value}")
        else:
            parts.append(f"{label}：（图上没有）")
    return "\n".join(parts)


if __name__ == "__main__":
    sys.exit(main())
