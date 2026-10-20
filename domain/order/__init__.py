"""
domain/order —— 订单管理

★ 和另外两个包的分工（别搞混）：

    domain/quote_rules.py   算价（给成本 → 出报价）
    domain/quote/           记住"报过什么价"（报价记录）
    domain/order/           记住"卖出去的单"（订单 + 绑定 + 付款 + 交付）

  一句话：quote 管"报了多少"，order 管"卖了什么、收没收到钱、票给了没有"。
"""

from .models import (
    APPROVAL_TTL_SECONDS,
    ATTENTION_STATES,
    TERMINAL_STATES,
    Approval,
    Order,
    OrderState,
    PaymentReceipt,
    PaymentState,
    QuoteBinding,
    idempotency_key_for,
    new_order_id,
)
from .store import OrderStore

__all__ = [
    "APPROVAL_TTL_SECONDS",
    "ATTENTION_STATES",
    "Approval",
    "Order",
    "OrderState",
    "OrderStore",
    "PaymentReceipt",
    "PaymentState",
    "QuoteBinding",
    "TERMINAL_STATES",
    "idempotency_key_for",
    "new_order_id",
]
