"""
application/tools.py —— 给模型用的【只读】工具

════════════════════════════════════════════════════════════════
★ 为什么这些工具是只读的

  模型错一次，如果是"答错一句话"，代价是买家不满意；
  如果是"下错单 / 退错票"，代价是几千块冻结 + 投诉 + 平台判责。

  ⇒ 所以模型的能力边界就画在这里：**能查，不能改。**
    下单、改价、退款、退票全部留在 workflow 里，一步不差。

★ 三条设计约束

  1. **工具不接收 chat_id。**
     模型不该也无法指定"查谁的"。数据源在装配时按当前会话注入，
     所以它只能查到这一个买家的东西 —— 身份隔离是结构保证的，
     不是靠 prompt 约束的。

  2. **返回值就是模型能看到的一切。**
     所以脱敏在这一层做：成本、毛利、进价、限价一律不返回。
     它们不进工具结果，模型就不可能说漏。

  3. **失败要说人话。**
     工具失败返回 {"ok": false, "reason": "..."}，
     让模型知道"查不到"而不是"没有" —— 这两件事答法完全不同。

★ 被测过

  实测：调一次工具 + 再问一轮 = 0.21 秒（deepseek-flash）。
  对比：截图识别中位 3.7 秒。工具循环那点开销在噪声里。
════════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ==================================================================== 声明
# ★ 这份 schema 是给模型看的。description 的写法直接决定它会不会用、用得对不对。
#
#   实测踩过：description 只写"读订单"时，模型宁可直接问买家要订单号
#   也不调工具（'请问您的订单号是？'）。
#   加上「买家问…时调用」这种触发条件之后，它才开始真的调。
#
#   ⇒ description 要写清【什么时候用】，不只是【能干什么】。
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_order",
            "description": (
                "读当前这个买家的订单：状态、影院、影片场次、是否可退票。"
                "当买家问「我那单怎么样了 / 到哪了 / 出票了吗 / 能不能退 / 退了吗」"
                "这类【关于他自己订单】的问题时调用。"
                "★ 不要向买家索要订单号 —— 这个工具自动查当前会话的订单。"
            ),
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_quote",
            "description": (
                "读当前报价：总价、单张价、几张、什么时候过期。"
                "当买家问「多少钱 / 怎么算的 / 报价还有效吗 / 能便宜点吗」时调用。"
                "★ 只返回售价，不返回成本。不许推测成本。"
            ),
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_faq",
            "description": (
                "在店铺 FAQ / 智能问答里查一条。"
                "当买家问【规则类】问题（多久出票、能不能开发票、能不能改签、"
                "取票码怎么用、退改规则）时调用，比凭记忆答准得多。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string",
                                "description": "根据本轮语义选择规则关键词，可将店铺同义表达归一，例如中间位置对应会员座；不得编造事实"},
                },
                "required": ["keyword"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_refund_status",
            "description": (
                "读退票进度：有没有提交过退票、退成没有、退了多少、票还有没有效。"
                "当买家问「退票到哪一步了 / 钱退了吗 / 退票成功没」时调用。"
            ),
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
        },
    },
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]


# ==================================================================== 工具集
class ReadOnlyTools:
    """
    只读工具集。

    ★ 所有数据源都是注入的。这样：
        · 测试能塞假数据，不碰数据库、不发网络请求
        · 生产能换真实实现
        · 模型永远拿不到"不属于这个会话"的东西（源在装配时就绑定了）
    """

    def __init__(self, *, order_source: Callable[[], dict] | None = None,
                 quote_source: Callable[[], dict] | None = None,
                 faq_source: Callable[[str], str | None] | None = None,
                 refund_source: Callable[[], dict] | None = None):
        self._order = order_source
        self._quote = quote_source
        self._faq = faq_source
        self._refund = refund_source
        # 记账：便于看模型到底查了什么（排查"它为什么这么答"要用）
        self.calls: list[tuple[str, dict]] = []

    # ---------------------------------------------------------------- 分发
    def handlers(self) -> dict[str, Callable]:
        return {
            "read_order": self.read_order,
            "read_quote": self.read_quote,
            "search_faq": self.search_faq,
            "read_refund_status": self.read_refund_status,
        }

    def execute(self, name: str, args: dict) -> dict:
        """执行一个工具调用。★ 未知工具/异常都变成模型看得懂的结果，不抛。"""
        self.calls.append((name, dict(args or {})))
        fn = self.handlers().get(name)
        if fn is None:
            # 模型编了个工具名 —— 明确告诉它没有，别让它以为查过了
            return {"ok": False, "reason": f"没有 {name} 这个工具"}
        try:
            return fn(**(args or {}))
        except TypeError as exc:
            return {"ok": False, "reason": f"参数不对：{exc}"}
        except Exception as exc:                            # noqa: BLE001
            # ★ 不把异常原文透出去（可能含路径/内部信息）
            return {"ok": False, "reason": f"查询失败（{type(exc).__name__}）"}

    # ---------------------------------------------------------------- 各工具
    def read_order(self) -> dict:
        if self._order is None:
            return {"ok": False, "reason": "这个会话还没有订单"}
        data = dict(self._order() or {})
        if not data:
            return {"ok": False, "reason": "这个会话还没有订单"}
        # ★ 白名单：只放这几个字段。
        #   黑名单（"去掉成本"）会漏 —— 以后加了新字段就自动泄露了。
        allowed = ("order_no", "status", "cinema", "movie", "hall",
                   "start_time", "seats", "refundable", "ticketed")
        out = {k: data[k] for k in allowed if k in data and data[k] is not None}
        return {"ok": True, "order": out}

    def read_quote(self) -> dict:
        if self._quote is None:
            return {"ok": False, "reason": "当前没有有效报价"}
        data = dict(self._quote() or {})
        if not data:
            return {"ok": False, "reason": "当前没有有效报价"}
        allowed = ("total_yuan", "unit_yuan", "seat_count", "expires_in_seconds",
                   "rule_name")
        out = {k: data[k] for k in allowed if k in data and data[k] is not None}
        return {"ok": True, "quote": out}

    def search_faq(self, keyword: str = "") -> dict:
        kw = (keyword or "").strip()
        if not kw:
            return {"ok": False, "reason": "没给关键词"}
        if self._faq is None:
            return {"ok": False, "reason": "FAQ 没配置"}
        hit = self._faq(kw)
        if not hit:
            # ★ "没查到"和"没有这条规则"是两件事，要让模型分得清
            return {"ok": False, "status": "POLICY_NOT_FOUND",
                    "reason": f"FAQ 里未找到跟「{kw}」相关的条目，不代表没有相关规则。",
                    "next_actions": ["核对本店经验；缺少适用条件就追问。仍无规则时如实说明需向店家或影院确认，不编造承诺。"]}
        return {"ok": True, "keyword": kw, "answer": hit}

    def read_refund_status(self) -> dict:
        if self._refund is None:
            return {"ok": False, "reason": "这个会话没有退票记录"}
        data = dict(self._refund() or {})
        if not data:
            return {"ok": False, "reason": "这个会话没有退票记录"}
        allowed = ("refund_no", "state", "fee_yuan", "refund_yuan",
                   "ticket_still_valid", "fail_reason")
        out = {k: data[k] for k in allowed if k in data and data[k] is not None}
        return {"ok": True, "refund": out}


# ==================================================================== 从会话造
def tools_for(conv, *, faq_lookup: Callable[[str], str | None] | None = None,
              order: Any = None) -> ReadOnlyTools:
    """
    按当前会话造一套只读工具。

    ★ 这就是"身份隔离"的实现处：
      数据源绑的是【这个 conv】和它自己的 pending_order，
      所以模型无论怎么问，都只能查到这个买家自己的东西。

    ★ 脱敏也在这里做：成本/毛利/限价一律不进返回值。
    """
    pending = getattr(conv, "pending_order", None)

    def _order() -> dict:
        if pending is None:
            return {}
        return {
            "order_no": pending.order_no,
            "status": getattr(order, "status", "") or "处理中",
            "cinema": pending.cinema_name,
            "movie": pending.movie_name,
            "hall": pending.hall_name,
            "start_time": pending.start_time,
            "seats": [s.get("seatNo") for s in (pending.seats or []) if s],
            "refundable": getattr(order, "refundable", None),
        }

    def _quote() -> dict:
        q = getattr(conv, "quote", None)
        if q is None:
            return {}
        left = None
        exp = getattr(conv, "quote_expires_at", None)
        if exp is not None:
            import domain.conversation as dc
            left = int((exp - dc.now()).total_seconds())
        return {
            "total_yuan": f"{q.total_price_fen / 100:.2f}",
            "unit_yuan": f"{q.unit_price_fen / 100:.2f}",
            "seat_count": q.seat_count,
            "expires_in_seconds": left,
            "rule_name": q.rule_name,
            # ★ 代码里没有 cost / margin 字段 —— 不是"忘了放"，
            #   是【故意不放】。上面 read_quote 的白名单也拦一道。
        }

    def _refund() -> dict:
        pending_refund = getattr(conv, "refund", None)
        if pending_refund is None:
            return {}
        return dict(pending_refund)

    return ReadOnlyTools(order_source=_order, quote_source=_quote,
                         faq_source=faq_lookup, refund_source=_refund)
