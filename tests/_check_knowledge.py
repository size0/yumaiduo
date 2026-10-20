"""知识库检查：每条规则都要能被【买家会怎么问】查到。

★★ 为什么必须断：
   这个知识库的价值全在"agent 能不能查到"。
   如果某条没写同义问句，agent 就永远查不到它 ——
   于是模型自己编答案，或者转人工。
   这种问题【不报错】，只是回答质量悄悄变差。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import qa                                                               # noqa: E402
from application.service import _faq_lookup                             # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


print("=" * 76)
print("一、知识库结构")
print("=" * 76)
check("规则表非空", bool(qa.QA_RULES), f"{len(qa.QA_RULES)} 条")
# ★ 每条都要有 qa_id 和 content
bad = [r.keyword for r in qa.QA_RULES if not r.qa_id or not (r.content or "").strip()]
check("每条都有 id 和内容", not bad, str(bad))

# ★★ 核心：每条【知识类】规则都要有同义问句，否则 agent 查不到。
#   ★ 社交应答类例外（「好的 / 谢谢 / 稍等」）——
#     买家不会去"问"谢谢，写不出真实的问法；
#     它们由 match() 按关键字直接回，是对的。
#   ★ 例外要【显式登记】，不能让"忘写问句"蒙混过去：
#     新增规则如果忘写问句，会被这条断言抓住。
SOCIAL = {"好的", "谢谢", "稍等"}
no_q = [r.keyword for r in qa.QA_RULES
        if r.keyword not in SOCIAL
        and not (getattr(r, "questions", None) or [])]
check("★★ 每条【知识类】规则都有「买家会怎么问」（否则 agent 查不到）",
      not no_q, f"★ 缺: {no_q}")
# ★ 反向：社交类不该有问句（有的话多半是硬凑的）
social_q = [r.keyword for r in qa.QA_RULES
            if r.keyword in SOCIAL and (getattr(r, "questions", None) or [])]
check("★ 社交应答类没有被硬凑问句", not social_q, str(social_q))

# 问句不能重复（重复说明口径会打架）
seen = {}
dups = []
for r in qa.QA_RULES:
    for q in (getattr(r, "questions", None) or []):
        if q in seen:
            dups.append(f"{q}（{seen[q]} 与 {r.keyword} 都有）")
        seen[q] = r.keyword
check("★ 同义问句不重复（重复会让口径打架）", not dups, str(dups[:4]))

print()
print("=" * 76)
print("二、★ 一个条目多个关键字（合并同话术时用的）")
print("=" * 76)
multi = [r for r in qa.QA_RULES if "|" in str(r.keyword)]
check("有合并条目（同话术的重复项已合并）", bool(multi),
      f"{len(multi)} 条：{[r.keyword for r in multi]}")
# ★ 合并条的每个关键字都还能命中（不能因为合并丢了命中）
miss = []
for r in multi:
    for k in str(r.keyword).split("|"):
        k = k.strip()
        if not k:
            continue
        if not qa.has_keyword(k) or qa.match(k) is None:
            miss.append(k)
check("★★ 合并条里的每个关键字都还能被 match() 命中",
      not miss, f"★ 丢了: {miss}")
# 而 reply() 也要给得出
empty = [k.strip() for r in multi for k in str(r.keyword).split("|")
         if k.strip() and not qa.reply(k.strip())]
check("★ 合并条里的每个关键字都能回出话", not empty, str(empty))

print()
print("=" * 76)
print("三、★★ agent 查得到吗（这是知识库的用途）")
print("=" * 76)
# 买家会自然这么问 —— 每条规则至少抽一个问句，验证 _faq_lookup 查得到
FAIL_SAMPLES = []
for r in qa.QA_RULES:
    if r.keyword in SOCIAL:
        continue          # 社交应答不走知识库查询
    for q in (getattr(r, "questions", None) or [])[:1]:
        got = _faq_lookup(q)
        if not got:
            FAIL_SAMPLES.append(f"{q}（{r.keyword}）")
check("★★ 每条规则至少有一个问句能查到", not FAIL_SAMPLES,
      f"★ 查不到: {FAIL_SAMPLES[:5]}")

# ★ 具体问句要压过笼统关键字（踩过的坑）
CASES = [
    ("取票码用不了怎么办", "录音"),      # 005，不能被"取票"抢走
    ("出票失败了", "退款"),              # 011，不能被"出票"抢走
    ("多久能出票", "出票时间"),          # 002
    ("可以改签吗", "兑换券"),            # 003
]
wrong = []
for q, want in CASES:
    got = _faq_lookup(q) or ""
    if want not in got:
        wrong.append(f"{q} → 期望含「{want}」")
check("★★ 具体问句压过笼统关键字（顺序不能反）", not wrong, str(wrong))

print()
print("=" * 76)
print("四、★ match() 的老行为没被改动（逐字可控那条路）")
print("=" * 76)
# 这些是"买家原话"级别的，必须还是原来的回法
OLD = [
    ("谢谢", "不客气呢"),
    ("999", "常见问题"),
    ("002", "10-30分钟出票"),
]
bad2 = []
for msg, want in OLD:
    got = qa.reply(msg) or ""
    if want not in got:
        bad2.append(f"{msg} → 期望含「{want}」，实际 {got[:24]!r}")
check("★★ match/reply 的旧行为没变（逐字可控）", not bad2, str(bad2))

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
