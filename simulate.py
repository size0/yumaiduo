"""
模拟回复线上真实买家问题（★ 用标准 agent）

★ 每条开【独立会话】—— 线上这些是不同买家在不同对话里说的话，
  不带上文才能看出"单看这一句它会怎么答"。
  有上下文的场景（补充信息、改单）后面单独测。

用法：
    python simulate.py              # 跑前 40 条
    python simulate.py --n 80       # 跑前 80 条
    python simulate.py --all        # 全部
    python simulate.py --pick 哪些  # 只跑高频的（默认）
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

from application import store                                     # noqa: E402
from application.agent import Facts, Tools, build_client, run_turn  # noqa: E402
from chatex import fake_order, fake_quote, search_faq              # noqa: E402

# ★ 从线上数据里挑出来的买家问话，覆盖各类意图。
#   括号里是线上出现次数（来自 live-triage.json，9/12–10/11 生产数据）。
CASES = [
    ("你好", 55),
    ("多少钱", 31),
    ("怎么买", 10),
    ("灰色的座位可以选是吗", 10),
    ("这个多少钱", 9),
    ("能退吗", 9),
    ("是给我二维码吗？", 8),
    ("多久出票", 7),
    ("多少", 7),
    ("会员座能代订吗", 6),
    ("这个可以买吗", 5),
    ("怎么拍", 5),
    ("在哪里拍", 5),
    ("什么价格", 4),
    ("给我算一下", 3),
    ("好了吗", 3),
    ("有国庆的票么", 2),
    ("石家庄万达imax奥德赛多少钱", 2),
    ("两张多少钱", 2),
    ("时间这些是对的吧？", 2),
    ("是", 18),
    ("1", 35),
    ("2张", 21),
    ("？", 14),
    ("在吗", 13),
    ("好的", 13),
    ("20:30", 11),
    ("西山万达哪吒", 9),
    ("确认", 6),
    ("改价吧", 4),
    ("换成21:30", 3),
    ("你已发货", 6),
    ("谢谢", 7),
    ("可以", 6),
    ("需要", 3),
    ("报名先获一波官方流量，转化越好，后续流量继续加码。「吃喝玩乐」招商已开启！", 6),
    ("买家已确认收货，交易成功", 3),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show-calls", action="store_true", default=True)
    args = ap.parse_args()

    cred = store.load_credentials()
    client = build_client(cred.get("model_api_key") or "")
    if not client.ready:
        print("✗ 没模型 Key")
        return 1

    cases = CASES if (args.all or not args.n) else CASES[:args.n]
    print("=" * 78)
    print(f"模拟回复线上真实买家问题（{len(cases)} 条，每条独立会话）")
    print("=" * 78)

    for i, (msg, freq) in enumerate(cases, 1):
        facts = Facts()
        tools = Tools(provider="WANDA", quote_fn=fake_quote,
                      order_fn=fake_order, faq_fn=search_faq)
        try:
            turn = run_turn(client, tools, facts, msg)
        except Exception as exc:                            # noqa: BLE001
            print(f"\n{i:>3}. 买家（线上×{freq}）：{msg}")
            print(f"     ✗ {type(exc).__name__}: {exc}")
            continue

        calls = [c["name"] + ("" if c["ok"] else f"✗{c['error_code']}")
                 for c in turn.calls]
        print(f"\n{i:>3}. 买家（线上×{freq}）：{msg}")
        if turn.messages:
            for m in turn.messages:
                for ln in m.splitlines():
                    print(f"     {ln}")
        else:
            print("     （没回话）")
        print(f"     工具：{calls or '（没调）'}"
              + ("   ★ 转人工" if turn.handoff else ""))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
