"""
_check_deadcode.py —— 死代码检查（未用导入 / 未用变量 / 未用常量）

════════════════════════════════════════════════════════════════
★ 为什么需要这个

  重构过程中最容易留下的就是"搬走了但没删干净"：
    · 模块搬去了 store，server 里还 import 着 json
    · 函数删了，它专用的常量还在
  这些不影响运行，但会让下一个人以为它们有用。

★ 判据要保守

  只报【确定没用】的：顶层 import 的名字在文件里一次都没再出现。
  动态用法（getattr、字符串引用）一律不报 —— 宁可漏报也不误报。
════════════════════════════════════════════════════════════════
"""

import ast
import sys
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

SKIP = {"__pycache__", ".git", "data", "web"}
# 这些模块是"为了副作用/重导出"而导入的，不算死代码
REEXPORT_FILES = {"application/store.py", "server.py"}

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def unused_imports(path: Path) -> list:
    """找出导入了但没用的名字。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    imported = {}          # 名字 → 行号
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                name = (a.asname or a.name).split(".")[0]
                imported[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    continue
                imported[a.asname or a.name] = node.lineno
    if not imported:
        return []

    # ★ 把这些都算"用过"：
    #   · 作为名字出現（Name 节点）
    #   · 作为属性访问的主体（cb._to_int 里的 cb）—— Attribute 的 value
    #   · 出现在 f-string / 字符串里
    #   早先只查 Name，结果 `cb._to_int(...)` 这种被误报成死导入。
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for name in imported:
                if name in node.value:
                    used.add(name)

    lines = path.read_text(encoding="utf-8").splitlines()
    dead = []
    for name, lineno in imported.items():
        if name in used:
            continue
        # 兜底：import 行以外还有没有出现
        others = [i for i, ln in enumerate(lines, 1)
                  if i != lineno and name in ln]
        if others:
            continue
        dead.append(f"{name} (行 {lineno})")
    return dead


def main() -> int:
    print("=" * 76)
    print("死代码检查")
    print("=" * 76)

    production = []
    for p in sorted(ROOT.rglob("*.py")):
        if any(s in p.parts for s in SKIP):
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel.startswith(("legacy/", "demos/")):
            continue
        production.append((rel, p))

    print()
    print("一、未使用的导入")
    print("-" * 76)
    total_bad = 0
    for rel, p in production:
        bad = unused_imports(p)
        # ★ store.py / server.py 有"转发式导入"（load_x = store.load_x），
        #   那些名字确实不出现在本文件别处，属于有意为之。
        if rel in REEXPORT_FILES:
            bad = [b for b in bad if not b.startswith("store")]
        if bad:
            total_bad += len(bad)
            for b in bad:
                print(f"  ⚠ {rel:<42} {b}")
    if total_bad == 0:
        print("  ✓ 没有未使用的导入")
    check("没有未使用的导入", total_bad == 0, f"{total_bad} 处")

    print()
    print("二、顶层常量有没有被用到")
    print("-" * 76)
    # 收集所有模块定义的顶层大写常量
    defined = {}
    for rel, p in production:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        defined.setdefault(t.id, []).append((rel, node.lineno))
    # 全文搜索每个常量名
    all_src = {rel: p.read_text(encoding="utf-8") for rel, p in production}
    dead_consts = []
    for name, where in sorted(defined.items()):
        count = sum(src.count(name) for src in all_src.values())
        if count <= len(where):          # 只有定义处出现，没人用
            dead_consts.append((name, where[0]))
    if dead_consts:
        for name, (rel, line) in dead_consts:
            print(f"  ⚠ {name:<34} 定义于 {rel}:{line}")
    else:
        print("  ✓ 没有孤立常量")
    # 孤立常量只提示，不断言 —— 有些常量是给界面展示用的
    print(f"  （{len(dead_consts)} 个，仅提示）")

    print()
    print("三、语法自检（空文件跳过）")
    print("-" * 76)
    fails = []
    empty = []
    for rel, p in production:
        text = p.read_text(encoding="utf-8")
        # ★ 空文件 / 只有 docstring 的 __init__.py 跳过 ——
        #   早先对它们做 ast.parse 也报错，是检查器自己的 bug。
        if not text.strip():
            empty.append(rel)
            continue
        try:
            ast.parse(text)
        except SyntaxError as exc:
            fails.append(f"{rel}: {exc}")
    check("所有生产文件语法正确", not fails, str(fails))
    if empty:
        print(f"  （跳过 {len(empty)} 个空文件：{', '.join(empty)}）")

    print()
    print("=" * 76)
    passed, total = sum(RESULTS), len(RESULTS)
    print(f" 结果：{passed}/{total} 项通过")
    print("=" * 76)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
