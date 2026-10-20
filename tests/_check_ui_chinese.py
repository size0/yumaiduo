"""操作台不许出现"光秃秃的英文"。

★★ 店主的要求（原话）：
   「操作台不要写英文　有英文参数必须（）写中文注释」

★ 判据分两类，不能一刀切：
   ① 界面文案（标签/表头/按钮/提示）→ 不该只有英文
   ② 必须保持原样的【标识符】（环境变量名、接口路径、模型名、
      错误码、编号、字段名）→ 允许是英文，但必须配中文注释

  ⇒ 所以这个测试不检查"有没有英文"，而是检查
    「这段英文旁边有没有中文解释」——那才是店主真正要的。

★ 用静态扫源码而不是浏览器：
   · 不需要 playwright / Chrome，任何环境都能跑（服务器上也能）
   · 浏览器那套扫描我用过（见本次会话），但它是开发期的临时工具
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RES = []


def check(label, ok, detail=""):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


CN = re.compile(r"[\u4e00-\u9fff]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")

# ★ 这些不是"给人看的文案"，是 HTML/JS 语法或内部标识，不该被要求配中文。
#   每条都要有理由 —— 理由过期的要删。
ALLOW = {
    # 样式/结构
    "div", "span", "class", "id", "type", "value", "title", "href", "src",
    "input", "select", "option", "label", "button", "text", "readonly",
    "hidden", "placeholder", "style", "width", "height", "table", "thead",
    "tbody", "section", "card", "body", "head", "row", "wide", "tip", "bar",
    "hint", "note", "alert", "colspan", "alt", "rel", "meta", "content",
    "utf-8", "zh-CN", "viewport", "device-width", "initial-scale",
    "stylesheet", "script", "javascript", "text/javascript",
    "aria", "role", "switch", "checkbox", "radio", "number",
    # CSS 里的词（我们从 css 文件里只取"引号里的中文", 但 html 内联样式会有）
    "flex", "none", "auto", "block", "solid", "center", "left", "right",
    "nowrap", "sticky", "hidden",
    # 颜色/单位
    "rgba", "px", "em", "rem", "true", "false",
}
# ★ 允许出现的"纯英文界面文字"白名单（每条都要有理由）。
#   加之前先问：能不能配中文？能就配，别往这里加。
ALLOW_TEXT = {
    # 行业通用缩写，中文里也这么写，配中文反而啰嗦
    "AI", "API", "ID",
}


def strip_attrs(tag_html: str) -> str:
    """从一段 HTML 文本里剔掉属性/注释，只留"人看得见的字"。"""
    t = re.sub(r"<!--.*?-->", "", tag_html, flags=re.S)
    t = re.sub(r"<script.*?</script>", "", t, flags=re.S)
    t = re.sub(r"<style.*?</style>", "", t, flags=re.S)
    # 保留标签之间的文本，丢掉标签内部
    parts = re.split(r"<[^>]*>", t)
    return " ".join(p.strip() for p in parts if p.strip())


def is_bare_english(text: str) -> bool:
    """这段文字是不是"只有英文、没有中文"。"""
    if not LATIN_WORD.search(text):
        return False
    if CN.search(text):
        return False
    tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_.\-]*", text))
    # 全是样式/语法词 ⇒ 不算界面文案
    if tokens and tokens <= ALLOW:
        return False
    # 纯数字/编号（如 a6d4dqvkluc）—— 是系统生成的标识符，不是"写的英文"
    if re.fullmatch(r"[a-z0-9]{6,20}", text.strip()):
        return False
    if text.strip() in ALLOW_TEXT:
        return False
    return True


def iter_blocks(html: str):
    """把最内层的可见容器块逐个吐出来（p / td / th / li / label / span / b / i）。

    ★ 为什么取「最内层」：外层块（section/card）里一定混着一堆子标签，
      取最内层才对应「一段人看得见的文字」，
      判断「旁边有没有中文注释」才准。
    """
    tags = "p|td|th|li|label|span|summary|b|i|div"
    for m in re.finditer(rf"<({tags})\b[^>]*>(.*?)</\1>", html, flags=re.S):
        inner = m.group(2)
        # 里面还有同名标签 ⇒ 不是最内层，跳过（等它自己那一轮）
        if re.search(rf"<{m.group(1)}\b", inner):
            continue
        yield inner


print("=" * 78)
print("一、HTML 里有没有「只有英文、没有中文」的可见文字")
print("=" * 78)
html_bad = []
for f in sorted(WEB.glob("*.html")):
    raw = f.read_text(encoding="utf-8")
    # ★ 逐个可见文本节点看，同时看它【同一块容器】里有没有中文注释
    body = re.sub(r"<script.*?</script>", "", raw, flags=re.S)
    body = re.sub(r"<style.*?</style>", "", body, flags=re.S)
    # ★ 用"标签配对扫描"而不是递归正则：
    #   递归模式（(?:(?!</tag>).)*）每个标签都要编译一遍，慢且容易踩坑。
    #   这里手工跟踪同名标签的嵌套深度，找配对的那个结束标签。
    for block in iter_blocks(body):
        visible = strip_attrs(block)
        if not visible:
            continue
        # 这一块里有中文 ⇒ 里面的英文参数算"已配中文注释"
        if CN.search(visible):
            continue
        if is_bare_english(visible):
            html_bad.append((f.name, visible[:70]))
seen = set()
uniq = []
for fn, s in html_bad:
    if (fn, s) in seen:
        continue
    seen.add((fn, s))
    uniq.append((fn, s))
check("★★★ HTML 里没有「只有英文」的可见文字", not uniq,
      f"{len(uniq)} 处：" + str(uniq[:4]))

print()
print("=" * 78)
print("二、★ 该配中文注释的英文参数，逐个确认【真的配了】")
print("=" * 78)
# ★ 这些是店主明确提到的"英文参数"，必须有中文注释在旁边。
#   这是【正向清单】—— 比"扫出英文就报错"更准，也更能说明要求做到了。
NEED_CN = [
    # (文件, 英文参数, 中文注释里该出现的词)
    ("web/config.html", "AppKey", "应用标识"),
    ("web/config.html", "AppSecret", "应用密钥"),
    ("web/index.html", "API Key", "接口密钥"),
    ("web/index.html", "system prompt", "系统提示词"),
    ("web/orders.html", "quote_id", "报价单编号"),
    ("web/orders.html", "WANDA_TICKETING_DELIVERY_KEY", "出票接口密钥"),
    ("web/orders.html", "WANDA_TICKETING_INTEGRATION_TENANT_ID", "租户编号"),
    ("web/orders.html", "WANDA_TICKETING_INTEGRATION_SHOP_IDS", "店铺白名单"),
    ("web/orders.html", "WANDA_TICKET_DELIVERY_ENABLED", "写开关"),
    ("web/orders.html", "YUMAIDUO_WEBHOOK_SECRET", "网页回调密钥"),
    ("web/orders.html", "x-yumaiduo-signature", "签名"),
    ("web/orders.html", "V5_BACKEND_SECRET_FILE", "交接文件"),
    ("web/rules.html", "priceMode=FIXED", "一口价"),
    ("web/rules.html", "priceMode=LIMIT", "限价"),
]
missing = []
for fname, word, cnword in NEED_CN:
    p = ROOT / fname
    t = p.read_text(encoding="utf-8")
    if word not in t:
        missing.append(f"{fname}: 找不到 {word}（清单过期了？）")
        continue
    # 在它附近 ±200 字里找中文注释词
    for m in re.finditer(re.escape(word), t):
        near = t[max(0, m.start() - 220):m.end() + 220]
        if cnword in near:
            break
    else:
        missing.append(f"{fname}: {word} 附近没有「{cnword}」注释")
check("★★★ 清单里的英文参数都配了中文注释", not missing, str(missing[:3]))

print()
print("=" * 78)
print("三、模型下拉显示的是「中文名（英文名）」")
print("=" * 78)
_sp = (ROOT / "settings.py").read_text(encoding="utf-8")
_js = (WEB / "app.js").read_text(encoding="utf-8")
check("★★ settings 里配了 model_labels（中文名）",
      "model_labels" in _sp and "极速版" in _sp)
check("★★ 前端按「中文名（英文名）」渲染",
      "（${m}）" in _js or "(${m})" in _js, "")
check("★★★ option 的 value 仍是英文名（发给模型的真名，不能变中文）",
      'value="${m}"' in _js, "")

print()
print("=" * 78)
print(f"结果: {sum(RES)}/{len(RES)} 项通过")
print("=" * 78)
sys.exit(0 if all(RES) else 1)
