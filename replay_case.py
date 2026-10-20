"""
回放真实案例：买家 2002808420 / 会话 65091694508

★ 用【线上真实发生过的对话】喂给现在的系统，看它会怎么回。
  识别结果用真实的（从线上 payload 里取），不用假数据。

真实经过（线上 V5）：
   1. 买家发图                                    → 报价卡（46元/张，问几张+圈座）
   2. 买家「9排中间四张」                          → 报价卡（提示会员座位，可圈座或报座号）
   3. 买家发图                                    → 报价卡（问几张）
   4. 买家「4张9排16 17 18 19」                    → 完整报价卡（184元）
   5. （下单/改价/付款/出票）

★ 本脚本只跑到第 4 步 —— 报价那部分。下单/付款是状态机的活，不在这次范围。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from application import store                                     # noqa: E402
from application.agent import Facts, Tools, build_client, run_turn  # noqa: E402
from chatex import fake_order, fake_quote, search_faq              # noqa: E402

# ==================================================================== 真实数据
# ★ 全部来自线上会话 65091694508 的 latest_recognition / current_purchase_request
RECOGNITION = {
    "cinema": "常州新北万达广场店",
    "cinema_address": "常州市新北区通江中路88号万达广场5楼万达影城",
    "city": "常州",
    "movie": "生化危机：爆发夜",
    "show_date": "2026-10-12",
    "show_time": "19:15",
    "hall": "3号IMAX厅（儿童需购票）",
    "provider": "WANDA",
    "seat_marking": "PRESENT",
}

# 线上真实对话（买家侧）
TURNS = [
    ("image", "买家发送了一张选座截图（万达影城，图上有圈选的座位标记）"),
    ("text", "9排中间四张"),
    ("image", "买家又发了一张选座截图（圈好了座位）"),
    ("text", "4张9排16 17 18 19"),
]


def image_note() -> str:
    """把真实识别结果拼成给 agent 看的文本（模拟"图里识别出来的东西"）。"""
    lines = ["【买家发来一张选座截图，以下是识别结果】"]
    for label, key in (("影院", "cinema"), ("城市", "city"), ("影片", "movie"),
                       ("日期", "show_date"), ("开场时间", "show_time"),
                       ("影厅", "hall")):
        lines.append(f"{label}：{RECOGNITION.get(key) or '（图上没有）'}")
    lines.append(f"座位标记：{RECOGNITION.get('seat_marking')}（图上圈了位置）")
    return "\n".join(lines)


def main() -> int:
    cred = store.load_credentials()
    client = build_client(cred.get("model_api_key") or "")
    if not client.ready:
        print("✗ 没模型 Key")
        return 1

    facts = Facts()
    tools = Tools(provider="WANDA", quote_fn=fake_quote, order_fn=fake_order,
                  faq_fn=search_faq)

    print("=" * 76)
    print("回放：买家 2002808420 / 会话 65091694508（万达 · 常州新北万达广场店）")
    print("=" * 76)
    print("★ 识别结果用【线上真实的】，买家消息用【线上真实的】")
    print()

    for i, (kind, msg) in enumerate(TURNS, 1):
        buyer_text = image_note() if kind == "image" else msg
        source = "image" if kind == "image" else "text"
        print("─" * 76)
        print(f"第 {i} 轮  买家：{msg}")
        print("─" * 76)

        turn = run_turn(client, tools, facts, buyer_text,
                        evidence_source=source)

        for m in (turn.messages or ["（没回话）"]):
            for ln in m.splitlines():
                print(f"    → {ln}")
        calls = [c["name"] + ("" if c["ok"] else f"✗{c['error_code']}")
                 for c in turn.calls]
        print(f"    工具：{calls or '（没调）'}")
        print(f"    判定：{turn.note}")
        known = {k: v for k, v in facts.snapshot().items()}
        print(f"    已确认：{known}")
        print()

    print("=" * 76)
    print("线上 V5 当时发的（对照）")
    print("=" * 76)
    for line in [
        "1. 常州新北万达广场店 生化危机：爆发夜 2026-10-12 19:15  46.00元/张。"
        "需要几张告诉我一下，座位可以直接在图上圈出来发我。",
        "2. …这个场次会员座位 46.00元/张。座位可以直接在图上圈出来发我，"
        "也可以告诉我几排几座。",
        "3. …46.00元/张。需要几张告诉我一下。",
        "4. 座位：9排16、9排17、9排18、9排19 / 价格：46.00元/张 / "
        "数量：4张 / 总价：184.00元",
    ]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
