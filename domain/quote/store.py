"""
domain/quote/store.py —— 报价记录的持久化

════════════════════════════════════════════════════════════════
★ 设计照 V5 的 domain/quote/store.py（57 行），但补了它没有的两件事

  V5 只有 4 个方法：create / get / update_status / supersede_for_identity

  它没做的（我补上，理由写在各自的方法上）：
    · usable_for —— 取"现在还能用的那笔报价"（下单前必须过这道）
    · expire_stale —— 把过期报价翻成 EXPIRED（V5 的过期只在 is_usable 里判，
                     状态永远停在 READY，列表里分不清"还有效"和"已过期"）
    · 过期状态落盘 —— 见上

★ 为什么用 SQLite 而不是内存
    线上 V5 就是 SQLite（quotes.sqlite3）。报价要能跨进程读：
    客服侧算价、回调侧对账、运营查历史 —— 内存对象做不到。

★ 表结构照 V5：一张表、两列（id 主键 + payload JSON）
    不拆列的理由（V5 的选择）：报价字段会变，拆列每次都要迁移；
    而报价几乎总是整条读、整条写，不按列查。
    ★ 代价：不能按 payload 内部字段建索引。真要按买家查，
      用下面的 by_identity（全表扫 + 过滤），报价量级下可以接受；
      量大了再加 GIN 式的辅助表。
════════════════════════════════════════════════════════════════
"""

import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock

from .models import QuoteRecord

_IDENTITY_KEYS = ("tenant_id", "shop_id", "buyer_id", "chat_id")


class QuoteStore:
    """报价记录的 SQLite 存储。"""

    def __init__(self, database: str = ":memory:"):
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.lock = RLock()
        with self.lock:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS quote_records ("
                "quote_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self.connection.commit()

    # ---------------------------------------------------------------- 写
    def create(self, record: QuoteRecord) -> QuoteRecord:
        """存一笔报价。★ 同一 quote_id 重复存要报错，不能静默覆盖。"""
        try:
            with self.lock:
                self.connection.execute(
                    "INSERT INTO quote_records (quote_id, payload) VALUES (?, ?)",
                    (record.quote_id,
                     json.dumps(record.to_dict(), ensure_ascii=False)))
                self.connection.commit()
        except sqlite3.IntegrityError as error:
            # ★ 不能覆盖：覆盖会把"上一笔用这个 id 的报价"悄悄换掉，
            #   而回调/对账可能还拿着旧 id。
            raise ValueError(
                f"QUOTE_DUPLICATE_ID: {record.quote_id} 已存在，不覆盖") from error
        except sqlite3.Error as error:
            raise RuntimeError("QUOTE_PERSIST_FAILED") from error
        return record

    def put(self, record: QuoteRecord) -> QuoteRecord:
        """整条覆盖写（状态变更用）。★ 与 create 分开：create 不许重复。"""
        try:
            with self.lock:
                self.connection.execute(
                    "UPDATE quote_records SET payload = ? WHERE quote_id = ?",
                    (json.dumps(record.to_dict(), ensure_ascii=False),
                     record.quote_id))
                self.connection.commit()
        except sqlite3.Error as error:
            raise RuntimeError("QUOTE_PERSIST_FAILED") from error
        return record

    def update_status(self, quote_id: str, status: str) -> QuoteRecord | None:
        """改状态。★ 找不到返回 None —— 不静默成功。"""
        record = self.get(quote_id)
        if record is None:
            return None
        record.status = status
        return self.put(record)

    # ---------------------------------------------------------------- 读
    def get(self, quote_id: str) -> QuoteRecord | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM quote_records WHERE quote_id = ?",
                (quote_id,)).fetchone()
        if not row:
            return None
        return QuoteRecord.from_dict(json.loads(row[0]))

    def by_identity(self, identity: dict) -> list[QuoteRecord]:
        """
        按身份四元组取这个买家的全部报价（新的在前）。

        ★ 为什么必须按四元组过滤：报价隔离。
          少任何一项都会串到别人的报价上。
        """
        missing = [k for k in _IDENTITY_KEYS if not identity.get(k)]
        if missing:
            raise ValueError(f"QUOTE_IDENTITY_REQUIRED: 缺 {missing}")
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload FROM quote_records").fetchall()
        out = []
        for (raw,) in rows:
            try:
                rec = QuoteRecord.from_dict(json.loads(raw))
            except (ValueError, TypeError):
                continue
            if all(getattr(rec, k) == identity[k] for k in _IDENTITY_KEYS):
                if rec.metadata.get('purpose') != 'MANUAL_SETTLEMENT':
                    out.append(rec)
        out.sort(key=lambda r: r.created_at or datetime.min.replace(
            tzinfo=timezone.utc), reverse=True)
        return out

    def usable_for(self, identity: dict, *,
                   now: datetime | None = None,
                   require_seats: bool = False) -> QuoteRecord | None:
        """
        取这个买家【现在还能用】的那笔报价。★ 下单前必须过这道。

        ★ 为什么要这个方法（V5 没有）：
          下单是一个"用旧价买东西"的动作。如果只是拿到 quote_id 就去下单，
          可能用的是【已经被作废】或【已过期】的报价 —— 那是拿旧价成交。
          ⇒ 统一在这里判：状态 READY + 未过期（+ 可选：必须有具体座位）。
        """
        for rec in self.by_identity(identity):
            if rec.is_usable(now=now, require_seats=require_seats):
                return rec
        return None

    # ---------------------------------------------------------------- 作废
    def supersede_for_identity(self, identity: dict, *,
                               except_quote_id: str | None = None,
                               protected_quote_ids: tuple = (),
                               demand_id: str | None = None) -> list[str]:
        """
        买家【改了需求】→ 把之前的可用报价全部作废。

        ★★ 这是 V5 里最值得抄的一个机制（store.py 的
           supersede_for_identity）。为什么必须有：
             买家先问了 20:30 那场（报价 84 元），又说「换成 21:30」。
             如果旧报价还停在 READY，而某处逻辑拿"最新一条 READY"下单，
             就会用 20:30 的价去买 21:30 的票。
           ⇒ 需求一变，旧的立刻标 SUPERSEDED。

        返回被作废的 quote_id 列表。
        """
        missing = [k for k in _IDENTITY_KEYS if not identity.get(k)]
        if missing:
            raise ValueError(f"QUOTE_IDENTITY_REQUIRED: 缺 {missing}")
        changed = []
        for rec in self.by_identity(identity):
            if demand_id is not None and rec.demand_id != demand_id:
                continue
            if rec.quote_id == except_quote_id:
                continue
            if rec.quote_id in protected_quote_ids:
                continue
            if rec.status not in ("READY", "CONFIRMED"):
                continue
            rec.status = "SUPERSEDED"
            self.put(rec)
            changed.append(rec.quote_id)
        return changed

    def expire_stale(self, *, now: datetime | None = None) -> list[str]:
        """
        把过期报价翻成 EXPIRED，返回被翻的 quote_id。

        ★ V5 只在读取时判过期，状态永远停在 READY ——
          于是列表里"还有效"和"已过期"长得一样。落盘才好排查。
        """
        now = now or datetime.now(timezone.utc)
        changed = []
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload FROM quote_records").fetchall()
        for (raw,) in rows:
            try:
                rec = QuoteRecord.from_dict(json.loads(raw))
            except (ValueError, TypeError):
                continue
            if rec.expire_if_needed(now=now):
                self.put(rec)
                changed.append(rec.quote_id)
        return changed

    def close(self) -> None:
        self.connection.close()
