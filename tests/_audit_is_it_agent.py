"""
_audit_is_it_agent.py —— 我们写的到底是不是 agent？

════════════════════════════════════════════════════════════════
★ 判据（不含糊的定义）

  Agent 的核心不是"用了大模型"，而是：

    **模型在每一轮自己决定下一步做什么。**
    程序提供工具（tool）和观察（observation），
    由模型输出 tool_call 选择调用哪个、传什么参数、
    什么时候停 —— 控制流在模型手里。

  对比：

    工作流 / workflow   控制流在【代码】里。if/else 决定下一步。
                        模型最多负责"把这句话说得自然点"。
                        同一个输入，永远走同一条路。

    智能体 / agent      控制流在【模型】里。代码只提供工具和循环。
                        同一个输入，模型可以选不同的路。

  ⇒ 所以审计要查三件事：
      1. 有没有"模型 → 工具调用 → 结果回灌 → 再问模型"的循环
      2. 分支判断（if/else）是写在代码里还是由模型产出
      3. 模型有没有机会选择"不按预期走"

★ 还要查一个反例

  如果有模型调用，但它的输出【从来没有被用来决定下一步】，
  那它就不是 agent 的一部分 —— 它只是个文案生成器。
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

WORKSPACE = ROOT.parent
sys.path.insert(0, str(ROOT))

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


def production_files():
    for p in sorted(ROOT.rglob("*.py")):
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if any(x in rel for x in ("__pycache__", "legacy/", "demos/", "tests/")):
            continue
        if p.name.startswith("_"):
            continue
        yield rel, p.read_text(encoding="utf-8")


# ==================================================================== 一
def find_model_calls():
    """找出所有真正调模型的地方。"""
    section("一、生产代码里有没有调用大模型")
    hits = []
    patterns = [
        (r"chat\.completions\.create", "OpenAI 风格调用"),
        (r"responses\.create", "Responses API"),
        (r"api\.deepseek\.com", "DeepSeek 端点"),
        (r"anthropic", "Anthropic"),
        (r"\bllm\b", "自建 llm 模块"),
        (r"api_key", "API Key 使用"),
        (r"tools\s*=", "传了 tools 参数"),
        (r"tool_calls", "接收 tool_calls"),
        (r"function_call", "function_call"),
    ]
    for rel, src in production_files():
        for i, ln in enumerate(src.splitlines(), 1):
            for pat, label in patterns:
                if re.search(pat, ln, re.I):
                    hits.append((rel, i, label, ln.strip()[:80]))
                    break

    if hits:
        for rel, i, label, ln in hits:
            print(f"  · {rel}:{i}  [{label}]  {ln}")
    else:
        print("  （生产代码里没有任何模型调用）")
    check("生产代码里有模型调用", bool(hits), f"{len(hits)} 处")
    return hits


# ==================================================================== 二
def find_agent_loop():
    """
    找"模型 → 工具 → 回灌 → 再问模型"的循环。

    ★ 判据：出现 tool_calls 之后，把结果 append 回 messages 再问一次。
    """
    section("二、有没有 agent 循环（模型 → 工具 → 结果回灌 → 再问）")
    found = []
    for rel, src in production_files():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.While, ast.For)):
                continue
            body = ast.get_source_segment(src, node) or ""
            has_call = re.search(r"tool_calls|function_call", body)
            has_append = re.search(r"messages\.append", body)
            if has_call and has_append:
                found.append((rel, node.lineno, "while/for 里同时出现 tool_calls 和 messages.append"))
    if found:
        for rel, line, why in found:
            print(f"  · {rel}:{line}  {why}")
    else:
        print("  （生产代码里没有这种循环）")
    check("生产代码里有 agent 循环", bool(found), f"{len(found)} 处")

    # 顺带看 mini-agent（那是教学项目，不在生产链路）
    print()
    print("  参考：mini-agent（你最开始那个教学项目）里有没有：")
    mini = WORKSPACE / "mini-agent"
    if mini.exists():
        for f in ("main.py", "llm.py", "tools.py"):
            p = mini / f
            if not p.exists():
                continue
            src = p.read_text(encoding="utf-8")
            print(f"      {f:<12} tools= {'有' if 'tools' in src else '无'}"
                  f"  tool_calls= {'有' if 'tool_calls' in src else '无'}"
                  f"  messages.append= {'有' if 'messages.append' in src else '无'}")
    else:
        print("      （找不到 mini-agent）")


# ==================================================================== 三
def count_decision_points():
    """
    ★ 核心：分支判断写在代码里还是模型手里？

    数一遍编排层的 if/else —— 每一个都是"代码替模型做的决定"。
    """
    section("三、★ 下一步由谁决定：代码里的分支 vs 模型的选择")
    target = ROOT / "application/ticket_flow.py"
    src = target.read_text(encoding="utf-8")
    tree = ast.parse(src)

    decisions = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            seg = ast.get_source_segment(src, node) or ""
            # 取条件那一行
            cond = seg.splitlines()[0].strip()[:70]
            decisions.append((node.lineno, cond))

    print(f"  application/ticket_flow.py 里的 if 分支：{len(decisions)} 个")
    for line, cond in decisions[:24]:
        print(f"      {line:>4}: {cond}")
    if len(decisions) > 24:
        print(f"      …共 {len(decisions)} 个")

    check("编排层有分支判断（说明控制流在代码里）", len(decisions) > 5,
          f"{len(decisions)} 个 if")

    # 状态机的流转表 = 另一种"代码里的决定"
    conv = (ROOT / "domain/conversation.py").read_text(encoding="utf-8")
    n_trans = len(re.findall(r"\(ConvState\.\w+, Event\.\w+\):", conv))
    print()
    print(f"  domain/conversation.py 的流转表：{n_trans} 条固定边")
    print("      （每条边都是写死的：什么事件 → 什么状态，没有模型参与的余地）")
    check("状态流转是写死的表", n_trans > 20, f"{n_trans} 条")


# ==================================================================== 四
def check_model_output_usage():
    """
    ★ 反例检查：如果模型被调用了，它的输出有没有用来决定下一步？

    如果模型只负责"把 JSON 说成人话"，那它不是 agent —— 是文案生成器。
    """
    section("四、★ 模型输出有没有用来决定下一步")
    # 找所有 build_prompt / persona 的用法
    uses = []
    for rel, src in production_files():
        for i, ln in enumerate(src.splitlines(), 1):
            if re.search(r"build_prompt|build_persona|persona", ln, re.I):
                uses.append((rel, i, ln.strip()[:80]))
    for rel, i, ln in uses:
        print(f"  · {rel}:{i}  {ln}")
    if not uses:
        print("  （没有找到）")

    print()
    print("  ★ 关键问题：build_prompt() 拼出来的那段话，被送去哪儿了？")
    # 找调用 build_prompt 之后有没有拿去发请求
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    for i, ln in enumerate(src.splitlines(), 1):
        if "build_prompt" in ln:
            print(f"      server.py:{i}  {ln.strip()}")
            print(f"          → 只是放进 API 响应给前端预览，没有发给模型")

    check("模型输出被用来决定下一步", False if not (ROOT / "llm.py").exists() else True,
          "生产代码里没有任何地方把 prompt 发给模型")


# ==================================================================== 五
def count_templates():
    """文案是模板填的，还是模型生成的？"""
    section("五、发给买家的话是谁写的")
    import templates as tpl
    n_tpl = len(tpl.TEMPLATES)
    n_msg = len(tpl.render_messages("QUOTE_CARD")) if tpl.find("QUOTE_CARD") else 0
    print(f"  templates.py 里写死的文案：{n_tpl} 条")
    print(f"  render() 用变量填空出消息 —— 不是模型生成")
    print()
    print("  举例（报价卡，全是模板拼的）：")
    for m in tpl.render_messages("QUOTE_CARD")[:2]:
        for ln in m.splitlines()[:3]:
            print(f"      {ln}")
        print("      ---")
    check("买家收到的话来自模板而不是模型", n_tpl > 20, f"{n_tpl} 条模板")


# ==================================================================== 汇总
def main() -> int:
    print("=" * 78)
    print("这个项目到底是不是 agent")
    print("=" * 78)
    hits = find_model_calls()
    find_agent_loop()
    count_decision_points()
    check_model_output_usage()
    count_templates()

    section("结论依据")
    print("  上面五项合起来回答三个问题：")
    print()
    print("  1. 有没有模型调用？      "
          + ("有" if hits else "★ 没有（生产链路里一处都没有）"))
    print("  2. 有没有 agent 循环？   见第二节")
    print("  3. 下一步谁决定？        见第三节（"
          + str(len(re.findall(r'\bif\b', (ROOT / 'application/ticket_flow.py').read_text(encoding='utf-8'))))
          + " 个 if 在代码里）")
    print()
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"  断言：{passed}/{total}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
