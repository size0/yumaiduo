"""
_check_fact_names.py —— ★ 核对「同一个购票事实」在各层叫什么名字。

════════════════════════════════════════════════════════════════
★ 为什么要有这个文件

  之前 _check_params.py 的「一之三：同一个事实有没有写在两个地方」
  只查出票时效那一个事实，查不出【字段命名有几套】。

  而实际发生过：
    · 我在 application/agent.py 里把城市叫 city，项目其他部分叫 city_name
    · 座位我叫 selected_seats，Conversation 叫 seats
  两个名字指同一个东西 ⇒ 数据合并/落库时要写映射，线上历史也没法对照。

★ 这个文件核对三件事
  ① 各层的字段名清单（识别层 / Facts / Conversation），列出来
  ② 同一个概念在层间名字不同就报出来（识别层除外 —— 那是良票 API 契约）
  ③ Facts 的每个字段必须能说清来源：来自识别结果、或来自项目现有命名
════════════════════════════════════════════════════════════════
"""
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ==================================================================== 数据
# ★ 识别层：字段名【由良票 API 决定】，不可改（cityName → city_name）
#   来源：integrations/liangpiao/api.py 的 RecognitionResult
RECOGNITION = {
    "city_name": "cityName", "cinema_name": "cinemaName",
    "cinema_address": "cinemaAddress", "movie_name": "movieName",
    "hall_name": "hallName", "start_time": "startTime",
    "seats": "seat/seatName", "cinema_id": "cinemaId",
    "movie_id": "movieId", "show_id": "showId",
}

# ★ 概念 → (识别层名, Facts 名, Conversation 名)
CONCEPTS = [
    ("城市",   "city_name",   "city_name",   "city_name"),
    ("影院",   "cinema_name", "cinema_name", None),
    ("影片",   "movie_name",  "movie_name",  None),
    ("影厅",   "hall_name",   "hall_name",   None),
    ("座位",   "seats",       "seats",       "seats"),
    ("开场时刻", "start_time", "show_time",   None),
    ("场次ID", "show_id",     "show_id",     None),
    ("影院ID", "cinema_id",   "cinema_id",   None),
]


def main() -> int:
    import application.agent as agent_module

    section("一、各层的字段名清单")
    print(f"  Facts（application/agent.py）{len(agent_module.FACT_FIELDS)} 个：")
    print(f"    {sorted(agent_module.FACT_FIELDS)}")
    print()
    print("  识别层（integrations/liangpiao/api.py 的 RecognitionResult）：")
    print(f"    {sorted(RECOGNITION)}")

    section("二、★ 同一个概念在层间名字是否一致")
    bad = []
    for concept, rec_name, fact_name, conv_name in CONCEPTS:
        names = {n for n in (rec_name, fact_name, conv_name) if n}
        same = len(names) == 1
        mark = "✓" if same else "★"
        print(f"  {mark} {concept:<8}识别={rec_name:<14}Facts={fact_name:<14}"
              f"Conversation={conv_name or '—'}")
        # ★ 只要求 Facts 与 Conversation 一致 ——
        #   识别层名字由良票 API 决定，改了反而对不上契约
        if conv_name and fact_name != conv_name:
            bad.append((concept, fact_name, conv_name))
    check("★ Facts 与 Conversation 用同一个名字（识别层除外）", not bad,
          str(bad))

    section("三、Facts 里每个字段的来源要能说清")
    # ★ 三类来源：
    #   识别层同名     → 由识别结果直接写
    #   识别层异名     → 由识别结果转换（start_time → show_date/show_time）
    #   自有的         → 项目里没有对应名字，是本项目新增概念
    from_recognition = set(RECOGNITION)
    derived = {"show_date": "start_time", "show_time": "start_time"}
    ours = {"seat_marking", "ticket_count", "cinema_code", "city_code"}
    unknown = []
    for f in agent_module.FACT_FIELDS:
        if f in from_recognition:
            src = "识别层同名"
        elif f in derived:
            src = f"识别层 {derived[f]} 派生"
        elif f in ours:
            src = "本项目自有（项目里无对应名字）"
        else:
            src = None
        print(f"  {f:<18}{src or '★ 说不清来源'}")
        if src is None:
            unknown.append(f)
    check("★ 每个 Facts 字段都能说清来源", not unknown, str(unknown))

    section("四、命名的依据（不能凭空起名）")
    # ★ 这条是防止"我随手起一个名字"：
    #   city_name / cinema_name / movie_name / hall_name / seats
    #   这 5 个都能在项目现有文件里找到出处
    import re
    sources = {
        "city_name": ["domain/conversation.py", "integrations/liangpiao/api.py"],
        "cinema_name": ["integrations/liangpiao/api.py"],
        "movie_name": ["integrations/liangpiao/api.py"],
        "hall_name": ["integrations/liangpiao/api.py"],
        "seats": ["domain/conversation.py"],
    }
    missing = []
    for name, files in sources.items():
        found = []
        for f in files:
            p = ROOT / f
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8")
            # ★ 不能用 ^...$ 配 re.M —— 这仓库里有的文件是
            #   "单行 Python 字符串"，其中的 \n 是字面两字符、不是真换行。
            #   踩过：domain/conversation.py 的 seats 声明就在这种单行块里，
            #   ^...$ 永远匹配不到。
            if re.search(r'(?<![\w])' + name + r'\s*[:=]\s*\S', text):
                found.append(f)
        ok = bool(found)
        print(f"  {'✓' if ok else '★'} {name:<16}{found or '★ 找不到出处'}")
        if not ok:
            missing.append(name)
    check("★ 每个业务字段名都有出处（不是随手起的）", not missing, str(missing))

    section("结果")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} 项通过")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
