"""
回放案例二（双通道版）：识别结果代码直写，不再靠模型转述。

★ 三个版本的变化：
   v1（错）手写文本，抄的是 V5 qwen 的失败输出（city=null）→ 系统"答错"
   v2      真调识别，但把结果【拼成文字】给模型 → 座位被工具 schema 挡住，丢了
   v3（本版）识别结果【直接写进事实】→ 座位/cinema_id 一次到位
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError): pass

from application import store
from application.agent import Facts, Tools, build_client, run_turn
from chatex import fake_order, fake_quote, search_faq
from integrations.liangpiao.api import LiangPiaoAPI
from integrations.liangpiao.client import LiangPiaoClient

IMG = ("https://img.alicdn.com/imgextra/i1/2909042263/"
       "O1CN01e2r20bVtzcD1lm8W_!!2909042263-0-xy_chat.jpg")
cred = store.load_credentials()
api = LiangPiaoAPI(LiangPiaoClient(cred["app_key"], cred["app_secret"]))
client = build_client(cred.get("model_api_key") or "")

print("=" * 76)
print("① 真调识别")
print("=" * 76)
r = api.ch7_recognize(IMG)
print(f"  matchLevel {r.match_level}  cinemaId {r.cinema_id}  "
      f"city {r.city_name}  showId {r.show_id}")
print(f"  cinema {r.cinema_name}")
print(f"  seats  {[s.get('seatName') for s in (r.seats or [])]}")

facts = Facts()
tools = Tools(provider="WANDA", quote_fn=fake_quote, order_fn=fake_order,
              faq_fn=search_faq)

print()
print("=" * 76)
print("② 系统处理（识别结果代码直写 + 模型只负责说话）")
print("=" * 76)
turn = run_turn(client, tools, facts, "买家发来一张选座截图",
                evidence_source="image", recognition=r)
for m in (turn.messages or ["（没回话）"]):
    for ln in m.splitlines():
        print(f"    -> {ln}")
print(f"    工具：{[c['name'] for c in turn.calls]}")
print(f"    判定：{turn.note}")

print()
print("★ 事实（不看模型，看系统真正记下了什么）:")
for k, v in facts.snapshot().items():
    print(f"      {k:<16}{v}")

print()
print("=" * 76)
print("③ 对齐线上：买家补「杭州 西溪银泰城博纳国际影城」（V5 被抑制的那句）")
print("=" * 76)
turn = run_turn(client, tools, facts, "杭州 西溪银泰城博纳国际影城")
for m in (turn.messages or ["（没回话）"]):
    for ln in m.splitlines():
        print(f"    -> {ln}")
print(f"    判定：{turn.note}")
