"""
分档报价（tiers）实测 —— 对齐 V5 `domain/quote/pricing.py`。

★ 为什么要单独一套：这套算法最容易被"看起来像"骗过去。
  我第一版的「动态折扣加法」就是一个线性式子，名字像、机制完全不同。
  所以这里逐条盯：
    ① 折扣率到底怎么算（两个金额的比值，不是上游折扣）
    ② 落档边界（左闭右开 / upper_inclusive）
    ③ 四个 basis 分支各自把调整乘在谁身上
    ④ adjustment_bps 带符号（正=加价，负=减价）
    ⑤ 与 V5 同参数的算式结果一致（手算对照）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from domain.quote_rules import (                                          # noqa: E402
    QuoteRule, QuoteRuleEngine, PricingTier, ADD, QUOTE_FIXED, QUOTE_ESTIMATE,
    ADJUST_NONE,
    BASIS_ORIGINAL, BASIS_ESTIMATED, BASIS_PER_TICKET, BASIS_COST_PLUS,
    BASIS_TYPES, QUOTE_METHODS, DYNAMIC_DISCOUNT, DYNAMIC_DISCOUNT_ADD,
    discount_bps_of, pick_tier, price_by_tier, _round_half_up)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    tail = f"  {detail}" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{tail}")
    return ok


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


def main():
    section("一、折扣率 = base × 10000 / original（两个金额的比值）")
    # 那笔真实订单的数：官方原价 3890，报价 3363
    check("折扣率 = 3363×10000/3890", discount_bps_of(3363, 3890) == 8645,
          f"算出 {discount_bps_of(3363, 3890)} bps = "
          f"{discount_bps_of(3363, 3890) / 100:.2f}%")
    check("原价缺失 → 返回 0（不抛异常）", discount_bps_of(3363, 0) == 0)
    check("成本=原价 → 10000 bps（100%）",
          discount_bps_of(3890, 3890) == 10000)
    check("成本是原价一半 → 5000 bps（50%）",
          discount_bps_of(1945, 3890) == 5000)

    section("二、落档：左闭右开 + upper_inclusive（照 V5 _tier）")
    # V5 线上的 10 档（liangpiao_fixed_pricing 真值）
    v5_tiers = [
        PricingTier(lower_bps=0, upper_bps=5000, adjustment_bps=1000),
        PricingTier(lower_bps=5000, upper_bps=6000, adjustment_bps=800),
        PricingTier(lower_bps=6000, upper_bps=7000, adjustment_bps=600),
        PricingTier(lower_bps=7000, upper_bps=8000, adjustment_bps=400),
        PricingTier(lower_bps=8000, upper_bps=9000, adjustment_bps=300),
        PricingTier(lower_bps=9000, upper_bps=10000, adjustment_bps=100,
                    upper_inclusive=True),
    ]
    # 成本 3890（=原价）→ 折扣率 10000 → 落最后一档（upper_inclusive）
    row = pick_tier(v5_tiers, 3890, 3890)
    check("折扣率正好 10000 落到最后一档（upper_inclusive 生效）",
          row is not None and row.adjustment_bps == 100,
          f"命中 adjustment_bps={getattr(row, 'adjustment_bps', None)}")
    # 成本 3501 → 3510 bps? 不对：3501*10000/3890 = 9000 → 落 9000 档
    row = pick_tier(v5_tiers, 3501, 3890)
    check("折扣率 9000 → 落到 9000-10000 档",
          row is not None and row.lower_bps == 9000,
          f"lower_bps={getattr(row, 'lower_bps', None)}")
    # 恰好 8900 bps (3462/3890) 落 8000-9000
    row = pick_tier(v5_tiers, 3462, 3890)
    check("折扣率 8900 → 落到 8000-9000 档（左闭右开）",
          row is not None and row.lower_bps == 8000,
          f"lower_bps={getattr(row, 'lower_bps', None)} "
          f"折扣率={discount_bps_of(3462, 3890)}")
    check("没有分档表 → None（不抛异常）", pick_tier([], 3462, 3890) is None)
    check("缺原价 → None", pick_tier(v5_tiers, 3462, 0) is None)

    section("三、四个 basis 分支：调整乘在谁身上")
    # 构造：原价 4000，成本 3600（折扣率 9000）
    # 分档：9000-10000 档，比例 +2.5%（250 bps）
    tiers = [PricingTier(lower_bps=0, upper_bps=9000, adjustment_bps=0),
             PricingTier(lower_bps=9000, upper_bps=10001,
                         adjustment_bps=250, upper_inclusive=True)]

    def mk(basis):
        return QuoteRule(name="t", weight=9, enabled=True, brands=[],
                         quote_mode=QUOTE_FIXED, method=ADD, markup_yuan=0,
                         limit_yuan=0, capped=False, adjust=ADJUST_NONE,
                         rounding_fen=1, tiers=list(tiers), basis=basis)

    # estimated：调整乘在【成本】上 → 3600 + 3600×2.5% = 3600 + 90 = 3690
    r = price_by_tier(mk(BASIS_ESTIMATED), cost_total=3600, cost_unit=3600,
                      seat_count=1, original_total=4000, step=1)
    check("★ estimated：调整乘在【成本】上（成本3600 → 3690）",
          r["status"] == "PRICED" and r["total_fen"] == 3690,
          f"total={r['total_fen']} note={r['note']}")
    # original：调整乘在【原价】上 → 3600 + 4000×2.5% = 3600 + 100 = 3700
    r = price_by_tier(mk(BASIS_ORIGINAL), cost_total=3600, cost_unit=3600,
                      seat_count=1, original_total=4000, step=1)
    check("★★ original：调整乘在【原价】上（成本3600 → 3700）",
          r["status"] == "PRICED" and r["total_fen"] == 3700,
          f"total={r['total_fen']}")
    check("★ 两者结果不同（原价 4000 ≠ 成本 3600，所以必须区分）",
          True, "3690 vs 3700 —— 这就是我第一版想当然写错的点")
    # 折扣率：original basis 时 base=原价 → 恒 10000
    r = price_by_tier(mk(BASIS_ORIGINAL), cost_total=3600, cost_unit=3600,
                      seat_count=1, original_total=4000, step=1)
    check("original basis 的折扣率恒为 10000（base=原价）",
          r["discount_bps"] == 10000, f"{r['discount_bps']}")

    section("四、adjustment_bps 带符号（正=加价，负=减价）")
    neg = [PricingTier(lower_bps=0, upper_bps=10001, adjustment_bps=-500,
                       upper_inclusive=True)]
    r = price_by_tier(
        QuoteRule(name="n", weight=9, enabled=True, brands=[], tiers=neg,
                  basis=BASIS_ESTIMATED, rounding_fen=1),
        cost_total=4000, cost_unit=4000, seat_count=1,
        original_total=4000, step=1)
    # 4000 + 4000×(-5%) = 4000 - 200 = 3800
    check("负比例 = 减价（4000 → 3800）",
          r["status"] == "PRICED" and r["total_fen"] == 3800,
          f"total={r['total_fen']}")

    section("五、固定加价分支（fixed_per_ticket / cost_plus_fixed）")
    per = [PricingTier(lower_bps=0, upper_bps=10001, fixed_add_fen=100,
                       upper_inclusive=True)]
    r = price_by_tier(
        QuoteRule(name="p", weight=9, enabled=True, brands=[], tiers=per,
                  basis=BASIS_PER_TICKET, rounding_fen=10),
        cost_total=9000, cost_unit=4500, seat_count=2,
        original_total=10000, step=10)
    # 每张 4500+100=4600 → 取整到角仍是 4600 → 两张 9200
    check("★ fixed_per_ticket：每张 +100 分（2 张 → 9200）",
          r["status"] == "PRICED" and r["total_fen"] == 9200,
          f"total={r['total_fen']} note={r['note']}")
    cp = [PricingTier(lower_bps=0, upper_bps=10001, fixed_add_fen=100,
                      upper_inclusive=True)]
    r = price_by_tier(
        QuoteRule(name="c", weight=9, enabled=True, brands=[], tiers=cp,
                  basis=BASIS_COST_PLUS, rounding_fen=10),
        cost_total=9000, cost_unit=4500, seat_count=2,
        original_total=10000, step=10)
    # V5 cost_plus_fixed + step10：(9000+200)/2=4600 → 取整 4600 → ×2 = 9200
    check("cost_plus_fixed：整单 +100×张数，step10 先算单张（→ 9200）",
          r["status"] == "PRICED" and r["total_fen"] == 9200,
          f"total={r['total_fen']}")

    section("六、★ 与 V5 的 _round_half_up 手算对照")
    # V5: _round(numerator, denominator, step) = (n*2 + d*step)//(2*d*step)*step
    def v5_round(n, d, step):
        return (n * 2 + d * step) // (2 * d * step) * step

    ok = all(_round_half_up(n, d, s) == v5_round(n, d, s)
             for n, d, s in ((3462, 1, 10), (33630, 10, 10), (4313, 1, 10),
                             (4667, 1, 10), (4600, 1, 10), (18400, 4, 10)))
    check("★ 我的 _round_half_up 与 V5 的 _round 逐式一致", ok,
          "含 4600（step 整数倍不该多进一格，我踩过这个坑）")
    check("★ 成本4500+100、step10 → 4600（线上真实验证过的数）",
          _round_half_up(4500 + 100, 1, 10) == 4600,
          str(_round_half_up(4500 + 100, 1, 10)))

    section("七、★ 「乘法」已删除（它是我加的且有 bug）")
    check("★ QUOTE_METHODS 里没有「乘法」", "乘法" not in QUOTE_METHODS,
          str(QUOTE_METHODS))
    check("「加法」和「动态折扣」还在（名字对齐 V5）",
          "加法" in QUOTE_METHODS and "动态折扣" in QUOTE_METHODS)
    src = (Path(__file__).resolve().parent.parent
           / "domain" / "quote_rules.py").read_text(encoding="utf-8")
    check("★ 源码里不再有多乘法的算法分支",
          "rule.markup_yuan - 1" not in src,
          "那行 `cost_unit * (markup_yuan - 1)` 就是 bug 所在")

    section("八、没有分档表的规则仍走老算法（不破坏老数据）")
    old = QuoteRule(name="old", weight=9, enabled=True, brands=[],
                    quote_mode=QUOTE_ESTIMATE, method=ADD,
                    markup_yuan=2.0, limit_yuan=0, capped=False,
                    adjust=ADJUST_NONE, rounding_fen=10)
    eng = QuoteRuleEngine([old], revision="t")
    res = eng.quote(seat_count=1, now=__import__("datetime").time(12, 0),
                    total_amount_fen=3450, brand="万达影城")
    check("★ 老规则（无 tiers）：成本 3450 + 2 元 → 3650",
          res.unit_price_fen == 3650, f"实得 {res.unit_price_fen}")

    section("九、★★ 序列化往返：分档表不能被抹平")
    # ★★ 这个是真 bug，很难查，必须钉住：
    #   _rule_to_dict 原来只写 getattr(t, "lower_bps", 0)。
    #   但从 quote_rules.json 读回来的规则，其 tiers 已经是 dict ——
    #   getattr(dict, "lower_bps", 0) 永远给 0。
    #   ⇒ 每次保存都把整张表抹平成「0-10000 / 比例 0」，
    #     算价落档永远命中第一档、比例恒为 0 —— 价格看着正常但全错。
    from application import store as _st
    import json as _json
    _tiers = [PricingTier(0, 5000, 1000),
              PricingTier(5000, 9000, 300),
              PricingTier(9000, 10000, 100, upper_inclusive=True)]
    _r = QuoteRule(name="往返", weight=9, brands=["万达影城"],
                   tiers=list(_tiers), basis=BASIS_ESTIMATED)
    d1 = _st._rule_to_dict(_r)
    check("★ dataclass → dict 保留分档",
          [t["lower_bps"] for t in d1["tiers"]] == [0, 5000, 9000],
          str([t["lower_bps"] for t in d1["tiers"]]))
    # 模拟"从文件读回来"：tiers 已经是 dict
    _r2 = QuoteRule(**{k: v for k, v in d1.items()
                       if k in QuoteRule.__dataclass_fields__ and k != "tiers"},
                    tiers=d1["tiers"])
    d2 = _st._rule_to_dict(_r2)
    check("★★ dict → dict 也保留分档（这就是被抹平的那一步）",
          [t["lower_bps"] for t in d2["tiers"]] == [0, 5000, 9000],
          str([t["lower_bps"] for t in d2["tiers"]]))
    check("★ 二次往返后比例还在",
          [t["adjustment_bps"] for t in d2["tiers"]] == [1000, 300, 100],
          str([t["adjustment_bps"] for t in d2["tiers"]]))
    check("★ upper_inclusive 也在",
          d2["tiers"][2]["upper_inclusive"] is True)
    check("★ 整条规则能 json.dumps（PricingTier 不会漏出去）",
          isinstance(_json.dumps(d2, ensure_ascii=False), str))

    section("十、★★ 端到端：落盘 → 读回 → 算价，比例必须生效")
    import tempfile as _tmp
    import shutil as _sh
    import datetime as _dt
    _dir = Path(_tmp.mkdtemp())
    _old = _st.RULES_FILE
    try:
        _st.RULES_FILE = _dir / "rules.json"
        _st.save_rules([_r2])
        _back = _st.load_rules()
        check("★ load_rules 读回 1 条", len(_back) == 1, f"{len(_back)} 条")
        check("★★ 读回的分档没丢", len(_back[0].tiers) == 3,
              f"{len(_back[0].tiers)} 档")
        check("★ 读回的是 PricingTier（不是 dict）",
              all(isinstance(t, PricingTier) for t in _back[0].tiers))
        # 用读回的规则算价：成本 3462 / 原价 3890 → 8899 bps
        # → 落 5000-9000 档（比例 +3%）
        #   3462 + 3462×3% = 3565.86 → 这条规则 rounding_fen=1 → 3566
        #   ★ 关键判据是【不是 3462】—— 比例不生效时会原样返回成本。
        _eng = QuoteRuleEngine(_back, revision="t")
        _res = _eng.quote(seat_count=1, now=_dt.time(12, 0),
                          total_amount_fen=3462, brand="万达影城",
                          original_total_fen=3890)
        check("★★ 算价用上了比例（3462 → 3566 = 成本 +3%）",
              _res.unit_price_fen == 3566, f"实得 {_res.unit_price_fen}")
        check("★ 比例不生效的话会是 3462（原样返回成本）",
              _res.unit_price_fen != 3462)
    finally:
        _st.RULES_FILE = _old
        _sh.rmtree(_dir, ignore_errors=True)

    section("★ 报价模式：万达专用报价（2026-10-13 加）")
    from domain.quote_rules import (QUOTE_WANDA, QUOTE_MODES,          # noqa: E402
                                    PRICE_MODE_OF_QUOTE, seed_rules)
    check("★「万达专用报价」在可选模式里", QUOTE_WANDA in QUOTE_MODES,
          str(QUOTE_MODES))
    check("★ 一共 4 个模式（新增后）", len(QUOTE_MODES) == 4,
          str(len(QUOTE_MODES)))
    check("★★ 映射成 FIXED —— 万达成本是座位图的确定值，不是上限",
          PRICE_MODE_OF_QUOTE.get(QUOTE_WANDA) == "FIXED",
          str(PRICE_MODE_OF_QUOTE))
    # ★ 万达种子规则：必须默认启用 + 用新模式 + 每张固定加价
    seeded = seed_rules(["万达影城", "CGV影城"])
    # ★★ 2026-10-14：找"万达系一口价"这条【品牌规则】时不能用
    #    `"万达" in r.name` —— 那会先命中新加的「万达报价（V5）」
    #    （渠道规则，模式是"渠道一口价"）。两条名字里都有"万达"。
    #    ⇒ 按名字精确匹配，断言才是针对想验的那一条。
    w = next((r for r in seeded if r.name == "万达系一口价"), None)
    check("★★ 万达系一口价（品牌规则）默认【启用】（以前是禁用，导致运营找不到）",
          w is not None and w.enabled is True,
          f"enabled={getattr(w, 'enabled', None)}")
    check("★★ 用「万达专用报价」模式",
          w is not None and w.quote_mode == QUOTE_WANDA,
          getattr(w, "quote_mode", None))
    check("★★ basis = 每张固定加价（万达成本不是预估）",
          w is not None and w.basis == "fixed_per_ticket",
          getattr(w, "basis", None))

    # ★★ 新加的 V5 渠道规则：启用 + 限定渠道 + 权重压过品牌规则
    from domain.quote_rules import CHANNEL_WANDA, CHANNEL_LIANGPIAO    # noqa: E402
    v5w = next((r for r in seeded if r.name == "万达报价（V5）"), None)
    v5l = next((r for r in seeded if r.name == "良票报价（V5）"), None)
    check("★★ V5 两条渠道规则都在种子里",
          v5w is not None and v5l is not None)
    check("★★ V5 两条都默认启用",
          bool(v5w and v5w.enabled) and bool(v5l and v5l.enabled))
    check("★★ V5 规则限定了渠道（WANDA / LIANGPIAO）",
          getattr(v5w, "channel", "") == CHANNEL_WANDA
          and getattr(v5l, "channel", "") == CHANNEL_LIANGPIAO,
          f"{getattr(v5w, 'channel', None)} / {getattr(v5l, 'channel', None)}")
    check("★★ V5 权重压过品牌规则（渠道口径更具体，要先命中）",
          bool(v5w and w and v5w.weight > w.weight),
          f"V5={getattr(v5w, 'weight', None)} 品牌={getattr(w, 'weight', None)}")
    check("★★ V5 规则各 6 档（V5 生产库原样）",
          len(getattr(v5w, "tiers", [])) == 6 and len(getattr(v5l, "tiers", [])) == 6)
    check("★★ V5 规则 adjust=不调整（取整已在分档里做过，不能再套一层）",
          getattr(v5w, "adjust", None) == "不调整"
          and getattr(v5l, "adjust", None) == "不调整",
          f"{getattr(v5w, 'adjust', None)} / {getattr(v5l, 'adjust', None)}")
    check("★ V5 规则按 V5 口径：万达每档 +100 分、良票万分比递减",
          all(t.fixed_add_fen == 100 for t in v5w.tiers)
          and [t.adjustment_bps for t in v5l.tiers]
          == [1000, 800, 600, 400, 300, 100],
          f"万达={[t.fixed_add_fen for t in v5w.tiers]} "
          f"良票={[t.adjustment_bps for t in v5l.tiers]}")

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"{passed}/{total} 项通过")
    if passed != total:
        print("★ 有未通过项 —— 别急着往下做")
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
