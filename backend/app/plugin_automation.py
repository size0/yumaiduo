from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from .chat import build_recognition_reply
from .errors import RecognitionError
from .models import MovieImageInfo, RealQuote, SelectedSeat
from .observability import LOGGER
from .reply_template_store import ReplyTemplates, render_template
from .rule_contracts import AiAssistResult


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_AGE_MS = 30 * 60 * 1000
MAX_QUOTE_CONFIRMATION_AGE_MS = 15 * 60 * 1000
ALLOWED_IMAGE_HOST_SUFFIXES = ("alicdn.com", "tbcdn.cn")
ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class RecognitionService(Protocol):
    async def recognize(
        self,
        image: bytes,
        content_type: str,
        buyer_message: str = "",
        *,
        prior_recognitions: list[MovieImageInfo] | None = None,
    ) -> MovieImageInfo: ...


class QuoteService(Protocol):
    async def quote(self, recognition: MovieImageInfo) -> RealQuote: ...


class ChatService(Protocol):
    async def reply(self, text: str, conversation_id: str) -> str: ...

    def remember_image_context(
        self,
        conversation_id: str,
        recognition: MovieImageInfo,
        quote: RealQuote | None,
        quote_error: str | None = None,
    ) -> None: ...


class ShopStore(Protocol):
    def is_enabled(self, tenant_id: str, shop_id: str) -> bool: ...


class PendingCinemaCandidateStore(Protocol):
    def get(self, key: str) -> MovieImageInfo | None: ...
    def save(self, key: str, recognition: MovieImageInfo) -> None: ...
    def delete(self, key: str) -> None: ...


ImageLoader = Callable[[str], Awaitable[tuple[bytes, str]]]


def validate_image_url(value: object) -> str:
    url = str(value or "").strip()
    if not url or len(url) > 2_000:
        raise ValueError("image_url_invalid")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("image_url_invalid")
    hostname = parsed.hostname.lower().rstrip(".")
    if not any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in ALLOWED_IMAGE_HOST_SUFFIXES):
        raise ValueError("image_host_not_allowed")
    return url


def _detected_image_type(content_type: str, body: bytes) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized in ALLOWED_IMAGE_TYPES:
        return normalized
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("image_type_not_allowed")


class SecureImageLoader:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20,
        retry_delays: tuple[float, ...] = (0.5, 1.5, 3.0),
    ) -> None:
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds, connect=4),
            follow_redirects=False,
        )
        self._retry_delays = retry_delays

    async def __call__(self, source: str) -> tuple[bytes, str]:
        url = validate_image_url(source)
        attempts = (0.0, *self._retry_delays)
        for index, delay in enumerate(attempts):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await self._download(url)
            except ValueError as error:
                retryable = str(error) in {"image_download_failed", "image_empty"}
                if not retryable or index == len(attempts) - 1:
                    raise
            except (httpx.TimeoutException, httpx.TransportError) as error:
                if index == len(attempts) - 1:
                    raise ValueError("image_download_failed") from error
        raise ValueError("image_download_failed")

    async def _download(self, url: str) -> tuple[bytes, str]:
        headers = {
            "accept": "image/jpeg,image/png,image/webp",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "referer": "https://www.goofish.com/",
        }
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code != 200:
                LOGGER.warning(
                    "event=image_download_rejected host=%s status=%d",
                    urlsplit(url).hostname,
                    response.status_code,
                )
                raise ValueError("image_download_failed")
            declared_length = response.headers.get("content-length")
            if declared_length and declared_length.isdigit() and int(declared_length) > MAX_IMAGE_BYTES:
                raise ValueError("image_too_large")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise ValueError("image_too_large")
                chunks.append(chunk)
            content_type = response.headers.get("content-type", "")
        body = b"".join(chunks)
        if not body:
            raise ValueError("image_empty")
        return body, _detected_image_type(content_type, body)

    async def aclose(self) -> None:
        await self._client.aclose()


def _text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _pick(source: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = _text(source.get(name))
        if value:
            return value
    return None


def _message_time_ms(message: Mapping[str, Any]) -> int | None:
    raw = message.get("sentAtMs", message.get("sent_at_ms", message.get("timestamp")))
    if isinstance(raw, (int, float)):
        value = int(raw)
        return value * 1000 if 0 < value < 10**11 else value
    return None


def _latest_human_seller_time_ms(messages: Sequence[object]) -> int | None:
    timestamps: list[int] = []
    for item in messages[-50:]:
        if not isinstance(item, Mapping) or item.get("agent_generated") is True:
            continue
        if _text(item.get("direction")) not in {"seller", "outbound", "sent", "staff", "human"}:
            continue
        timestamp = _message_time_ms(item)
        if timestamp is not None:
            timestamps.append(timestamp)
    return max(timestamps, default=None)


def _event_time_ms(envelope: Mapping[str, Any]) -> int | None:
    raw = envelope.get("ts", envelope.get("timestamp"))
    if isinstance(raw, (int, float)) and 0 < raw < 10**16:
        return int(raw)
    return None


def _is_pending_order_status(envelope: Mapping[str, Any], messages: Sequence[object]) -> bool:
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    if str(payload.get("messageType", payload.get("message_type", ""))).strip() != "26":
        return False
    if not _pick(payload, "orderId", "order_id"):
        return False
    content = _text(payload.get("content", payload.get("text"))) or ""
    if "待付款" in content:
        return True
    expected_id = _pick(payload, "remoteMessageId", "remote_message_id", "messageId", "message_id")
    for item in reversed(messages[-20:]):
        if not isinstance(item, Mapping):
            continue
        message_id = _pick(item, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id")
        if expected_id and message_id != expected_id:
            continue
        message_type = str(item.get("messageType", item.get("message_type", ""))).strip()
        message_content = _text(item.get("content", item.get("text"))) or ""
        if message_type == "26" and "待付款" in message_content:
            return True
    return False


def _declared_ticket_count(value: str) -> int | None:
    normalized = "".join(value.split())
    matches = re.findall(r"(?<!\d)(\d{1,2})(?:张|人|位|个)(?!票?\d)", normalized)
    chinese_matches = re.findall(r"([一二两三四五六七八九十])(?:张|人|位|个)", normalized)
    chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    counts = {int(item) for item in matches if 1 <= int(item) <= 20}
    counts.update(chinese[item] for item in chinese_matches)
    if re.fullmatch(r"\d{1,2}", normalized) and 1 <= int(normalized) <= 20:
        counts.add(int(normalized))
    if normalized in chinese:
        counts.add(chinese[normalized])
    return next(iter(counts)) if len(counts) == 1 else None


def _cinema_choice(value: str, candidate_count: int) -> int | None:
    normalized = re.sub(r"[，。！？!?、,\.\s]+", "", value).strip()
    match = re.fullmatch(r"(?:选|选择|第)?([1-9])(?:号|个|家)?", normalized)
    if not match:
        return None
    choice = int(match.group(1))
    return choice if choice <= candidate_count else None


def _bare_ticket_count(value: str) -> bool:
    normalized = "".join(value.split())
    return bool(re.fullmatch(r"\d{1,2}", normalized)) or normalized in {
        "一", "二", "两", "三", "四", "五", "六", "七", "八", "九", "十",
    }


def _immediately_follows_ticket_count_prompt(
    messages: Sequence[object], current: Mapping[str, Any],
) -> bool:
    current_id = _pick(current, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id")
    history_ids = {
        _pick(item, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id")
        for item in messages if isinstance(item, Mapping)
    }
    passed_current = not current_id or current_id not in history_ids
    for item in reversed(messages[-50:]):
        if not isinstance(item, Mapping):
            continue
        message_id = _pick(item, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id")
        if not passed_current:
            if message_id == current_id:
                passed_current = True
            continue
        direction = _text(item.get("direction"))
        if direction in {"inbound", "buyer", "received"}:
            return False
        if direction not in {"seller", "outbound", "sent", "staff", "assistant"}:
            continue
        content = _text(item.get("content", item.get("text"))) or ""
        return any(marker in content for marker in ("需要几张", "请问需要几张", "几张票"))
    return False


def _recent_authoritative_quote_signal(
    messages: Sequence[object], reference_time_ms: int | None = None, *, max_age_ms: int | None = None,
) -> bool:
    for item in reversed(messages[-50:]):
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("direction")) not in {"seller", "outbound", "sent", "staff", "assistant"}:
            continue
        if reference_time_ms is not None and max_age_ms is not None:
            sent_at = item.get("sentAtMs", item.get("sent_at_ms"))
            if not isinstance(sent_at, (int, float)) or not 0 <= reference_time_ms - int(sent_at) <= max_age_ms:
                continue
        content = _text(item.get("content", item.get("text"))) or ""
        if re.search(r"(?:[¥￥]\s*)?\d+(?:\.\d{1,2})?\s*(?:元\s*)?(?:一张|/张)", content) or (
            "报价合计" in content
            and re.search(r"(?:[¥￥]\s*)?\d+(?:\.\d{1,2})?\s*元?", content)
        ):
            return True
    return False


def _structured_ticket_request(value: str) -> MovieImageInfo | None:
    text = str(value or "").strip()
    if not text:
        return None

    def field(*labels: str) -> str | None:
        names = "|".join(re.escape(label) for label in labels)
        match = re.search(
            rf"(?:^|[\n；;])\s*(?:{names})\s*[:：]\s*([^\n；;]+)", text, re.IGNORECASE,
        )
        return match.group(1).strip() if match else None

    city = field("城市")
    cinema = field("影院", "影城")
    movie = field("影片", "电影")
    date_text = field("日期")
    showtime = field("场次", "时间", "开场时间")
    hall = field("影厅", "厅")
    seats_text = field("座位", "座号") or ""
    count_text = field("张数", "数量")
    showtime_match = re.search(r"(?<!\d)([0-2]?\d)[:：.]([0-5]\d)(?!\d)", showtime or "")
    if not all((city, cinema, movie, date_text, showtime_match)):
        return None
    start = f"{int(showtime_match.group(1)):02d}:{showtime_match.group(2)}"
    seat_numbers = list(dict.fromkeys(
        match.group(0).replace(" ", "")
        for match in re.finditer(r"\d{1,2}\s*排\s*\d{1,2}\s*座", seats_text)
    ))
    count_match = re.search(r"([1-9]|1\d|20)\s*张", count_text or "")
    count = int(count_match.group(1)) if count_match else len(seat_numbers)
    if seat_numbers and count != len(seat_numbers):
        return None
    return MovieImageInfo(
        platform="buyer_structured_text", city=city, cinema_name=cinema,
        movie_name=movie, date_text=date_text, showtime_start=start, hall_name=hall,
        selected_seats=[SelectedSeat(seat_number=seat) for seat in seat_numbers],
        selected_count_visible=len(seat_numbers), confidence=1,
        missing_fields=[] if count else ["ticket_count"],
        warnings=[] if seat_numbers else ["structured_text_without_specific_seats"],
    )


def _explicit_quote_confirmation(value: str) -> int | None | Literal[False]:
    normalized = re.sub(r"[，。！？!?、,.\s]+", "", value).lower()
    if not normalized or any(marker in normalized for marker in ("不确认", "取消", "不要", "退款")):
        return False
    if normalized in {"确认", "确认报价", "按报价确认", "正确"}:
        return None
    match = re.fullmatch(
        r"(?:(?:确认|按报价确认)|(?:是|否)?(?:需要|要)?)([1-9]|1\d|20|[一二三四五六七八九十两]{1,3})张",
        normalized,
    )
    if not match:
        return False
    count = _declared_ticket_count(match.group(1) + "张")
    return count if count is not None else False


def _ambiguous_quote_confirmation(value: str) -> bool:
    normalized = re.sub(r"[，。！？!?、,.\s]+", "", value).lower()
    if any(marker in normalized for marker in ("不确认", "取消", "不要", "退款")):
        return False
    return (
        _accepts_quote_or_declares_quantity(value)
        or normalized in {"ok", "okay", "嗯", "嗯嗯", "收到", "明白"}
        or "确认" in normalized
    )


def _accepts_quote_or_declares_quantity(value: str) -> bool:
    normalized = "".join(value.lower().replace("＋", "+").split()).strip("，。！？!?~")
    normalized = re.sub(r"[^0-9a-z+\u4e00-\u9fff]", "", normalized)
    if not normalized or any(marker in normalized for marker in ("不需要", "不要", "先不", "考虑下")):
        return False
    if _declared_ticket_count(normalized) is not None:
        return True
    if any(marker in normalized for marker in ("几张", "多少张", "怎么下单")):
        return False
    return normalized in {
        "需要", "需要的", "要", "要的", "可以", "好的", "好", "行", "确认",
        "就这个", "就要这个", "帮我买", "我要买", "可以下单", "可以拍", "现在拍",
    }


def _trade_terms_changed_after(messages: Sequence[object], reference_ms: int) -> bool:
    for item in messages:
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("direction")) not in {"inbound", "buyer", "received"}:
            continue
        sent_at = item.get("sentAtMs", item.get("sent_at_ms"))
        if not isinstance(sent_at, (int, float)) or int(sent_at) <= reference_ms:
            continue
        message_type = str(item.get("messageType", item.get("message_type", ""))).strip()
        if message_type == "2" or item.get("imageUrls") or item.get("image_urls"):
            return True
        if message_type and message_type != "1":
            continue
        content = re.sub(
            r"[，。！？!?、,.\s]+", "",
            _text(item.get("content", item.get("text"))) or "",
        ).lower()
        if re.search(
            r"(?:取消|退款|退票|不要了|不买了|先不买|换|改成|改为|座位|座号|排|"
            r"场次|影院|影城|电影|影片|开场|时间|日期|今天|明天|后天)",
            content,
        ):
            return True
        if _declared_ticket_count(content) is not None:
            return True
    return False


def _recent_ticket_count(messages: Sequence[object]) -> int | None:
    for item in reversed(messages[-50:]):
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("direction")) not in {"inbound", "buyer", "received"}:
            continue
        if str(item.get("messageType", item.get("message_type", ""))).strip() != "1":
            continue
        content = _text(item.get("content", item.get("text")))
        count = _declared_ticket_count(content or "")
        if count is not None:
            return count
    return None


def _apply_declared_ticket_count(quote: RealQuote | None, count: int | None) -> RealQuote | None:
    if quote is None or quote.quote_scope not in {"area_probe", "area_preview"} or count is None:
        return quote
    updates: dict[str, object] = {"ticket_count": count, "needs_ticket_count": False}
    if isinstance(quote.base_unit_cents, int) and quote.base_unit_cents > 0:
        updates["base_total_cents"] = quote.base_unit_cents * count
    if isinstance(quote.unit_quote_cents, int) and quote.unit_quote_cents > 0:
        updates["total_quote_cents"] = quote.unit_quote_cents * count
    return quote.model_copy(update=updates)


def _quantity_order_guidance(
    record: Mapping[str, Any] | None,
    count: int,
    buyer_message: str,
    templates: ReplyTemplates,
) -> str | None:
    if not isinstance(record, Mapping) or not 1 <= count <= 20:
        return None
    scope = str(record.get("quote_scope") or "")
    unit = record.get("unit_quote_cents")
    total: int | None = None
    if scope in {"area_probe", "area_preview"}:
        if isinstance(unit, int) and not isinstance(unit, bool) and unit > 0:
            total = unit * count
    elif scope == "exact_seats":
        quoted_count = record.get("ticket_count")
        candidate_total = record.get("total_quote_cents")
        if (
            isinstance(quoted_count, int) and not isinstance(quoted_count, bool)
            and quoted_count == count
            and isinstance(unit, int) and not isinstance(unit, bool) and unit > 0
            and isinstance(candidate_total, int) and not isinstance(candidate_total, bool)
            and candidate_total > 0
        ):
            total = candidate_total
    if total is None or not isinstance(unit, int) or isinstance(unit, bool) or unit <= 0:
        return None
    normalized = re.sub(r"[，。！？!?、,.\s]+", "", buyer_message)
    if normalized.startswith("是"):
        seat_text = templates.quote_quantity_marked_seat_template
    elif normalized.startswith("否"):
        seat_text = templates.quote_quantity_flexible_seat_template
    else:
        seat_text = templates.quote_quantity_default_seat_template
    variables = {
        "张数": count,
        "报价单价": f"{Decimal(unit) / Decimal(100):.2f}",
        "报价合计": f"{Decimal(total) / Decimal(100):.2f}",
        "座位说明": seat_text,
        "下单引导": render_template(
            templates.order_submit_unpaid_template, {"张数": count},
        ),
    }
    return render_template(templates.quote_quantity_order_guidance_template, variables)


def _purchase_guide_rule(templates: ReplyTemplates):
    candidates = sorted(
        (
            rule for rule in templates.keyword_replies
            if rule.enabled and rule.reply and rule.image_asset_id
            and any("确认" in keyword for keyword in rule.keywords)
        ),
        key=lambda rule: rule.priority,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _looks_like_cinema_clarification(value: str) -> bool:
    compact = "".join(value.split())
    if not 2 <= len(compact) <= 80 or any(marker in compact for marker in ("？", "?", "哪里", "哪家", "什么", "怎么")):
        return False
    return "万达" in compact or compact.endswith(("影院", "影城", "电影院"))


def _cinema_venue_hint(value: str, canonical_city: str) -> str | None:
    compact = "".join(unicodedata.normalize("NFKC", value).split())
    # Buyer messages often combine a venue identity with seats or a trade
    # question. Only the identity prefix may be sent to the authoritative
    # cinema matcher; the whole sentence is never a cinema name.
    compact = re.split(r"[0-9一二三四五六七八九十两]{1,3}排", compact, maxsplit=1)[0]
    compact = re.sub(
        r"(?:这场|这家|有吗|有没有|还有吗|能买吗|能买到吗|可以吗|多少钱|什么价|怎么卖|帮我看看|查一下)+$",
        "",
        compact,
    ).strip("，。！？!?、,.;；:：~～")
    if not compact or compact in {canonical_city, f"{canonical_city}市"}:
        return None
    if not any(marker in compact for marker in ("万达", "影城", "影院", "电影院", "广场", "店")):
        return None
    return compact


def _recognition_from_quote_record(
    record: Mapping[str, Any], seats: Sequence[str],
) -> MovieImageInfo | None:
    quote_date = record.get("quote_date") or record.get("date")
    try:
        parsed_date = date.fromisoformat(str(quote_date)) if quote_date else None
    except ValueError:
        return None
    if not all(str(record.get(field) or "").strip() for field in (
        "city", "cinema", "movie", "showtime_start",
    )) or parsed_date is None:
        return None
    return MovieImageInfo(
        platform="quote_context",
        city=str(record["city"]),
        cinema_name=str(record["cinema"]),
        movie_name=str(record["movie"]),
        date=parsed_date,
        date_text=str(record.get("date_text") or parsed_date),
        showtime_start=str(record["showtime_start"]),
        showtime_end=str(record.get("showtime_end") or "") or None,
        hall_name=str(record.get("hall") or "") or None,
        selected_seats=[SelectedSeat(seat_number=seat) for seat in seats],
        selected_count_visible=len(seats),
        missing_fields=[],
    )


def _explicit_seats_in_buyer_hint(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value)
    seats: list[str] = []
    pattern = re.compile(
        r"(?P<row>\d{1,2})\s*排\s*"
        r"(?P<seats>\d{1,3}(?:\s*座)?"
        r"(?:\s*(?:[、,，/和及与]|以及)\s*\d{1,3}(?:\s*座)?){0,9})",
    )
    for match in pattern.finditer(normalized):
        row = int(match.group("row"))
        seat_numbers = [int(item) for item in re.findall(r"\d{1,3}", match.group("seats"))]
        if not 1 <= row <= 50 or any(not 1 <= seat <= 100 for seat in seat_numbers):
            continue
        for seat in seat_numbers:
            label = f"{row}排{seat}座"
            if label not in seats:
                seats.append(label)
    return tuple(seats)


def _recent_buyer_cinema_hints(messages: Sequence[object], before_ms: int | None) -> tuple[str, ...]:
    candidates: list[tuple[int, str]] = []
    for item in messages[-20:]:
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("direction")) not in {"inbound", "buyer", "received"}:
            continue
        message_type = item.get("messageType", item.get("message_type"))
        if str(message_type or "").strip() != "1":
            continue
        content = _text(item.get("content", item.get("text")))
        sent_at = item.get("sentAtMs", item.get("sent_at_ms"))
        if not content or not 2 <= len(content) <= 80 or not isinstance(sent_at, (int, float)):
            continue
        timestamp = int(sent_at)
        if before_ms is not None and (timestamp >= before_ms or before_ms - timestamp > MAX_IMAGE_AGE_MS):
            continue
        candidates.append((timestamp, content))
    unique: list[str] = []
    for _, content in sorted(candidates, reverse=True):
        if content not in unique:
            unique.append(content)
    return tuple(unique[:3])


def _immediately_previous_buyer_image(
    messages: Sequence[object], current: Mapping[str, Any], event_time_ms: int | None,
) -> str | None:
    current_id = _pick(
        current, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id",
    )
    if not current_id:
        return None
    inbound: list[tuple[int, Mapping[str, Any]]] = []
    for item in messages[-20:]:
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("direction")) not in {"inbound", "buyer", "received"}:
            continue
        sent_at = item.get("sentAtMs", item.get("sent_at_ms"))
        if isinstance(sent_at, (int, float)):
            inbound.append((int(sent_at), item))
    ordered = sorted(inbound, key=lambda value: value[0])
    index = next((
        position for position, (_, item) in enumerate(ordered)
        if _pick(item, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id") == current_id
    ), None)
    if index is None or index == 0:
        return None
    timestamp, previous = ordered[index - 1]
    if event_time_ms is not None and (
        timestamp > event_time_ms + 5 * 60 * 1000
        or event_time_ms - timestamp > MAX_IMAGE_AGE_MS
    ):
        return None
    urls = previous.get("imageUrls", previous.get("image_urls"))
    if not isinstance(urls, list) or not urls:
        return None
    try:
        return validate_image_url(urls[0])
    except ValueError:
        return None


def _identity_context(body: Mapping[str, Any], *, require_order: bool) -> tuple[dict[str, str] | None, str]:
    envelope = body.get("envelope")
    session = body.get("session")
    order_value = body.get("order")
    if not isinstance(envelope, Mapping) or not isinstance(session, Mapping):
        return None, "authoritative_context_missing"
    if require_order and not isinstance(order_value, Mapping):
        return None, "authoritative_context_missing"
    order: Mapping[str, Any] = order_value if isinstance(order_value, Mapping) else {}
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    values = {
        "event_id": _pick(envelope, "id"),
        "tenant_id": _pick(envelope, "tenantId", "tenant_id"),
        "shop_id": _pick(session, "accountUnb", "account_unb"),
        "buyer_id": _pick(session, "peerUnb", "peer_unb"),
        "chat_id": _pick(session, "chatId", "chat_id"),
    }
    order_id = _pick(order, "orderId", "order_id", "platformOrderId", "platform_order_id")
    if require_order or order_id is not None:
        values["order_id"] = order_id
    if any(value is None for value in values.values()):
        return None, "authoritative_context_incomplete"
    expected = {
        "shop_id": [_pick(payload, "accountUnb", "account_unb"), _pick(order, "accountUnb", "account_unb", "shopId", "shop_id")],
        "buyer_id": [_pick(payload, "peerUnb", "peer_unb"), _pick(order, "buyerUnb", "buyer_unb", "peerUnb", "peer_unb", "buyerId", "buyer_id")],
        "chat_id": [_pick(payload, "chatId", "chat_id"), _pick(order, "chatId", "chat_id")],
        "tenant_id": [_pick(order, "tenantId", "tenant_id")],
    }
    if order_id is not None:
        expected["order_id"] = [_pick(payload, "orderId", "order_id")]
    for field, candidates in expected.items():
        if any(candidate is not None and candidate != values[field] for candidate in candidates):
            return None, "order_session_identity_mismatch"
    return {key: str(value) for key, value in values.items()}, "ok"


def _latest_inbound_message(messages: Sequence[object], envelope: Mapping[str, Any]) -> Mapping[str, Any] | None:
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    expected_id = _pick(payload, "remoteMessageId", "remote_message_id", "messageId", "message_id")
    inbound = [
        item for item in messages[-20:]
        if isinstance(item, Mapping) and _text(item.get("direction")) in {"inbound", "buyer", "received"}
    ]
    if expected_id:
        for item in inbound:
            if _pick(item, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id") == expected_id:
                return item
        # FishMore history can lag a webhook by several seconds. Falling back to
        # the newest historical item can process an older screenshot as though
        # it were the buyer's current text and create a confusing duplicate quote.
        return {**payload, "direction": "inbound", "messageId": expected_id}
    return max(
        inbound,
        default=None,
        key=lambda item: int(item.get("sentAtMs", item.get("sent_at_ms", 0)))
        if isinstance(item.get("sentAtMs", item.get("sent_at_ms", 0)), (int, float)) else 0,
    )


def _is_pending_unpaid_order(order: object) -> bool:
    if not isinstance(order, Mapping):
        return False
    status = (_pick(order, "orderStatus", "order_status", "status") or "").strip().lower()
    if _pick(order, "payTime", "pay_time", "paidAt", "paid_at"):
        return False
    if any(marker in status for marker in ("paid", "支付", "付款", "ship", "发货", "complete", "完成", "close", "关闭", "cancel", "取消", "refund", "退款")):
        return False
    return status in {"1", "created", "pending", "pending_payment", "unpaid", "待付款"}


def _authoritative_order_state(order: object) -> str:
    if not isinstance(order, Mapping):
        return "unknown"
    status = (_pick(order, "orderStatus", "order_status", "status") or "").strip().lower()
    if status in {"3", "shipped", "delivered", "已发货", "待收货"}:
        return "shipped"
    if status in {"4", "completed", "finished", "已完成", "交易成功"}:
        return "completed"
    if status in {"2", "paid", "payment_success", "已付款", "支付成功"}:
        return "paid"
    if _pick(order, "payTime", "pay_time", "paidAt", "paid_at"):
        return "paid"
    return "unknown"


def _is_authoritatively_paid(order: object) -> bool:
    return _authoritative_order_state(order) in {"paid", "shipped", "completed"}


def _recent_outbound_exact_text(messages: Sequence[object], wanted: str) -> bool:
    for item in reversed(messages[-50:]):
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("direction")) not in {"seller", "outbound", "sent", "staff", "assistant"}:
            continue
        if (_text(item.get("content", item.get("text"))) or "") == wanted:
            return True
    return False


def _authoritative_status_reply_decision(
    identity: Mapping[str, str], messages: Sequence[object], order_state: str,
    templates: ReplyTemplates,
) -> dict[str, object] | None:
    if order_state in {"shipped", "completed"}:
        reply = templates.order_shipped_template
        reason = "authoritative_shipped_status_reply_ready"
        suffix = "shipment-confirmation"
    elif order_state == "paid":
        reply = templates.payment_success_pending_ticket_template
        reason = "authoritative_payment_confirmation_ready"
        suffix = "payment-confirmation"
    else:
        return None
    if _recent_outbound_exact_text(messages, reply):
        return {"decision": {
            "mode": "auto", "actions": [], "reason": f"{reason}_already_sent",
        }}
    return {"decision": {"mode": "auto", "actions": [{
        "id": f'{identity["event_id"]}:{suffix}',
        "type": "send_message", "order_id": identity.get("order_id"),
        "text": reply, "preserve_on_new_buyer_message": True,
        "rule_governed": True,
    }], "reason": reason}}


def _is_acknowledgement(value: str) -> bool:
    normalized = re.sub(r"[，。！？!?、,.\s]+", "", value).lower()
    return normalized in {
        "ok", "okay", "好的", "好", "好了", "收到", "明白", "知道了", "可以", "嗯", "嗯嗯",
    }


def _is_buyer_deferral(value: str) -> bool:
    normalized = re.sub(r"[，。！？!?、,.\s]+", "", value).lower()
    if any(marker in normalized for marker in (
        "稍等", "等一下", "等一会", "等会", "待会", "稍后", "晚点回复", "一会回复",
    )):
        return True
    return bool(re.search(
        r"(?:我|我先|先)?(?:确认|核对|看看|看下|查下|查一下)(?:一下)?"
        r"(?:时间|日期|场次|行程)",
        normalized,
    ))


def _custom_keyword_rule(value: str, templates: ReplyTemplates):
    normalized = re.sub(r"[，。！？!?、,.\s]+", "", value).lower()
    ranked = sorted(
        (rule for rule in templates.keyword_replies if rule.enabled),
        key=lambda rule: rule.priority,
        reverse=True,
    )
    for rule in ranked:
        for keyword in rule.keywords:
            wanted = re.sub(r"[，。！？!?、,.\s]+", "", keyword).lower()
            if not wanted:
                continue
            matched = normalized == wanted if rule.match_mode == "exact" else wanted in normalized
            if matched:
                return rule
    return None


def _custom_keyword_reply(value: str, templates: ReplyTemplates) -> str | None:
    rule = _custom_keyword_rule(value, templates)
    return rule.reply if rule is not None else None


def _mentions_confirmation(value: str) -> bool:
    normalized = re.sub(r"[，。！？!?、,.\s]+", "", value).lower()
    return "确认" in normalized or "confirm" in normalized


def _is_thanks(value: str) -> bool:
    normalized = re.sub(r"[，。！？!?、,.\s]+", "", value).lower()
    return normalized in {"谢谢", "谢谢你", "谢谢啦", "谢了", "感谢", "感谢你", "thankyou", "thanks", "thx"}


def _is_simple_greeting(value: str) -> bool:
    normalized = re.sub(r"[，。！？!?、,.\s]+", "", value).lower()
    return normalized in {"在吗", "在么", "在不在", "你好", "您好", "hello", "hi"}


def _asks_transaction_status(value: str) -> bool:
    normalized = "".join(value.lower().split())
    return any(marker in normalized for marker in (
        "ok了吗", "好了吗", "订单状态", "改好价", "改价了吗", "付款", "付了", "支付", "出票", "发货",
        "取票", "取码", "票码",
    ))


def _has_protected_transaction_claim(value: str) -> bool:
    normalized = "".join(str(value or "").split())
    return any(marker in normalized for marker in (
        "出票成功", "票码已发", "已经出票", "已出票", "付款成功", "支付成功",
        "改价已完成", "价格已改", "已经发货", "已发货", "退款成功", "已经退款",
        "已退款", "订单已关闭", "可以付款", "可付款", "等待出票",
    ))


def _has_unverified_price_claim(value: str) -> bool:
    """Reject amount-bearing generic AI copy; only the quote engine may state prices."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    compact = "".join(normalized.split())
    return bool(
        re.search(r"[¥￥]\d+(?:\.\d{1,2})?", compact)
        or re.search(
            r"(?<!\d)\d{1,5}(?:\.\d{1,2})?(?:元|块钱?|一张|每张|/张|一套|每套|/套)",
            compact,
        )
    )


def _is_new_flow_transaction(
    envelope: Mapping[str, Any], order: object = None,
) -> bool:
    """Identify the V2 transaction marker without routing legacy business rules."""
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    sources = (envelope, payload, order if isinstance(order, Mapping) else {})
    for source in sources:
        flow_version = _text(source.get("flow_version") or source.get("flowVersion"))
        origin = _text(source.get("source") or source.get("quote_source"))
        if flow_version == "V4_NEW_FLOW_V2" or origin in {"wanda_pricing_v2", "phase_9a_authorization"}:
            return True
    return False


def _business_stage(event_type: str | None, order: object) -> str:
    if event_type == "order.created":
        return "order_pending"
    if event_type == "order.paid":
        return "payment"
    if event_type in {"order.shipped", "order.finished", "order.refund.applied", "order.refund.finished"}:
        return "shipping_refund"
    if isinstance(order, Mapping):
        status = _text(order.get("orderStatus", order.get("order_status", order.get("status"))))
        normalized = (status or "").lower()
        if normalized in {"3", "4"} or any(marker in normalized for marker in ("ship", "finish", "refund", "发货", "退款", "完成")):
            return "shipping_refund"
        if normalized == "2" or any(marker in normalized for marker in ("paid", "payment", "付款", "已支付")):
            return "payment"
        if normalized:
            return "order_pending"
    return "consultation"


def _quote_version(identity: Mapping[str, str], recognition: MovieImageInfo, quote: RealQuote) -> str:
    material = {
        **identity,
        "movie": recognition.movie_name,
        "cinema": quote.matched_cinema_name,
        "date": recognition.date.isoformat() if recognition.date else None,
        "showtime": recognition.showtime_start,
        "seats": [seat.seat_number for seat in recognition.selected_seats],
        "target": quote.total_quote_cents,
        "rule": quote.pricing_rule_version,
        "seat_quotes": [item.model_dump(mode="json") for item in quote.seat_quotes],
    }
    digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"v4q-{digest[:20]}"


def _unverified_order_guard_decision(
    identity: Mapping[str, str],
    *,
    reason: str,
    templates: ReplyTemplates | None = None,
) -> dict[str, object]:
    configured = templates or ReplyTemplates()
    return {"decision": {"mode": "auto", "actions": [{
        "id": f'{identity["event_id"]}:guard-unverified-order',
        "type": "guard_unverified_order",
        "order_id": identity["order_id"],
        "dedupe_key": f'{identity["order_id"]}:guard-unverified-order',
        "unpaid_text": configured.order_pending_without_quote_template,
        "paid_text": configured.payment_manual_review_template,
    }], "reason": reason}}


def _authoritative_order_amount_cents(order: object) -> int | None:
    if not isinstance(order, Mapping):
        return None
    for field in ("payment", "amount_cents", "priceFee", "price_fee"):
        value = order.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        if isinstance(value, str) and re.fullmatch(r"[1-9]\d{0,8}", value.strip()):
            return int(value.strip())
    return None


def build_action_result_decision(
    body: Mapping[str, Any],
    *,
    mode: str,
    templates: ReplyTemplates | None = None,
) -> dict[str, object]:
    result = body.get("result")
    if mode != "auto" or not isinstance(result, Mapping):
        return {"actions": []}
    configured = templates or ReplyTemplates()
    order_id = _pick(result, "order_id")
    target = result.get("target_amount_cents")
    verified = result.get("verified_amount_cents")
    observed_order_amount = result.get("observed_order_amount_cents")
    event_id = _pick(body, "event_id")
    action_id = _pick(body, "action_id")
    if (
        result.get("status") == "skipped"
        and _pick(result, "reason_code") == "order_already_paid"
        and event_id and action_id == f"{event_id}:change-order-price"
        and order_id
        and isinstance(target, int) and target > 0
        and isinstance(verified, int) and verified > 0 and verified != target
        and result.get("auto_refund_eligible") is True
        and isinstance(observed_order_amount, int) and observed_order_amount == verified
    ):
        return {"actions": [{
            "id": f"{event_id}:cancel-paid-amount-mismatch",
            "type": "cancel_paid_amount_mismatch",
            "order_id": order_id,
            "target_amount_cents": target,
            "observed_order_amount_cents": observed_order_amount,
            "observed_amount_cents": verified,
            "refund_authorization": "unchanged_prechange_amount",
            "closed_text": configured.paid_mismatch_closed_template,
            "refund_text": configured.paid_mismatch_refund_template,
        }]}
    result_status = result.get("status")
    if result_status in {"failed", "unknown"}:
        if not event_id or not action_id or action_id != f"{event_id}:change-order-price" or not order_id:
            return {"actions": []}
        text = render_template(
            configured.price_change_failure_template,
            {"失败原因": "结果尚未完成官方核验" if result_status == "unknown" else "平台改价未完成"},
        )
        return {"actions": [{
            "id": f"{event_id}:price-change-{result_status}",
            "type": "send_message", "order_id": order_id, "text": text,
            "preserve_on_new_buyer_message": True,
            "rule_governed": True,
        }]}
    if result_status != "succeeded":
        return {"actions": []}
    if not order_id or not isinstance(target, int) or target <= 0 or (verified is not None and verified != target):
        return {"actions": []}
    if not event_id or not action_id or action_id != f"{event_id}:change-order-price":
        return {"actions": []}
    amount = Decimal(target) / Decimal(100)
    rendered_amount = f"{amount:.2f}"
    text = render_template(
        configured.price_change_confirmation_template,
        {"订单金额": f"{rendered_amount}元"},
    )
    return {"actions": [{
        "id": f"{event_id}:confirm-price-change",
        "type": "send_price_change_confirmation",
        "order_id": order_id,
        "text": text,
        "_completed_action_result": {
            "order_id": order_id,
            "target_amount_cents": target,
            "verified_amount_cents": verified,
        },
    }]}


class RulesFirstDecisionEngine:
    def __init__(
        self,
        recognition_service: RecognitionService,
        quote_service: QuoteService,
        *,
        mode: str | None = None,
        image_loader: ImageLoader | None = None,
        chat_service: ChatService | None = None,
        shop_store: ShopStore | None = None,
        template_provider: Callable[[], ReplyTemplates] | None = None,
        conversation_policy_provider: Callable[[], object] | None = None,
        quote_recorder: Callable[[Mapping[str, Any]], object] | None = None,
        quote_finder: Callable[..., Mapping[str, Any] | None] | None = None,
        quote_confirmer: Callable[..., Mapping[str, Any] | None] | None = None,
        quote_binder: Callable[..., Mapping[str, Any] | None] | None = None,
        quote_order_confirmer: Callable[..., Mapping[str, Any] | None] | None = None,
        pending_cinema_candidate_store: PendingCinemaCandidateStore | None = None,
        ai_assist_enabled: bool = True,
    ) -> None:
        # The rules engine is the only production decision path. ``mode`` is
        # retained solely for isolated unit tests; production is always active
        # and external writes are controlled independently at command claim.
        configured_mode = (mode or "auto").strip().lower()
        self._mode = configured_mode if configured_mode in {"off", "auto"} else "auto"
        self._recognition = recognition_service
        self._quote = quote_service
        self._chat = chat_service
        self._shops = shop_store
        self._templates = template_provider or ReplyTemplates
        self._conversation_policy = conversation_policy_provider
        self._quote_recorder = quote_recorder
        self._quote_finder = quote_finder
        self._quote_confirmer = quote_confirmer
        self._quote_binder = quote_binder
        self._quote_order_confirmer = quote_order_confirmer
        self._pending_candidate_store = pending_cinema_candidate_store
        self._ai_assist_enabled = bool(ai_assist_enabled)
        self._owned_loader = SecureImageLoader() if image_loader is None else None
        self._load_image = image_loader or self._owned_loader
        # Candidate selection must survive the candidate reply and be scoped to
        # one buyer conversation. The source image URL is retained only long
        # enough to bind the official Liangpiao confirmation request; image bytes
        # and credentials are never stored here.
        self._pending_cinema_candidates: dict[str, MovieImageInfo] = {}

    @staticmethod
    def _candidate_key(identity: Mapping[str, str]) -> str:
        return ":".join(identity[field] for field in ("tenant_id", "shop_id", "buyer_id", "chat_id"))

    def _remember_pending_candidate(self, identity: Mapping[str, str], recognition: MovieImageInfo) -> None:
        key = self._candidate_key(identity)
        self._pending_cinema_candidates[key] = recognition
        if self._pending_candidate_store is not None:
            self._pending_candidate_store.save(key, recognition)

    def _get_pending_candidate(self, identity: Mapping[str, str]) -> MovieImageInfo | None:
        key = self._candidate_key(identity)
        pending = self._pending_cinema_candidates.get(key)
        if pending is None and self._pending_candidate_store is not None:
            pending = self._pending_candidate_store.get(key)
            if pending is not None:
                self._pending_cinema_candidates[key] = pending
        return pending

    def _forget_pending_candidate(self, identity: Mapping[str, str]) -> None:
        key = self._candidate_key(identity)
        self._pending_cinema_candidates.pop(key, None)
        if self._pending_candidate_store is not None:
            self._pending_candidate_store.delete(key)

    @property
    def mode(self) -> str:
        return self._mode

    async def aclose(self) -> None:
        if self._owned_loader is not None:
            await self._owned_loader.aclose()

    async def process_event(self, body: Mapping[str, Any]) -> dict[str, object]:
        envelope = body.get("envelope")
        event_type = _pick(envelope, "event") if isinstance(envelope, Mapping) else None
        if self._mode != "auto" or event_type not in {"im.message.received", "order.created", "order.paid", "order.shipped"}:
            return {"decision": {"mode": self._mode, "actions": [], "reason": "automation_inert_for_event"}}
        policy = self._conversation_policy() if self._conversation_policy is not None else None
        generic_ai_reply_enabled = bool(
            self._ai_assist_enabled and getattr(policy, "ai_reply_enabled", True)
        )
        stage_suppresses_generic_ai = False
        if policy is not None:
            if bool(getattr(policy, "stage_gate_enabled", False)):
                stages = ("consultation", "order_pending", "payment", "shipping_refund")
                current_stage = _business_stage(event_type, body.get("order"))
                start = str(getattr(policy, "intervention_start", "consultation"))
                end = str(getattr(policy, "intervention_end", "payment"))
                stage_suppresses_generic_ai = bool(
                    stages.index(current_stage) < stages.index(start)
                    or stages.index(current_stage) > stages.index(end)
                )
        messages = body.get("recent_messages")
        if not isinstance(messages, list):
            return {"decision": {"mode": "auto", "actions": [], "reason": "conversation_snapshot_unavailable"}}
        pending_order_status = bool(
            event_type == "im.message.received"
            and isinstance(envelope, Mapping)
            and _is_pending_order_status(envelope, messages)
        )
        identity, reason = _identity_context(
            body,
            require_order=event_type in {"order.created", "order.paid", "order.shipped"} or pending_order_status,
        )
        if identity is None:
            return {"decision": {"mode": "auto", "actions": [], "reason": reason}}
        if self._shops is not None and not self._shops.is_enabled(identity["tenant_id"], identity["shop_id"]):
            return {"decision": {"mode": "auto", "actions": [], "reason": "shop_automation_disabled"}}
        human_takeover_active = False
        if policy is not None:
            event_time = _event_time_ms(envelope)
            human_time = _latest_human_seller_time_ms(messages)
            delay_seconds = int(getattr(policy, "human_takeover_delay_seconds", 20))
            human_takeover_active = bool(
                event_time is not None and human_time is not None
                and 0 <= event_time - human_time < delay_seconds * 1000
            )
        if event_type == "im.message.received":
            return await self._reply_to_message(
                envelope, identity, messages, body.get("order"),
                suppress_generic_ai=human_takeover_active or stage_suppresses_generic_ai,
                generic_ai_reply_enabled=generic_ai_reply_enabled,
            )
        if event_type in {"order.paid", "order.shipped"}:
            order_state = _authoritative_order_state(body.get("order"))
            status_decision = _authoritative_status_reply_decision(
                identity, messages, order_state, self._templates(),
            )
            if status_decision is not None:
                return status_decision
            return {"decision": {
                "mode": "auto", "actions": [],
                "reason": "shipped_order_state_unverified" if event_type == "order.shipped" else "paid_order_state_unverified",
            }}
        return await self._price_created_order(envelope, identity, messages, body.get("order"))

    def _record_quote(
        self,
        identity: Mapping[str, str],
        envelope: Mapping[str, Any],
        recognition: MovieImageInfo,
        quote: RealQuote | None,
        *,
        source: str,
        quote_error: str | None = None,
    ) -> None:
        if self._quote_recorder is None or (quote is None and not quote_error):
            return
        event_time = _event_time_ms(envelope)
        created_at = datetime.fromtimestamp(event_time / 1000, tz=timezone.utc).isoformat() if event_time else datetime.now(timezone.utc).isoformat()
        record = {
            "record_id": identity["event_id"],
            "tenant_id": identity["tenant_id"],
            "shop_id": identity["shop_id"],
            "buyer_id": identity["buyer_id"],
            "chat_id": identity["chat_id"],
            # A message event may be enriched with the buyer's previous completed order.
            # Fresh image/text quotes stay unbound until a later compatible order is verified.
            "order_id": identity.get("order_id") if source == "order_created" else None,
            "source": source,
            "created_at": created_at,
            "item_id": _pick(
                envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {},
                "itemId", "item_id",
            ),
            "status": "succeeded" if quote is not None else "failed",
            "city": (quote.matched_city_name if quote else None) or recognition.city,
            "cinema": (quote.matched_cinema_name if quote else None) or recognition.cinema_name,
            "movie": recognition.movie_name,
            "quote_date": (
                (quote.quote_date if quote else recognition.date).isoformat()
                if (quote.quote_date if quote else recognition.date) else None
            ),
            "date_text": recognition.date_text,
            "showtime_start": (
                quote.matched_showtime_start if quote and quote.matched_showtime_start
                else recognition.showtime_start
            ),
            "showtime_end": (
                quote.matched_showtime_end if quote and quote.matched_showtime_end
                else recognition.showtime_end
            ),
            "hall": quote.matched_hall_name if quote and quote.matched_hall_name else recognition.hall_name,
            "seat_display": recognition.seat_display,
        }
        if quote is not None:
            record.update({
                "quote_scope": quote.quote_scope,
                "seat_zone_type": quote.seat_zone_type,
                "ticket_count": quote.ticket_count,
                "member_unit_price_cents": quote.member_unit_price_cents,
                "original_unit_price_cents": quote.original_unit_price_cents,
                "seat_type": quote.seat_type,
                "base_unit_cents": quote.base_unit_cents,
                "base_total_cents": quote.base_total_cents,
                "price_source": quote.price_source,
                "unit_quote_cents": quote.unit_quote_cents,
                "total_quote_cents": quote.total_quote_cents,
                "pricing_rule_version": quote.pricing_rule_version,
            })
        else:
            record["failure_reason"] = quote_error
        try:
            self._quote_recorder(record)
        except Exception:
            LOGGER.exception("event=quote_record_save_failed record_id=%s", identity["event_id"])

    def _quote_record_context(
        self, identity: Mapping[str, str], envelope: Mapping[str, Any], order: object = None,
    ) -> dict[str, Any]:
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        order_data = order if isinstance(order, Mapping) else {}
        event_time = _event_time_ms(envelope)
        return {
            "tenant_id": identity["tenant_id"],
            "shop_id": identity["shop_id"],
            "buyer_id": identity["buyer_id"],
            "chat_id": identity["chat_id"],
            "item_id": _pick(payload, "itemId", "item_id") or _pick(order_data, "itemId", "item_id"),
            "at": datetime.fromtimestamp(event_time / 1000, tz=timezone.utc) if event_time else datetime.now(timezone.utc),
        }

    def _find_quote_record(
        self, identity: Mapping[str, str], envelope: Mapping[str, Any], order: object = None,
        *, confirmed: bool = False,
    ) -> Mapping[str, Any] | None:
        if self._quote_finder is None:
            return None
        try:
            return self._quote_finder(
                **self._quote_record_context(identity, envelope, order), confirmed=confirmed,
            )
        except Exception:
            LOGGER.exception("event=quote_record_lookup_failed event_id=%s", identity["event_id"])
            return None

    def _confirm_quote_record(
        self, identity: Mapping[str, str], envelope: Mapping[str, Any], ticket_count: int | None,
    ) -> Mapping[str, Any] | None:
        if self._quote_confirmer is None:
            return None
        context = self._quote_record_context(identity, envelope)
        confirmed_at = context.pop("at")
        try:
            return self._quote_confirmer(
                **context,
                confirmation_id=identity["event_id"],
                ticket_count=ticket_count,
                confirmed_at=confirmed_at,
            )
        except Exception:
            LOGGER.exception("event=quote_confirmation_save_failed event_id=%s", identity["event_id"])
            return None

    def _bind_quote_record(
        self, identity: Mapping[str, str], envelope: Mapping[str, Any], record_id: str,
    ) -> None:
        if self._quote_binder is None:
            return
        context = self._quote_record_context(identity, envelope)
        bound_at = context.pop("at")
        context.pop("item_id", None)
        try:
            self._quote_binder(
                **context, record_id=record_id, order_id=identity["order_id"], bound_at=bound_at,
            )
        except Exception:
            LOGGER.exception("event=quote_order_binding_save_failed event_id=%s", identity["event_id"])

    def _confirm_quote_from_later_order(
        self, identity: Mapping[str, str], envelope: Mapping[str, Any],
        messages: Sequence[object], order: object, *, allow_implicit: bool,
    ) -> Mapping[str, Any] | None:
        if self._quote_order_confirmer is None or not isinstance(order, Mapping):
            return None
        is_order_signal = (
            _pick(envelope, "event") == "order.created"
            or _is_pending_order_status(envelope, messages)
        )
        if not is_order_signal or not _is_pending_unpaid_order(order):
            return None
        candidate = self._find_quote_record(identity, envelope, order)
        if not isinstance(candidate, Mapping) or candidate.get("delivery_state") != "delivered":
            return None
        # Marketplace listing quantity is not a ticket-count fact. Ticket count
        # comes only from the delivered quote or the buyer's explicit confirmation.
        count = candidate.get("confirmed_ticket_count") or candidate.get("ticket_count")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20:
            return None
        has_explicit_confirmation = bool(
            candidate.get("confirmation_source") == "buyer_message"
            and candidate.get("confirmation_version")
        )
        if not allow_implicit and not has_explicit_confirmation:
            return None
        try:
            created = datetime.fromisoformat(str(candidate.get("created_at") or "").replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        event_time_ms = _event_time_ms(envelope)
        if event_time_ms is None or int(created.timestamp() * 1000) >= event_time_ms:
            return None
        if _trade_terms_changed_after(messages, int(created.timestamp() * 1000)):
            return None
        context = self._quote_record_context(identity, envelope, order)
        order_created_at = context.pop("at")
        try:
            return self._quote_order_confirmer(
                **context,
                order_id=identity["order_id"], order_created_at=order_created_at,
                ticket_count=count, confirmation_id=identity["event_id"],
            )
        except Exception:
            LOGGER.exception("event=implicit_order_confirmation_failed event_id=%s", identity["event_id"])
            return None

    def _confirmed_quote_decision(
        self, identity: Mapping[str, str], envelope: Mapping[str, Any], order: object,
        *, confirmed_record: Mapping[str, Any] | None = None,
    ) -> dict[str, object] | None:
        if _is_new_flow_transaction(envelope, order):
            return None
        record = confirmed_record or self._find_quote_record(identity, envelope, order, confirmed=True)
        if (
            not isinstance(record, Mapping)
            or record.get("delivery_state") != "delivered"
            or not isinstance(order, Mapping)
        ):
            return None
        # A confirmation already bound to another order cannot authorize a new
        # platform write, even when the previous order later became terminal.
        # The buyer must receive a fresh persistent confirmation for the new order.
        bound_order_id = str(record.get("order_id") or "").strip()
        if bound_order_id and bound_order_id != identity["order_id"]:
            return None
        count = record.get("confirmed_ticket_count")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20:
            return None
        scope = str(record.get("quote_scope") or "")
        zone = str(record.get("seat_zone_type") or "").upper()
        target: int | None = None
        if scope == "exact_seats":
            candidate = record.get("total_quote_cents")
            if isinstance(candidate, int) and candidate > 0 and str(record.get("seat_display") or "") != "W+座位":
                target = candidate
        elif scope in {"area_probe", "area_preview"} and zone == "W+":
            unit = record.get("unit_quote_cents")
            if isinstance(unit, int) and unit > 0:
                target = unit * count
        if target is None or target <= 0 or target > 200_000:
            return None
        confirmation_version = str(record.get("confirmation_version") or "").strip()
        quote_expires_at = str(record.get("quote_expires_at") or "").strip()
        record_id = str(record.get("record_id") or "").strip()
        if not confirmation_version or not quote_expires_at or not record_id:
            return None
        self._bind_quote_record(identity, envelope, record_id)
        if _authoritative_order_amount_cents(order) == target:
            amount = Decimal(target) / Decimal(100)
            text = render_template(
                self._templates().price_change_confirmation_template,
                {"订单金额": f"{amount:.2f}元"},
            )
            return {"decision": {"mode": "auto", "actions": [{
                "id": f'{identity["event_id"]}:order-amount-confirmation',
                "type": "send_message", "order_id": identity["order_id"], "text": text,
                "rule_governed": True,
            }], "reason": "bound_order_amount_already_matches_quote"}}
        snapshot = {
            "quote_version": str(record.get("quote_version") or f"{record_id}:{confirmation_version}"),
            "quote_record_id": record_id,
            "terms_fingerprint": str(record.get("terms_fingerprint") or ""),
            "supersedes_quote_id": str(record.get("supersedes_quote_id") or ""),
            "confirmation_version": confirmation_version,
            "confirmation_source": str(record.get("confirmation_source") or "buyer_message"),
            "quote_expires_at": quote_expires_at,
            "confirmed_ticket_count": count,
            "order_id": identity["order_id"],
            "tenant_id": identity["tenant_id"],
            "shop_id": identity["shop_id"],
            "buyer_id": identity["buyer_id"],
            "chat_id": identity["chat_id"],
            "target_amount_cents": target,
        }
        observed_order_amount = _authoritative_order_amount_cents(order)
        if observed_order_amount is not None:
            snapshot["observed_order_amount_cents"] = observed_order_amount
        return {"decision": {"mode": "auto", "actions": [{
            "id": f'{identity["event_id"]}:change-order-price',
            "type": "change_order_price", "quote_snapshot": snapshot,
        }], "reason": "confirmed_quote_record_bound_to_order"}}

    async def _canonical_city_hint(self, value: str) -> str | None:
        canonicalizer = getattr(self._quote, "canonical_city_hint", None)
        if callable(canonicalizer):
            try:
                canonical = await canonicalizer(value)
                return str(canonical).strip() if canonical else None
            except Exception:
                return None
        resolver = getattr(self._quote, "is_known_city_hint", None)
        if not callable(resolver):
            return None
        try:
            return value if await resolver(value) else None
        except Exception:
            return None

    async def _is_known_city_hint(self, value: str) -> bool:
        return await self._canonical_city_hint(value) is not None

    async def _reprice_after_cinema_choice(
        self,
        envelope: Mapping[str, Any],
        identity: Mapping[str, str],
        messages: Sequence[object],
        pending: MovieImageInfo,
        choice: int,
    ) -> dict[str, object]:
        conversation_id = f'{identity["tenant_id"]}:{identity["shop_id"]}:{identity["chat_id"]}'
        confirm = getattr(self._recognition, "confirm_recognition_candidate", None)
        candidate = pending.candidate_cinemas[choice - 1]
        if not callable(confirm) or not pending.recognition_id:
            return {"decision": {"mode": "auto", "actions": [], "reason": "liangpiao_candidate_confirmation_unavailable"}}
        try:
            recognition = await confirm(pending.recognition_id, candidate.cinema_id)
            selected_candidate_confirmed = bool(
                len(recognition.candidate_cinemas) == 1
                and recognition.candidate_cinemas[0].cinema_id == candidate.cinema_id
            )
            if recognition.match_level != "EXACT" and not selected_candidate_confirmed:
                if recognition.candidate_cinemas:
                    self._remember_pending_candidate(identity, recognition)
                    reply = build_recognition_reply(recognition, templates=self._templates())
                    return {"decision": {"mode": "auto", "actions": [{
                        "id": f'{identity["event_id"]}:cinema-candidates', "type": "send_message", "text": reply,
                        "rule_governed": True,
                    }], "reason": "liangpiao_candidate_confirmation_still_ambiguous"}}
                return {"decision": {"mode": "auto", "actions": [], "reason": "liangpiao_candidate_confirmation_not_exact"}}
            if selected_candidate_confirmed:
                # The buyer, not the system, selected this cinema. Liangpiao may
                # retain matchLevel=CANDIDATE and omit showId after confirm even
                # though only that cinema remains. Clear advisory candidates so
                # rendering cannot ask for the same selection again; Wanda's
                # authoritative matcher resolves the show from the confirmed
                # city/cinema/movie/date/time/hall fields and still fails closed.
                recognition = recognition.model_copy(update={"candidate_cinemas": []})
            quote = await self._quote.quote(recognition)
            quote = _apply_declared_ticket_count(quote, _recent_ticket_count(messages[:-1]))
            self._record_quote(identity, envelope, recognition, quote, source="buyer_cinema_choice")
            remember = getattr(self._chat, "remember_image_context", None)
            if callable(remember):
                remember(conversation_id, recognition, quote, None)
            self._forget_pending_candidate(identity)
            reply = build_recognition_reply(recognition, quote=quote, templates=self._templates())
            return {"decision": {"mode": "auto", "actions": [{
                "id": f'{identity["event_id"]}:repriced-quote', "type": "send_message", "text": reply,
                "rule_governed": True,
            }], "reason": "liangpiao_cinema_confirmed_repriced"}}
        except RecognitionError as error:
            return {"decision": {"mode": "auto", "actions": [], "reason": "liangpiao_reprice_failed", "error": error.code}}
        except Exception:
            LOGGER.exception("event=liangpiao_reprice_failed event_id=%s", identity["event_id"])
            return {"decision": {"mode": "auto", "actions": [], "reason": "liangpiao_reprice_failed"}}

    async def _recognize_and_quote(
        self,
        image_url: str,
        *,
        cinema_hints: Sequence[str] = (),
        _verification_retry: bool = False,
    ) -> tuple[MovieImageInfo, RealQuote | None, str | None]:
        recognize_from_url = getattr(self._recognition, "recognize_from_url", None)
        verification_instruction = (
            "第一次识别未能匹配官方场次。请重新逐字核对图片底部影片信息栏："
            "日期必须来自‘今天/明天/月日’，开场时间必须来自场次时间段的第一个时间；"
            "不要把手机状态栏时间、结束时间、座位号或价格识别成开场时间。"
            if _verification_retry else ""
        )
        if callable(recognize_from_url):
            recognition = await recognize_from_url(image_url)
        else:
            image, content_type = await self._load_image(image_url)
            recognition = await self._recognition.recognize(
                image, content_type, buyer_message=verification_instruction,
            )
        if cinema_hints:
            explicit_seats = _explicit_seats_in_buyer_hint(cinema_hints[0])
            if explicit_seats:
                recognition = recognition.model_copy(update={
                    "selected_seats": [SelectedSeat(seat_number=seat) for seat in explicit_seats],
                    "selected_count_visible": len(explicit_seats),
                    "missing_fields": [
                        field for field in recognition.missing_fields if field != "ticket_count"
                    ],
                })
        complete_cinema = getattr(self._quote, "complete_cinema", None)
        if callable(complete_cinema):
            city_hints: list[tuple[str, str | None]] = []
            cinema_name_hints: list[str] = []
            for hint in cinema_hints:
                canonical_city = await self._canonical_city_hint(hint)
                if canonical_city:
                    venue_hint = _cinema_venue_hint(hint, canonical_city)
                    candidate = (canonical_city, venue_hint)
                    if candidate not in city_hints:
                        city_hints.append(candidate)
                elif hint not in cinema_name_hints:
                    cinema_name_hints.append(hint)
            if city_hints:
                completed_by_cinema: dict[tuple[str | None, str | None], MovieImageInfo] = {}
                for city_hint, venue_hint in city_hints:
                    try:
                        completed = await complete_cinema(recognition.model_copy(update={
                            "city": city_hint,
                            "cinema_name": venue_hint or recognition.cinema_name,
                            "missing_fields": [
                                field for field in recognition.missing_fields if field != "city"
                            ],
                        }))
                    except RecognitionError:
                        continue
                    completed_by_cinema[(completed.city, completed.cinema_name)] = completed
                if len(completed_by_cinema) != 1:
                    return recognition, None, "买家补充的城市与截图影院无法唯一对应，请核对城市或完整影院名称。"
                recognition = next(iter(completed_by_cinema.values()))
            else:
                try:
                    recognition = await complete_cinema(recognition)
                except RecognitionError:
                    completed_by_cinema = {}
                    for hint in cinema_name_hints:
                        try:
                            completed = await complete_cinema(recognition.model_copy(update={
                                "cinema_name": hint,
                                # The buyer supplied a venue identity. Allow the
                                # authoritative cache matcher to infer its city only
                                # when that venue resolves uniquely; ambiguity still
                                # fails closed inside complete_cinema/_resolve_cinema.
                                "missing_fields": [
                                    field for field in recognition.missing_fields
                                    if field not in {"city", "cinema_name"}
                                ],
                            }))
                        except RecognitionError:
                            continue
                        completed_by_cinema[(completed.city, completed.cinema_name)] = completed
                    if len(completed_by_cinema) == 1:
                        recognition = next(iter(completed_by_cinema.values()))
        if recognition.match_level == "CANDIDATE":
            # A candidate cinema is not an authoritative venue. Send the
            # numbered choices first; pricing starts only after confirm.
            return recognition, None, None
        try:
            quote = await self._quote.quote(recognition)
            if quote.matched_cinema_name or quote.matched_city_name or quote.matched_movie_name:
                completed_fields = set()
                if quote.matched_city_name:
                    completed_fields.add("city")
                if quote.matched_movie_name:
                    completed_fields.add("movie_name")
                recognition = recognition.model_copy(update={
                    "cinema_name": quote.matched_cinema_name or recognition.cinema_name,
                    "city": quote.matched_city_name or recognition.city,
                    "movie_name": quote.matched_movie_name or recognition.movie_name,
                    "missing_fields": [
                        field for field in recognition.missing_fields if field not in completed_fields
                    ],
                })
            return recognition, quote, None
        except RecognitionError as error:
            retryable_mismatch = any(marker in error.message for marker in (
                "候选影院", "影片、日期和开场时间", "场次不存在", "没有匹配场次",
            ))
            if retryable_mismatch and not _verification_retry:
                return await self._recognize_and_quote(
                    image_url, cinema_hints=cinema_hints, _verification_retry=True,
                )
            return recognition, None, error.message
        except Exception:
            return recognition, None, "万达实时报价暂时不可用，请稍后重试。"

    async def _reply_to_message(
        self,
        envelope: Mapping[str, Any],
        identity: Mapping[str, str],
        messages: list[object],
        order: object = None,
        *,
        suppress_generic_ai: bool = False,
        generic_ai_reply_enabled: bool = True,
    ) -> dict[str, object]:
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        payload_message_type = str(payload.get("messageType", payload.get("message_type", ""))).strip()
        order_state = _authoritative_order_state(order)
        payload_content = _text(payload.get("content", payload.get("text"))) or ""
        if (
            (payload_message_type == "26" and any(marker in payload_content for marker in ("已付款", "等待你发货")))
            or (payload_message_type == "14" and "发货" in payload_content)
        ):
            status_decision = _authoritative_status_reply_decision(
                identity, messages, order_state, self._templates(),
            )
            if status_decision is not None:
                return status_decision
        if payload_message_type == "26":
            if _is_pending_order_status(envelope, messages):
                pricing = await self._price_created_order(envelope, identity, messages, order)
                decision = pricing.get("decision") if isinstance(pricing, Mapping) else None
                actions = decision.get("actions") if isinstance(decision, Mapping) else None
                if isinstance(actions, list) and any(
                    isinstance(action, Mapping) and action.get("type") == "change_order_price"
                    for action in actions
                ):
                    # Do not send a promise before the platform mutation is verified.
                    # The executor's durable action report is the only source allowed
                    # to produce a successful price-change confirmation.
                    return {"decision": {
                        "mode": "auto", "actions": actions,
                        "reason": "pending_order_authoritative_quote_ready",
                    }}
                return pricing
            return {"decision": {
                "mode": "auto", "actions": [], "reason": "platform_transaction_status_deferred",
            }}
        payload_urls = payload.get("imageUrls", payload.get("image_urls"))
        if payload_message_type and payload_message_type not in {"1", "2"} and not (isinstance(payload_urls, list) and payload_urls):
            return {"decision": {
                "mode": "auto", "actions": [], "reason": "platform_non_buyer_text_deferred",
            }}
        current = _latest_inbound_message(messages, envelope)
        if current is None:
            return {"decision": {"mode": "auto", "actions": [], "reason": "current_buyer_message_unavailable"}}
        message_type = current.get("messageType", current.get("message_type", payload.get("messageType")))
        normalized_message_type = str(message_type or "").strip()
        urls = current.get("imageUrls", current.get("image_urls"))
        if normalized_message_type not in {"1", "2"} and not (isinstance(urls, list) and urls):
            return {"decision": {
                "mode": "auto", "actions": [], "reason": "platform_non_buyer_text_deferred",
            }}
        conversation_id = f'{identity["tenant_id"]}:{identity["shop_id"]}:{identity["chat_id"]}'
        synchronize = getattr(self._chat, "sync_platform_history", None)
        if callable(synchronize):
            current_message_id = _pick(
                current,
                "messageId", "message_id", "remoteMessageId", "remote_message_id", "id",
            )
            try:
                synchronize(
                    conversation_id,
                    messages,
                    current_message_id=current_message_id,
                    reference_time_ms=_event_time_ms(envelope),
                )
            except Exception:
                return {"decision": {"mode": "auto", "actions": [], "reason": "conversation_history_sync_failed"}}
        decision_reason = "automatic_reply_ready"
        generic_ai_reply = False
        image_workflow = False
        direct_image_workflow = False
        keyword_rule = None
        if isinstance(urls, list) and urls:
            image_workflow = True
            direct_image_workflow = True
            if not self._ai_assist_enabled:
                reply = self._templates().ai_disabled_structured_intake_template
                decision_reason = "ai_assist_disabled_structured_intake_ready"
                recognition = None
                quote = None
                quote_error = None
            else:
                try:
                    image_url = validate_image_url(urls[0])
                    sent_at = current.get("sentAtMs", current.get("sent_at_ms"))
                    before_ms = int(sent_at) if isinstance(sent_at, (int, float)) else _event_time_ms(envelope)
                    recognition, quote, quote_error = await self._recognize_and_quote(
                        image_url,
                        cinema_hints=_recent_buyer_cinema_hints(messages, before_ms),
                    )
                    quote = _apply_declared_ticket_count(quote, _recent_ticket_count(messages))
                    self._record_quote(
                        identity, envelope, recognition, quote,
                        source="buyer_image", quote_error=quote_error,
                    )
                    if recognition.match_level == "CANDIDATE" and recognition.candidate_cinemas and quote is None:
                        self._remember_pending_candidate(identity, recognition)
                    else:
                        self._forget_pending_candidate(identity)
                except Exception:
                    reply = render_template(
                        self._templates().recognition_failure_other_template,
                        {"失败原因": "图片暂时无法读取或识别"},
                    )
                    decision_reason = "image_recognition_failure_reply_ready"
                else:
                    remember = getattr(self._chat, "remember_image_context", None)
                    if callable(remember):
                        remember(conversation_id, recognition, quote, quote_error)
                    if (
                        quote is None
                        and _is_pending_unpaid_order(order)
                        and not recognition.movie_name
                        and not recognition.cinema_name
                        and not recognition.showtime_start
                    ):
                        reply = self._templates().order_pending_without_quote_template
                        decision_reason = "confirmed_quote_unavailable"
                    else:
                        reply = build_recognition_reply(
                            recognition,
                            quote=quote,
                            quote_error=quote_error,
                            templates=self._templates(),
                        )
        else:
            message = _text(current.get("content", current.get("text")))
            if not message or len(message) > 2_000:
                return {"decision": {"mode": "auto", "actions": [], "reason": "replyable_text_unavailable"}}
            pending = self._get_pending_candidate(identity)
            if pending is not None:
                choice = _cinema_choice(message, len(pending.candidate_cinemas))
                if choice is None:
                    reply = build_recognition_reply(pending, templates=self._templates())
                    return {"decision": {"mode": "auto", "actions": [{
                        "id": f'{identity["event_id"]}:cinema-choice-reminder', "type": "send_message", "text": reply,
                        "rule_governed": True,
                    }], "reason": "liangpiao_cinema_choice_required"}}
                return await self._reprice_after_cinema_choice(
                    envelope, identity, messages, pending, choice,
                )
            if _is_buyer_deferral(message):
                return {"decision": {
                    "mode": "auto", "actions": [], "reason": "buyer_deferral_no_reply",
                }}
            reference_time_ms = _event_time_ms(envelope)
            templates = self._templates()
            keyword_rule = _custom_keyword_rule(message, templates)
            explicit_confirmation = _explicit_quote_confirmation(message)
            declared_ticket_count = _declared_ticket_count(message)
            if (
                declared_ticket_count is not None and _bare_ticket_count(message)
                and not _immediately_follows_ticket_count_prompt(messages, current)
            ):
                declared_ticket_count = None
            has_explicit_confirmation = (
                explicit_confirmation is not False or declared_ticket_count is not None
            )
            durable_quote = self._find_quote_record(identity, envelope, order)
            active_quote_signal = _recent_authoritative_quote_signal(
                messages, reference_time_ms, max_age_ms=MAX_QUOTE_CONFIRMATION_AGE_MS,
            )
            historical_quote_signal = _recent_authoritative_quote_signal(messages)
            order_state = _authoritative_order_state(order)
            quote_bound_order_id = _pick(durable_quote, "order_id") if isinstance(durable_quote, Mapping) else None
            current_order_id = identity.get("order_id") or (
                _pick(order, "orderId", "order_id") if isinstance(order, Mapping) else None
            )
            lifecycle_order_relevant = bool(
                not active_quote_signal
                or (quote_bound_order_id and quote_bound_order_id == current_order_id)
            )
            explicit_seats = _explicit_seats_in_buyer_hint(message)
            if explicit_seats and isinstance(durable_quote, Mapping):
                # A seat list is an explicit purchase instruction in response to
                # the quote's seat prompt. Reprice those seats authoritatively,
                # then go straight to the unpaid-order guide without asking for
                # a second confirmation. The old area quote is never reused.
                seat_request = _recognition_from_quote_record(durable_quote, explicit_seats)
                if seat_request is None:
                    reply = self._templates().quote_ticket_count_request_template
                    decision_reason = "seat_selection_context_unavailable"
                else:
                    try:
                        quote = await self._quote.quote(seat_request)
                        self._record_quote(
                            identity, envelope, seat_request, quote,
                            source="buyer_seat_selection",
                        )
                        quote_text = build_recognition_reply(
                            seat_request, quote=quote, templates=self._templates(),
                        )
                        order_guide = render_template(
                            self._templates().order_submit_unpaid_template,
                            {"张数": len(explicit_seats)},
                        )
                        reply = f"{quote_text}\n\n{order_guide}"
                        decision_reason = "seat_selection_order_guidance_ready"
                    except RecognitionError as error:
                        reply = build_recognition_reply(
                            seat_request, quote=None, quote_error=error.message,
                            templates=self._templates(),
                        )
                        decision_reason = "seat_selection_quote_unavailable"
                keyword_rule = None
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            elif (structured_request := _structured_ticket_request(message)) is not None:
                try:
                    quote = await self._quote.quote(structured_request)
                    self._record_quote(
                        identity, envelope, structured_request, quote, source="buyer_text",
                    )
                    reply = build_recognition_reply(
                        structured_request, quote=quote, templates=self._templates(),
                    )
                    decision_reason = "structured_text_quote_ready"
                except RecognitionError as error:
                    reply = build_recognition_reply(
                        structured_request, quote=None, quote_error=error.message,
                        templates=self._templates(),
                    )
                    decision_reason = "structured_text_quote_unavailable"
                keyword_rule = None
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            elif lifecycle_order_relevant and order_state in {"shipped", "completed"} and (
                _asks_transaction_status(message)
                or _is_acknowledgement(message)
                or _is_thanks(message)
                or _mentions_confirmation(message)
                or _accepts_quote_or_declares_quantity(message)
            ):
                reply = self._templates().order_shipped_template
                decision_reason = "authoritative_shipped_status_reply_ready"
                keyword_rule = None
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            elif lifecycle_order_relevant and order_state == "paid" and (
                _asks_transaction_status(message) or _is_acknowledgement(message)
            ):
                reply = self._templates().payment_success_pending_ticket_template
                decision_reason = "authoritative_paid_status_reply_ready"
                keyword_rule = None
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            elif _is_simple_greeting(message):
                reply = self._templates().guidance_template
                decision_reason = "guidance_reply_ready"
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            elif (
                has_explicit_confirmation
                and historical_quote_signal
                and not active_quote_signal
                and durable_quote is None
            ):
                reply = self._templates().quote_expired_template
                decision_reason = "authoritative_quote_expired_reply_ready"
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            elif (
                (active_quote_signal or durable_quote is not None)
                and has_explicit_confirmation
            ):
                durable_count = (
                    durable_quote.get("confirmed_ticket_count") or durable_quote.get("ticket_count")
                    if isinstance(durable_quote, Mapping) else None
                )
                ticket_count = (
                    explicit_confirmation
                    if isinstance(explicit_confirmation, int) and not isinstance(explicit_confirmation, bool)
                    else declared_ticket_count or _recent_ticket_count(messages)
                )
                if ticket_count is None and isinstance(durable_count, int) and not isinstance(durable_count, bool):
                    ticket_count = durable_count
                confirmed_quote = self._confirm_quote_record(identity, envelope, ticket_count)
                if isinstance(order, Mapping) and ticket_count is not None:
                    pricing = self._confirmed_quote_decision(identity, envelope, order)
                    if pricing is not None:
                        return self._with_keyword_actions(pricing, identity, keyword_rule)
                purchase_guide_rule = _purchase_guide_rule(self._templates()) if ticket_count is not None else None
                quantity_guidance = (
                    _quantity_order_guidance(
                        confirmed_quote if isinstance(confirmed_quote, Mapping) else durable_quote,
                        ticket_count, message, self._templates(),
                    )
                    if ticket_count is not None else None
                )
                if ticket_count is None:
                    reply = self._templates().quote_ticket_count_request_template
                elif purchase_guide_rule is not None:
                    # An explicit confirmation should use the configured
                    # purchase guide as the single buyer-facing response. The
                    # paired image is appended by _with_keyword_actions.
                    reply = purchase_guide_rule.reply
                    keyword_rule = purchase_guide_rule
                elif quantity_guidance:
                    reply = quantity_guidance
                elif keyword_rule is not None:
                    reply = keyword_rule.reply
                else:
                    reply = render_template(
                        self._templates().order_submit_unpaid_template,
                        {"张数": ticket_count},
                    )
                decision_reason = "order_submission_guidance_ready"
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            elif (
                (active_quote_signal or durable_quote is not None)
                and _ambiguous_quote_confirmation(message)
            ):
                # Do not force buyers through a magic “确认报价” phrase. A clear
                # quantity is handled above as acceptance; other ambiguous
                # acknowledgements simply ask for the quantity and remain
                # unconfirmed until the buyer supplies it or creates an order.
                reply = self._templates().quote_ticket_count_request_template
                decision_reason = "quote_ticket_count_request_ready"
                keyword_rule = None
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            elif _is_acknowledgement(message):
                return {"decision": {"mode": "auto", "actions": [], "reason": "acknowledgement_no_reply"}}
            elif keyword_rule is not None:
                reply = keyword_rule.reply
                decision_reason = "custom_keyword_reply_ready"
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            else:
                event_time_ms = _event_time_ms(envelope)
                is_identity_clarification = (
                    _looks_like_cinema_clarification(message)
                    or await self._is_known_city_hint(message)
                )
                prior_image = (
                    _immediately_previous_buyer_image(messages, current, event_time_ms)
                    if is_identity_clarification else None
                )
                is_context_clarification = is_identity_clarification
            if decision_reason in {
                "authoritative_paid_status_reply_ready", "authoritative_shipped_status_reply_ready",
                "guidance_reply_ready", "order_submission_guidance_ready",
                "authoritative_quote_expired_reply_ready", "quote_ticket_count_request_ready",
                "custom_keyword_reply_ready", "structured_text_quote_ready",
                "structured_text_quote_unavailable", "seat_selection_order_guidance_ready",
                "seat_selection_context_unavailable", "seat_selection_quote_unavailable",
            }:
                pass
            elif prior_image and is_context_clarification:
                image_workflow = True
                try:
                    recent_hints = tuple(
                        hint for hint in _recent_buyer_cinema_hints(messages, event_time_ms)
                        if hint != message
                    )
                    recognition, quote, quote_error = await self._recognize_and_quote(
                        prior_image,
                        cinema_hints=((message,) if is_identity_clarification else ()) + recent_hints,
                    )
                    quote = _apply_declared_ticket_count(quote, _recent_ticket_count(messages))
                    self._record_quote(
                        identity, envelope, recognition, quote,
                        source="contextual_image", quote_error=quote_error,
                    )
                except Exception:
                    reply = render_template(
                        self._templates().recognition_failure_other_template,
                        {"失败原因": "图片暂时无法读取或识别"},
                    )
                    decision_reason = "image_recognition_failure_reply_ready"
                else:
                    remember = getattr(self._chat, "remember_image_context", None)
                    if callable(remember):
                        remember(conversation_id, recognition, quote, quote_error)
                    reply = build_recognition_reply(
                        recognition,
                        quote=quote,
                        quote_error=quote_error,
                        templates=self._templates(),
                    )
                    decision_reason = "contextual_image_reply_ready"
            else:
                if suppress_generic_ai:
                    return {"decision": {
                        "mode": "auto", "actions": [], "reason": "human_takeover_cooldown",
                    }}
                if not self._ai_assist_enabled or self._chat is None:
                    return {"decision": {"mode": "auto", "actions": [{
                        "id": f'{identity["event_id"]}:reply', "type": "send_message",
                        "text": self._templates().ai_disabled_structured_intake_template,
                        "rule_governed": True,
                    }], "reason": "ai_assist_disabled_structured_intake_ready"}}
                if not generic_ai_reply_enabled:
                    return {"decision": {
                        "mode": "auto", "actions": [], "reason": "generic_ai_reply_disabled",
                    }}
                try:
                    candidate = await self._chat.reply(message, conversation_id)
                    assist = AiAssistResult(
                        reply_candidate=candidate,
                        confidence=0.5,
                        source_message_ids=[identity["event_id"]],
                    )
                    reply = assist.reply_candidate
                    generic_ai_reply = True
                except Exception:
                    return {"decision": {"mode": "auto", "actions": [], "reason": "chat_reply_unavailable"}}
        if not reply or len(reply) > 1_000:
            return {"decision": {"mode": "auto", "actions": [], "reason": "safe_reply_unavailable"}}
        if generic_ai_reply and (
            _has_protected_transaction_claim(reply) or _has_unverified_price_claim(reply)
        ):
            return {"decision": {
                "mode": "auto", "actions": [], "reason": "generic_ai_transaction_claim_rejected",
            }}
        action: dict[str, object] = {
            "id": f'{identity["event_id"]}:reply',
            "type": "send_message",
            "text": reply,
        }
        if image_workflow:
            action["preserve_on_new_buyer_message"] = True
        if direct_image_workflow:
            action["suppress_on_newer_image"] = True
        if decision_reason != "automatic_reply_ready":
            action["rule_governed"] = True
        actions = [action]
        if keyword_rule is not None:
            # A purchase-guide keyword is a paired text + image instruction.  The
            # quantity guidance remains the authoritative quote message, while
            # the configured operator text must not be silently dropped.
            if reply != str(getattr(keyword_rule, "reply", "") or "").strip():
                keyword_reply = str(getattr(keyword_rule, "reply", "") or "").strip()
                if keyword_reply:
                    actions.append({
                        "id": f'{identity["event_id"]}:keyword-reply',
                        "type": "send_message",
                        "text": keyword_reply,
                        "preserve_on_new_buyer_message": True,
                        "rule_governed": True,
                    })
            actions.extend(self._keyword_image_actions(identity, keyword_rule))
        return {"decision": {"mode": "auto", "actions": actions, "reason": decision_reason}}

    @staticmethod
    def _keyword_image_actions(identity: Mapping[str, str], rule: object) -> list[dict[str, object]]:
        asset_id = str(getattr(rule, "image_asset_id", None) or "").strip()
        image_tenant_id = str(getattr(rule, "image_tenant_id", None) or "").strip()
        if not asset_id or image_tenant_id != identity["tenant_id"]:
            return []
        return [{
            "id": f'{identity["event_id"]}:keyword-image',
            "type": "send_image",
            "image_asset_id": asset_id,
            "image_filename": str(getattr(rule, "image_filename", None) or "keyword-image"),
            "rule_governed": True,
        }]

    def _with_keyword_actions(
        self, decision: dict[str, object], identity: Mapping[str, str], rule: object,
    ) -> dict[str, object]:
        if rule is None:
            return decision
        payload = decision.get("decision")
        if not isinstance(payload, dict):
            return decision
        existing = payload.get("actions")
        if not isinstance(existing, list):
            return decision
        keyword_actions: list[dict[str, object]] = [{
            "id": f'{identity["event_id"]}:keyword-reply',
            "type": "send_message",
            "text": str(getattr(rule, "reply", "")),
            "rule_governed": True,
        }]
        keyword_actions.extend(self._keyword_image_actions(identity, rule))
        return {"decision": {**payload, "actions": [*keyword_actions, *existing]}}

    async def _price_created_order(
        self,
        envelope: Mapping[str, Any],
        identity: Mapping[str, str],
        messages: list[object],
        order: object = None,
    ) -> dict[str, object]:
        if _is_new_flow_transaction(envelope, order):
            return {"decision": {
                "mode": "auto", "actions": [],
                "reason": "new_flow_reprice_delegated_to_phase_9a",
            }}
        # A confirmed, unexpired durable quote is the buyer's write authorization.
        # Prefer it even while the original screenshot is still in recent history;
        # re-recognition must not discard a confirmed W+ quantity.
        bound = self._confirmed_quote_decision(identity, envelope, order)
        if bound is not None:
            return bound
        implicit_confirmation = self._confirm_quote_from_later_order(
            identity, envelope, messages, order,
            allow_implicit=True,
        )
        if implicit_confirmation is not None:
            implicit_decision = self._confirmed_quote_decision(
                identity, envelope, order, confirmed_record=implicit_confirmation,
            )
            if implicit_decision is not None:
                return implicit_decision
        # Never reinterpret a historical image when an order event arrives. Only
        # a durable, confirmed quote may authorize binding and price mutation.
        return _unverified_order_guard_decision(
            identity, reason="confirmed_quote_unavailable", templates=self._templates(),
        )

    def process_action_result(self, body: Mapping[str, Any]) -> dict[str, object]:
        return {
            "ok": True,
            "status": "recorded",
            **build_action_result_decision(body, mode=self._mode, templates=self._templates()),
        }


# Compatibility import for local characterization tests. The application
# composition root uses RulesFirstDecisionEngine exclusively.
PluginAutomation = RulesFirstDecisionEngine
