"""
domain/order/store.py —— 订单的持久化

════════════════════════════════════════════════════════════════
★ 表结构照线上 transactions.sqlite3 + liangpiao-orders.sqlite3
  线上分了五张表，我合并到两张（理由在下面）：

  线上：                                本文件：
    liangpiao_orders        ─┐
    order_quote_bindings    ─┼──→  orders（主表，payload 带绑定信息）
    order_payment_receipts  ─┼──→  order_payments
    reprice_authorizations  ─┤      （授权放进 Order.approval 字段）
    order_refund_notices    ─┘      （退款是下一步的事，先不建）

  ★ 为什么合：线上按"表"分是因为它那几个模块各自独立演进。
    我们支付回执本来就需要【独立主键 + 幂等】(tenant_id, event_id)，
    所以单独一张；其余（绑定/授权/失败码）都是订单的属性，
    放同一个 payload 里，读一次全有 —— 少一次查询，也少一处不一致可能。

  ★ 为什么支付回执【必须】单独一张表（不能塞进订单）：
    它是幂等的主体。同一个支付事件推两次，第二次必须能被识别为重复。
    如果把它塞进订单 payload，"重复"就得靠比较订单内容来判断 —— 不可靠。
════════════════════════════════════════════════════════════════
"""

import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock

from .models import Order, PaymentReceipt, QuoteBinding

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    quote_id TEXT UNIQUE NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    state TEXT NOT NULL,
    provider_order_id TEXT,
    tenant_id TEXT NOT NULL,
    shop_id TEXT NOT NULL,
    buyer_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    failure_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_identity
    ON orders (tenant_id, shop_id, buyer_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_orders_provider_order
    ON orders (provider_order_id);

CREATE TABLE IF NOT EXISTS order_payments (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    order_id TEXT,
    fingerprint TEXT,
    state TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id)
);
"""


class OrderStore:
    """订单存储。"""

    def __init__(self, database: str = ":memory:"):
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.lock = RLock()
        with self.lock:
            self.connection.executescript(_SCHEMA)
            self.connection.commit()
        from .platform import PlatformOrderStore
        self.platforms = PlatformOrderStore(self)

    # ---------------------------------------------------------------- 建单
    def create(self, order: Order) -> Order:
        """
        建单。

        ★★ 两道唯一约束都在这里起作用 —— 这是防重复下单的最后一道：
            ① quote_id UNIQUE     一笔报价只能建一笔单
            ② idempotency_key UNIQUE 同一把幂等键只能建一笔单
          违反任一条都报 ValueError（不静默返回旧单）——
          调用方必须自己决定"是重试还是要新建"，
          静默返回旧单会让调用方以为建成功了而继续走付款流程。
        """
        if not order.quote_id:
            raise ValueError("ORDER_QUOTE_REQUIRED: 订单必须绑一笔报价")
        if not order.idempotency_key:
            raise ValueError("ORDER_IDEMPOTENCY_REQUIRED: 必须有幂等键")
        try:
            with self.lock:
                self.connection.execute(
                    "INSERT INTO orders (order_id, quote_id, idempotency_key,"
                    " state, provider_order_id, tenant_id, shop_id, buyer_id,"
                    " chat_id, failure_code, created_at, updated_at, payload)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order.order_id, order.quote_id, order.idempotency_key,
                     order.state, order.provider_order_id or None,
                     order.tenant_id, order.shop_id, order.buyer_id,
                     order.chat_id, order.failure_code or None,
                     order.created_at.isoformat(), order.updated_at.isoformat(),
                     json.dumps(order.to_dict(), ensure_ascii=False)))
                self.connection.commit()
        except sqlite3.IntegrityError as error:
            text = str(error)
            if "quote_id" in text:
                raise ValueError(
                    f"ORDER_QUOTE_ALREADY_USED: 报价 {order.quote_id} 已建过单"
                ) from error
            if "idempotency_key" in text:
                raise ValueError(
                    f"ORDER_IDEMPOTENT_REPLAY: 幂等键 {order.idempotency_key} "
                    f"已用过") from error
            raise ValueError(f"ORDER_CREATE_CONFLICT: {text}") from error
        except sqlite3.Error as error:
            raise RuntimeError("ORDER_PERSIST_FAILED") from error
        return order

    def find_by_idempotency(self, key: str) -> Order | None:
        """
        按幂等键找已有订单。

        ★ 调用方应该先查这个再 create ——
          这样"重试"走的是同一笔单，而不是撞唯一约束报错。
        """
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM orders WHERE idempotency_key = ?",
                (key,)).fetchone()
        return Order.from_dict(json.loads(row[0])) if row else None

    def find_by_quote(self, quote_id: str) -> Order | None:
        """按报价找订单。★ 一笔报价最多一笔单（quote_id 是 UNIQUE）。"""
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM orders WHERE quote_id = ?",
                (quote_id,)).fetchone()
        return Order.from_dict(json.loads(row[0])) if row else None

    # ---------------------------------------------------------------- 读
    def get(self, order_id: str) -> Order | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM orders WHERE order_id = ?",
                (order_id,)).fetchone()
        return Order.from_dict(json.loads(row[0])) if row else None

    def find_by_provider_order(self, provider_order_id: str) -> Order | None:
        """按上游订单号找（回调只给上游单号，要给对买家）。"""
        if not provider_order_id:
            return None
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM orders WHERE provider_order_id = ?",
                (provider_order_id,)).fetchone()
        return Order.from_dict(json.loads(row[0])) if row else None

    def by_identity(self, identity: dict) -> list[Order]:
        """取这个买家的全部订单（新的在前）。★ 身份四元组缺一不可。"""
        keys = ("tenant_id", "shop_id", "buyer_id", "chat_id")
        missing = [k for k in keys if not identity.get(k)]
        if missing:
            raise ValueError(f"ORDER_IDENTITY_REQUIRED: 缺 {missing}")
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload FROM orders ORDER BY created_at DESC").fetchall()
        out = []
        for (raw,) in rows:
            try:
                o = Order.from_dict(json.loads(raw))
            except (ValueError, TypeError):
                continue
            if all(getattr(o, k) == identity[k] for k in keys):
                out.append(o)
        return out

    # ---------------------------------------------------------------- 改
    def put(self, order: Order) -> Order:
        """整条覆盖写。★ 同步主表上的冗余列（state/失败码），便于直接查。"""
        order.touch()
        try:
            with self.lock:
                self.connection.execute(
                    "UPDATE orders SET state=?, provider_order_id=?,"
                    " failure_code=?, updated_at=?, payload=? WHERE order_id=?",
                    (order.state, order.provider_order_id or None,
                     order.failure_code or None, order.updated_at.isoformat(),
                     json.dumps(order.to_dict(), ensure_ascii=False),
                     order.order_id))
                self.connection.commit()
        except sqlite3.Error as error:
            raise RuntimeError("ORDER_PERSIST_FAILED") from error
        return order

    # ---------------------------------------------------------------- 支付
    def record_payment(self, receipt: PaymentReceipt) -> tuple:
        """
        记一笔支付回执。返回 (是否新记, 已有记录或本条)。

        ★★ 幂等 + 矛盾检测（两条都要，缺一不可）：
            ① 同 (tenant_id, event_id) 再来 → 视为重放，不重复记
            ② ★ 但若【指纹不同】→ 那不是重放，是上游改了事件内容。
               必须报错让人看，不能当重放默默忽略 ——
               否则账上会留下一条过时的支付记录而没人知道。
        """
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM order_payments WHERE tenant_id=? AND event_id=?",
                (receipt.tenant_id, receipt.event_id)).fetchone()
            if row is not None:
                old = PaymentReceipt.from_dict(json.loads(row[0]))
                if (receipt.fingerprint and old.fingerprint
                        and receipt.fingerprint != old.fingerprint):
                    raise ValueError(
                        "PAYMENT_EVENT_CONFLICT: 同一 event_id 内容指纹不同"
                        f"（旧 {old.fingerprint[:12]}… 新 "
                        f"{receipt.fingerprint[:12]}…）—— 需要人工核对")
                return False, old
            try:
                self.connection.execute(
                    "INSERT INTO order_payments (tenant_id, event_id, order_id,"
                    " fingerprint, state, received_at, payload)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (receipt.tenant_id, receipt.event_id, receipt.order_id or None,
                     receipt.fingerprint or None, receipt.state,
                     receipt.received_at.isoformat(),
                     json.dumps(receipt.to_dict(), ensure_ascii=False)))
                self.connection.commit()
            except sqlite3.Error as error:
                raise RuntimeError("PAYMENT_PERSIST_FAILED") from error
        return True, receipt

    def payments_for(self, order_id: str) -> list[PaymentReceipt]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload FROM order_payments WHERE order_id = ?",
                (order_id,)).fetchall()
        return [PaymentReceipt.from_dict(json.loads(r[0])) for r in rows]

    # ---------------------------------------------------------------- 绑定
    def bind_quote(self, order_id: str, binding: QuoteBinding) -> Order:
        """
        把订单和报价绑起来（含【绑定时的金额】）。

        ★ 金额写进订单的 amount_fen —— 这是"买家该付多少"的依据。
          改价之后报价变了，但绑定金额不变，对账才不会错。
        """
        order = self.get(order_id)
        if order is None:
            raise ValueError(f"ORDER_NOT_FOUND: {order_id}")
        order.quote_id = binding.quote_id
        order.amount_fen = binding.amount_fen
        order.ticket_count = binding.ticket_count or order.ticket_count
        order.payload["quote_binding"] = binding.to_dict()
        return self.put(order)

    def close(self) -> None:
        self.connection.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
