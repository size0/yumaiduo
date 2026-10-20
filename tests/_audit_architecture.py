"""
_audit_architecture.py —— 架构审计

════════════════════════════════════════════════════════════════
★ 它查什么

  1. 分层是否被违反（domain 不该依赖 integrations / application）
  2. 有没有循环依赖
  3. 有没有孤儿模块（没人 import，也没在用）
  4. 有没有重复实现的模块（两套算价、两套状态机）
  5. 文件大小分布 —— 找出该拆的

★ 为什么用程序查而不是眼看

  14,000 行 / 40 个文件，靠读是读不完的；
  而"谁 import 了谁"这件事程序一眼就能算出来。
════════════════════════════════════════════════════════════════
"""

import ast
import sys
from collections import defaultdict
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass



ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {"__pycache__", ".git", "data", "web", "context", "prompts"}

# ★ 分层契约：谁可以依赖谁
#   读法：键是层，值是"允许被它依赖的层"
LAYERS = {
    "domain":          set(),                       # 最内层，谁也不许依赖
    "integrations":    set(),                       # 外部世界，只能自己站着
    "application":     {"domain", "integrations"},  # 编排：两边都能用
}
# 顶层模块（settings/templates/qa/server 等）不属于任何层，允许被依赖


def py_files():
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        if any(s in p.parts for s in SKIP_DIRS):
            continue
        if p.name.startswith("_"):          # 自检脚本不算架构的一部分
            continue
        out.append(p)
    return out


def stray_scratch_files() -> list:
    """
    ★★ 找出【根目录上的下划线草稿文件】。

    ★ 为什么需要这个：py_files() 把所有 `_` 开头的文件一律跳过
      （本意是跳过 tests/ 里的自检脚本和 __init__.py），
      于是根目录上的草稿文件在架构审计里【完全看不见】——
      它不被 import（所以孤儿检查不会报）、不被解析（所以分层检查也漏）。

      踩过：`_std_agent.py`（206 行）在根目录躺了很久没人发现，
      里面用的还是改名前的字段名（city / selected_seats）。

    ★ 判据：根目录（不在任何子目录）的 `_*.py`，且不是 __init__.py。
      这类文件要么是没登记的草稿，要么应该挪进 tests/。
      自检脚本一律在 tests/ 下 —— 所以在根目录出现就是可疑的。
    """
    out = []
    for p in sorted(ROOT.glob("_*.py")):
        if p.name == "__init__.py":
            continue
        out.append(p)
    return out


def module_name(path: Path) -> str:
    return str(path.relative_to(ROOT).with_suffix("")).replace("\\", ".").replace("/", ".")


def layer_of(path: Path) -> str | None:
    first = path.relative_to(ROOT).parts[0]
    return first if first in LAYERS else None


def imports_of(path: Path) -> set:
    """
    取出一个文件 import 了哪些【本项目内部】的模块。

    ★ 用 ast 而不是正则 —— 正则会把注释里的 import 也算进去。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        print(f"  ★ 语法错误 {path}: {exc}")
        return set()

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
                # `from domain import quote_rules` 也要能算到 domain.quote_rules
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
            else:
                # ★★ 2026-10-14 修审计自己的漏洞：
                #   `from . import manual_control` 这种【没有 module】的
                #   相对 import 原来【完全不记】——
                #   ⇒ 用它引用的模块会被误报成"孤儿模块"。
                #   （我已经中过一次：manual_control 就是这么被误报的。
                #     当时我第一反应是"再加一个 import 把警告压掉"，
                #     但那是在绕开审计，不是修问题。）
                #   ★ level 表示往上几层；这里按"同包兄弟模块"处理，
                #     够用且不会误伤别处。
                for alias in node.names:
                    found.add(alias.name)
    return found


def resolve(dep: str, mods: dict) -> str | None:
    """
    把一个 import 名解析成项目内的模块全名。

    ★ 三种写法都要能对上（上一个版本就错在这里）：
        import settings                     → settings
        from domain.conversation import X   → domain.conversation
        from domain import quote_rules      → domain.quote_rules
    """
    if dep in mods:
        return dep
    # 前缀形式：domain.quote_rules
    for name in mods:
        if name == dep or name.endswith("." + dep):
            return name
    # alias 形式：from domain import quote_rules → 最后一段匹配
    tail = dep.split(".")[-1]
    for name in mods:
        if name.split(".")[-1] == tail:
            return name
    return None


def main() -> int:
    files = py_files()
    mods = {module_name(p): p for p in files}
    top_names = {m.split(".")[0] for m in mods}

    print("=" * 76)
    print("一、模块清单与分层")
    print("=" * 76)
    by_layer = defaultdict(list)
    for name, path in mods.items():
        by_layer[layer_of(path) or "(顶层)"].append(name)
    for layer in ["domain", "integrations", "application", "(顶层)"]:
        names = by_layer.get(layer, [])
        print(f"  {layer:<16} {len(names)} 个: {', '.join(sorted(names))}")

    print()
    print("=" * 76)
    print("二、★ 分层违规（内层不许依赖外层）")
    print("=" * 76)
    violations = []
    for name, path in mods.items():
        src_layer = layer_of(path)
        if src_layer is None:
            continue
        for raw in imports_of(path):
            target = resolve(raw, mods)
            if target is None:
                continue
            dep = target
            dep_path = mods.get(target)
            dep_layer = layer_of(dep_path) if dep_path else None
            if dep_layer is None:
                continue
            # 内层依赖外层 = 违规
            order = {"domain": 0, "integrations": 1, "application": 2}
            if order.get(dep_layer, 9) > order.get(src_layer, 9):
                violations.append(f"{name} ({src_layer}) → {dep} ({dep_layer})")
    if violations:
        for v in violations:
            print(f"  ✗ {v}")
    else:
        print("  ✓ 没有违规：domain 不依赖 integrations/application")

    print()
    print("=" * 76)
    print("三、★ 循环依赖")
    print("=" * 76)
    graph = {}
    for name, path in mods.items():
        deps = set()
        for raw in imports_of(path):
            target = resolve(raw, mods)
            if target and target != name:
                deps.add(target)
        graph[name] = deps

    cycles = []

    def find_cycles(start, node, path, seen):
        for nxt in graph.get(node, ()):
            if nxt == start and len(path) > 1:
                cycles.append(path + [nxt])
            elif nxt not in seen:
                find_cycles(start, nxt, path + [nxt], seen | {nxt})

    for name in graph:
        find_cycles(name, name, [name], {name})
    # 去掉反向重复
    uniq = []
    for c in cycles:
        key = tuple(sorted(c[:-1]))
        if key not in [tuple(sorted(u[:-1])) for u in uniq]:
            uniq.append(c)
    if uniq:
        for c in uniq[:10]:
            print(f"  ✗ {' → '.join(c)}")
    else:
        print("  ✓ 没有循环依赖")

    print()
    print("=" * 76)
    print("四、★ 孤儿模块（内部没人 import）")
    print("=" * 76)

    # ★ 判断"被引用"要比对【完整模块名】和【顶层包名】两种形式：
    #   `from domain.conversation import X`  → 我记的是 "domain.conversation"
    #   `import settings`                    → 我记的是 "settings"
    #   早先只取 base（domain.conversation → domain），结果全部误报成孤儿。
    referenced = set()
    for deps in graph.values():
        referenced |= deps

    # ★ 三类【预期内】的无人引用，不算孤儿：
    #   1. 入口文件（被系统调用，不被模块 import）
    #   2. demos/  —— 教学演示，手动跑，本来就不被引用
    #   3. tests/  —— 自检脚本，同样是手动跑
    #   判据写在这里而不是散在打印语句里，是为了让"什么算孤儿"这件事可审。
    #
    #   ★ 早先还有一类 legacy/（从 V5 搬来的参考模块）。
    #     按「V5 只作参考、不得引用」的规则已经删掉了，
    #     所以这里不再为它留例外 —— 留例外等于给它留后门。
    # ★ 入口文件：被【人】启动，不被模块 import。
    #   chat.py 是本地对话测试工具（python chat.py 直接跑）。
    ENTRIES = {"server", "run_checks", "chat", "chatex", "simulate", "simulate2", "simulate_ctx", "replay_case", "replay_case2"}
    INTENTIONALLY_UNREFERENCED = ("demos.", "yumaiduo.shadow.")
    # ★★ 待接入：模块写完了但【还没接进 application/】。
    #   这些不是"预期内不引用"，是【已知的未完成工作】——
    #   所以要单独报出来，带原因，而不是混进 demos/ 那种正常例外里。
    #
    #   ★ 为什么不让它们直接算孤儿：算孤儿的话每次体检都报红，
    #     人就习惯了，真正的孤儿出现时看不见。
    #   ★ 为什么不让它们静默通过：静默通过会让"写了但没人用"
    #     烂在那里 —— 这正是 _check_params 里说的"名字存在、实现不存在"。
    #   ⇒ 折中：报出来，标"待接入"，附原因。它不该长期留在这个名单里。
    PENDING_INTEGRATION = {
        # ★ 2026-10-13：删掉了 integrations.wanda 的 api/client/signature/matcher
        #   （直连万达官方那一套，1756 行 + 189 项测试）。
        #   原因见整合进 integrations/wanda/__init__.py 的架构说明：
        #   我们走【出票系统的插件桥】，不自己调万达 ——
        #   主要是账号租约（一个号同时只允许一个订单占用）只能在
        #   出票系统进程内生效，直连会绕过它。
        #   ⇒ 那四个模块从"待接入"变成"已删除"，不再是待接入项。
        # ★ 出票交付（店家在出票后台手动出票 → 系统自动发码 + 发货）。
        #   ★ 已经写完、有 37 项测试覆盖，但【等出票后台那个调用方】——
        #     它由后台侧实例化（WandaDelivery(order_store)），
        #     现在还没有那个侧，所以不该算孤儿，也还不能说"已接入"。
        "application.wanda_delivery":
            "万达出票交付。★ 待接入：等店家出票后台那个调用方 —— "
            "submit/send/queue_followups 链路已有 37 项测试覆盖",
        # ★ 2026-10-13：万达成本解析层。已经写完、32 项测试覆盖、
        #   并用真实数据端到端验过（映射 350900/7261 → 80/379 →
        #   会员价 50.26 → 规则引擎 → 售价 51.26），
        #   但【还差最后一步接线】：接进 ticket_flow 的报价主链需要
        #   「用万达影院 ID 取万达 showtimeId」，而桥的白名单里
        #   还没有 /api/showtime/by-cinema（路由已存在，我们已发需求）。
        #   ⇒ 不是"写了没人用"，是"等一个外部接口"。
        #   该接口一加进来，本项立即从名单移除（那时它就被 ticket_flow 引用了）。
        "application.wanda_cost":
            "万达成本解析（渠道判定 + 会员价）。★ 待接入："
            "接进 ticket_flow 报价主链（桥已全部开通，正在接线）",
        "application.wanda_showtime":
            "万达场次解析（自己滤日期 / 影片+时间匹配）。★ 待接入：同上",
        # ★ 2026-10-13：以下不是模块，是【运维工具】——
        #   用 `python xxx.py` 直接跑，设计上就不该被 import。
        #   · check_bridge_drift.py    线上 vs 文档 的动态漂移核对
        #     （和出票系统那边 11 条静态测试互补的那条腿）
        "check_bridge_drift":
            "运维工具（命令行直接跑）：出票系统开放平台的动态漂移核对。"
            "★ 不该被 import —— 它要凭证、要打线上，不能进普通体检",
    }
    # ★★ 被【包级再导出】的子模块，不算孤儿。
    #   判据：父包的 __init__.py 里 `from .xxx import ...` 了它。
    #   ★ 为什么要单独一类：这种引用在模块级 import 图上【看不见】——
    #     调用方写 `from domain.quote import QuoteStore`，
    #     引用的是包 domain.quote，不是模块 domain.quote.store。
    #     早先没这条判据，domain.quote.store / models、domain.order.store
    #     全被误报成孤儿（实际天天在用）。
    def is_package_reexported(name: str) -> bool:
        parts = name.split(".")
        if len(parts) < 2:
            return False
        pkg_init = ROOT.joinpath(*parts[:-1], "__init__.py")
        if not pkg_init.exists():
            return False
        text = pkg_init.read_text(encoding="utf-8", errors="replace")
        return f"from .{parts[-1]} import" in text

    orphans = []
    expected = []
    pending = []
    for n in mods:
        if n in referenced or n in ENTRIES:
            continue
        if n in PENDING_INTEGRATION:
            pending.append(n)
        elif is_package_reexported(n):
            expected.append(n)
        elif n.startswith(INTENTIONALLY_UNREFERENCED):
            expected.append(n)
        else:
            orphans.append(n)

    if pending:
        for n in sorted(pending):
            print(f"  ★ 待接入  {n:<40}{PENDING_INTEGRATION[n]}")
    if orphans:
        for o in sorted(orphans):
            n = len(mods[o].read_text(encoding="utf-8").splitlines())
            print(f"  ⚠ {o:<36} {n:>4} 行  ← 没有任何生产代码引用它")
    else:
        print("  ✓ 没有意外的孤儿模块")
    if expected:
        print(f"  · {len(expected)} 个预期内不引用（demos/ 演示）："
              f"{', '.join(sorted(expected))}")

    # ★★ 根目录上的下划线草稿 —— py_files() 看不见它们，单独查。
    #   踩过：_std_agent.py（206 行，用的是改名前的老字段名）在根目录躺了很久。
    stray = stray_scratch_files()
    if stray:
        print()
        print("  ★ 根目录上有下划线草稿文件（架构审计默认看不见这类）：")
        for p in stray:
            n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            print(f"      ⚠ {p.name:<32} {n:>4} 行  ← 是草稿就删，是自检就挪到 tests/")
    else:
        print("  ✓ 根目录没有下划线草稿文件")

    # 单独说明：被谁引用
    print()
    print("  各模块被谁引用：")
    for name in sorted(mods):
        users = sorted(k for k, deps in graph.items() if name in deps)
        mark = "✓" if users else "⚠ 无人引用"
        print(f"    {name:<38} ← {', '.join(users) if users else '（无）'}  {mark}")

    print()
    print("=" * 76)
    print("五、★ 重复实现（同一个概念两套代码）")
    print("=" * 76)
    suspects = [
        ("算价", ["domain/quote_rules.py", "legacy/pricing.py"]),
        ("状态机", ["domain/conversation.py", "legacy/stages.py"]),
        ("报价", ["domain/quote_rules.py", "legacy/quote.py"]),
        ("订单/识别", ["integrations/liangpiao/api.py", "legacy/orders.py"]),
    ]
    for label, paths in suspects:
        exist = [p for p in paths if (ROOT / p).exists()]
        if len(exist) > 1:
            flags = []
            for p in exist:
                key = p.replace("/", ".").replace(".py", "")
                used = any(key in deps for deps in graph.values())
                n = len((ROOT / p).read_text(encoding="utf-8").splitlines())
                flags.append((p, used, n))
            live = [p for p, u, _ in flags if u]
            dead = [p for p, u, _ in flags if not u]
            if not dead:
                print(f"  ⚠ {label}：两套都在用 ← 需要人裁决")
            else:
                print(f"  · {label}：{len(live)} 套在用，{len(dead)} 套是参考")
            for p, u, n in flags:
                mark = "在用" if u else "参考"
                print(f"      [{mark}] {p:<34} {n:>4} 行")
    print("  ★ 「参考」的那些已归入 legacy/，不在生产链路上")

    print()
    print("=" * 76)
    print("六、文件大小（找该拆的）")
    print("=" * 76)
    sizes = []
    for name, path in mods.items():
        n = len(path.read_text(encoding="utf-8").splitlines())
        sizes.append((n, name))
    sizes.sort(reverse=True)
    for n, name in sizes[:12]:
        bar = "█" * max(1, n // 25)
        flag = "  ← 偏大" if n > 600 else ""
        print(f"  {n:>5} 行  {name:<36} {bar}{flag}")

    print()
    print("=" * 76)
    print("七、自检脚本的分布")
    print("=" * 76)
    checks = sorted(p.name for p in ROOT.glob("_*.py"))
    demos = [c for c in checks if c.startswith(("_demo", "_selftest", "_audit"))]
    tests = [c for c in checks if c.startswith("_check")]
    print(f"  根目录下自检脚本 {len(checks)} 个：")
    print(f"    _check_*   {len(tests)} 个（断言型）")
    print(f"    _demo/_selftest/_audit  {len(demos)} 个（演示型）")
    print(f"  → 建议收进 tests/ 目录，根目录只留 run_checks.py")

    print()
    print("=" * 76)
    print("七、目录与文件分布")
    print("=" * 76)
    dirs = {}
    for p in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(ROOT)
        top = rel.parts[0] if len(rel.parts) > 1 else "(根目录)"
        dirs.setdefault(top, []).append(p)
    for top, files in sorted(dirs.items()):
        total = sum(len(f.read_text(encoding="utf-8").splitlines()) for f in files)
        names = ", ".join(sorted(f.name for f in files)[:6])
        more = f" …共 {len(files)} 个" if len(files) > 6 else ""
        print(f"  {top:<16} {len(files):>2} 个文件 {total:>5} 行   {names}{more}")

    print()
    print("=" * 76)
    print("汇总")
    print("=" * 76)
    print(f"  Python 模块      {len(mods)}")
    print(f"  分层违规         {len(violations)}")
    print(f"  循环依赖         {len(uniq)}")
    print(f"  孤儿模块         {len(orphans)}"
          + ("（都在 legacy/，属预期）" if orphans and
             all(o.startswith("legacy") for o in orphans) else ""))
    print(f"  超大文件(>600行) {len([1 for n, _ in sizes if n > 600])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
