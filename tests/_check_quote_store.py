"""
_check_quote_store.py —— 报价记录 / 存储 / 作废机制

★ 覆盖三件事
  ① 数据模型：报价记录该有的字段都在（含规则版本）
  ② 存储：存得进、取得出、状态翻得动
  ③ ★ 作废机制：买家改需求 → 旧报价立刻失效（防拿旧价下单）
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from domain.quote import (QuoteRecord, QuoteStore, default_expiry,  # noqa: E402
                          new_quote_id)
from domain.quote_rules import (ADD, QUOTE_FIXED, QuoteRule,       # noqa: E402
                                QuoteRuleEngine)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ★ 线上真实数据（来自 quotes.sqlite3 的 quote_id
#   b896fbce-449e-4f94-86db-9cc8032f7ea2，买家 2002808420）
REAL = {
    "quote_id": "b896fbce-449e-4f94-86db-9cc8032f7ea2",
    "tenant_id": "107", "shop_id": "2313315754",
    "buyer_id": "2002808420", "chat_id": "65091694508",
    "provider": "WANDA", "request_type": "EXACT_SEATS",
    "cinema": "常州新北万达广场店", "movie": "生化危机：爆发夜",
    "show": "2026-10-12 19:15",
    "selected_seats": ["9排16", "9排17", "9排18", "9排19"],
    "unit_cost_fen": 4500, "unit_sell_price_fen": 4600, "ticket_count": 4,
    "total_cost_fen": 18000, "total_sell_price_fen": 18400,
    "pricing_rule_revision": "settings-52",
}
IDENT = {k: REAL[k] for k in ("tenant_id", "shop_id", "buyer_id", "chat_id")}


def main() -> int:                                       # noqa: C901
    section("一、★ 规则版本：报价结果要能说清是哪版规则算的")
    eng = QuoteRuleEngine(
        [QuoteRule(name="万达一口价", weight=9, enabled=True,
                   brands=["万达影城"], quote_mode=QUOTE_FIXED, method=ADD,
                   markup_yuan=1.0, capped=False, rounding_fen=10)],
        rule_id="runtime-config", revision="settings-52")
    from datetime import time as _t
    q = eng.quote(seat_count=4, now=_t(12, 0), estimate_total_fen=4500 * 4,
                  original_total_fen=4900 * 4, brand="万达影城")
    check("报价结果带规则版本", q.pricing_rule_revision == "settings-52",
          repr(q.pricing_rule_revision))
    check("报价结果带规则标识", q.pricing_rule_id == "万达一口价",
          repr(q.pricing_rule_id))
    check("★ 与线上真实数据吻合（4600/18400）",
          q.unit_price_fen == 4600 and q.total_price_fen == 18400,
          f"{q.unit_price_fen}/{q.total_price_fen}")

    section("二、★ 线上真实报价记录：存进去再取出来，字段不丢")
    store = QuoteStore(":memory:")
    rec = QuoteRecord(**REAL, pricing_rule_id="万达一口价",
                      expires_at=default_expiry())
    store.create(rec)
    got = store.get(REAL["quote_id"])
    check("取回来了", got is not None)
    for field, want in (("cinema", REAL["cinema"]), ("show", REAL["show"]),
                        ("selected_seats", REAL["selected_seats"]),
                        ("unit_cost_fen", 4500),
                        ("unit_sell_price_fen", 4600),
                        ("total_cost_fen", 18000),
                        ("total_sell_price_fen", 18400),
                        ("ticket_count", 4),
                        ("pricing_rule_revision", "settings-52")):
        check(f"  {field} 不丢", getattr(got, field) == want,
              f"{getattr(got, field)!r}")

    section("三、★ 成本与售价分开存（能核对毛利）")
    check("单张毛利 = 100 分（4600-4500）", got.margin_unit_fen == 100,
          str(got.margin_unit_fen))
    check("整单毛利 = 400 分", got.margin_total_fen == 400,
          str(got.margin_total_fen))

    section("四、可用性判定")
    check("READY + 未过期 → 可用", got.is_usable())
    check("要求有座位时也可用（线上这单有 4 个座位）",
          got.is_usable(require_seats=True))
    no_seat = QuoteRecord(quote_id=new_quote_id(), **IDENT,
                          total_sell_price_fen=100,
                          expires_at=default_expiry())
    store.create(no_seat)
    check("★ 没有座位号的报价【不能】用于下单",
          not no_seat.is_usable(require_seats=True),
          "下单要 seatNo/rowNo/colNo")

    section("五、★ 作废机制（防拿旧价下单）")
    # 改动前：带座位那笔是能用的
    check("改动前：带座位的报价可用",
          store.usable_for(IDENT, require_seats=True).quote_id == REAL["quote_id"])
    # 买家改了需求：换场次
    changed = store.supersede_for_identity(IDENT, except_quote_id=no_seat.quote_id)
    check("★ 旧报价被标 SUPERSEDED", REAL["quote_id"] in changed, str(changed))
    check("★ 作废后取不到任何可用于下单的报价",
          store.usable_for(IDENT, require_seats=True) is None,
          str(store.usable_for(IDENT, require_seats=True)))
    check("★ 记录还在（不是被删了，只是失效）",
          store.get(REAL["quote_id"]) is not None)
    check("状态真的落盘成 SUPERSEDED",
          store.get(REAL["quote_id"]).status == "SUPERSEDED",
          store.get(REAL["quote_id"]).status)

    section("六、过期")
    quick = QuoteRecord(quote_id=new_quote_id(), **IDENT,
                        total_sell_price_fen=100,
                        expires_at=datetime.now(timezone.utc)
                        - timedelta(seconds=1))
    store.create(quick)
    check("★ 已过期的报价不可用", not quick.is_usable())
    flipped = store.expire_stale()
    check("★ expire_stale 把过期报价翻成 EXPIRED",
          quick.quote_id in flipped
          and store.get(quick.quote_id).status == "EXPIRED",
          str(flipped))

    section("七、不许静默覆盖（同一 quote_id 重复存要报错）")
    try:
        store.create(rec)
        check("★ 重复 quote_id 会报错", False, "居然没报错")
    except ValueError as exc:
        check("★ 重复 quote_id 会报错", "QUOTE_DUPLICATE_ID" in str(exc),
              str(exc)[:60])

    section("八、★ 报价隔离：只能取到自己买家的报价")
    other = dict(IDENT, buyer_id="9999999999")
    check("别的买家取不到这笔", store.by_identity(other) == [],
          str(store.by_identity(other)))
    for bad in ({"tenant_id": "107"}, {}):
        try:
            store.by_identity(bad)
            check(f"★ 身份不全要报错 {bad}", False, "居然没报错")
        except ValueError:
            check(f"★ 身份不全要报错 {bad}", True)

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
