"""
domain/quote —— 报价记录

★ 这个包和 domain/quote_rules.py 的分工（别搞混）：

    quote_rules.py   算价：给成本 → 出报价（纯函数，不落库）
    quote/models.py  报价的【数据模型】：一笔报价长什么样
    quote/store.py   报价的【持久化】：怎么存、怎么取、怎么作废

  一句话：quote_rules 决定"卖多少钱"，quote 负责"记住卖过多少钱"。
"""

from .models import (
    DEFAULT_TTL_MINUTES,
    REQUEST_TYPES,
    QuoteRecord,
    QuoteStatus,
    default_expiry,
    new_quote_id,
)
from .store import QuoteStore

__all__ = [
    "DEFAULT_TTL_MINUTES",
    "REQUEST_TYPES",
    "QuoteRecord",
    "QuoteStore",
    "QuoteStatus",
    "default_expiry",
    "new_quote_id",
]
