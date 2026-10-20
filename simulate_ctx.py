"""
模拟回复（★ 带上下文版）—— 这才是线上的真实情况

★ 为什么要有这个：
  前一个脚本每条消息开【独立会话】。结果"1"、"是"、"好的"这种
  短回复，agent 只能重新问一遍 —— 因为不知道刚问过什么。
  但线上它【刚问过】。所以必须带上上一轮的问题再测。

★ 这里模拟两种线上最常见的上下文：
  ① 我们问「需要几张？」   → 买家答「2张」
  ② 我们问「哪天几点的？」 → 买家答「20:30」
  ③ 已经报了价            → 买家答「改价吧」
  ④ 发了报价卡            → 买家答「确认」
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

# ★ (我们上一句说了什么, 买家这一句, 线上次数)
#   初始事实模拟"之前已经确认了什么"
CASES = [
    ("请问需要几张票呢？", "2张", 21, {}),
    ("麻烦告诉我哪天、几点的场次？", "20:30", 11, {}),
    ("想看哪部电影？", "1", 35, {}),
    ("影院和影片我记下了，请问哪天看？", "是", 18, {}),
    ("我把报价发给您了，确认下单吗？", "确认", 6, {}),
    ("报价 42.03 元/张，要改价吗？", "改价吧", 4, {}),
    ("万达得在选座图上圈一下位置，圈好发我", "好了", 3, {}),
    ("稍等，我算好了发您", "？", 14, {}),
    ("在的，有什么可以帮您？", "在吗", 13, {}),
    ("付款后我这边安排出票～", "你已发货", 6, {}),
]


def main() -> int:
    cred = store.load_credentials()
    client = build_client(cred.get("model_api_key") or "")
    if not client.ready:
        print("✗ 没模型 Key")
        return 1

    print("=" * 78)
    print("带上下文模拟（我们刚问过一句，买家短回复）")
    print("=" * 78)

    for i, (our_line, buyer_line, freq, seed) in enumerate(CASES, 1):
        facts = Facts()
        for k, v in seed.items():
            facts.merge({k: v}, source="image")
        tools = Tools(provider="WANDA", quote_fn=fake_quote,
                      order_fn=fake_order, faq_fn=search_faq)
        # ★ 用对话历史把"我们上一句"喂进去
        history = [{"role": "assistant", "content": our_line}]
        try:
            turn = run_turn(client, tools, facts, buyer_line,
                            history=history)
        except Exception as exc:                            # noqa: BLE001
            print(f"\n{i}. 我们说：{our_line}")
            print(f"   买家（线上×{freq}）：{buyer_line}")
            print(f"   ✗ {type(exc).__name__}: {exc}")
            continue

        calls = [c["name"] + ("" if c["ok"] else f"✗{c['error_code']}")
                 for c in turn.calls]
        print(f"\n{i}. 我们说：{our_line}")
        print(f"   买家（线上×{freq}）：{buyer_line}")
        for m in (turn.messages or ["（没回话）"]):
            for ln in m.splitlines():
                print(f"      {ln}")
        print(f"      工具：{calls or '（没调）'}"
              + ("   ★ 转人工" if turn.handoff else "")
              + (f"   事实：{facts.snapshot()}" if facts.data else ""))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
