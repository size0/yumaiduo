"""
_audit_v5_taint.py —— 查 V5 代码有没有被【未经授权地】引用进来

════════════════════════════════════════════════════════════════
★ 规则

  V5 的代码（E:\\200行的agent\\v5-prod-9bee216\\）默认只作【参考】，
  不得【引用】。需要什么自己写。

  ★★ 唯一的例外：yumaiduo/ —— 经用户明确授权
       「这里你可以抄 V5 的」
       「只限鱼麦多插件这一块」
       「拷进来 + 注明出处」

     出处写在 yumaiduo/PROVENANCE.md 里，精确到文件清单与 sha256。

     ★ 这个例外【写在代码里】，不是让审计睁一只眼闭一只眼 ——
       授权范围之外（良票接入、报价、状态机、文案、前端）照样要拦。
       例外一旦不写在明面上，就等于审计失灵。

★ 什么叫"引用"

  1. 直接 import V5 的模块
  2. 把 V5 的文件【拷进】本项目（哪怕改了名字）
  3. 文档里写着"移植自 V5"/"来自 V5"
  4. 代码注释里写"参照 V5 实现"

  ★ 第 2 条最隐蔽：它看起来是"我们自己的文件"，
    实际是把别人的代码搬进了自己的仓库。
    所以被授权的拷贝必须留下 PROVENANCE.md —— 说清哪些文件、从哪来、什么版本。

★ 它查什么

  · 本项目里有没有真的 import / 加载 V5
  · 有没有【未声明出处】的从 V5 拷来的文件
  · 注释/文档里有没有把 V5 当来源的写法
════════════════════════════════════════════════════════════════
"""

import ast
import re
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

WORKSPACE = ROOT.parent                            # E:\200行的agent

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return bool(ok)


def section(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


# ==================================================================== 授权范围
# ★★ 经用户明确授权的 V5 拷贝范围 —— 写在明面上，不藏在判断里
#
#   授权原话：「这里你可以抄 V5 的」
#             「只限鱼麦多插件这一块」
#             「拷进来 + 注明出处」
#
#   ★ 为什么要有这个常量：
#     例外一旦散在代码各处，就等于审计失灵 —— 下次没人知道
#     "为什么这里可以有 V5 的代码"。集中写一处，改的时候必须动这里。
AUTHORIZED = {
    "yumaiduo/": {
        "source": "v5-prod-9bee216/plugin-runtime/",
        "declaration": "yumaiduo/PROVENANCE.md",
        "date": "拷贝时一次写定",
    },
}


def is_authorized(rel: str) -> str | None:
    """这个相对路径属于哪个授权范围？不属于就返回 None。"""
    for prefix, info in AUTHORIZED.items():
        if rel.replace("\\", "/").startswith(prefix):
            return prefix
    return None


def project_files():
    """本项目所有文件（不含 __pycache__，不含授权拷贝范围）。

    ★ 两处要排除：
      1. 审计脚本自己 —— 它的 docstring 里必然写着 "V5"，不排除就自己举报自己。
         这不是作弊：检查器不是被检查的对象。
      2. 授权拷贝范围（yumaiduo/）—— 它由 PROVENANCE.md 单独管，
         见 check_authorized_provenance()。
    """
    SELF = {"tests/_audit_v5_taint.py"}
    # ★ node_modules 也要排除：那是 npm 装的第三方包，不是我们的代码
    SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if any(s in p.parts for s in SKIP_DIRS):
            continue
        if p.suffix not in (".py", ".js", ".html", ".css", ".md", ".json",
                            ".mjs", ".ts"):
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel in SELF:
            continue
        if is_authorized(rel):
            continue
        yield p


def check_authorized_provenance():
    """
    ★ 授权拷贝必须留下出处文档，而且文档里要能对得上。

    有授权、没出处 = 等于没授权 —— 下一个人不知道这些文件从哪来、能不能改。
    """
    section("零、★ 授权拷贝范围的出处（yumaiduo/）")
    ok = True
    for prefix, info in AUTHORIZED.items():
        root = ROOT / prefix.rstrip("/")
        decl = ROOT / info["declaration"]
        if not root.exists():
            print(f"  · {prefix} 不存在（没拷或已删）")
            continue
        n_files = len([p for p in root.rglob("*")
                       if p.is_file() and "node_modules" not in p.parts])
        print(f"  · {prefix}  {n_files} 个文件（不含 node_modules）")
        print(f"      来源: {info['source']}")
        if not decl.exists():
            print(f"      ⚠ 出处文档缺失: {info['declaration']}")
            ok = False
            continue
        text = decl.read_text(encoding="utf-8", errors="replace")
        # 出处文档必须写明来源目录
        has_source = info["source"].split("/")[0] in text
        print(f"      出处文档: {info['declaration']} "
              f"({len(text.splitlines())} 行)")
        print(f"      写了来源目录: {'是' if has_source else '★ 否'}")
        if not has_source:
            ok = False
    check("授权拷贝都留下了出处文档", ok, "" if ok else "有授权没出处")
    return ok


# ==================================================================== 一
def find_v5_dirs():
    section("一、V5 代码在哪")
    dirs = [d for d in WORKSPACE.iterdir()
            if d.is_dir() and re.match(r"v5[-_]?prod", d.name, re.I)]
    for d in dirs:
        n = len(list(d.rglob("*.py")))
        print(f"  · {d.name}/  （{n} 个 .py）—— 参考目录，不该被引用")
    if not dirs:
        print("  （没找到 v5-prod* 目录）")
    return dirs


# ==================================================================== 二
def find_path_references():
    """
    本项目里有没有【真的去用】V5 的路径。

    ★ 判据要区分两件事：
        `import v5_prod`            → 真引用，报
        `sys.path.insert(..., "v5-prod-9bee216")` → 真引用，报
        "V5 只作参考，不得引用"      → 说明文字，不报
        文档里写 V5 目录的完整路径   → 说明文字，不报

      早先把"任何提到路径的行"都算成引用，结果纪律本身被举报了。
      检查器的判据错了，比不检查更糟 —— 它会教人绕开它。
    """
    section("二、本项目里有没有真的去 import/加载 V5")
    hits = []
    for p in project_files():
        # markdown / html 是给人看的说明，不可能是"引用"
        if p.suffix in (".md", ".html", ".css"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            stripped = ln.strip()
            if stripped.startswith("#"):          # 注释里的说明不算
                continue
            # 真的 import / 动态加载
            if re.search(r"(^|\s)(import|from)\s+v5[-_]?prod", ln, re.I):
                hits.append((str(p.relative_to(ROOT)), i, ln.strip()[:90]))
            elif re.search(r"(sys\.path|importlib|__import__|open)\s*\(.*v5[-_]?prod", ln, re.I):
                hits.append((str(p.relative_to(ROOT)), i, ln.strip()[:90]))
    if hits:
        for rel, i, ln in hits:
            print(f"  ⚠ {rel}:{i}  {ln}")
    else:
        print("  ✓ 没有任何生产代码 import / 加载 V5")
    check("没有 import / 加载 V5 的代码", not hits, f"{len(hits)} 处")
    return hits


# ==================================================================== 三
def find_ported_files():
    """
    ★ 本项目里哪些文件是"从 V5 搬过来的"。

    判据：文件顶部声明了来源，或者文件名/类名与 V5 高度重合。
    """
    section("三、★ 本项目里有没有【从 V5 搬来】的文件")
    # 先列出 V5 的文件名，供比对
    v5_dirs = [d for d in WORKSPACE.iterdir()
               if d.is_dir() and re.match(r"v5[-_]?prod", d.name, re.I)]
    v5_names = set()
    for d in v5_dirs:
        for p in d.rglob("*.py"):
            v5_names.add(p.name)

    # 本项目里声明来源的
    declared = []
    same_name = []
    for p in project_files():
        if p.suffix != ".py":
            continue
        rel = str(p.relative_to(ROOT))
        text = p.read_text(encoding="utf-8", errors="replace")
        head = "\n".join(text.splitlines()[:40])
        if re.search(r"移植|port(ed)?\s+from|来自\s*V5|源自\s*V5|迁移自|从\s*V5\s*抽出", head, re.I):
            declared.append(rel)
        # ★ 同名【不算】问题：同一个业务写同样的文件名很正常
        #   （service.py / store.py 这种通用名，V5 里也必然有）。
        #   真正要判的是内容，见第五节。
        if p.name in v5_names and p.name not in ("__init__.py",):
            same_name.append((rel, p.name))

    if declared:
        for rel in declared:
            print(f"  ⚠ {rel}  ← 文件头声明了来源（等于说「这段是别人的」）")
    else:
        print("  ✓ 没有文件声明「移植/来自 V5」")

    print()
    print(f"  同名文件 {len(same_name)} 个（★ 同名不等于搬运，仅列出）：")
    for rel, name in same_name:
        print(f"      {rel}  ↔ V5 的 {name}")

    # ★ 断言只针对"声明了来源"这一类 —— 同名不算证据
    check("没有文件声明从 V5 移植", not declared, f"{len(declared)} 个")
    return declared, same_name


# ==================================================================== 四
def find_textual_references():
    """
    注释/文档里提到 V5 的地方。

    ★ 判据：只报【把 V5 当来源】的写法（移植/原文/抽出/参照 V5），
      不报「V5 只作参考、不得引用」这类【纪律说明】——
      那正是我们希望大家看到的文字。

      `ev5` 这种变量名也不算 —— 词边界要卡住。
    """
    section("四、注释/文档里把 V5 当来源的地方")
    # 真正"把 V5 当来源"的写法：说某段代码是从 V5 来的
    CLAIM = re.compile(r"移植|从\s*V5\s*抽出|V5\s*原文|参照\s*V5|"
                       r"继承自\s*V5|来自\s*V5|V5\s*线上代码|V5\s*里就是这么")
    # ★ 描述【纪律】的写法：在说"不许引用/已经删了"。这是我们要的文字，不报。
    POLICY = re.compile(r"不得引用|只作参考|已删除|已经删|不许|看得，但不能搬|"
                        r"纪律|清掉|禁止")
    hits = []
    for p in project_files():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            if not CLAIM.search(ln):
                continue
            # ★ 判据顺序：先看它是不是在讲纪律。
            #   早先把这条放在后面，结果"V5 的代码看得但不能搬"被误报 ——
            #   那句话里的 "V5 的代码" 命中了 CLAIM，
            #   但它讲的是纪律，不是来源。
            if POLICY.search(ln):
                continue
            hits.append((str(p.relative_to(ROOT)), i, ln.strip()[:100]))
    if hits:
        for rel, i, ln in hits:
            print(f"  ⚠ {rel}:{i}  {ln}")
    else:
        print("  ✓ 没有把 V5 当来源的写法")
    check("没有把 V5 当来源的声明", not hits, f"{len(hits)} 处")
    return hits


# ==================================================================== 五
def scan_own_data_classes():
    """
    ★ 逐个确认我们没有"抄" V5 的数据结构。

    做法：把本项目 domain/ 的类名和字段名，与 V5 里同类模块比一比，
    重合度过高就值得人看一眼。
    """
    section("五、★ 数据结构重合度（逐个体检，不作断言）")
    v5_dirs = [d for d in WORKSPACE.iterdir()
               if d.is_dir() and re.match(r"v5[-_]?prod", d.name, re.I)]
    v5_src = {}
    for d in v5_dirs:
        for p in d.rglob("*.py"):
            try:
                v5_src[str(p.relative_to(d))] = p.read_text(encoding="utf-8",
                                                            errors="replace")
            except OSError:
                pass
    if not v5_src:
        print("  （没有 V5 源码可比）")
        return

    v5_classes = {}
    for rel, src in v5_src.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                fields = tuple(sorted(
                    s.target.id for s in node.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)))
                v5_classes[node.name] = (rel, fields)

    ours = []
    for p in sorted((ROOT / "domain").rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                fields = tuple(sorted(
                    s.target.id for s in node.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)))
                if fields:
                    ours.append((node.name, fields))

    for name, fields in ours:
        if name in v5_classes and fields:
            v5_rel, v5_fields = v5_classes[name]
            overlap = len(set(fields) & set(v5_fields))
            total = len(set(fields) | set(v5_fields))
            ratio = overlap / total if total else 0
            mark = "⚠ 高度重合" if ratio > 0.6 else "·"
            print(f"  {mark} {name}: 与 V5 的 {v5_rel} 重合 {overlap}/{total} ({ratio:.0%})")
    print("  ★ 类名/字段同名不一定算抄（业务概念本来就会同名），仅作提示")


# ==================================================================== 主流程
def main() -> int:
    print("=" * 78)
    print("V5 引用审计：有没有【未经授权】引用 V5 的代码")
    print("=" * 78)
    print()
    for prefix, info in AUTHORIZED.items():
        print(f"  ★ 已授权例外：{prefix}  ←  {info['source']}")
    print("     （授权出处见各自目录下的 PROVENANCE.md）")
    check_authorized_provenance()
    find_v5_dirs()
    find_path_references()
    declared, same = find_ported_files()
    find_textual_references()
    scan_own_data_classes()

    section("汇总")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"  结果：{passed}/{total} 项通过")
    if passed != total:
        print("  ★ 有【未经授权】的引用/搬运，需要处理")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
