"""
chat.py —— 本地对话测试（一句一句跟它聊）

════════════════════════════════════════════════════════════════
★ 它测什么

  对话层的行为 —— 也就是「买家说一句，它怎么回」：
    · 命中关键词 → 回哪条
    · 没命中     → 问模型，模型怎么答（★ 真调模型，要 Key）
    · 答不了     → 转人工
    · 询价/下单/退款这些动作有没有按流程走

  ★ 它**不测**良票/万达的真实接口（那些用 tests/_check_wanda_bridge.py、
    tests/_check_wanda_cost.py 等带假 transport 的套件测）。
    这里给良票配的是【假响应】，所以：
      · 不需要 AppKey/AppSecret
      · 不发真实请求、不花钱、不下单
      · 但流程、状态机、文案渲染都是真的

★ 怎么用

  python chat.py                  # 开始聊（直接输入就行）
  python chat.py --chat 张三       # 指定会话 id（默认随机）
  python chat.py --show-state     # 每轮之后打印完整会话状态

  对话里的特殊指令（都是本地命令，不会发出去）：
    /img <图片URL>   当作"买家发了这张选座截图"
    /state          看会话状态（进度/报价/订单/候选）
    /keys           看当前配了哪些关键词回复
    /notes          看重看上一轮的判定说明
    /reset          清空这个会话重来
    /quit           退出

★ 两个模式

  默认：假良票。截图识别拿不到真实结果，会走"识别失败"分支 ——
        这对测【对话】够用，而且不花钱。
  --live：用真凭证（data/liangpiao.json 里配的），真识别真询价。
        ★ 真调会花钱、有限频，只在需要时用。
════════════════════════════════════════════════════════════════
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

from application import store                                 # noqa: E402
from application.service import TicketService, build_service  # noqa: E402
from domain.quote_rules import QuoteRuleEngine, tianjin_default_rules  # noqa: E402
from integrations.liangpiao.api import LiangPiaoAPI           # noqa: E402
from integrations.liangpiao.client import LiangPiaoClient     # noqa: E402

KEY, SECRET = "FAKE-KEY", "FAKE-SECRET"


# ==================================================================== 假良票
class RoutingTransport:
    """
    按路径返回预设响应的假传输。

    ★ 从 tests/_check_service.py 借的做法 —— 那边已经调好了这些假响应，
      这里搬过来复用，避免"测试用一套假数据、工具用另一套"。
    """

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.sent = []

    def send(self, url, body, headers, timeout):
        self.sent.append({"url": url, "body": body})
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return 200, json.dumps({"code": 0, "message": "success",
                                "data": {}})


def fake_routes() -> dict:
    """
    假良票响应。

    ★ 关键：识别故意返回【匹配不到】—— 这样走 RECOGNIZE_FAIL 分支。
      目的是测对话（它怎么跟买家说"识别不出来"），
      而不是测识别准确率（那要真截图 + 真凭证）。
    """
    def envelope(data, code=0):
        return json.dumps({"code": code, "message": "success",
                           "requestId": "FAKE", "data": data})

    return {
        "/ping": (200, envelope({"channelId": 1, "env": "FAKE",
                                 "serverTime": "2026-01-01T00:00:00+08:00"})),
        "/city/list": (200, envelope({"list": [
            {"code": "370800", "name": "济宁", "pinyin": "jining",
             "initial": "J"}]})),
        "/cinema/list": (200, envelope({"total": 1, "page": 1, "pageSize": 20,
                                        "list": [
            {"cinemaId": 5433, "cinemaCode": "37070101",
             "name": "济宁影城", "cityCode": "370800", "cityName": "济宁",
             "brandName": "万达影城", "districtName": "市中区"}]})),
        "/show/dates": (200, envelope({"dates": [
            {"date": "2026-10-12", "showCount": 3}]})),
        "/show/list": (200, envelope({"list": [
            {"showId": "37333719", "cinemaId": 5433, "movieId": 856,
             "movieName": "神探之痕迹", "hallName": "二楼4号厅",
             "startTime": "2026-10-12T10:00:00+08:00",
             "endTime": "2026-10-12T12:20:00+08:00",
             "dimension": "2D", "language": "国语",
             "prices": [{"ticketMode": "STANDARD", "priceMode": "FIXED",
                         "price": "2369", "originalPrice": "2800",
                         "available": True}]}]})),
        # ★ 识别：故意不给 matchLevel —— 走识别失败分支
        "/recognize/seat-shot": (200, envelope({
            "recognizeId": None, "cached": False,
            "rawResults": {"isSeatSelection": False, "cinema": None},
            "finalResults": {"matchLevel": "NONE",
                             "noMatchReason": None, "candidates": {
                                 "cinemas": [], "movies": [], "shows": []}}})),
        "/order/preflight": (200, envelope({
            "available": True, "totalAmount": "2369",
            "estimateAmount": "2369", "marketAmount": "2800",
            "estimated": False})),
        "/order/create": (200, envelope({
            "orderNo": "LP-FAKE-001", "status": "SUBMITTING",
            "freezeAmount": "2369", "seatCount": 1, "duplicated": False})),
        "/order/detail": (200, envelope({
            "orderNo": "LP-FAKE-001", "status": "TICKETED",
            "refundable": True, "tickets": []})),
    }


# ==================================================================== 装配
def build_local(*, live: bool, chat_id: str, tel: str):
    """装配一个本地服务。live=True 用真凭证。"""
    store.load_template_overrides()
    store.load_qa_overrides()

    if live:
        cred = store.load_credentials()
        if not cred.get("app_key") or not cred.get("app_secret"):
            print("✗ --live 需要良票凭证。当前 data/liangpiao.json 里 "
                  "app_key / app_secret 是空的。")
            print("  先用界面「配置」页填上，或者不加 --live 用假良票。")
            return None
        svc = build_service(
            tel=cred.get("tel") or tel,
            app_key=cred["app_key"], app_secret=cred["app_secret"],
            base_url=cred.get("base_url") or None,
            model_api_key=cred.get("model_api_key") or "",
            persist=True)
        print("★ live 模式：真调良票（会花钱、有限频）")
    else:
        from application.conversation_ai import build_ai
        tr = RoutingTransport(fake_routes())
        client = LiangPiaoClient(KEY, SECRET,
                                 base_url="https://fake.local",
                                 transport=tr, sleep=lambda _: None)
        cred = store.load_credentials()
        # ★ TicketService 收的是 ai 对象，不是 key ——
        #   用 build_ai 造一个（和 build_service 内部一样的做法）
        ai = build_ai(api_key=cred.get("model_api_key") or "")
        svc = TicketService(
            LiangPiaoAPI(client), QuoteRuleEngine(tianjin_default_rules()),
            tel=tel, app_key=KEY, app_secret=SECRET, persist=False, ai=ai)
        print("★ 本地模式：良票是假的（不发真实请求、不下单）")
        # ★★ 必须说清后果，否则看到"识别失败"会以为系统坏了。
        #    这个坑真踩过：用户用本地模式发截图，看到识别失败，
        #    以为识别功能有问题 —— 其实是假数据本来就没场次。
        print("  ⚠ 所以【截图识别一定失败】—— 假数据里没有场次。")
        print("    要真识别、真报价，用：python chat.py --live")

    print(f"  会话 id  {chat_id}")
    print(f"  模型 Key {'已配' if svc.ai.client.ready else '★ 没配 —— 模型答话会走转人工'}")
    print()
    return svc


# ==================================================================== 显示
def show_reply(result, *, show_state: bool = False) -> None:
    """把这一轮的结果打出来 —— ★ 重点是把【它怎么判的】也露出来。"""
    print()
    if result.messages:
        for i, msg in enumerate(result.messages, 1):
            tag = f"[{i}]" if len(result.messages) > 1 else "  "
            print(f"  它会发 {tag} {msg}")
    else:
        print("  它会发：（这一轮不发消息）")

    # ★ 判定依据 —— 这是测试时最需要看的
    if result.template_keys:
        print(f"  用了模板  {result.template_keys}")
    if result.notes:
        for note in result.notes:
            print(f"  说明    {note}")
    print(f"  状态    {result.state.value}"
          + (f"   （要人接手）" if result.need_human else "")
          + (f"   error={result.error}" if result.error else ""))
    if result.quote:
        q = result.quote
        print(f"  报价    单张 {q.unit_price_fen/100:.2f} 元 × "
              f"{q.seat_count} 张 = {q.total_price_fen/100:.2f} 元"
              f"   priceMode={q.price_mode}"
              + (f"  maxPrice={q.max_price_fen/100:.2f} 元"
                 if q.max_price_fen else "")
              + ("   ★ 预估（非确定价）" if result.price_is_estimate else ""))
    if result.order_no:
        print(f"  订单    {result.order_no}")

    if show_state:
        print_state(result)


def print_state(result) -> None:
    """打印会话状态（/state 用）。"""
    print("  ── 会话状态 ──")
    for k in ("state", "quote", "pending_order", "candidates",
              "recognition_id", "city_name"):
        if hasattr(result, k):
            print(f"    {k}: {getattr(result, k)!r}"[:150])


# ==================================================================== 主循环
HELP = """
本地命令（不会发给买家）：
  /img <URL>   当作"买家发了这张选座截图"
  /state       看会话状态
  /keys        看当前关键词回复表
  /notes       重看上一轮的判定
  /reset       清空会话重来
  /quit        退出
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="本地对话测试")
    ap.add_argument("--chat", default="", help="会话 id（默认 local-1）")
    ap.add_argument("--live", action="store_true",
                    help="★ 用真良票凭证（会花钱）")
    ap.add_argument("--tel", default="13800000000", help="下单用手机号")
    ap.add_argument("--show-state", action="store_true",
                    help="每轮之后打印会话状态")
    args = ap.parse_args()

    chat_id = args.chat or "local-1"

    print("=" * 74)
    print("本地对话测试")
    print("=" * 74)
    svc = build_local(live=args.live, chat_id=chat_id, tel=args.tel)
    if svc is None:
        return 1
    print(HELP)

    last_result = None
    while True:
        try:
            line = input("买家> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        # ---- 本地命令 ----
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/notes":
            if last_result is None:
                print("  （还没有对话）")
            else:
                print(f"  模板 {last_result.template_keys}")
                for n in last_result.notes:
                    print(f"  {n}")
            continue
        if line == "/state":
            # ★ 直接从服务里取会话
            try:
                session = svc.session(chat_id)
                conv = session.conversation
                print(f"  进度      {conv.state.value}")
                print(f"  渠道      {conv.channel!r}")
                print(f"  城市      {conv.city_name!r}")
                print(f"  识别句柄  {conv.recognition_id!r}")
                print(f"  在途订单  "
                      f"{getattr(conv.pending_order, 'order_no', None)!r}")
                q = getattr(conv, "quote", None)
                print(f"  报价      "
                      + (f"{q.total_price_fen/100:.2f} 元" if q else "（无）"))
                cands = getattr(conv, "last_candidates", {}) or {}
                print(f"  候选      "
                      + (f"{ {k: len(v) for k, v in cands.items()}}"
                         if cands else "（无）"))
                hist = getattr(session, "history", [])
                print(f"  对话历史  {len(hist)} 条")
            except Exception as exc:                    # noqa: BLE001
                print(f"  取状态失败：{type(exc).__name__}: {exc}")
            continue
        if line == "/keys":
            import qa as qa_module
            print(f"  关键词回复 {len(qa_module.QA_RULES)} 条：")
            for rule in qa_module.QA_RULES:
                print(f"    {rule.keyword:<12} → "
                      f"{(rule.content or '')[:60]}")
            continue
        if line == "/reset":
            svc.reset(chat_id) if hasattr(svc, "reset") else None
            print(f"  （会话 {chat_id} 已清空 —— 换个 id 更干净："
                  f"重启时用 --chat 换一个）")
            continue

        # ---- 图片 ----
        #
        # ★★ 命令解析必须【严格】：认不出的斜杠命令要明确报错，
        #    不能当买家消息发出去。
        #    踩过的坑：打成 /imagr 或 /image 时，
        #    旧代码只匹配 "/img "（带空格），于是整行被当成买家文本，
        #    模型看到一串 URL 就【自己编】了句"截图没收到"。
        #    那看起来像系统在工作，其实是命令没被识别。
        image_url = ""
        text = line
        if line.startswith("/"):
            parts = line.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            if cmd in ("/img", "/image", "/pic"):
                if not arg:
                    print("  ★ /img 后面要跟图片 URL。例："
                          "/img https://img.alicdn.com/xxx.jpg")
                    continue
                if not arg.startswith(("http://", "https://")):
                    print(f"  ★ 这不是 URL：{arg[:60]}")
                    print("    良票要求【公网可读、无防盗链】的图片地址，"
                          "本地文件不行。")
                    continue
                image_url, text = arg, ""
                print(f"  （当作买家发了截图：{arg[:70]}…）")
            else:
                print(f"  ★ 不认识的命令：{cmd}")
                print("    可用：/img <URL>  /state  /keys  /notes  "
                      "/reset  /quit")
                print("    （这一句没有发出去）")
                continue

        # ---- 真发一轮 ----
        try:
            result = svc.on_message(chat_id, text, image_url=image_url)
        except Exception as exc:                        # noqa: BLE001
            print(f"  ✗ 抛异常了（★ 这是 bug —— 对话层不该把异常抛给调用方）："
                  f"{type(exc).__name__}: {exc}")
            import traceback
            for ln in traceback.format_exc().splitlines()[-6:]:
                print(f"     {ln}")
            continue

        show_reply(result, show_state=args.show_state)
        last_result = result

    print("\n结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
