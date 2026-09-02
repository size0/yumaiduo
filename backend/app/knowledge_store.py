from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


KnowledgeCategory = Literal[
    "售后人工", "下单确认", "订单出票", "报价规则", "截图识别",
    "异常处理", "购票流程", "常见问题",
]

# Knowledge is injected by conversation stage instead of sending the whole
# catalogue to every model request.  The value is deliberately a small
# closed set so callers cannot invent a routing branch through free-form text.
KnowledgeStage = Literal[
    "consultation", "quotation", "order", "ticketing", "after_sales", "general",
]

_STAGE_CATEGORIES: dict[KnowledgeStage, frozenset[KnowledgeCategory]] = {
    "consultation": frozenset({"常见问题", "购票流程", "截图识别"}),
    "quotation": frozenset({"报价规则", "截图识别", "异常处理", "常见问题"}),
    "order": frozenset({"下单确认", "订单出票", "异常处理", "报价规则"}),
    "ticketing": frozenset({"订单出票", "异常处理", "常见问题"}),
    "after_sales": frozenset({"售后人工", "异常处理", "订单出票"}),
    "general": frozenset(KnowledgeCategory.__args__),
}

_RUNTIME_STAGE_ALIASES: dict[str, KnowledgeStage] = {
    "consultation": "consultation",
    "inquiry": "consultation",
    "quotation": "quotation",
    "quote": "quotation",
    "order": "order",
    "order_pending": "order",
    "confirmation": "order",
    "payment": "ticketing",
    "paid": "ticketing",
    "ticketing": "ticketing",
    "fulfillment": "ticketing",
    "after_sales": "after_sales",
    "shipping_refund": "after_sales",
    "refund": "after_sales",
    "completed": "after_sales",
    "general": "general",
}


def normalize_knowledge_stage(stage: KnowledgeStage | str | None) -> KnowledgeStage:
    """Map runtime business stages to the small knowledge-routing contract."""
    normalized = str(stage or "").strip().lower().replace("-", "_")
    return _RUNTIME_STAGE_ALIASES.get(normalized, "general")


class KnowledgeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^kb-[0-9a-f]{12}$")
    title: str = Field(min_length=1, max_length=120)
    category: KnowledgeCategory
    common_questions: str = Field(min_length=1, max_length=2_000)
    reply_guidance: str = Field(min_length=1, max_length=2_000)
    handling_rules: str = Field(min_length=1, max_length=4_000)
    enabled: bool = True
    sort_order: int = Field(default=100, ge=0, le=100_000)
    revision: int = Field(default=0, ge=0)
    updated_at: str | None = None

    @field_validator("title", "common_questions", "reply_guidance", "handling_rules")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()


class KnowledgeBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[KnowledgeEntry] = Field(default_factory=list, max_length=100)
    revision: int = Field(default=0, ge=0)
    updated_at: str | None = None


_SEED_DATA: tuple[dict[str, Any], ...] = (
    {
        "title": "退改退款投诉和订单异常", "category": "售后人工",
        "common_questions": "能退吗、要改签、买错场次了、我要投诉、扣款没出票、金额不对。",
        "reply_guidance": "我先查询这笔订单的真实状态和当前可执行的售后方式，再按查询结果为您处理。",
        "handling_rules": "先调用当前会话的订单或退款查询工具。满足固定退票条件时可通过受控工具发起；不得自行承诺退款金额、到账时间或处理结果。只有订单归属不清、金额冲突、状态查询多次失败、投诉争议或工具明确要求人工时才创建人工任务。",
    },
    {
        "title": "买家如何确认下单", "category": "下单确认",
        "common_questions": "怎么买、可以下单了、就要这个。",
        "reply_guidance": "请核对城市、影院、电影、场次、座位和金额；确认无误后按当前报价拍下，系统会按实际订单继续改价和出票。",
        "handling_rules": "结合当前报价和买家最新原话判断是否确认，不要求固定关键词。文字确认只能确认当前有效报价，不能代替真实闲鱼拍下和付款事件，也不能直接创建良票订单。",
    },
    {
        "title": "查询订单和出票结果", "category": "订单出票",
        "common_questions": "出票了吗、订单成功没有、票在哪里、怎么取票？",
        "reply_guidance": "我正在查询订单最新状态。只有系统确认出票成功后，我才会向您发送取票信息或票券链接。",
        "handling_rules": "必须调用当前会话订单查询接口。创建中、处理中、状态未知或回调异常时，不得回复“已出票”或“购票成功”；查询超时先安全重试一次并如实告知仍在核对，只有重复失败、状态冲突或工具明确要求时才创建人工任务。",
    },
    {
        "title": "未选座的万达影院如何报价", "category": "报价规则",
        "common_questions": "还没选座、这个万达会员价多少、W+多少钱？",
        "reply_guidance": "我先根据影院、电影和场次查询万达官方实时座位及当前可用的 W+ 会员价。",
        "handling_rules": "仅在没有明确座位且影院为万达时走万达实时座位与 W+ 报价链路。场次无法匹配时询问缺失信息；价格缺失或查询状态未知时不报价，允许安全重试一次。查询成功后使用店铺配置的未选座位报价话术。",
    },
    {
        "title": "已选择明确座位如何报价", "category": "报价规则",
        "common_questions": "8排6座和8排7座多少钱、这两个座位能下单吗？",
        "reply_guidance": "我会按照您指定的座位逐个查询实时库存和价格，确认后发送单价与合计金额。",
        "handling_rules": "有明确座位必须走良票查询、报价和下单链路，逐个匹配指定座位；不得用其他座位、区域价格或截图标价代替。查询成功后使用店铺配置的已选座位报价话术。",
    },
    {
        "title": "报价有效期和重新询价", "category": "报价规则",
        "common_questions": "刚才的价格还算吗、价格能保留多久？",
        "reply_guidance": "电影票价格和座位状态可能实时变化，下单前需要重新校验，以最新确认结果为准。",
        "handling_rules": "报价过期，或场次、座位、价格发生变化时，必须重新询价并让买家重新确认；不得沿用旧报价。",
    },
    {
        "title": "多个候选影院需要买家确认", "category": "截图识别",
        "common_questions": "就是这个万达、影院没错吧、选哪个？",
        "reply_guidance": "当前截图中的影院暂时无法唯一确定，请回复对应序号。确认后我再查询实时场次和价格。",
        "handling_rules": "按接口返回顺序编号展示真实候选影院，最多展示实际返回项；买家回复序号前不得自行选择或继续报价。买家确认后，有明确座位走良票；无明确座位且所选影院为万达，走万达实时 W+ 报价。",
    },
    {
        "title": "座位售罄或价格变化", "category": "异常处理",
        "common_questions": "刚才还有怎么没了、怎么涨价了、原座位买不了？",
        "reply_guidance": "座位和活动价格会实时变化。当前结果已经更新，我可以为您重新查询其他可售座位或最新价格。",
        "handling_rules": "必须重新调用实时接口并重新报价，不得沿用旧库存或旧金额。先向买家解释实时变化；只有金额事实冲突或买家明确提出争议售后时才创建人工任务。",
    },
    {
        "title": "截图标价不是最终报价", "category": "报价规则",
        "common_questions": "截图写 67.9 元就按这个买吗、图片上的价格算数吗？",
        "reply_guidance": "截图中的标价仅供参考，实际报价需要根据当前场次、指定座位库存和实时接口结果重新确认。",
        "handling_rules": "必须重新查询，不得直接使用截图价格、历史价格、其他座位价格或估算价格。",
    },
    {
        "title": "收到选座截图后的处理", "category": "截图识别",
        "common_questions": "这张图多少钱、这两个座位能买么、帮我看下截图？",
        "reply_guidance": "好的，我先识别截图中的城市、影院、电影、场次和座位，再查询实时库存与价格，请稍等。",
        "handling_rules": "优先调用良票图片识别接口；短暂网络失败可按同一图片幂等重试一次，仍失败时才使用备用识别。截图不完整时准确说明缺失项并请买家补图；只有重复失败、金额冲突或状态无法确认时才创建人工任务。",
    },
    {
        "title": "候选影院都不正确", "category": "截图识别",
        "common_questions": "都不是、没有我要的影院、影院不对。",
        "reply_guidance": "好的，当前候选影院与您的实际影院不一致。请重新发送一张包含影院名称、电影和场次信息的完整截图。",
        "handling_rules": "先让买家补充完整影院名、城市或新截图，再调用影院查询或官方候选确认；不得强行匹配相似影院。连续补充后仍无法收敛且买家要求继续处理时，才创建人工任务。",
    },
    {
        "title": "不能只占座不确认购买", "category": "下单确认",
        "common_questions": "先帮我占座、先锁着我考虑一下。",
        "reply_guidance": "座位需要以正式提交时的实时库存为准，暂不支持仅占座不确认购买。",
        "handling_rules": "没有有效报价、真实闲鱼订单和付款事件时不得创建良票订单，也不得向买家承诺已保留座位。",
    },
    {
        "title": "非万达影院且未选座", "category": "报价规则",
        "common_questions": "不是万达、还没选座能先报价吗？",
        "reply_guidance": "需要先确认具体影院、场次和可用座位，才能给出准确报价。您可以发送完整选座截图，我帮您查询。",
        "handling_rules": "非万达且没有明确座位时不得套用万达 W+ 规则；没有可验证的实时价格时不报价，应引导发送选座图或补齐影院、影片、日期和场次。",
    },
    {
        "title": "W+会员价说明", "category": "报价规则",
        "common_questions": "这是会员价吗、为什么有 W+ 价格、怎么优惠的？",
        "reply_guidance": "这是根据当前影院、场次和座位实时查询到的可用会员或活动价格，最终以本次实时查询结果为准。",
        "handling_rules": "只引用接口当前返回的可用价格。不得向买家披露内部加价金额、折扣分段、利润公式、账号池或探针实现。",
    },
    {
        "title": "购票需要提供什么信息", "category": "购票流程",
        "common_questions": "怎么买票、需要发什么、怎么询价？",
        "reply_guidance": "请提供城市、影院、电影、场次和人数；如果已经选座，请直接发送包含影院、场次和座位的完整选座截图。",
        "handling_rules": "信息不足时只引导补充，不猜影院、场次、座位或价格，也不调用下单接口。",
    },
    {
        "title": "座位偏好实时查询", "category": "常见问题",
        "common_questions": "某排某座还有吗、中间位置能选吗、连续座位还有吗？",
        "reply_guidance": "我先查询对应场次当前可售的位置，再按实时结果回复。",
        "handling_rules": "只有实时接口返回 available=true 且可售时才能说当前可选。字段缺失、接口失败或场次映射不存在时不得判断库存；截图颜色、W+图标和买家偏好不能代替实时座位结果。",
    },
    {
        "title": "W+未标记位置只要求重发截图", "category": "截图识别",
        "common_questions": "W+图没有圈座、还没标记位置、空座位图怎么处理？",
        "reply_guidance": "请把需要出票的位置在座位图上圈好后，重新发送一张标记好的截图给我。",
        "handling_rules": "后端明确返回 isSeatSelection=true 且 seat=[] 时，这是W+位置标记流程，不是普通识别失败。未标记时只发送上述一句，不追问张数、排数、靠左靠右或具体X排Y座，不报截图价格，不说可以买。收到标记图后进入人工出票流程；已有张数不得重复询问。",
    },
    {
        "title": "多张图片场次冲突处理", "category": "截图识别",
        "common_questions": "发了两张图、两张截图不一样、一个是今天一个是明天。",
        "reply_guidance": "两张截图的影院、影片或场次不一致，请确认需要哪一场；确认后我再继续处理。",
        "handling_rules": "逐图比较影院、影片、日期、场次、影厅和座位。出现关键字段冲突时，只让买家选择对应图片或场次，不合并事实、不替买家猜测、不继续报价。确认后仅使用被确认图片对应的识别快照和报价引用。",
    },
)


def _seed_entries() -> list[KnowledgeEntry]:
    return [KnowledgeEntry(
        id=f"kb-{index:012x}", sort_order=index * 10, **item,
    ) for index, item in enumerate(_SEED_DATA, start=1)]


class KnowledgeStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()

    def current(self) -> KnowledgeBase:
        with self._lock:
            if not self._path.exists():
                return KnowledgeBase(entries=_seed_entries())
            try:
                return KnowledgeBase.model_validate_json(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return KnowledgeBase(entries=_seed_entries())

    def active_for_prompt(self, stage: KnowledgeStage | str | None = None) -> list[KnowledgeEntry]:
        current = self.current()
        # ``None`` keeps the management/API behaviour backwards compatible and
        # is useful for audits.  Runtime callers should always pass a stage so
        # unrelated transaction rules do not pollute a consultation prompt.
        allowed = _STAGE_CATEGORIES[normalize_knowledge_stage(stage)] if stage is not None else None
        return [
            entry for entry in sorted(current.entries, key=lambda item: (item.sort_order, item.title))
            if entry.enabled and (allowed is None or entry.category in allowed)
        ][:30]

    def save(self, entries: list[dict[str, Any] | KnowledgeEntry]) -> KnowledgeBase:
        with self._lock:
            current = self.current()
            normalized = [item if isinstance(item, KnowledgeEntry) else KnowledgeEntry.model_validate(item) for item in entries]
            if len({item.id for item in normalized}) != len(normalized):
                raise ValueError("knowledge_duplicate_id")
            saved = KnowledgeBase(
                entries=normalized,
                revision=current.revision + 1,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            self._write(saved)
            return saved

    def create(self, payload: dict[str, Any]) -> KnowledgeEntry:
        with self._lock:
            current = self.current()
            entry = KnowledgeEntry.model_validate({
                **payload, "id": payload.get("id") or f"kb-{uuid4().hex[:12]}",
                "revision": 0, "updated_at": None,
            })
            self.save([*current.entries, entry])
            return entry

    def update(self, entry_id: str, payload: dict[str, Any]) -> KnowledgeEntry:
        with self._lock:
            current = self.current()
            for index, entry in enumerate(current.entries):
                if entry.id != entry_id:
                    continue
                updated = KnowledgeEntry.model_validate({
                    **entry.model_dump(), **payload, "id": entry.id,
                    "revision": entry.revision + 1,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                entries = list(current.entries)
                entries[index] = updated
                self.save(entries)
                return updated
            raise KeyError("knowledge_not_found")

    def delete(self, entry_id: str) -> None:
        with self._lock:
            current = self.current()
            entries = [entry for entry in current.entries if entry.id != entry_id]
            if len(entries) == len(current.entries):
                raise KeyError("knowledge_not_found")
            self.save(entries)

    def _write(self, payload: KnowledgeBase) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        temporary.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)
