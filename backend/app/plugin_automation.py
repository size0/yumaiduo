from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .agent import AgentHarness
from .chat import (
    build_recognition_reply,
    build_wplus_marker_confirmation_reply,
    build_wplus_marker_confirmed_reply,
    build_wplus_marker_missing_reply,
    build_wplus_quote_marker_reply,
    build_wplus_unit_price_reply,
    is_wplus_unselected_image,
)
from .cinema_routing import CinemaRouteResolver
from .errors import ProviderError, RecognitionError
from .liangpiao_exact_quote import LiangpiaoExactQuoteAdapter
from .models import MovieImageInfo, RealQuote, SelectedSeat, ShowCandidate
from .observability import LOGGER
from .recognition_snapshot_store import (
    RecognitionSnapshot,
    RecognitionSnapshotAccessDenied,
    RecognitionSnapshotConflict,
    RecognitionSnapshotStore,
)
from .reply_template_store import ReplyTemplates, render_template
from .rule_contracts import AiAssistResult


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_AGE_MS = 30 * 60 * 1000
ALLOWED_IMAGE_HOST_SUFFIXES = ("alicdn.com", "tbcdn.cn")
ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
CRITICAL_IMAGE_CONFLICT_FIELDS = frozenset({
    "cinema_id", "cinema_name", "city", "movie_name", "date", "date_text",
    "showtime_start", "hall_name", "show_id", "selected_seats",
})


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
    async def reply(
        self, text: str, conversation_id: str,
        runtime_context: Mapping[str, Any] | None = None,
    ) -> str: ...

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
    def get_show_candidates(self, key: str) -> list[dict[str, Any]]: ...
    def save_show_candidates(self, key: str, candidates: list[dict[str, Any]]) -> None: ...
    def delete_show_candidates(self, key: str) -> None: ...


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


def _recognized_image_kind(recognition: MovieImageInfo) -> str:
    if recognition.ticket_codes:
        return "ticket_voucher"
    if recognition.is_seat_selection is True:
        return "seat_selection"
    if recognition.is_seat_selection is False:
        if recognition.movie_name or recognition.cinema_name or recognition.showtime_start:
            return "show_selection"
        return "unsupported"
    if recognition.selected_seats or recognition.price_zones:
        return "seat_selection"
    if recognition.movie_name or recognition.cinema_name or recognition.showtime_start:
        return "show_selection"
    return "unsupported"


def _recognition_ready_for_wanda_quote(recognition: MovieImageInfo) -> bool:
    """Check facts needed by the Wanda quote service, not Liangpiao mapping.

    Liangpiao recognition can identify a Wanda seat by its visible row/column
    while leaving ``match_level`` or ``seat_matched`` unset.  Wanda's own quote
    service resolves the cinema, show and live seat identifiers independently.
    """
    if _recognized_image_kind(recognition) != "seat_selection":
        return False
    if not recognition.selected_seats:
        return False
    if recognition.match_level == "SHOW_EXPIRED" or recognition.no_match_reason:
        return False
    if recognition.price_mismatch is True:
        return False
    required_values = (
        recognition.cinema_name,
        recognition.movie_name,
        recognition.date or recognition.date_text,
        recognition.showtime_start,
    )
    return all(required_values) and not {
        "cinema_name", "movie_name", "date", "date_text", "showtime_start",
    }.intersection(recognition.missing_fields)


def _recognition_ready_for_preflight(recognition: MovieImageInfo) -> bool:
    if _recognized_image_kind(recognition) != "seat_selection":
        return False
    is_wplus_preview = is_wplus_unselected_image(recognition)
    if not recognition.selected_seats and not is_wplus_preview:
        return False
    if recognition.match_level in {"CANDIDATE", "NONE", "SHOW_EXPIRED"}:
        return False
    if recognition.no_match_reason or (
        recognition.recognition_blocker
        and not (
            is_wplus_preview
            and recognition.recognition_blocker == "SEAT_UNMATCHED"
        )
    ):
        return False
    if (recognition.seat_matched is False and not is_wplus_preview) or recognition.price_mismatch is True:
        return False
    if (
        recognition.match_level != "EXACT"
        and (
            len(recognition.candidate_cinemas) > 1
            or len(recognition.candidate_movies) > 1
            or len(recognition.candidate_shows) > 1
        )
    ):
        return False
    required_values = (
        recognition.cinema_name,
        recognition.movie_name,
        recognition.date or recognition.date_text,
        recognition.showtime_start,
    )
    return all(required_values) and not {
        "cinema_name", "movie_name", "date", "date_text", "showtime_start",
    }.intersection(recognition.missing_fields)


def _public_recognition_payload(recognition: MovieImageInfo) -> dict[str, Any]:
    """Return Agent-safe facts without screenshot or provider price amounts."""
    payload = recognition.model_dump(
        mode="json", exclude={
            "ticket_codes", "displayed_total", "provider_prices", "raw_response",
        },
    )

    def sanitize(value: Any) -> Any:
        if isinstance(value, Mapping):
            sanitized: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = str(raw_key)
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                # Flags and channel modes are useful facts. Monetary values are
                # not: only a persisted preflight quote may expose an amount.
                if normalized not in {
                    "pricemismatch", "pricemode", "prices", "pricezones",
                } and ("price" in normalized or "amount" in normalized):
                    continue
                sanitized[key] = sanitize(item)
            return sanitized
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    payload = sanitize(payload)
    if recognition.match_level == "EXACT":
        # Provider candidate arrays are diagnostic leftovers once authoritative
        # cinema/movie/show IDs have converged. Do not ask the Agent or buyer to
        # choose again after an EXACT match.
        payload["candidate_cinemas"] = []
        payload["candidate_movies"] = []
        payload["candidate_shows"] = []
    zones = payload.get("price_zones")
    if isinstance(zones, list):
        payload["price_zones"] = [
            {key: value for key, value in zone.items() if key != "displayed_price"}
            if isinstance(zone, Mapping) else zone
            for zone in zones
        ]
    return payload


def _image_review_required(recognition: MovieImageInfo) -> bool:
    required_fields = {"cinema_name", "movie_name", "date", "date_text", "showtime_start"}
    return bool(
        recognition.confidence < .85
        or recognition.match_level in {"CANDIDATE", "NONE"}
        or required_fields.intersection(recognition.missing_fields)
        or not recognition.cinema_name
        or not recognition.movie_name
        or not (recognition.date or recognition.date_text)
        or not recognition.showtime_start
    )


def _merge_image_recognitions(
    recognitions: Sequence[MovieImageInfo],
) -> tuple[MovieImageInfo, list[str]]:
    if not recognitions:
        raise ValueError("image_recognition_empty")
    if len(recognitions) == 1:
        return recognitions[0], []

    scalar_fields = (
        "platform", "cinema_id", "cinema_name", "cinema_address", "brand_name",
        "cinema_truncated", "city_code", "city",
        "movie_name", "movie_id", "date_text", "date", "showtime_start",
        "showtime_end", "hall_name", "language", "format", "displayed_total",
        "currency", "show_id", "no_match_reason", "cinema_hit_count",
        "price_mismatch", "seat_matched",
    )
    updates: dict[str, Any] = {}
    conflicts: list[str] = []
    selection_flags = [item.is_seat_selection for item in recognitions]
    updates["is_seat_selection"] = (
        True if True in selection_flags
        else (False if selection_flags and all(flag is False for flag in selection_flags) else None)
    )
    for field in scalar_fields:
        values: list[Any] = []
        for recognition in recognitions:
            value = getattr(recognition, field)
            if value is not None and value != "" and value not in values:
                values.append(value)
        if len(values) > 1:
            conflicts.append(field)
            updates[field] = None
        elif values:
            updates[field] = values[0]

    def merge_exact_list(field: str) -> list[Any]:
        populated = [getattr(item, field) for item in recognitions if getattr(item, field)]
        if not populated:
            return []
        baseline = populated[0]
        if any(value != baseline for value in populated[1:]):
            conflicts.append(field)
            return []
        return list(baseline)

    updates["selected_seats"] = merge_exact_list("selected_seats")
    updates["selected_count_visible"] = len(updates["selected_seats"])
    updates["ticket_codes"] = merge_exact_list("ticket_codes")
    updates["price_zones"] = merge_exact_list("price_zones")
    updates["provider_prices"] = merge_exact_list("provider_prices")

    candidates: dict[int, Any] = {}
    for recognition in recognitions:
        for candidate in recognition.candidate_cinemas:
            candidates.setdefault(candidate.cinema_id, candidate)
    updates["candidate_cinemas"] = list(candidates.values())[:5]
    movies: dict[int, Any] = {}
    shows: dict[str, Any] = {}
    for recognition in recognitions:
        for candidate in recognition.candidate_movies:
            movies.setdefault(candidate.movie_id, candidate)
        for candidate in recognition.candidate_shows:
            shows.setdefault(candidate.show_id, candidate)
    updates["candidate_movies"] = list(movies.values())[:5]
    updates["candidate_shows"] = list(shows.values())[:10]

    missing_fields = {
        str(field) for recognition in recognitions for field in recognition.missing_fields
    }
    missing_fields.update(conflicts)
    updates["missing_fields"] = sorted(missing_fields)[:30]
    warnings = {
        str(warning) for recognition in recognitions for warning in recognition.warnings
    }
    warnings.update(f"conflict:{field}" for field in conflicts)
    updates["warnings"] = sorted(warnings)[:20]
    updates["confidence"] = min(item.confidence for item in recognitions)
    updates["recognition_id"] = None
    if conflicts:
        updates["match_level"] = "CANDIDATE" if updates["candidate_cinemas"] else "NONE"
    else:
        levels = [item.match_level for item in recognitions if item.match_level]
        updates["match_level"] = levels[0] if levels and len(set(levels)) == 1 else None
    return recognitions[0].model_copy(update=updates), sorted(conflicts)


def _build_image_quote_targets(
    recognitions: Sequence[MovieImageInfo],
) -> list[dict[str, Any]]:
    """Build independent quote targets without merging separate seat choices.

    A show/cinema screenshot may enrich a seat screenshot, while every seat
    screenshot remains its own transaction candidate. This avoids treating
    different seats in two screenshots as contradictory fields.
    """
    if not recognitions:
        return []
    seat_indexes = [
        index for index, recognition in enumerate(recognitions)
        if _recognized_image_kind(recognition) == "seat_selection"
    ]
    if not seat_indexes:
        merged, conflicts = _merge_image_recognitions(recognitions)
        return [{
            "recognition": merged,
            "image_indexes": list(range(len(recognitions))),
            "conflict_fields": conflicts,
        }]

    supplemental_indexes = [
        index for index, recognition in enumerate(recognitions)
        if index not in seat_indexes
        and _recognized_image_kind(recognition) == "show_selection"
    ]
    targets: list[dict[str, Any]] = []
    for seat_index in seat_indexes:
        seat_recognition = recognitions[seat_index]
        included = [seat_index]
        merged = seat_recognition
        conflicts: list[str] = []
        for supplemental_index in supplemental_indexes:
            candidate, candidate_conflicts = _merge_image_recognitions([
                merged, recognitions[supplemental_index],
            ])
            critical = set(candidate_conflicts).intersection(CRITICAL_IMAGE_CONFLICT_FIELDS)
            if len(seat_indexes) > 1 and critical:
                # The supplemental screenshot belongs to another transaction
                # group; it must not poison this independent seat target.
                continue
            merged = candidate
            included.append(supplemental_index)
            conflicts.extend(candidate_conflicts)
        targets.append({
            "recognition": merged,
            "image_indexes": sorted(included),
            "conflict_fields": sorted(set(conflicts)),
        })
    return targets


def _extract_show_candidates(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    containers: list[Any] = []
    for key in ("shows", "records", "items", "list", "data"):
        value = result.get(key)
        if isinstance(value, list):
            containers.extend(value)
        elif isinstance(value, Mapping):
            for nested_key in ("shows", "records", "items", "list"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    containers.extend(nested)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    aliases = {
        "show_id": ("show_id", "showId", "id"),
        "showtime_start": ("showtime_start", "showtimeStart", "start_time", "startTime"),
        "showtime_end": ("showtime_end", "showtimeEnd", "end_time", "endTime"),
        "hall_name": ("hall_name", "hallName"),
        "date_text": ("date_text", "date", "showDate"),
        "language": ("language", "lang"),
        "format": ("format", "movieFormat"),
        "cinema_id": ("cinema_id", "cinemaId"),
        "movie_name": ("movie_name", "movieName"),
    }
    for item in containers:
        if not isinstance(item, Mapping):
            continue
        candidate: dict[str, Any] = {}
        for target, source_names in aliases.items():
            value = next((item.get(name) for name in source_names if item.get(name) is not None), None)
            if value is not None:
                candidate[target] = value
        show_id = str(candidate.get("show_id") or "").strip()
        if not show_id or show_id in seen:
            continue
        seen.add(show_id)
        candidate["show_id"] = show_id
        candidates.append(candidate)
    return candidates[:20]


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


def _pick(source: Mapping[str, Any] | None, *names: str) -> str | None:
    if not isinstance(source, Mapping):
        return None
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


def _human_seller_image_urls(messages: Sequence[object]) -> list[str]:
    """Return seller images that were not generated by this automation.

    These URLs are context references only. They are exposed to the Agent so
    it can recognize a human-sent seat map, but they never become the current
    buyer target or an automatic quote by themselves.
    """
    urls: list[str] = []
    for item in messages[-50:]:
        if not isinstance(item, Mapping) or item.get("agent_generated") is True:
            continue
        if _text(item.get("direction")) not in {"seller", "outbound", "sent", "staff", "human"}:
            continue
        raw_urls = item.get("imageUrls", item.get("image_urls"))
        if not isinstance(raw_urls, list):
            continue
        for value in raw_urls:
            candidate = str(value or "").strip()
            if candidate.startswith(("http://", "https://")) and candidate not in urls:
                urls.append(candidate)
    return urls[:3]


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


def _wplus_marker_confirmation(value: str) -> str | None:
    """Classify only a direct yes/no answer to the pending marker question."""
    raw = str(value or "").strip()
    normalized = re.sub(r"[\s，。！!、,.；;:：]+", "", raw).lower()
    if not normalized or any(token in raw for token in ("吗", "么", "？", "?")):
        return None
    if normalized in {"没有", "没有标记", "没标记", "未标记", "没圈", "没有圈", "未圈", "没画", "没有画"}:
        return "missing"
    if normalized in {
        "有", "有标记", "有画笔标记", "已标记", "已经标记", "标记了", "已经标记了",
        "标记好", "标记好了", "已用画笔标记",
        "圈了", "圈好了", "已圈", "已经圈", "已经圈好", "已经圈好位置", "画了", "已画", "是", "对",
    } or any(token in normalized for token in ("有画笔", "已圈好位置", "已经圈好位置", "已用画笔圈")):
        return "confirmed"
    return None


def _wplus_marker_semantic_candidate(value: str) -> bool:
    """Return whether an unclassified reply may answer the marker question."""
    text = str(value or "").strip()
    if not text or _declared_ticket_count(text) is not None or _is_price_request(text):
        return False
    return True


def _is_price_request(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(value or "")).replace(" ", "")
    if any(marker in normalized for marker in ("张", "票", "座位数", "几张", "几票")):
        return False
    return any(marker in normalized for marker in ("多少钱", "什么价格", "什么价", "价格多少", "报价", "要多少", "多少"))


def _quote_record_seats(record: Mapping[str, Any]) -> tuple[str, ...]:
    values = record.get("selected_seats")
    if not isinstance(values, list):
        return tuple()
    seats: list[str] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        seat = str(value.get("seat_no") or value.get("seat_number") or "").strip()
        if seat and seat not in seats:
            seats.append(seat)
    return tuple(seats)


def _real_quote_from_record(record: Mapping[str, Any]) -> RealQuote | None:
    try:
        quote = RealQuote.model_validate(record)
    except Exception:
        return None
    if quote.unit_quote_cents is None or quote.total_quote_cents is None:
        return None
    return quote


def _is_wplus_quote(quote: RealQuote) -> bool:
    return bool(
        quote.seat_type == "wplus"
        or str(quote.seat_zone_type or "").strip().upper() == "W+"
        or str(quote.price_source or "").startswith("realtime_wplus")
    )


def _requires_wplus_marker(recognition: MovieImageInfo, quote: RealQuote) -> bool:
    return bool(
        is_wplus_unselected_image(recognition)
        or (
            recognition.is_seat_selection is True
            and quote.quote_scope != "exact_seats"
            and _is_wplus_quote(quote)
        )
    )


def _exact_quote_has_amounts(quote: RealQuote) -> bool:
    return bool(
        isinstance(quote.unit_quote_cents, int)
        and quote.unit_quote_cents > 0
        and isinstance(quote.total_quote_cents, int)
        and quote.total_quote_cents > 0
        and (quote.ticket_count or len(quote.seat_quotes)) > 0
    )


def _exact_quote_actions(
    identity: Mapping[str, str], recognition: MovieImageInfo, quote: RealQuote,
) -> list[dict[str, object]]:
    """Send exact-seat identity first, then price/order guidance separately."""
    count = quote.ticket_count or len(quote.seat_quotes) or len(recognition.selected_seats)
    if not _exact_quote_has_amounts(quote) or count <= 0:
        raise ValueError("exact_quote_amounts_incomplete")
    seat_labels = sorted(
        {
            item.seat_number.replace(" ", "")
            for item in quote.seat_quotes
            if item.seat_number.strip()
        } or {
            item.seat_number.replace(" ", "")
            for item in recognition.selected_seats
            if item.seat_number.strip()
        },
        key=lambda value: (
            int(match.group(1)), int(match.group(2))
        ) if (match := re.match(r"(\d+)排(\d+)座$", value)) else (999, 999),
    )
    cinema = quote.matched_cinema_name or recognition.cinema_name or ""
    movie = quote.matched_movie_name or recognition.movie_name or ""
    date_text = recognition.date_text or quote.quote_date or ""
    showtime = quote.matched_showtime_start or recognition.showtime_start or ""
    hall = quote.matched_hall_name or recognition.hall_name or ""
    seat_text = "、".join(seat_labels)
    identity_lines = [
        f"影院：{cinema}",
        f"影片：《{movie}》" if movie else "影片：",
        f"时间：{date_text} {showtime}".rstrip(),
        f"影厅：{hall}",
        f"已选座位：{seat_text}",
        f"张数：{count}张",
    ]
    unit = Decimal(quote.unit_quote_cents) / Decimal(100)
    total = Decimal(quote.total_quote_cents) / Decimal(100)
    price_text = (
        f"单价：{unit:.2f}元/张\n"
        f"合计：{total:.2f}元\n"
        f"请直接提交{count}张订单，拍下后先不要付款，我这边改价。"
    )
    return [
        {
            "id": f'{identity["event_id"]}:exact-quote-info',
            "type": "send_message",
            "text": "\n".join(identity_lines),
            "rule_governed": True,
            "preserve_on_new_buyer_message": True,
        },
        {
            "id": f'{identity["event_id"]}:exact-quote-price',
            "type": "send_message",
            "text": price_text,
            "rule_governed": True,
            "preserve_on_new_buyer_message": True,
        },
    ]


def _wplus_quote_actions(
    identity: Mapping[str, str], recognition: MovieImageInfo, quote: RealQuote,
    templates: ReplyTemplates,
) -> list[dict[str, object]]:
    """Send the W+ price first, then confirm the marked position and count."""
    return [
        {
            "id": f'{identity["event_id"]}:wplus-unit-price',
            "type": "send_message",
            "text": build_wplus_unit_price_reply(quote, templates=templates),
            "rule_governed": True,
            "preserve_on_new_buyer_message": True,
        },
        {
            "id": f'{identity["event_id"]}:reply',
            "type": "send_message",
            "text": build_wplus_quote_marker_reply(
                recognition, quote, templates=templates,
            ),
            "rule_governed": True,
            "preserve_on_new_buyer_message": True,
        },
    ]


def _fixed_switch_consent(value: str) -> bool:
    normalized = "".join(str(value or "").split()).lower()
    if normalized in {"\u53ef\u4ee5", "\u597d\u7684", "\u884c", "\u786e\u8ba4"}:
        return True
    return any(marker in normalized for marker in (
        "\u6362\u4e00\u53e3\u4ef7", "\u6362\u4e00\u53e3", "\u4e00\u53e3\u4ef7\u7ee7\u7eed", "\u6309\u4e00\u53e3\u4ef7", "\u540c\u610f\u6362",
    ))


def _fixed_switch_rejection(value: str) -> bool:
    normalized = "".join(str(value or "").split()).lower()
    return any(marker in normalized for marker in (
        "\u4e0d\u6362", "\u4e0d\u8981\u4e86", "\u53d6\u6d88", "\u4e0d\u4e70",
    ))


def _fixed_switch_price_confirmation(value: str) -> bool:
    normalized = "".join(str(value or "").split()).lower()
    return any(marker in normalized for marker in (
        "\u786e\u8ba4\u4e00\u53e3\u4ef7", "\u6309\u8fd9\u4e2a\u4e00\u53e3\u4ef7", "\u6309\u4e00\u53e3\u4ef7\u7ee7\u7eed", "\u7ee7\u7eed\u51fa\u7968",
    ))


def _cinema_choice(value: str, candidate_count: int) -> int | None:
    normalized = re.sub(r"[，。！？!?、,\.\s]+", "", value).strip()
    match = re.fullmatch(r"(?:选|选择|第)?([1-9])(?:号|个|家)?", normalized)
    if not match:
        return None
    choice = int(match.group(1))
    return choice if choice <= candidate_count else None


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
    natural_language = False
    if not all((city, cinema, movie, date_text, showtime_match)):
        # Accept the ordinary buyer sentence used by the workbench while
        # keeping the authoritative path narrow: city + Wanda venue + movie
        # + concrete showtime are required; date remains an explicit gap.
        natural_match = re.search(
            r"^(?P<city>[\u4e00-\u9fff]{2,4}?)"
            r"(?P<cinema>[\u4e00-\u9fffA-Za-z0-9·（）()]{2,40}?万达(?:广场|影城))"
            r"(?P<movie>[\u4e00-\u9fffA-Za-z0-9·（）()—-]{1,40}?)"
            r"[，,；;\s]+(?P<showtime>[0-2]?\d[:：.]\d{2})",
            text,
        )
        if natural_match is None:
            return None
        natural_language = True
        city = natural_match.group("city").strip()
        cinema = natural_match.group("cinema").strip()
        movie = natural_match.group("movie").strip()
        showtime = natural_match.group("showtime")
        date_text = next(
            (match.group(0) for match in re.finditer(
                r"今天|明天|后天|\d{1,2}月\d{1,2}(?:日|号)?", text,
            )),
            None,
        )
        seats_text = ""
        count_text = text
        showtime_match = re.search(r"(?<!\d)([0-2]?\d)[:：.]([0-5]\d)(?!\d)", showtime)
    start = f"{int(showtime_match.group(1)):02d}:{showtime_match.group(2)}"
    seat_numbers = list(dict.fromkeys(
        match.group(0).replace(" ", "")
        for match in re.finditer(r"\d{1,2}\s*排\s*\d{1,2}\s*座", seats_text)
    ))
    count_match = re.search(r"([1-9]|1\d|20)\s*张", count_text or "")
    count = int(count_match.group(1)) if count_match else (_declared_ticket_count(count_text or "") or len(seat_numbers))
    if seat_numbers and count != len(seat_numbers):
        return None
    return MovieImageInfo(
        platform="buyer_structured_text", city=city, cinema_name=cinema,
        movie_name=movie, date_text=date_text, showtime_start=start, hall_name=hall,
        selected_seats=[SelectedSeat(seat_number=seat) for seat in seat_numbers],
        selected_count_visible=len(seat_numbers), confidence=0.9 if natural_language else 1,
        missing_fields=([] if date_text else ["date"]) + ([] if count else ["ticket_count"]),
        warnings=[] if seat_numbers else ["structured_text_without_specific_seats"],
    )


def _merge_buyer_ticket_context(
    recognition: MovieImageInfo, hints: Sequence[str],
) -> MovieImageInfo:
    """Fill missing image facts from the buyer's preceding ticket sentence.

    Seat-map screenshots frequently omit the cinema/movie/showtime header. The
    preceding buyer text is trusted as explicit trade context for identity and
    showtime fields; it never invents seats or prices.
    """
    for hint in hints:
        parsed = _structured_ticket_request(hint)
        if parsed is None:
            continue
        updates: dict[str, object] = {}
        for field in ("city", "cinema_name", "movie_name", "date_text", "date", "showtime_start", "hall_name"):
            value = getattr(parsed, field, None)
            # The buyer's preceding sentence is an explicit trade-term input;
            # prefer it over a low-quality/partial OCR value for the same
            # field. Dates from the image remain untouched when text has none.
            if value is not None:
                updates[field] = value
        if updates:
            filled = set(updates)
            updates["missing_fields"] = [
                field for field in recognition.missing_fields if field not in filled
            ]
            recognition = recognition.model_copy(update=updates)
        return recognition
    return recognition


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


def _recent_structured_ticket_request(
    messages: Sequence[object], current: Mapping[str, Any],
) -> MovieImageInfo | None:
    """Recover the latest buyer ticket facts for a quantity-only follow-up."""
    current_id = _pick(current, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id")
    for item in reversed(messages[-50:]):
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("direction")) not in {"inbound", "buyer", "received"}:
            continue
        if current_id and _pick(item, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id") == current_id:
            continue
        if str(item.get("messageType", item.get("message_type", ""))).strip() != "1":
            continue
        parsed = _structured_ticket_request(_text(item.get("content", item.get("text"))) or "")
        if parsed is not None:
            return parsed
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
    *,
    wplus_marker_confirmed: bool = False,
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
    if wplus_marker_confirmed or normalized.startswith("是"):
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


def _looks_like_cinema_clarification(value: str) -> bool:
    compact = "".join(value.split())
    if not 2 <= len(compact) <= 80 or any(marker in compact for marker in ("？", "?", "哪里", "哪家", "什么", "怎么")):
        return False
    return "万达" in compact or compact.endswith(("影院", "影城", "电影院"))


def _cinema_venue_hint(value: str, canonical_city: str) -> str | None:
    compact = "".join(unicodedata.normalize("NFKC", value).split())
    parsed = _structured_ticket_request(compact)
    if parsed is not None and parsed.cinema_name:
        return parsed.cinema_name
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
        # Buyers commonly correct a row in natural language, e.g.
        # ``9排的13 14`` or ``第9排13、14``.  Keep the parser bounded to the
        # seat clause so it can be used to reprice from the existing quote
        # context without requiring another screenshot.
        r"(?:第\s*)?(?P<row>\d{1,2})\s*排\s*(?:的\s*)?"
        r"(?P<seats>\d{1,3}(?:\s*座)?"
        r"(?:(?:\s*(?:[、,，/和及与]|以及)\s*|\s+)\d{1,3}(?:\s*座)?){0,9})",
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
    # FishMore currently returns newest-first pages.  Slicing ``[-20:]`` before
    # matching dropped the newest webhook message whenever a conversation had
    # more than 20 history rows, which suppressed screenshot recognition with
    # ``current_buyer_message_unavailable``.  Search the complete bounded page;
    # callers already cap the platform snapshot at 50 messages.
    inbound = [
        item for item in messages
        if isinstance(item, Mapping) and _text(item.get("direction")) in {"inbound", "buyer", "received"}
    ]
    if expected_id:
        for item in inbound:
            if _pick(item, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id") == expected_id:
                return item
        # History may lag a webhook, but fabricating an inbound message from
        # the webhook payload is unsafe: seller/outbound echoes can then be
        # reprocessed as buyer intent and trigger duplicate rules.  Wait for a
        # matching buyer message instead of guessing its direction.
        return None
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


def _liangpiao_event_decision(
    identity: Mapping[str, str], event_type: str | None, body: Mapping[str, Any],
    messages: Sequence[object], templates: ReplyTemplates,
) -> dict[str, object]:
    """Translate provider lifecycle events into idempotent buyer actions.

    This is deliberately a status mapper, not an intent classifier.  It never
    calls refund/create APIs and never retries a business mutation.  The
    callback handler remains the source of verified provider facts; this bridge
    path exists so older runtimes cannot silently drop lifecycle events.
    """
    event = str(event_type or "").strip().lower()
    envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    order = body.get("order") if isinstance(body.get("order"), Mapping) else {}
    merged: dict[str, Any] = {**dict(order), **dict(payload)}
    order_id = str(identity.get("order_id") or _pick(merged, "orderId", "order_id", "outOrderNo") or "").strip()
    if event in {"order.ticketed", "ticket.updated"}:
        # Empty ticket lists are legal on order.ticketed; a later ticket.updated
        # event or authoritative order detail supplies the code/link.
        status = _authoritative_order_state({**merged, "orderStatus": "shipped"})
        if status not in {"shipped", "completed"}:
            return {"decision": {"mode": "auto", "actions": [], "reason": "ticket_event_without_authoritative_order"}}
        return {"decision": {"mode": "auto", "actions": [], "reason": "ticket_event_recorded"}}
    if event in {"order.refund.applied", "order.refund.finished", "order.refunded", "order.refund_rejected", "order.refund.rejected"}:
        # Refund facts are consumed by the state coordinator.  In particular,
        # this path must not call /order/refund again.
        return {"decision": {"mode": "auto", "actions": [], "reason": "refund_event_recorded_no_api_call"}}
    if event in {"order.failed"}:
        mode = str(_pick(merged, "priceMode", "price_mode") or "FIXED").strip().upper()
        reason = _text(_pick(merged, "failReason", "fail_reason", "message")) or "良票平台未能完成出票"
        if mode == "LIMIT":
            text = (
                f"很抱歉，特惠渠道未能完成出票（{reason}）。"
                "原订单关闭状态正在确认，确认关闭后再询问您是否切换一口价渠道。"
            )
            fallback = {"available": True, "from_price_mode": "LIMIT", "to_price_mode": "FIXED",
                        "requires_buyer_consent": True, "source_order_closed": False,
                        "refund_api_allowed": False}
        else:
            text = render_template(templates.liangpiao_fixed_failed_template, {})
            fallback = {"available": False, "from_price_mode": mode, "to_price_mode": None,
                        "requires_buyer_consent": False, "refund_api_allowed": False}
        if _recent_outbound_exact_text(messages, text):
            return {"decision": {"mode": "auto", "actions": [], "reason": "liangpiao_failure_already_notified"}}
        return {"decision": {"mode": "auto", "actions": [{
            "id": f'{identity["event_id"]}:liangpiao-failed', "type": "send_message", "order_id": order_id,
            "text": text, "dedupe_key": f"liangpiao:{order_id}:failed",
            "preserve_on_new_buyer_message": True, "rule_governed": True,
            "safety_notice": True, "fallback": fallback,
            "refund_api_allowed": False,
        }], "reason": "liangpiao_failure_switch_offer_ready" if fallback["available"] else "liangpiao_failure_manual_policy_ready"}}
    return {"decision": {"mode": "auto", "actions": [], "reason": "liangpiao_event_recorded"}}


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


def _asks_transaction_status(value: str) -> bool:
    """Recognize explicit order-status questions only when order state is known.

    This is a narrow workflow guard, not a general buyer-intent classifier:
    it never creates, reprices, or confirms an order and is evaluated only
    alongside an authoritative paid/shipped order below.
    """
    normalized = "".join(value.lower().split())
    return any(marker in normalized for marker in (
        "订单状态", "改好价", "改价了吗", "付款", "付了", "支付", "出票", "发货",
        "取票", "取码", "票码", "好了吗", "ok了吗",
    ))


def _is_post_order_status_intent(value: str) -> bool:
    """Route only explicit status questions to deterministic order replies.

    A bare acknowledgement is conversation, not an order-state query.  It is
    therefore left to the Agent and cannot accidentally reopen a transaction:
    all write operations still require authoritative workflow gates.
    """
    return _asks_transaction_status(value)


def _has_protected_transaction_claim(value: str) -> bool:
    normalized = "".join(str(value or "").split())
    return any(marker in normalized for marker in (
        "出票成功", "票码已发", "已经出票", "已出票", "付款成功", "支付成功",
        "改价已完成", "价格已改", "已经发货", "已发货", "退款成功", "已经退款",
        "已退款", "订单已关闭", "可以付款", "可付款", "等待出票",
    ))


def _price_claim_cents(value: str) -> set[int]:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    compact = "".join(normalized.split())
    amounts = re.findall(
        r"(?:[¥￥](\d{1,5}(?:\.\d{1,2})?)|(?<!\d)(\d{1,5}(?:\.\d{1,2})?)(?:元|块钱?|一张|每张|/张|一套|每套|/套))",
        compact,
    )
    cents: set[int] = set()
    for currency_amount, suffix_amount in amounts:
        raw = currency_amount or suffix_amount
        try:
            cents.add(int((Decimal(raw) * 100).quantize(Decimal("1"))))
        except (ArithmeticError, ValueError):
            continue
    return cents


def _has_unverified_price_claim(value: str) -> bool:
    """Reject amount-bearing generic AI copy unless grounded by this request's quote tool."""
    return bool(_price_claim_cents(value))


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
    order_id = _pick(result, "order_id") or _pick(body, "order_id")
    target = result.get("target_amount_cents")
    verified = result.get("verified_amount_cents")
    observed_order_amount = result.get("observed_order_amount_cents")
    event_id = _pick(body, "event_id")
    action_id = _pick(body, "action_id")
    command_type = _pick(body, "command_type")
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
    if command_type == "create_liangpiao_order" and result_status in {"failed", "unknown"}:
        if not event_id or not order_id:
            return {"actions": []}
        text = (
            "良票出票失败，下单未完成。系统已停止后续出票和渠道切换，正在核对订单状态。"
            if result_status == "failed" else
            "良票下单结果暂未确认，系统不会重复下单或切换渠道，正在等待权威状态。"
        )
        return {"actions": [{
            "id": f"{event_id}:liangpiao-order-{result_status}", "type": "send_message", "order_id": order_id,
            "text": text, "preserve_on_new_buyer_message": True, "safety_notice": True, "rule_governed": True,
        }]}
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
        automation_mode_provider: Callable[[Mapping[str, str]], str] | None = None,
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
        recognition_snapshot_store: RecognitionSnapshotStore | None = None,
        cinema_route_resolver: CinemaRouteResolver | None = None,
        liangpiao_exact_quote_adapter: LiangpiaoExactQuoteAdapter | None = None,
        transaction_state_store: object | None = None,
        liangpiao_order_finder: Callable[..., Mapping[str, Any] | None] | None = None,
        liangpiao_quote_finder: Callable[[str], Mapping[str, Any] | None] | None = None,
        liangpiao_fixed_quote_creator: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
        liangpiao_order_phone: str = "",
        ai_assist_enabled: bool = True,
        agent_harness: AgentHarness | None = None,
        new_agent_harness_enabled: bool = False,
    ) -> None:
        # The rules engine is the only production decision path. ``mode`` is
        # retained solely for isolated unit tests; production is always active
        # and external writes are controlled independently at command claim.
        configured_mode = (mode or "auto").strip().lower()
        self._mode = configured_mode if configured_mode in {"off", "auto"} else "auto"
        self._automation_mode_provider = automation_mode_provider
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
        self._recognition_snapshots = recognition_snapshot_store
        self._cinema_route_resolver = cinema_route_resolver
        self._liangpiao_exact_quote = liangpiao_exact_quote_adapter
        self._transaction_states = transaction_state_store
        self._liangpiao_order_finder = liangpiao_order_finder
        self._liangpiao_quote_finder = liangpiao_quote_finder
        self._liangpiao_fixed_quote_creator = liangpiao_fixed_quote_creator
        self._liangpiao_order_phone = str(liangpiao_order_phone or "").strip()
        self._ai_assist_enabled = bool(ai_assist_enabled)
        self._agent_harness = agent_harness
        self._new_agent_harness_enabled = bool(new_agent_harness_enabled)
        self._owned_loader = SecureImageLoader() if image_loader is None else None
        self._load_image = image_loader or self._owned_loader
        # Candidate selection must survive the candidate reply and be scoped to
        # one buyer conversation. The source image URL is retained only long
        # enough to bind the official Liangpiao confirmation request; image bytes
        # and credentials are never stored here.
        self._pending_cinema_candidates: dict[str, MovieImageInfo] = {}
        self._pending_image_conflicts: dict[str, tuple[str, ...]] = {}
        self._pending_show_candidates: dict[str, list[dict[str, Any]]] = {}
        # One event may inspect an image once and, only when confidence or
        # required fields justify it, perform one controlled verification.
        self._agent_image_attempts: dict[str, int] = {}
        self._agent_image_results: dict[str, MovieImageInfo] = {}

    async def _quote_for_recognition(
        self, recognition: MovieImageInfo, identity: Mapping[str, str] | None = None,
    ) -> tuple[MovieImageInfo, RealQuote]:
        if self._cinema_route_resolver is None:
            return recognition, await self._quote.quote(recognition)
        route = await self._cinema_route_resolver.resolve(recognition)
        if route.route == "WANDA_SELF":
            mapped_quote = getattr(self._quote, "quote_mapped", None)
            if route.wanda_cinema_id and callable(mapped_quote):
                return route.recognition, await mapped_quote(
                    route.recognition, wanda_cinema_id=route.wanda_cinema_id,
                )
            return route.recognition, await self._quote.quote(route.recognition)
        if route.route == "LIANGPIAO_EXACT":
            if self._liangpiao_exact_quote is None or identity is None:
                raise ProviderError(
                    "liangpiao_exact_quote_unavailable",
                    "非万达影院的精确座位报价暂时不可用，请稍后重试。",
                )
            return route.recognition, await self._liangpiao_exact_quote.quote(
                route.recognition,
                tenant_id=identity["tenant_id"],
                conversation_id=f'{identity["tenant_id"]}:{identity["shop_id"]}:{identity["chat_id"]}',
            )
        raise ProviderError("quote_route_unavailable", route.reason or "当前影院暂时无法核价，请补充完整影院信息。")

    @staticmethod
    def _snapshot_mutation_event_id(
        identity: Mapping[str, str], tool_name: str, target_id: str,
    ) -> str:
        value = f'{identity["event_id"]}:{tool_name}:{target_id}'
        if len(value) <= 200:
            return value
        return value[:158] + ":" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:40]

    def _resolution_snapshot(
        self,
        arguments: Mapping[str, Any],
        identity: Mapping[str, str],
    ) -> tuple[RecognitionSnapshot | None, Mapping[str, Any] | None]:
        """Load the exact current target revision supplied by the Agent."""
        if self._recognition_snapshots is None:
            return None, None
        snapshot_id = str(arguments.get("snapshot_id") or "").strip()
        target_id = str(arguments.get("target_id") or "").strip()
        try:
            expected_revision = int(arguments.get("snapshot_revision"))
        except (TypeError, ValueError):
            expected_revision = 0
        if not snapshot_id or not target_id or expected_revision < 1:
            return None, {
                "ok": False,
                "error": "recognition_snapshot_reference_required",
            }
        try:
            current = self._recognition_snapshots.get_current(
                tenant_id=identity["tenant_id"],
                shop_id=identity["shop_id"],
                buyer_id=identity["buyer_id"],
                chat_id=identity["chat_id"],
                target_id=target_id,
            )
        except (RecognitionSnapshotAccessDenied, RecognitionSnapshotConflict, ValueError):
            return None, {"ok": False, "error": "recognition_snapshot_access_denied"}
        if current is None or current.snapshot_id != snapshot_id:
            return None, {"ok": False, "error": "recognition_snapshot_not_current"}
        if current.revision != expected_revision:
            return None, {
                "ok": False,
                "error": "recognition_snapshot_revision_conflict",
            }
        return current, None

    def _commit_resolution_snapshot(
        self,
        *,
        snapshot: RecognitionSnapshot,
        identity: Mapping[str, str],
        tool_name: str,
        recognition: MovieImageInfo,
        confirmation: Mapping[str, Any],
    ) -> tuple[RecognitionSnapshot | None, Mapping[str, Any] | None]:
        if self._recognition_snapshots is None:
            return None, None
        try:
            updated = self._recognition_snapshots.compare_and_swap(
                tenant_id=identity["tenant_id"],
                shop_id=identity["shop_id"],
                buyer_id=identity["buyer_id"],
                chat_id=identity["chat_id"],
                snapshot_id=snapshot.snapshot_id,
                target_id=snapshot.target_id,
                expected_revision=snapshot.revision,
                event_id=self._snapshot_mutation_event_id(
                    identity, tool_name, snapshot.target_id,
                ),
                recognition=recognition,
                raw_results=recognition.raw_results,
                final_results=recognition.final_results,
                raw_response=recognition.raw_response,
                recognize_id=recognition.recognition_id,
                provider_request_id=recognition.provider_request_id,
                trace_id=recognition.trace_id,
                confirmation=confirmation,
            )
        except RecognitionSnapshotConflict as exc:
            error = str(exc)
            if error not in {
                "recognition_snapshot_revision_conflict",
                "recognition_snapshot_expired",
                "recognition_snapshot_not_current",
            }:
                error = "recognition_snapshot_conflict"
            return None, {"ok": False, "error": error}
        except (RecognitionSnapshotAccessDenied, ValueError):
            return None, {"ok": False, "error": "recognition_snapshot_access_denied"}
        return updated, None

    def _current_resolution_targets(
        self, identity: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        if self._recognition_snapshots is None:
            return []
        try:
            snapshots = self._recognition_snapshots.list_current(
                tenant_id=identity["tenant_id"],
                shop_id=identity["shop_id"],
                buyer_id=identity["buyer_id"],
                chat_id=identity["chat_id"],
            )
        except Exception:
            LOGGER.exception(
                "event=recognition_snapshot_targets_unavailable chat_id=%s",
                identity.get("chat_id"),
            )
            return []
        targets: list[dict[str, Any]] = []
        for snapshot in snapshots:
            recognition = snapshot.normalized
            conflicts = [
                warning.removeprefix("conflict:")
                for warning in recognition.warnings
                if warning.startswith("conflict:")
                and warning.removeprefix("conflict:") in CRITICAL_IMAGE_CONFLICT_FIELDS
            ]
            if not (
                recognition.candidate_cinemas
                or recognition.candidate_movies
                or recognition.candidate_shows
                or conflicts
                or recognition.missing_fields
            ):
                continue
            targets.append({
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_revision": snapshot.revision,
                "target_id": snapshot.target_id,
                "recognition": _public_recognition_payload(recognition),
                "candidate_cinemas": [
                    item.model_dump(mode="json") for item in recognition.candidate_cinemas
                ],
                "candidate_movies": [
                    item.model_dump(mode="json") for item in recognition.candidate_movies
                ],
                "candidate_shows": [
                    item.model_dump(mode="json") for item in recognition.candidate_shows
                ],
                "conflict_fields": conflicts,
                "missing_fields": list(recognition.missing_fields),
            })
        return targets

    async def _execute_agent_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        identity: Mapping[str, str],
        order: object = None,
        envelope: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Execute only request-scoped read/quote tools for the Agent.

        Transaction writes deliberately return a rule-owned rejection here;
        order lifecycle events are the only entry point for those mutations.
        """
        tool_name = str(name or "").strip()
        if not isinstance(arguments, Mapping) or len(arguments) > 80:
            return {"ok": False, "error": "tool_arguments_invalid"}
        if tool_name in {"change_order_price", "submit_fulfillment", "send_ticket", "refund_or_intercept"}:
            return {"ok": False, "error": "tool_requires_rule_event"}
        if tool_name == "get_order_state":
            if arguments:
                # The current Xianyu order is already bound by the request
                # scope; accepting an arbitrary provider/order id here would
                # turn this read into a cross-buyer lookup side door.
                return {"ok": False, "error": "order_state_arguments_invalid"}
            if not isinstance(order, Mapping):
                return {"ok": False, "error": "order_state_unavailable"}
            return {"ok": True, "order": {
                key: order.get(key) for key in (
                    "orderId", "order_id", "orderStatus", "order_status", "payment",
                    "priceFee", "price_fee", "payTime", "pay_time",
                ) if order.get(key) is not None
            }}
        if tool_name == "recognize_screenshot":
            raw_urls = arguments.get("image_urls")
            if raw_urls is None:
                raw_urls = [arguments.get("image_url")]
            if not isinstance(raw_urls, list) or not 1 <= len(raw_urls) <= 3:
                return {"ok": False, "error": "image_count_invalid"}
            try:
                image_urls = [validate_image_url(value) for value in raw_urls]
                if len(set(image_urls)) != len(image_urls):
                    return {"ok": False, "error": "duplicate_image_url"}
                event_id = str(identity.get("event_id") or "").strip()
                attempt_keys = [
                    hashlib.sha256(f"{event_id}\x1f{image_url}".encode("utf-8")).hexdigest()
                    for image_url in image_urls
                ]
                buyer_message = str(arguments.get("buyer_message") or "")[:2_000]
                recognize_from_url = getattr(self._recognition, "recognize_from_url", None)
                recognitions: list[MovieImageInfo] = []
                recognition_indexes: list[int] = []
                review_conflicts: list[str] = []
                image_results: list[dict[str, Any]] = []
                image_types = ["failed"] * len(image_urls)
                for image_index, (image_url, attempt_key) in enumerate(
                    zip(image_urls, attempt_keys, strict=True)
                ):
                    previous = self._agent_image_results.get(attempt_key)
                    attempts = self._agent_image_attempts.get(attempt_key, 0)
                    if attempts >= 2:
                        image_results.append({
                            "image_index": image_index,
                            "status": "error",
                            "error": "image_review_limit_reached",
                        })
                        continue
                    if previous is not None and not _image_review_required(previous):
                        if len(image_urls) == 1:
                            return {"ok": False, "error": "image_review_not_required"}
                        recognition = previous
                    else:
                        # Claim each image independently. A failed second image
                        # must never discard the first image's successful fact.
                        self._agent_image_attempts[attempt_key] = attempts + 1
                        try:
                            if callable(recognize_from_url):
                                recognition_kwargs: dict[str, Any] = {
                                    "buyer_message": buyer_message,
                                    "out_trade_no": f"rec-{attempt_key[:60]}",
                                }
                                while True:
                                    try:
                                        recognition = await recognize_from_url(
                                            image_url, **recognition_kwargs,
                                        )
                                        break
                                    except TypeError as error:
                                        detail = str(error)
                                        removable = next((
                                            key for key in ("out_trade_no", "buyer_message")
                                            if key in recognition_kwargs and key in detail
                                        ), None)
                                        if removable is None:
                                            raise
                                        recognition_kwargs.pop(removable)
                            else:
                                image, content_type = await self._load_image(image_url)
                                recognition = await self._recognition.recognize(
                                    image, content_type, buyer_message=buyer_message,
                                )
                            if not isinstance(recognition, MovieImageInfo):
                                raise TypeError("recognition_result_invalid")
                        except Exception:  # noqa: BLE001 - isolate one image failure
                            image_results.append({
                                "image_index": image_index,
                                "status": "error",
                                "error": "recognition_failed",
                            })
                            continue
                        self._agent_image_results[attempt_key] = recognition
                        if previous is not None:
                            recognition, conflicts = _merge_image_recognitions([
                                previous, recognition,
                            ])
                            review_conflicts.extend(conflicts)
                    recognitions.append(recognition)
                    recognition_indexes.append(image_index)
                    image_types[image_index] = _recognized_image_kind(recognition)
                    image_results.append({
                        "image_index": image_index, "status": "success",
                    })
                image_results.sort(key=lambda item: int(item["image_index"]))
                if not recognitions:
                    if len(image_urls) == 1 and image_results:
                        single_error = str(image_results[0].get("error") or "")
                        if single_error == "image_review_limit_reached":
                            return {"ok": False, "error": single_error}
                    return {
                        "ok": False,
                        "error": "recognition_failed",
                        "image_count": len(image_urls),
                        "recognized_image_count": 0,
                        "partial_failure": False,
                        "image_types": image_types,
                        "image_results": image_results,
                    }
                quote_targets = _build_image_quote_targets(recognitions)
                if not quote_targets:
                    raise ValueError("image_quote_targets_empty")
                for target in quote_targets:
                    target["image_indexes"] = [
                        recognition_indexes[index] for index in target["image_indexes"]
                    ]
                recognition = quote_targets[0]["recognition"]
                conflict_fields = sorted({
                    *review_conflicts,
                    *(
                        field
                        for target in quote_targets
                        for field in target["conflict_fields"]
                    ),
                })
                quote_image_allowed = any(
                    image_type in {"seat_selection", "show_selection"}
                    for image_type in image_types
                )
                image_kind_warning = "quote_image_kind:allowed" if quote_image_allowed else "quote_image_kind:blocked"
                recognition = recognition.model_copy(update={
                    "warnings": [
                        warning for warning in recognition.warnings
                        if not warning.startswith("quote_image_kind:")
                    ][:19] + [image_kind_warning],
                })
                if len(self._agent_image_attempts) > 4_096:
                    stale_keys = list(self._agent_image_attempts)[:1_024]
                    for stale_key in stale_keys:
                        self._agent_image_attempts.pop(stale_key, None)
                        self._agent_image_results.pop(stale_key, None)
            except Exception:
                return {"ok": False, "error": "recognition_failed"}
            # Recognition is deliberately a pure tool: no cinema completion,
            # quote request, or fixed reply happens in this branch. Ambiguous
            # candidates and critical multi-image conflicts are retained so
            # they cannot be bypassed by editing the next tool arguments.
            key = self._candidate_key(identity)
            if recognition.candidate_shows and self._recognition_snapshots is None:
                self._remember_pending_shows(identity, [{
                    "show_id": candidate.show_id,
                    "showtime_start": candidate.start_time,
                    "showtime_end": candidate.end_time,
                    "hall_name": candidate.hall_name,
                    "date_text": candidate.start_time[:10] if candidate.start_time else None,
                    "language": candidate.language,
                    "format": candidate.dimension,
                    "cinema_id": candidate.cinema_id,
                    "movie_id": candidate.movie_id,
                    "movie_name": candidate.movie_name,
                } for candidate in recognition.candidate_shows])
            elif self._recognition_snapshots is None:
                self._forget_pending_shows(identity)
            critical_conflicts = tuple(
                field for field in conflict_fields
                if field in CRITICAL_IMAGE_CONFLICT_FIELDS
            )
            if critical_conflicts:
                self._pending_image_conflicts[key] = critical_conflicts
            else:
                self._pending_image_conflicts.pop(key, None)
            followup_required = bool(
                not recognition.cinema_name
                or not recognition.movie_name
                or not (recognition.date or recognition.date_text)
                or not recognition.showtime_start
                or recognition.missing_fields
                or recognition.match_level in {"CANDIDATE", "NONE", "SHOW_EXPIRED"}
                or recognition.no_match_reason is not None
                or recognition.recognition_blocker is not None
                or (
                    recognition.cinema_truncated is True
                    and recognition.match_level != "EXACT"
                )
                or recognition.seat_matched is False
                or recognition.price_mismatch is True
            )
            if (
                len(quote_targets) == 1
                and (
                    critical_conflicts
                    or len(recognition.candidate_cinemas) > 1
                    or followup_required
                    or not quote_image_allowed
                )
            ):
                self._remember_pending_candidate(identity, recognition)
            else:
                self._forget_pending_candidate(identity)
            public_recognition = _public_recognition_payload(recognition)
            public_quote_targets: list[dict[str, Any]] = []
            for index, target in enumerate(quote_targets):
                target_id = f"image-target-{index + 1}"
                target_recognition = target["recognition"]
                public_target = {
                    "target_id": target_id,
                    "image_indexes": target["image_indexes"],
                    "conflict_fields": target["conflict_fields"],
                    "recognition": _public_recognition_payload(target_recognition),
                }
                if self._recognition_snapshots is not None:
                    snapshot_event_id = f'{identity["event_id"]}:{target_id}'
                    if len(snapshot_event_id) > 200:
                        snapshot_event_id = (
                            snapshot_event_id[:158]
                            + ":"
                            + hashlib.sha256(snapshot_event_id.encode("utf-8")).hexdigest()[:40]
                        )
                    snapshot = self._recognition_snapshots.create(
                        tenant_id=identity["tenant_id"],
                        shop_id=identity["shop_id"],
                        buyer_id=identity["buyer_id"],
                        chat_id=identity["chat_id"],
                        event_id=snapshot_event_id,
                        target_id=target_id,
                        recognition=target_recognition,
                        raw_results=target_recognition.raw_results,
                        final_results=target_recognition.final_results,
                        raw_response=target_recognition.raw_response,
                        recognize_id=target_recognition.recognition_id,
                        provider_request_id=target_recognition.provider_request_id,
                        trace_id=target_recognition.trace_id,
                    )
                    public_target.update({
                        "snapshot_id": snapshot.snapshot_id,
                        "snapshot_revision": snapshot.revision,
                    })
                public_quote_targets.append(public_target)
            return {
                "ok": True,
                "image_count": len(image_urls),
                "recognized_image_count": len(recognitions),
                "partial_failure": len(recognitions) != len(image_urls),
                "image_types": image_types,
                "image_results": image_results,
                "conflict_fields": conflict_fields,
                "recognition": public_recognition,
                "quote_targets": public_quote_targets,
            }
        if tool_name == "resolve_cinema":
            snapshot, snapshot_error = self._resolution_snapshot(arguments, identity)
            if snapshot_error is not None:
                return snapshot_error
            pending = snapshot.normalized if snapshot is not None else self._get_pending_candidate(identity)
            if pending is None or not pending.candidate_cinemas:
                return {"ok": False, "error": "cinema_candidates_unavailable"}
            payload = envelope.get("payload") if isinstance(envelope, Mapping) and isinstance(envelope.get("payload"), Mapping) else {}
            current_message = str(payload.get("content") or payload.get("text") or "").strip()
            supplied_message = str(arguments.get("buyer_message") or "").strip()
            if not current_message or supplied_message != current_message:
                return {"ok": False, "error": "cinema_choice_buyer_confirmation_required"}
            try:
                cinema_id = int(arguments.get("cinema_id"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "cinema_choice_invalid"}
            selected = next(
                (candidate for candidate in pending.candidate_cinemas if candidate.cinema_id == cinema_id),
                None,
            )
            if selected is None:
                return {"ok": False, "error": "cinema_choice_invalid"}
            resolved_fields = {"cinema_id", "cinema_name", "cinema_address", "city"}
            remaining_conflicts = tuple(
                field for field in (
                    tuple(
                        warning.removeprefix("conflict:")
                        for warning in pending.warnings
                        if warning.startswith("conflict:")
                    )
                    if snapshot is not None
                    else self._pending_image_conflicts.get(self._candidate_key(identity), ())
                )
                if field not in resolved_fields
            )
            resolved = pending.model_copy(update={
                "cinema_id": selected.cinema_id, "cinema_name": selected.name,
                "city": selected.city_name or pending.city,
                "cinema_address": selected.address or pending.cinema_address,
                "match_level": "EXACT" if not remaining_conflicts else "NONE",
                "candidate_cinemas": [],
                "missing_fields": [field for field in pending.missing_fields if field not in resolved_fields],
                "warnings": [
                    warning for warning in pending.warnings
                    if warning not in {f"conflict:{field}" for field in resolved_fields}
                ],
            })
            if pending.recognition_id:
                confirm = getattr(self._recognition, "confirm_recognition_candidate", None)
                if not callable(confirm):
                    return {"ok": False, "error": "recognition_confirmation_unavailable"}
                try:
                    try:
                        authoritative = await confirm(
                            pending.recognition_id,
                            cinema_id=selected.cinema_id,
                            city_name=selected.city_name or pending.city,
                        )
                    except TypeError:
                        authoritative = await confirm(
                            pending.recognition_id, selected.cinema_id,
                        )
                except Exception:
                    return {"ok": False, "error": "recognition_confirmation_failed"}
                if not isinstance(authoritative, MovieImageInfo):
                    return {"ok": False, "error": "recognition_confirmation_invalid"}
                resolved = authoritative
            updated_snapshot = None
            if snapshot is not None:
                updated_snapshot, snapshot_error = self._commit_resolution_snapshot(
                    snapshot=snapshot,
                    identity=identity,
                    tool_name=tool_name,
                    recognition=resolved,
                    confirmation={
                        "tool": tool_name,
                        "buyer_message": supplied_message,
                        "cinema_id": selected.cinema_id,
                    },
                )
                if snapshot_error is not None:
                    return snapshot_error
            key = self._candidate_key(identity)
            if snapshot is None:
                if (
                    remaining_conflicts
                    or resolved.match_level != "EXACT"
                    or len(resolved.candidate_cinemas) > 1
                ):
                    self._pending_image_conflicts[key] = remaining_conflicts
                    self._remember_pending_candidate(identity, resolved)
                else:
                    self._pending_image_conflicts.pop(key, None)
                    self._forget_pending_candidate(identity)
            result = {"ok": True, "recognition": _public_recognition_payload(resolved)}
            if updated_snapshot is not None:
                result.update({
                    "snapshot_id": updated_snapshot.snapshot_id,
                    "snapshot_revision": updated_snapshot.revision,
                    "target_id": updated_snapshot.target_id,
                })
            return result
        if tool_name == "resolve_movie":
            snapshot, snapshot_error = self._resolution_snapshot(arguments, identity)
            if snapshot_error is not None:
                return snapshot_error
            pending = snapshot.normalized if snapshot is not None else self._get_pending_candidate(identity)
            if pending is None or not pending.candidate_movies:
                return {"ok": False, "error": "movie_candidates_unavailable"}
            payload = envelope.get("payload") if isinstance(envelope, Mapping) and isinstance(envelope.get("payload"), Mapping) else {}
            current_message = str(payload.get("content") or payload.get("text") or "").strip()
            supplied_message = str(arguments.get("buyer_message") or "").strip()
            if not current_message or supplied_message != current_message:
                return {"ok": False, "error": "movie_choice_buyer_confirmation_required"}
            try:
                movie_id = int(arguments.get("movie_id"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "movie_choice_invalid"}
            selected_movie = next(
                (candidate for candidate in pending.candidate_movies if candidate.movie_id == movie_id),
                None,
            )
            if selected_movie is None:
                return {"ok": False, "error": "movie_choice_invalid"}
            if pending.recognition_id:
                confirm = getattr(self._recognition, "confirm_recognition_candidate", None)
                if not callable(confirm):
                    return {"ok": False, "error": "recognition_confirmation_unavailable"}
                try:
                    resolved = await confirm(
                        pending.recognition_id, movie_id=movie_id,
                        city_name=pending.city,
                    )
                except Exception:
                    return {"ok": False, "error": "recognition_confirmation_failed"}
                if not isinstance(resolved, MovieImageInfo):
                    return {"ok": False, "error": "recognition_confirmation_invalid"}
            else:
                if snapshot is not None:
                    return {"ok": False, "error": "recognition_confirmation_context_required"}
                resolved = pending.model_copy(update={
                    "movie_id": selected_movie.movie_id,
                    "movie_name": selected_movie.name,
                    "candidate_movies": [],
                    "missing_fields": [
                        field for field in pending.missing_fields
                        if field not in {"movie_id", "movie_name"}
                    ],
                })
            updated_snapshot = None
            if snapshot is not None:
                updated_snapshot, snapshot_error = self._commit_resolution_snapshot(
                    snapshot=snapshot,
                    identity=identity,
                    tool_name=tool_name,
                    recognition=resolved,
                    confirmation={
                        "tool": tool_name,
                        "buyer_message": supplied_message,
                        "movie_id": movie_id,
                    },
                )
                if snapshot_error is not None:
                    return snapshot_error
            elif resolved.match_level == "EXACT" and len(resolved.candidate_movies) <= 1:
                self._forget_pending_candidate(identity)
            else:
                self._remember_pending_candidate(identity, resolved)
            result = {"ok": True, "recognition": _public_recognition_payload(resolved)}
            if updated_snapshot is not None:
                result.update({
                    "snapshot_id": updated_snapshot.snapshot_id,
                    "snapshot_revision": updated_snapshot.revision,
                    "target_id": updated_snapshot.target_id,
                })
            return result
        if tool_name == "resolve_showtime":
            snapshot, snapshot_error = self._resolution_snapshot(arguments, identity)
            if snapshot_error is not None:
                return snapshot_error
            if snapshot is not None:
                candidates = [{
                    "show_id": candidate.show_id,
                    "showtime_start": candidate.start_time,
                    "showtime_end": candidate.end_time,
                    "hall_name": candidate.hall_name,
                    "language": candidate.language,
                    "format": candidate.dimension,
                    "movie_name": candidate.movie_name,
                    "movie_id": candidate.movie_id,
                    "cinema_id": candidate.cinema_id,
                } for candidate in snapshot.normalized.candidate_shows]
            else:
                candidates = self._get_pending_shows(identity)
            if not candidates:
                return {"ok": False, "error": "showtime_candidates_unavailable"}
            pending = snapshot.normalized if snapshot is not None else self._get_pending_candidate(identity)
            if pending is None:
                return {"ok": False, "error": "showtime_context_unavailable"}
            payload = envelope.get("payload") if isinstance(envelope, Mapping) and isinstance(envelope.get("payload"), Mapping) else {}
            current_message = str(payload.get("content") or payload.get("text") or "").strip()
            supplied_message = str(arguments.get("buyer_message") or "").strip()
            if not current_message or supplied_message != current_message:
                return {"ok": False, "error": "showtime_choice_buyer_confirmation_required"}
            show_id = str(arguments.get("show_id") or arguments.get("showId") or "").strip()
            selected = next(
                (candidate for candidate in candidates if str(candidate.get("show_id") or "") == show_id),
                None,
            )
            if selected is None:
                return {"ok": False, "error": "showtime_choice_invalid"}
            try:
                resolved_payload = {
                    field: value for field, value in pending.model_dump(mode="json").items()
                    if field in MovieImageInfo.model_fields
                }
                for field in (
                    "show_id", "showtime_start", "showtime_end", "hall_name",
                    "language", "format", "movie_name", "cinema_id",
                ):
                    if selected.get(field) is not None:
                        resolved_payload[field] = selected[field]
                resolved_payload["missing_fields"] = [
                    field for field in pending.missing_fields
                    if field not in {"show_id", "showtime_start", "showtime_end", "hall_name"}
                ]
                resolved = MovieImageInfo.model_validate(resolved_payload)
            except Exception:
                return {"ok": False, "error": "showtime_choice_invalid"}
            if pending.recognition_id:
                confirm = getattr(self._recognition, "confirm_recognition_candidate", None)
                if not callable(confirm):
                    return {"ok": False, "error": "recognition_confirmation_unavailable"}
                try:
                    authoritative = await confirm(
                        pending.recognition_id, show_id=show_id,
                        city_name=pending.city,
                    )
                except Exception:
                    return {"ok": False, "error": "recognition_confirmation_failed"}
                if not isinstance(authoritative, MovieImageInfo):
                    return {"ok": False, "error": "recognition_confirmation_invalid"}
                resolved = authoritative
            elif snapshot is not None and "source:show.list" not in pending.warnings:
                return {"ok": False, "error": "recognition_confirmation_context_required"}
            if snapshot is not None and "source:show.list" in pending.warnings:
                # A show/list response already came from the authoritative
                # Liangpiao read endpoint and has no recognition ID to
                # re-confirm. Buyer selection is still required above; the
                # durable snapshot CAS is the authority boundary here.
                resolved = resolved.model_copy(update={
                    "candidate_shows": [],
                    "match_level": "EXACT",
                })
            updated_snapshot = None
            if snapshot is not None:
                updated_snapshot, snapshot_error = self._commit_resolution_snapshot(
                    snapshot=snapshot,
                    identity=identity,
                    tool_name=tool_name,
                    recognition=resolved,
                    confirmation={
                        "tool": tool_name,
                        "buyer_message": supplied_message,
                        "show_id": show_id,
                    },
                )
                if snapshot_error is not None:
                    return snapshot_error
            else:
                self._remember_pending_candidate(identity, resolved)
                self._remember_pending_shows(identity, [selected])
            result = {"ok": True, "recognition": _public_recognition_payload(resolved)}
            if updated_snapshot is not None:
                result.update({
                    "snapshot_id": updated_snapshot.snapshot_id,
                    "snapshot_revision": updated_snapshot.revision,
                    "target_id": updated_snapshot.target_id,
                })
            return result
        if tool_name == "resolve_image_conflict":
            snapshot, snapshot_error = self._resolution_snapshot(arguments, identity)
            if snapshot_error is not None:
                return snapshot_error
            pending = snapshot.normalized if snapshot is not None else self._get_pending_candidate(identity)
            if pending is None:
                return {"ok": False, "error": "image_conflict_unavailable"}
            key = self._candidate_key(identity)
            persisted_conflicts = tuple(
                warning.removeprefix("conflict:")
                for warning in pending.warnings
                if warning.startswith("conflict:")
                and warning.removeprefix("conflict:") in CRITICAL_IMAGE_CONFLICT_FIELDS
            )
            conflicts = (
                persisted_conflicts
                if snapshot is not None
                else self._pending_image_conflicts.get(key, persisted_conflicts)
            )
            if not conflicts:
                return {"ok": False, "error": "image_conflict_unavailable"}
            payload = envelope.get("payload") if isinstance(envelope, Mapping) and isinstance(envelope.get("payload"), Mapping) else {}
            current_message = str(payload.get("content") or payload.get("text") or "").strip()
            supplied_message = str(arguments.get("buyer_message") or "").strip()
            if not current_message or supplied_message != current_message:
                return {"ok": False, "error": "image_conflict_buyer_confirmation_required"}
            updates = arguments.get("field_updates")
            if not isinstance(updates, Mapping) or not updates:
                return {"ok": False, "error": "image_conflict_updates_invalid"}
            buyer_clarifiable_fields = {
                "city", "cinema_name", "movie_name", "date_text",
                "showtime_start", "hall_name",
            }
            update_fields = {str(field) for field in updates}
            if (
                not update_fields.issubset(buyer_clarifiable_fields)
                or not update_fields.issubset(set(conflicts))
            ):
                return {"ok": False, "error": "image_conflict_updates_not_allowed"}
            normalized_updates = {
                field: str(value or "").strip() for field, value in updates.items()
            }
            if any(not value or len(value) > 240 for value in normalized_updates.values()):
                return {"ok": False, "error": "image_conflict_updates_invalid"}
            authoritative = pending
            if snapshot is not None:
                confirm = getattr(self._recognition, "confirm_recognition_candidate", None)
                if not callable(confirm) or not pending.recognition_id:
                    return {"ok": False, "error": "recognition_confirmation_unavailable"}
                if pending.cinema_id is None and pending.movie_id is None and pending.show_id is None:
                    return {"ok": False, "error": "recognition_confirmation_context_required"}
                try:
                    authoritative = await confirm(
                        pending.recognition_id,
                        cinema_id=pending.cinema_id,
                        movie_id=pending.movie_id,
                        show_id=pending.show_id,
                        city_name=pending.city,
                    )
                except Exception:
                    return {"ok": False, "error": "recognition_confirmation_failed"}
                if not isinstance(authoritative, MovieImageInfo):
                    return {"ok": False, "error": "recognition_confirmation_invalid"}
            try:
                resolved_payload = {
                    field: value for field, value in authoritative.model_dump(mode="json").items()
                    if field in MovieImageInfo.model_fields
                }
                resolved_payload.update(normalized_updates)
                resolved_fields = set(normalized_updates)
                resolved_payload["missing_fields"] = [
                    field for field in authoritative.missing_fields if field not in resolved_fields
                ]
                resolved_payload["warnings"] = [
                    warning for warning in authoritative.warnings
                    if warning not in {f"conflict:{field}" for field in resolved_fields}
                ]
                remaining_conflicts = tuple(
                    field for field in conflicts if field not in resolved_fields
                )
                resolved_payload["match_level"] = (
                    "NONE" if remaining_conflicts
                    else (
                        "CANDIDATE"
                        if len(authoritative.candidate_cinemas) > 1
                        else authoritative.match_level
                    )
                )
                resolved = MovieImageInfo.model_validate(resolved_payload)
            except Exception:
                return {"ok": False, "error": "image_conflict_updates_invalid"}
            updated_snapshot = None
            if snapshot is not None:
                updated_snapshot, snapshot_error = self._commit_resolution_snapshot(
                    snapshot=snapshot,
                    identity=identity,
                    tool_name=tool_name,
                    recognition=resolved,
                    confirmation={
                        "tool": tool_name,
                        "buyer_message": supplied_message,
                        "field_updates": normalized_updates,
                    },
                )
                if snapshot_error is not None:
                    return snapshot_error
            elif remaining_conflicts or len(resolved.candidate_cinemas) > 1:
                if remaining_conflicts:
                    self._pending_image_conflicts[key] = remaining_conflicts
                else:
                    self._pending_image_conflicts.pop(key, None)
                self._remember_pending_candidate(identity, resolved)
            else:
                self._pending_image_conflicts.pop(key, None)
                self._forget_pending_candidate(identity)
            result = {
                "ok": True,
                "recognition": _public_recognition_payload(resolved),
                "remaining_conflict_fields": list(remaining_conflicts),
            }
            if updated_snapshot is not None:
                result.update({
                    "snapshot_id": updated_snapshot.snapshot_id,
                    "snapshot_revision": updated_snapshot.revision,
                    "target_id": updated_snapshot.target_id,
                })
            return result
        if tool_name in {
            "quote.preflight_current", "get_quote", "get_authoritative_quote",
            "reprice_seats", "recognition.resolve_seats",
        }:
            pending = self._get_pending_candidate(identity)
            snapshot_request: MovieImageInfo | None = None
            if self._recognition_snapshots is not None:
                snapshot_id = str(arguments.get("snapshot_id") or "").strip()
                target_id = str(arguments.get("target_id") or "").strip()
                try:
                    snapshot_revision = int(arguments.get("snapshot_revision"))
                except (TypeError, ValueError):
                    snapshot_revision = 0
                if not snapshot_id or not target_id or snapshot_revision < 1:
                    return {"ok": False, "error": "recognition_snapshot_reference_required"}
                try:
                    current_snapshot = self._recognition_snapshots.get_current(
                        tenant_id=identity["tenant_id"],
                        shop_id=identity["shop_id"],
                        buyer_id=identity["buyer_id"],
                        chat_id=identity["chat_id"],
                        target_id=target_id,
                    )
                except Exception:
                    return {"ok": False, "error": "recognition_snapshot_unavailable"}
                if current_snapshot is None:
                    return {"ok": False, "error": "recognition_snapshot_expired"}
                if (
                    current_snapshot.snapshot_id != snapshot_id
                    or current_snapshot.revision != snapshot_revision
                ):
                    return {"ok": False, "error": "recognition_snapshot_conflict"}
                snapshot_request = current_snapshot.normalized
            # A validated target snapshot is the complete authority boundary.
            # Never let the legacy conversation-wide pending slot from another
            # image override or block this target.
            if snapshot_request is not None:
                pending = None
                authoritative_recognition = snapshot_request
                show_candidates = [{
                    "show_id": candidate.show_id,
                    "showtime_start": candidate.start_time,
                    "showtime_end": candidate.end_time,
                    "hall_name": candidate.hall_name,
                    "language": candidate.language,
                    "format": candidate.dimension,
                    "movie_name": candidate.movie_name,
                    "movie_id": candidate.movie_id,
                    "cinema_id": candidate.cinema_id,
                } for candidate in snapshot_request.candidate_shows]
            else:
                authoritative_recognition = pending
                show_candidates = self._get_pending_shows(identity)
            if authoritative_recognition is not None and "quote_image_kind:blocked" in authoritative_recognition.warnings:
                return {"ok": False, "error": "image_type_not_quotable"}
            if authoritative_recognition is not None and (
                authoritative_recognition.match_level == "SHOW_EXPIRED"
                or authoritative_recognition.no_match_reason == "SHOW_EXPIRED"
            ):
                return {"ok": False, "error": "showtime_expired"}
            quote_route = None
            if authoritative_recognition is not None and self._cinema_route_resolver is not None:
                try:
                    quote_route = (
                        await self._cinema_route_resolver.resolve(authoritative_recognition)
                    ).route
                except Exception:  # noqa: BLE001 - fail closed for routing
                    quote_route = None
            if (
                authoritative_recognition is not None
                and authoritative_recognition.seat_matched is False
                and not is_wplus_unselected_image(authoritative_recognition)
                and quote_route != "WANDA_SELF"
            ):
                # Liangpiao's recognition payload may identify a Wanda seat by
                # row/column while omitting Liangpiao's own ``seat_no`` and
                # ``area_id``. Keep this gate for the Liangpiao exact-seat
                # route; WandaDirectQuoteService performs its own lookup.
                return {"ok": False, "error": "seat_mapping_required"}
            if authoritative_recognition is not None and authoritative_recognition.price_mismatch is True:
                return {"ok": False, "error": "image_price_mismatch"}
            if len(show_candidates) > 1:
                return {"ok": False, "error": "showtime_choice_required"}
            persisted_conflicts = tuple(
                warning.removeprefix("conflict:")
                for warning in (
                    authoritative_recognition.warnings
                    if authoritative_recognition is not None else []
                )
                if warning.startswith("conflict:")
                and warning.removeprefix("conflict:") in CRITICAL_IMAGE_CONFLICT_FIELDS
            )
            conflicts = (
                persisted_conflicts
                if snapshot_request is not None
                else self._pending_image_conflicts.get(
                    self._candidate_key(identity), persisted_conflicts,
                )
            )
            if conflicts:
                return {
                    "ok": False, "error": "image_fields_conflict",
                    "conflict_fields": list(conflicts),
                }
            if (
                authoritative_recognition is not None
                and authoritative_recognition.match_level != "EXACT"
                and len(authoritative_recognition.candidate_cinemas) > 1
            ):
                return {"ok": False, "error": "cinema_choice_required"}
            try:
                if snapshot_request is not None:
                    request = snapshot_request
                    payload = (
                        envelope.get("payload")
                        if isinstance(envelope, Mapping)
                        and isinstance(envelope.get("payload"), Mapping)
                        else {}
                    )
                    explicit_seats = _explicit_seats_in_buyer_hint(
                        str(payload.get("content") or payload.get("text") or "")
                    )
                    if explicit_seats:
                        # The latest buyer text overrides seats visible in the
                        # screenshot, including when the model chose a legacy
                        # quote tool instead of recognition.resolve_seats.
                        request = request.model_copy(update={
                            "selected_seats": [
                                SelectedSeat(seat_number=seat) for seat in explicit_seats
                            ],
                            "selected_count_visible": len(explicit_seats),
                        })
                    elif tool_name in {"reprice_seats", "recognition.resolve_seats"}:
                        return {"ok": False, "error": "buyer_seat_confirmation_required"}
                else:
                    request_payload = {
                        key: value for key, value in arguments.items()
                        if key in MovieImageInfo.model_fields
                    }
                    request = MovieImageInfo.model_validate(request_payload)
                if snapshot_request is not None and not _recognition_ready_for_preflight(request):
                    if quote_route != "WANDA_SELF" or not _recognition_ready_for_wanda_quote(request):
                        return {"ok": False, "error": "recognition_not_ready_for_preflight"}
                if show_candidates:
                    expected_show_id = str(show_candidates[0].get("show_id") or "")
                    if not request.show_id or request.show_id != expected_show_id:
                        return {"ok": False, "error": "showtime_candidate_mismatch"}
                recognition, quote = await self._quote_for_recognition(request, identity)
            except Exception:
                return {"ok": False, "error": "quote_unavailable"}
            if envelope is not None:
                call_fingerprint = hashlib.sha256(
                    json.dumps(
                        {"tool": tool_name, "arguments": dict(arguments)},
                        ensure_ascii=False, sort_keys=True, default=str,
                    ).encode("utf-8")
                ).hexdigest()[:24]
                self._record_quote(
                    identity, envelope, recognition, quote,
                    source="agent_tool_quote",
                    record_id=f'{identity["event_id"]}:agent:{call_fingerprint}',
                )
                remember = getattr(self._chat, "remember_image_context", None)
                if callable(remember):
                    remember(
                        f'{identity["tenant_id"]}:{identity["shop_id"]}:{identity["chat_id"]}',
                        recognition, quote, None,
                    )
            if pending is not None and not conflicts and len(pending.candidate_cinemas) <= 1:
                self._forget_pending_candidate(identity)
            return {
                "ok": True,
                "recognition": _public_recognition_payload(recognition),
                "quote": quote.model_dump(mode="json"),
            }
        return {"ok": False, "error": "tool_not_allowed"}

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
        self._pending_show_candidates.pop(key, None)
        if self._pending_candidate_store is not None:
            self._pending_candidate_store.delete(key)

    def _remember_pending_shows(
        self, identity: Mapping[str, str], candidates: list[dict[str, Any]],
    ) -> None:
        key = self._candidate_key(identity)
        self._pending_show_candidates[key] = [dict(item) for item in candidates[:20]]
        saver = getattr(self._pending_candidate_store, "save_show_candidates", None)
        if callable(saver):
            saver(key, self._pending_show_candidates[key])

    def _get_pending_shows(self, identity: Mapping[str, str]) -> list[dict[str, Any]]:
        key = self._candidate_key(identity)
        candidates = self._pending_show_candidates.get(key)
        if candidates is None:
            getter = getattr(self._pending_candidate_store, "get_show_candidates", None)
            if callable(getter):
                loaded = getter(key)
                if isinstance(loaded, list):
                    candidates = [dict(item) for item in loaded if isinstance(item, Mapping)][:20]
                    self._pending_show_candidates[key] = candidates
        return [dict(item) for item in (candidates or [])]

    def _forget_pending_shows(self, identity: Mapping[str, str]) -> None:
        key = self._candidate_key(identity)
        self._pending_show_candidates.pop(key, None)
        deleter = getattr(self._pending_candidate_store, "delete_show_candidates", None)
        if callable(deleter):
            deleter(key)

    def _record_show_list_snapshot(
        self,
        identity: Mapping[str, str],
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Persist a ``show.list`` response as an independent target.

        ``show.list`` is a read-only Agent tool, but its candidates are later
        consumed by ``resolve_showtime``.  Keeping them in the old
        conversation-wide pending slot lets a later image overwrite the list
        (or vice versa).  When the durable snapshot store is enabled, every
        query gets a stable target and the references are returned to the
        Agent so the next tool call can carry the CAS revision.
        """
        if self._recognition_snapshots is None or result.get("ok") is False:
            return result
        candidates = _extract_show_candidates(result)
        if not candidates:
            return result

        def value(*names: str) -> Any:
            for source in (arguments, result):
                for name in names:
                    candidate = source.get(name)
                    if candidate is not None and str(candidate).strip() != "":
                        return candidate
            return None

        def positive_int(raw: Any) -> int | None:
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        normalized_shows: list[ShowCandidate] = []
        for item in candidates[:10]:
            try:
                normalized_shows.append(ShowCandidate.model_validate({
                    "show_id": item.get("show_id"),
                    "cinema_id": item.get("cinema_id"),
                    "movie_id": item.get("movie_id"),
                    "movie_name": item.get("movie_name"),
                    "hall_name": item.get("hall_name"),
                    "start_time": item.get("showtime_start"),
                    "end_time": item.get("showtime_end"),
                    "dimension": item.get("format"),
                    "language": item.get("language"),
                }))
            except Exception:
                # A malformed provider row must not poison the other rows or
                # create a snapshot that cannot be loaded on the next turn.
                continue
        if not normalized_shows:
            return result

        cinema_id = positive_int(value("cinemaId", "cinema_id"))
        movie_id = positive_int(value("movieId", "movie_id"))
        cinema_name = value("cinemaName", "cinema_name")
        movie_name = value("movieName", "movie_name")
        city = value("city", "cityName", "city_name")
        date_text = value("showDate", "show_date", "date", "date_text")
        recognition = MovieImageInfo(
            cinema_id=cinema_id,
            cinema_name=str(cinema_name)[:240] if cinema_name is not None else None,
            movie_id=movie_id,
            movie_name=str(movie_name)[:160] if movie_name is not None else None,
            city=str(city)[:80] if city is not None else None,
            date_text=str(date_text)[:80] if date_text is not None else None,
            match_level="EXACT" if len(normalized_shows) == 1 else "CANDIDATE",
            candidate_shows=normalized_shows,
            warnings=["source:show.list"],
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {"arguments": dict(arguments), "shows": [item.model_dump(mode="json") for item in normalized_shows]},
                ensure_ascii=False, sort_keys=True, default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        target_id = f"show-list-{fingerprint[:32]}"
        event_id = f'{identity["event_id"]}:show-list:{fingerprint[:24]}'
        if len(event_id) > 200:
            event_id = event_id[:158] + ":" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:40]
        persisted_result = {
            key: value for key, value in result.items()
            if str(key) not in {
                "snapshot_id", "snapshot_revision", "target_id", "snapshot_source",
            }
        }
        try:
            snapshot = self._recognition_snapshots.create(
                tenant_id=identity["tenant_id"], shop_id=identity["shop_id"],
                buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
                event_id=event_id, target_id=target_id,
                recognition=recognition,
                raw_results=persisted_result,
                final_results=persisted_result,
                raw_response=persisted_result,
                trace_id=str(identity.get("event_id") or "") or None,
            )
        except Exception:
            LOGGER.exception("event=show_list_snapshot_create_failed event_id=%s", identity.get("event_id"))
            return result
        return {
            **dict(result),
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_revision": snapshot.revision,
            "target_id": snapshot.target_id,
            "snapshot_source": "show.list",
        }

    def _observe_agent_tool_result(
        self,
        name: str,
        _arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        identity: Mapping[str, str],
    ) -> Mapping[str, Any] | None:
        if str(name).strip() != "show.list" or result.get("ok") is False:
            return None
        candidates = _extract_show_candidates(result)
        if candidates:
            if self._recognition_snapshots is not None:
                return self._record_show_list_snapshot(identity, _arguments, result)
            else:
                self._remember_pending_shows(identity, candidates)
        return None

    @property
    def mode(self) -> str:
        return self._mode

    async def aclose(self) -> None:
        if self._owned_loader is not None:
            await self._owned_loader.aclose()

    def _paid_liangpiao_action(
        self, identity: Mapping[str, str], envelope: Mapping[str, Any], order: object,
    ) -> dict[str, object] | None:
        if not isinstance(order, Mapping) or _authoritative_order_state(order) != "paid":
            return None
        record = self._find_quote_record(identity, envelope, order, confirmed=True)
        if not isinstance(record, Mapping) or record.get("quote_route") != "liangpiao_exact":
            return None
        selected_offer = record.get("selected_offer")
        offers = record.get("offers")
        if isinstance(offers, list) and len(offers) > 1 and not isinstance(selected_offer, Mapping):
            return None
        offer = selected_offer if isinstance(selected_offer, Mapping) else record
        quote_id = str(offer.get("quote_id") or record.get("provider_quote_id") or "").strip()
        quote_hash = str(offer.get("quote_hash") or record.get("provider_quote_hash") or "").strip()
        confirmation_id = str(record.get("confirmation_id") or record.get("confirmation_event_id") or "").strip()
        generation = offer.get("generation") or record.get("quote_generation")
        count = record.get("confirmed_ticket_count") or offer.get("ticket_count") or record.get("ticket_count")
        if (
            not quote_id or len(quote_hash) != 64 or not confirmation_id
            or not isinstance(generation, int) or generation < 1
            or not isinstance(count, int) or not 1 <= count <= 20
            or not self._liangpiao_order_phone
        ):
            return None
        target = offer.get("total_quote_cents")
        if not isinstance(target, int) or target <= 0:
            unit = offer.get("unit_quote_cents")
            target = unit * count if isinstance(unit, int) and unit > 0 else None
        if target is None or _authoritative_order_amount_cents(order) != target:
            return None
        event_id = identity["event_id"]
        return {
            "id": f"{event_id}:create-liangpiao-order",
            "type": "create_liangpiao_order",
            "order_id": identity["order_id"],
            "quote_id": quote_id,
            "quote_hash": quote_hash,
            "confirmation_id": confirmation_id,
            "latest_buyer_message": "付款已确认，按当前有效报价进入出票流程",
            "buyer_phone": self._liangpiao_order_phone,
            "generation": generation,
            "ticket_count": count,
            "buyer_confirmed": True,
            "allow_seat_change": False,
            "shop_id": identity["shop_id"],
            "buyer_id": identity["buyer_id"],
            "chat_id": identity["chat_id"],
            "trace_id": event_id,
            "rule_governed": True,
        }

    async def process_event(self, body: Mapping[str, Any]) -> dict[str, object]:
        envelope = body.get("envelope")
        event_type = _pick(envelope, "event") if isinstance(envelope, Mapping) else None
        supported_events = {
            "im.message.received", "order.created", "order.paid", "order.shipped",
            # Provider callbacks are normally handled by /api/liangpiao/callback,
            # but bridge retries and older plugins can deliver them through the
            # generic event queue. Keep the same state/safety mapping reachable.
            "order.ticketed", "ticket.updated", "order.failed", "order.settled",
            "order.refund.applied", "order.refund.finished", "order.refunded",
            "order.refund_rejected", "order.refund.rejected",
        }
        if self._mode != "auto" or event_type not in supported_events:
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
            require_order=event_type in {
                "order.created", "order.paid", "order.shipped", "order.ticketed", "ticket.updated",
                "order.failed", "order.settled", "order.refund.applied", "order.refund.finished",
                "order.refunded", "order.refund_rejected", "order.refund.rejected",
            } or pending_order_status,
        )
        if identity is None:
            return {"decision": {"mode": "auto", "actions": [], "reason": reason}}
        if self._shops is not None and not self._shops.is_enabled(identity["tenant_id"], identity["shop_id"]):
            return {"decision": {"mode": "auto", "actions": [], "reason": "shop_automation_disabled"}}
        automation_mode = "hybrid"
        if self._automation_mode_provider is not None:
            try:
                provider = self._automation_mode_provider
                parameters = inspect.signature(provider).parameters
                accepts_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                keyword_names = {
                    name for name, parameter in parameters.items()
                    if parameter.kind in {
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    }
                }
                if accepts_kwargs:
                    raw_mode = provider(**identity)
                elif keyword_names and keyword_names.issubset(identity):
                    raw_mode = provider(**{
                        name: identity[name] for name in keyword_names
                    })
                else:
                    raw_mode = provider(identity)
                candidate_mode = str(raw_mode or "hybrid").strip().lower()
                if candidate_mode in {"rules", "hybrid", "agent", "full"}:
                    automation_mode = candidate_mode
            except Exception:
                automation_mode = "hybrid"
        human_takeover_active = False
        if policy is not None:
            event_time = _event_time_ms(envelope)
            human_time = _latest_human_seller_time_ms(messages)
            delay_seconds = int(getattr(policy, "human_takeover_delay_seconds", 20))
            human_takeover_active = bool(
                event_time is not None and human_time is not None
                and 0 <= event_time - human_time < delay_seconds * 1000
            )
        if (
            event_type == "im.message.received"
            and self._new_agent_harness_enabled
            and self._agent_harness is not None
            and not human_takeover_active
            and not stage_suppresses_generic_ai
            and generic_ai_reply_enabled
            and not pending_order_status
        ):
            return await self._reply_with_new_harness_message(envelope, identity, messages)
        if event_type == "im.message.received":
            return await self._reply_to_message(
                envelope, identity, messages, body.get("order"),
                suppress_generic_ai=human_takeover_active or stage_suppresses_generic_ai,
                generic_ai_reply_enabled=generic_ai_reply_enabled and automation_mode != "rules",
                automation_mode=automation_mode,
            )
        if event_type not in {"order.created", "order.paid", "order.shipped"}:
            return _liangpiao_event_decision(
                identity, event_type, body, messages, self._templates(),
            )
        if event_type in {"order.paid", "order.shipped"}:
            order_state = _authoritative_order_state(body.get("order"))
            if event_type == "order.paid":
                paid_action = self._paid_liangpiao_action(identity, envelope, body.get("order"))
                if paid_action is not None:
                    return {"decision": {
                        "mode": "auto", "actions": [paid_action],
                        "reason": "paid_liangpiao_fulfillment_ready",
                        "reply_route": "rule", "ai_called": False,
                        "order_state": order_state,
                    }}
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

    async def _reply_with_new_harness_message(
        self,
        envelope: Mapping[str, Any],
        identity: Mapping[str, str],
        messages: list[object],
    ) -> dict[str, object]:
        current = _latest_inbound_message(messages, envelope)
        if current is None:
            return {"decision": {"mode": "auto", "actions": [], "reason": "current_buyer_message_unavailable"}}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        content = _text(current.get("content", current.get("text"))) or _text(payload.get("content", payload.get("text"))) or ""
        urls = current.get("imageUrls", current.get("image_urls")) or payload.get("imageUrls", payload.get("image_urls"))
        conversation_id = f'{identity["tenant_id"]}:{identity["shop_id"]}:{identity["chat_id"]}'
        synchronize = getattr(self._chat, "sync_platform_history", None)
        if callable(synchronize):
            try:
                synchronize(
                    conversation_id, messages,
                    current_message_id=_pick(current, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id"),
                    reference_time_ms=_event_time_ms(envelope),
                )
            except Exception:
                LOGGER.warning("event=new_agent_harness_history_sync_failed", exc_info=True)
                return {"decision": {"mode": "auto", "actions": [], "reason": "conversation_history_sync_failed"}}
        runtime_context = {
            "tenant_id": identity["tenant_id"], "shop_id": identity["shop_id"],
            "buyer_id": identity["buyer_id"], "chat_id": identity["chat_id"],
            "event_id": identity["event_id"],
            "current_event": {
                "event_id": identity["event_id"],
                "message_id": _pick(current, "messageId", "message_id", "remoteMessageId", "remote_message_id", "id"),
                "content": content, "image_urls": list(urls) if isinstance(urls, list) else [],
            },
        }
        try:
            reply = await self._chat.reply(content or "请根据这张截图查询价格", conversation_id, runtime_context=runtime_context)
        except Exception:
            LOGGER.exception("event=new_agent_harness_reply_failed")
            reply = "这个场次暂时没有查到可用价格，我需要人工确认。"
        agent_result = runtime_context.get("agent_result")
        if isinstance(agent_result, Mapping) and str(agent_result.get("reason") or "") in {
            "cancelled_stale", "cancelled_human_reply",
        }:
            return {"decision": {
                "mode": "auto", "actions": [],
                "reason": str(agent_result.get("reason")),
                "reply_route": "agent",
                "agent_result": agent_result,
            }}
        if not str(reply or "").strip():
            reply = "这个场次暂时没有查到可用价格，我需要人工确认。"
        return {"decision": {
            "mode": "auto", "actions": [{
                "id": f'{identity["event_id"]}:agent-harness-reply',
                "type": "send_message", "text": str(reply).strip(),
                "preserve_on_new_buyer_message": True,
                "rule_governed": True, "agent_harness": True,
            }],
            "reason": "new_agent_harness_reply_ready", "reply_route": "agent",
            "agent_result": runtime_context.get("agent_result"),
        }}

    def _record_quote(
        self,
        identity: Mapping[str, str],
        envelope: Mapping[str, Any],
        recognition: MovieImageInfo,
        quote: RealQuote | None,
        *,
        source: str,
        quote_error: str | None = None,
        record_id: str | None = None,
    ) -> None:
        if self._quote_recorder is None or (quote is None and not quote_error):
            return
        event_time = _event_time_ms(envelope)
        created_at = datetime.fromtimestamp(event_time / 1000, tz=timezone.utc).isoformat() if event_time else datetime.now(timezone.utc).isoformat()
        record = {
            "record_id": record_id or identity["event_id"],
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
            "delivery_state": "pending" if quote is not None else "not_applicable",
            "city": (quote.matched_city_name if quote else None) or recognition.city,
            "cinema": (quote.matched_cinema_name if quote else None) or recognition.cinema_name,
            "cinema_id": recognition.cinema_id,
            "brand_name": recognition.brand_name,
            "quote_route": (
                "liangpiao_exact" if quote and quote.price_source == "liangpiao_realtime_preflight"
                else "wanda_self" if quote else None
            ),
            "movie": recognition.movie_name,
            "show_id": recognition.show_id,
            "selected_seats": [
                {
                    "row_no": seat.row_no, "col_no": seat.col_no,
                    "seat_no": seat.seat_number, "area_id": seat.area_id,
                }
                for seat in recognition.selected_seats
                if seat.row_no is not None and seat.col_no is not None
            ],
            "ticket_mode": "STANDARD",
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
            record["quote_expires_at"] = (
                datetime.fromisoformat(created_at) + timedelta(seconds=900)
            ).isoformat()
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
                "price_mode": quote.price_mode,
                "max_price_cents": quote.max_price_cents,
                "unit_quote_cents": quote.unit_quote_cents,
                "total_quote_cents": quote.total_quote_cents,
                "pricing_rule_version": quote.pricing_rule_version,
                "provider_quote_id": quote.provider_quote_id,
                "provider_quote_hash": quote.provider_quote_hash,
                "quote_generation": quote.quote_generation,
                "offers": [{
                    "offer_id": "primary",
                    "route": (
                        "liangpiao" if quote.price_source == "liangpiao_realtime_preflight"
                        else "wanda"
                    ),
                    "price_mode": quote.price_mode,
                    "ticket_mode": "STANDARD",
                    "quote_scope": quote.quote_scope,
                    "unit_quote_cents": quote.unit_quote_cents,
                    "total_quote_cents": quote.total_quote_cents,
                    "ticket_count": quote.ticket_count,
                    "quote_expires_at": record.get("quote_expires_at"),
                    "quote_id": record.get("provider_quote_id") or record.get("record_id"),
                    "quote_hash": record.get("provider_quote_hash"),
                }],
                "selected_offer_id": None,
                "selected_offer": None,
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
        selected_offer = record.get("selected_offer")
        offers = record.get("offers")
        if isinstance(offers, list) and len(offers) > 1 and not isinstance(selected_offer, Mapping):
            return None
        offer = selected_offer if isinstance(selected_offer, Mapping) else record
        target: int | None = None
        if scope == "exact_seats":
            candidate = offer.get("total_quote_cents")
            if isinstance(candidate, int) and candidate > 0 and str(record.get("seat_display") or "") != "W+座位":
                target = candidate
        elif scope in {"area_probe", "area_preview"} and zone == "W+":
            unit = offer.get("unit_quote_cents")
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
        quote_snapshot = {
            "quote_record_id": record_id,
            "confirmation_version": confirmation_version,
            "confirmation_source": str(record.get("confirmation_source") or "buyer_message"),
            "confirmation_event_id": str(record.get("confirmation_event_id") or ""),
            "confirmed_ticket_count": count,
            "target_amount_cents": target,
            "selected_offer_id": str(record.get("selected_offer_id") or "primary"),
        }
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
            }], "reason": "bound_order_amount_already_matches_quote",
                "quote_snapshot": quote_snapshot}}
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
            "selected_offer_id": str(record.get("selected_offer_id") or "primary"),
        }
        observed_order_amount = _authoritative_order_amount_cents(order)
        if observed_order_amount is not None:
            snapshot["observed_order_amount_cents"] = observed_order_amount
        return {"decision": {"mode": "auto", "actions": [{
            "id": f'{identity["event_id"]}:change-order-price',
            "type": "change_order_price", "quote_snapshot": snapshot,
        }], "reason": "confirmed_quote_record_bound_to_order"}}

    def _current_wplus_marker_snapshot(
        self, identity: Mapping[str, str],
        envelope: Mapping[str, Any] | None = None,
        order: object = None,
    ) -> MovieImageInfo | None:
        if self._recognition_snapshots is None:
            return None
        getter = getattr(self._recognition_snapshots, "get_current", None)
        if not callable(getter):
            return None
        try:
            snapshot = getter(
                tenant_id=identity["tenant_id"], shop_id=identity["shop_id"],
                buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
                target_id="image-target-1",
            )
        except Exception:
            return None
        if snapshot is None:
            return None
        if is_wplus_unselected_image(snapshot.normalized):
            return snapshot.normalized
        if envelope is not None and self._quote_finder is not None:
            try:
                record = self._find_quote_record(identity, envelope, order)
                quote = _real_quote_from_record(record) if isinstance(record, Mapping) else None
            except Exception:
                quote = None
            if (
                quote is not None
                and quote.quote_scope != "exact_seats"
                and _is_wplus_quote(quote)
                and snapshot.normalized.is_seat_selection is True
            ):
                return snapshot.normalized
        return None

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
            recognition, quote = await self._quote_for_recognition(recognition, identity)
            # A cinema choice does not itself declare a quantity.  Keep the
            # quote in its authoritative state instead of inheriting an
            # unrelated count from old chat text.
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
        identity: Mapping[str, str] | None = None,
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
            # Stable per-event idempotency prevents a worker retry from
            # creating a second billable async recognition task.
            out_trade_no = None
            if identity:
                out_trade_no = "rec-" + hashlib.sha256(
                    f'{identity["tenant_id"]}:{identity["shop_id"]}:{identity["buyer_id"]}:{identity["chat_id"]}:{identity["event_id"]}'.encode()
                ).hexdigest()[:60]
            if out_trade_no:
                try:
                    context = "；".join(cinema_hints)
                    parsed_context = next(
                        (_structured_ticket_request(hint) for hint in cinema_hints
                         if _structured_ticket_request(hint) is not None),
                        None,
                    )
                    kwargs: dict[str, object] = {"out_trade_no": out_trade_no}
                    if parsed_context is not None and parsed_context.city:
                        kwargs["city_name"] = parsed_context.city
                    if context:
                        kwargs["buyer_message"] = context
                    try:
                        recognition = await recognize_from_url(image_url, **kwargs)
                    except TypeError as error:
                        # Older adapters only accept out_trade_no/city_name;
                        # retain compatibility with those implementations.
                        if "buyer_message" not in str(error):
                            raise
                        kwargs.pop("buyer_message", None)
                        recognition = await recognize_from_url(image_url, **kwargs)
                except TypeError as error:
                    if "out_trade_no" not in str(error):
                        raise
                    recognition = await recognize_from_url(image_url)
            else:
                context = "；".join(cinema_hints)
                kwargs = {"buyer_message": context} if context else {}
                try:
                    recognition = await recognize_from_url(image_url, **kwargs)
                except TypeError as error:
                    if "buyer_message" not in str(error):
                        raise
                    recognition = await recognize_from_url(image_url)
        else:
            image, content_type = await self._load_image(image_url)
            recognition = await self._recognition.recognize(
                image, content_type,
                buyer_message=("；".join(cinema_hints) or verification_instruction),
            )
        recognition = _merge_buyer_ticket_context(recognition, cinema_hints)
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
            if len(recognition.candidate_cinemas) == 1:
                # A single official candidate is already unambiguous. Confirm
                # it through Liangpiao before quoting instead of making the
                # buyer perform a meaningless “1” selection. Multiple
                # candidates still require an explicit buyer choice.
                confirm = getattr(self._recognition, "confirm_recognition_candidate", None)
                candidate = recognition.candidate_cinemas[0]
                if callable(confirm) and recognition.recognition_id:
                    try:
                        confirmed = await confirm(recognition.recognition_id, candidate.cinema_id)
                    except RecognitionError as error:
                        return recognition, None, error.message
                    selected_candidate_confirmed = bool(
                        len(confirmed.candidate_cinemas) == 1
                        and confirmed.candidate_cinemas[0].cinema_id == candidate.cinema_id
                    )
                    if confirmed.match_level == "EXACT" or selected_candidate_confirmed:
                        recognition = confirmed.model_copy(update={
                            "candidate_cinemas": [],
                        })
                    else:
                        return recognition, None, "良票影院确认结果仍不唯一，请重新发送截图。"
                else:
                    return recognition, None, "良票影院确认接口暂时不可用，请重新发送截图。"
            else:
                # Multiple official candidates are not an authoritative venue.
                # Send the numbered choices first; pricing starts only after
                # the buyer selects one.
                return recognition, None, None
        try:
            recognition, quote = await self._quote_for_recognition(recognition, identity)
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
                    image_url, identity=identity, cinema_hints=cinema_hints, _verification_retry=True,
                )
            return recognition, None, error.message
        except Exception:
            return recognition, None, "万达实时报价暂时不可用，请稍后重试。"

    async def _fixed_switch_decision(
        self, identity: Mapping[str, str], envelope: Mapping[str, Any], message: str,
    ) -> dict[str, object] | None:
        """Handle the durable, explicit LIMIT -> FIXED recovery handshake."""
        getter = getattr(self._transaction_states, "get", None)
        if not callable(getter):
            return None
        state = getter(**{key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")})
        status = str(getattr(state, "fixed_switch_status", "none") or "none") if state else "none"
        if status not in {"pending", "confirmed"}:
            return None
        event_id = identity["event_id"]
        expiry_raw = str(getattr(state, "fixed_switch_expires_at", "") or "").strip()
        try:
            expiry = datetime.fromisoformat(expiry_raw) if expiry_raw else None
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except ValueError:
            expiry = None
        if expiry is None or expiry <= datetime.now(timezone.utc):
            if _fixed_switch_rejection(message) or _fixed_switch_consent(message) or _fixed_switch_price_confirmation(message):
                return {"decision": {"mode": "auto", "actions": [], "reason": "fixed_switch_expired"}}
            return None
        if status == "pending":
            if _fixed_switch_rejection(message):
                return {"decision": {"mode": "auto", "actions": [{
                    "id": f"{event_id}:fixed-switch-rejected", "type": "send_message",
                    "text": "好的，本次不切换一口价，原订单按出票失败结果处理。",
                    "rule_governed": True, "preserve_on_new_buyer_message": True,
                }], "reason": "fixed_switch_rejected"}}
            if not _fixed_switch_consent(message):
                return None
            if str(getattr(state, "fixed_switch_source_order_status", "none") or "none") != "closed":
                return {"decision": {"mode": "auto", "actions": [{
                    "id": f"{event_id}:fixed-switch-wait-source-close", "type": "send_message",
                    "text": "原订单正在按出票失败结果退款并关闭，完成后我再为您生成一口价新订单报价。",
                    "rule_governed": True, "preserve_on_new_buyer_message": True,
                }], "reason": "fixed_switch_source_order_not_closed"}}
            if not all(callable(item) for item in (self._liangpiao_order_finder, self._liangpiao_quote_finder, self._liangpiao_fixed_quote_creator)):
                return {"decision": {"mode": "auto", "actions": [], "reason": "fixed_switch_unavailable"}}
            source_order_no = str(getattr(state, "fixed_switch_source_order_no", "") or "").strip()
            order = self._liangpiao_order_finder(out_order_no=source_order_no, tenant_id=identity["tenant_id"])
            source_quote_id = str((order or {}).get("quote_id") or "").strip() if isinstance(order, Mapping) else ""
            source_quote = self._liangpiao_quote_finder(source_quote_id) if source_quote_id else None
            if not isinstance(source_quote, Mapping):
                return {"decision": {"mode": "auto", "actions": [], "reason": "fixed_switch_source_quote_missing"}}
            snapshot = source_quote.get("snapshot") if isinstance(source_quote.get("snapshot"), Mapping) else source_quote
            seats = source_quote.get("seats") if isinstance(source_quote.get("seats"), list) else snapshot.get("seats")
            if not isinstance(seats, list) or not seats:
                return {"decision": {"mode": "auto", "actions": [], "reason": "fixed_switch_source_seats_missing"}}
            payload = {
                "tenant_id": identity["tenant_id"], "conversation_id": f'{identity["tenant_id"]}:{identity["shop_id"]}:{identity["chat_id"]}',
                "cinema_id": snapshot.get("cinema_id"), "show_id": snapshot.get("show_id"),
                "cinema_name": snapshot.get("cinema_name"), "movie_name": snapshot.get("movie_name"),
                "show_date": snapshot.get("show_date") or snapshot.get("date"), "showtime_start": snapshot.get("showtime_start"),
                "hall_name": snapshot.get("hall_name"), "seats": seats, "ticket_mode": snapshot.get("ticket_mode", "STANDARD"),
                "price_mode": "FIXED", "generation": int(snapshot.get("generation") or getattr(state, "generation", 1)), "trace_id": event_id,
            }
            try:
                fixed_quote = await self._liangpiao_fixed_quote_creator(payload)
            except Exception:
                LOGGER.exception("event=fixed_switch_quote_failed event_id=%s", event_id)
                return {"decision": {"mode": "auto", "actions": [], "reason": "fixed_switch_quote_failed"}}
            if not isinstance(fixed_quote, Mapping):
                return {"decision": {"mode": "auto", "actions": [], "reason": "fixed_switch_quote_invalid"}}
            amount_fen = int(fixed_quote.get("buyer_amount_fen") or 0)
            quote_id = str(fixed_quote.get("quote_id") or "").strip()
            quote_hash = str(fixed_quote.get("quote_hash") or "").strip()
            if amount_fen <= 0 or not quote_id or len(quote_hash) != 64:
                return {"decision": {"mode": "auto", "actions": [], "reason": "fixed_switch_quote_invalid"}}
            text = f"特惠渠道出票失败了，可以按同场次一口价继续出票：一口价{amount_fen / 100:.2f}元，共{len(seats)}张。报价有效期30分钟，确认后再继续出票，可以吗？"
            return {"decision": {"mode": "auto", "actions": [{
                "id": f"{event_id}:fixed-switch-quote", "type": "send_message", "text": text,
                "rule_governed": True, "preserve_on_new_buyer_message": True,
                "fixed_switch_quote": {"quote_id": quote_id, "quote_hash": quote_hash, "generation": int(fixed_quote.get("generation") or payload["generation"]), "ticket_count": len(seats), "amount_fen": amount_fen},
            }], "reason": "fixed_switch_quote_ready"}}
        if str(getattr(state, "fixed_switch_quote_confirmation_status", "none")) == "pending":
            count = _declared_ticket_count(message)
            if count is None or not _fixed_switch_price_confirmation(message):
                return None
            quoted_count = getattr(state, "confirmed_ticket_count", None)
            if isinstance(quoted_count, int) and count != quoted_count:
                return {"decision": {"mode": "auto", "actions": [{
                    "id": f"{event_id}:fixed-switch-count-mismatch", "type": "send_message",
                    "text": f"当前一口价报价对应{quoted_count}张，请按报价票数确认；如需更改票数，请重新发送选座信息。",
                    "rule_governed": True, "preserve_on_new_buyer_message": True,
                }], "reason": "fixed_switch_ticket_count_mismatch"}}
            quote_id = str(getattr(state, "fixed_switch_quote_id", "") or "").strip()
            quote_hash = str(getattr(state, "fixed_switch_quote_hash", "") or "").strip()
            if not quote_id or len(quote_hash) != 64:
                return {"decision": {"mode": "auto", "actions": [], "reason": "fixed_switch_quote_missing"}}
            action = {
                "id": f"{event_id}:fixed-switch-new-order", "type": "send_message",
                "text": "一口价报价已确认，请重新拍下一个新订单；新订单生成后系统会按本次一口价自动改价，付款成功后再继续出票。",
                "rule_governed": True, "preserve_on_new_buyer_message": True,
            }
            return {"decision": {"mode": "auto", "actions": [action], "reason": "fixed_switch_price_confirmed"}}
        return None

    def _fixed_switch_order_created_decision(
        self, identity: Mapping[str, str], order: object,
    ) -> dict[str, object] | None:
        """Bind a confirmed FIXED recovery quote only to a fresh platform order."""
        getter = getattr(self._transaction_states, "get", None)
        if not callable(getter) or not isinstance(order, Mapping):
            return None
        state = getter(**{key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")})
        if state is None:
            return None
        if (
            str(getattr(state, "fixed_switch_status", "none") or "none") != "confirmed"
            or str(getattr(state, "fixed_switch_source_order_status", "none") or "none") != "closed"
            or str(getattr(state, "fixed_switch_quote_confirmation_status", "none") or "none") != "confirmed"
        ):
            return None
        expiry_raw = str(getattr(state, "fixed_switch_expires_at", "") or "").strip()
        try:
            expiry = datetime.fromisoformat(expiry_raw) if expiry_raw else None
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except ValueError:
            expiry = None
        if expiry is None or expiry <= datetime.now(timezone.utc):
            return {"decision": {"mode": "auto", "actions": [{
                "id": f'{identity["event_id"]}:fixed-switch-expired', "type": "send_message",
                "text": "本次一口价报价已过期，请重新确认是否更换一口价渠道。",
                "rule_governed": True, "preserve_on_new_buyer_message": True,
            }], "reason": "fixed_switch_expired"}}
        order_id = str(identity.get("order_id") or "").strip()
        source_order_id = str(getattr(state, "fixed_switch_source_platform_order_id", "") or "").strip()
        replacement_order_id = str(getattr(state, "fixed_switch_replacement_order_id", "") or "").strip()
        if not order_id or order_id == source_order_id or (replacement_order_id and replacement_order_id != order_id):
            return None
        quote_id = str(getattr(state, "fixed_switch_quote_id", "") or "").strip()
        quote_hash = str(getattr(state, "fixed_switch_quote_hash", "") or "").strip()
        generation = getattr(state, "fixed_switch_quote_generation", None)
        ticket_count = getattr(state, "confirmed_ticket_count", None)
        target = getattr(state, "target_amount_cents", None)
        if (
            not quote_id or len(quote_hash) != 64
            or not isinstance(generation, int) or generation < 1
            or not isinstance(ticket_count, int) or not 1 <= ticket_count <= 20
            or not isinstance(target, int) or target <= 0 or target > 200_000
        ):
            return None
        event_id = identity["event_id"]
        confirmation_version = str(
            getattr(state, "fixed_switch_confirmation_event_id", "") or f"fixed:{quote_id}"
        )
        snapshot = {
            "fixed_switch": True,
            "quote_record_id": f"fixed:{quote_id}",
            "confirmation_version": confirmation_version,
            "confirmation_source": "buyer_message",
            "confirmation_event_id": confirmation_version,
            "confirmed_ticket_count": ticket_count,
            "order_id": order_id,
            "tenant_id": identity["tenant_id"],
            "shop_id": identity["shop_id"],
            "buyer_id": identity["buyer_id"],
            "chat_id": identity["chat_id"],
            "target_amount_cents": target,
            "provider_quote_id": quote_id,
            "provider_quote_hash": quote_hash,
            "quote_generation": generation,
        }
        if _authoritative_order_amount_cents(order) == target:
            amount = Decimal(target) / Decimal(100)
            text = render_template(
                self._templates().price_change_confirmation_template,
                {"订单金额": f"{amount:.2f}元"},
            )
            return {"decision": {
                "mode": "auto", "actions": [{
                    "id": f"{event_id}:fixed-switch-order-amount-confirmation",
                    "type": "send_message", "order_id": order_id, "text": text,
                    "rule_governed": True,
                }],
                "reason": "fixed_switch_order_amount_already_matches",
                "quote_snapshot": snapshot,
            }}
        observed = _authoritative_order_amount_cents(order)
        if observed is not None:
            snapshot["observed_order_amount_cents"] = observed
        return {"decision": {
            "mode": "auto", "actions": [{
                "id": f"{event_id}:change-order-price", "type": "change_order_price",
                "quote_snapshot": snapshot,
            }],
            "reason": "fixed_switch_quote_bound_to_new_order",
        }}

    async def _agent_led_image_reply(
        self,
        envelope: Mapping[str, Any],
        identity: Mapping[str, str],
        messages: list[object],
        order: object,
        urls: list[str],
        *,
        generic_ai_reply_enabled: bool,
    ) -> dict[str, object]:
        """Let the Agent request recognition and quotes instead of pre-routing them.

        The tools remain authoritative and read-only. The Agent owns the
        sequence and natural-language turn; only the W+ customer format is
        rendered from the returned quote so it cannot invent or reshape price
        facts.
        """
        try:
            validated_urls = [validate_image_url(url) for url in urls]
            if not validated_urls or len(validated_urls) > 3:
                raise ValueError("image_count_invalid")
        except Exception:
            return {"decision": {
                "mode": "auto", "actions": [],
                "reason": "image_validation_failed", "reply_route": "rule",
                "ai_called": False,
            }}
        conversation_id = f'{identity["tenant_id"]}:{identity["shop_id"]}:{identity["chat_id"]}'
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        buyer_message = str(payload.get("content") or payload.get("text") or "").strip()
        human_image_urls = _human_seller_image_urls(messages)
        latest_recognition: MovieImageInfo | None = None
        latest_quote: RealQuote | None = None
        latest_quote_arguments: dict[str, Any] | None = None
        latest_quote_error: str | None = None
        quote_attempted = False

        async def execute_request_tool(
            name: str, arguments: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            nonlocal latest_recognition, latest_quote, latest_quote_arguments
            nonlocal latest_quote_error, quote_attempted
            tool_name = str(name).strip()
            if tool_name in {
                "quote.preflight_current", "get_quote", "get_authoritative_quote",
                "reprice_seats", "recognition.resolve_seats",
            }:
                quote_attempted = True
            tool_arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
            if (
                tool_name in {
                    "quote.preflight_current", "get_quote", "get_authoritative_quote",
                    "reprice_seats", "recognition.resolve_seats",
                }
                and latest_quote_arguments is not None
            ):
                # The recognition tool returns the current snapshot references.
                # If the model omits them, fill only the missing fields from the
                # same turn instead of failing before the authoritative quote.
                for field in ("snapshot_id", "snapshot_revision", "target_id"):
                    if tool_arguments.get(field) in (None, ""):
                        tool_arguments[field] = latest_quote_arguments.get(field)
            if tool_name == "recognize_screenshot" and not (
                tool_arguments.get("image_url") or tool_arguments.get("image_urls")
            ):
                tool_arguments["image_urls"] = validated_urls
            result = await self._execute_agent_tool(
                tool_name, tool_arguments, identity, order, envelope=envelope,
            )
            requested_image_urls = []
            if tool_arguments.get("image_url"):
                requested_image_urls.append(str(tool_arguments["image_url"]).strip())
            if isinstance(tool_arguments.get("image_urls"), list):
                requested_image_urls.extend(
                    str(value or "").strip()
                    for value in tool_arguments["image_urls"]
                    if str(value or "").strip()
                )
            context_only = bool(
                tool_name == "recognize_screenshot"
                and requested_image_urls
                and set(requested_image_urls).issubset(set(human_image_urls))
                and not set(requested_image_urls).intersection(validated_urls)
            )
            if context_only:
                result = {**result, "context_only": True, "source": "human_seller_image"}
            if tool_name in {
                "quote.preflight_current", "get_quote", "get_authoritative_quote",
                "reprice_seats", "recognition.resolve_seats",
            } and result.get("ok") is False:
                latest_quote_error = str(result.get("error") or "") or None
            if result.get("ok") is not False:
                if tool_name == "recognize_screenshot" and result.get("context_only") is not True:
                    for image_url in validated_urls:
                        attempt_key = hashlib.sha256(
                            f'{identity["event_id"]}\x1f{image_url}'.encode("utf-8")
                        ).hexdigest()
                        cached_recognition = self._agent_image_results.get(attempt_key)
                        if cached_recognition is not None:
                            latest_recognition = cached_recognition
                    raw_targets = result.get("quote_targets")
                    if isinstance(raw_targets, list) and len(raw_targets) == 1:
                        target = raw_targets[0]
                        target_recognition = target.get("recognition")
                        if isinstance(target_recognition, Mapping):
                            latest_quote_arguments = dict(target_recognition)
                            for reference_field in (
                                "snapshot_id", "snapshot_revision", "target_id",
                            ):
                                if target.get(reference_field) is not None:
                                    latest_quote_arguments[reference_field] = target[reference_field]
                recognition_value = result.get("recognition")
                if isinstance(recognition_value, Mapping):
                    try:
                        latest_recognition = MovieImageInfo.model_validate(recognition_value)
                    except Exception:
                        pass
                quote_value = result.get("quote")
                if isinstance(quote_value, Mapping):
                    try:
                        latest_quote = RealQuote.model_validate(quote_value)
                        latest_quote_error = None
                    except Exception:
                        pass
            observer_result = self._observe_agent_tool_result(
                tool_name, tool_arguments, result, identity,
            )
            if observer_result is not None:
                result = observer_result
            return result

        def observe_agent_tool_result(
            name: str, _arguments: Mapping[str, Any], result: Mapping[str, Any],
        ) -> None:
            nonlocal latest_recognition, latest_quote
            if result.get("ok") is False:
                return
            recognition_value = result.get("recognition")
            if isinstance(recognition_value, Mapping):
                try:
                    latest_recognition = MovieImageInfo.model_validate(recognition_value)
                except Exception:
                    pass
            quote_value = result.get("quote")
            if isinstance(quote_value, Mapping):
                try:
                    latest_quote = RealQuote.model_validate(quote_value)
                except Exception:
                    pass

        runtime_context = {
            "agent_led_image_workflow": True,
            "tenant_id": identity["tenant_id"], "shop_id": identity["shop_id"],
            "buyer_id": identity["buyer_id"], "chat_id": identity["chat_id"],
            "event_id": identity["event_id"],
            "buyer_message": buyer_message,
            "pending_image_url": validated_urls[0],
            "pending_image_urls": validated_urls,
            "human_seller_image_urls": human_image_urls,
            "pending_image_recognition": None,
            "image_quote_targets": [],
            "pending_image_conflicts": [],
            "current_stage": _business_stage("im.message.received", order),
            "order_status": _authoritative_order_state(order),
            "order_id": _pick(order, "orderId", "order_id"),
            "_agent_tool_executor": execute_request_tool,
            "_agent_tool_result_observer": observe_agent_tool_result,
            "allowed_query_tools": [
                str(item.get("function", {}).get("name"))
                for item in (getattr(self._chat, "_tool_schemas", []) or [])
                if isinstance(item, Mapping)
            ],
        }
        image_prompt = (
            "买家发送了图片，请先用工具识别，再按权威工具结果主导本轮回复。"
            + (f"买家本轮原话：{buyer_message}" if buyer_message else "")
        )
        try:
            reply_method = self._chat.reply
            parameters = inspect.signature(reply_method).parameters
            if "runtime_context" in parameters:
                reply = await reply_method(
                    image_prompt, conversation_id, runtime_context=runtime_context,
                )
            else:
                reply = await reply_method(image_prompt, conversation_id)
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("agent_image_reply_empty")
        except Exception:
            # A provider outage must not acknowledge the event with an empty
            # action list. This deterministic reply contains no price, seat,
            # payment, or fulfillment claim and keeps the conversation alive.
            return {"decision": {
                "mode": "auto", "actions": [{
                    "id": f'{identity["event_id"]}:agent-unavailable',
                    "type": "send_message",
                    "text": "图片已收到，但客服系统暂时不可用，暂时无法完成核价，请稍后再发消息。",
                    "rule_governed": True,
                    "preserve_on_new_buyer_message": True,
                }],
                "reason": "agent_led_image_reply_unavailable",
                "reply_route": "agent", "ai_called": True,
            }}

        quote_required = bool(
            latest_recognition is not None
            and latest_recognition.is_seat_selection is True
            and _recognition_ready_for_preflight(latest_recognition)
        )
        # A model may stop after recognition even though a seat-selection turn
        # requires a quote. Enforce the mandatory read-only quote step before
        # accepting its prose; the model still chose and consumed recognition,
        # while authoritative price facts remain deterministic and fail closed.
        if quote_required and latest_quote is None and not quote_attempted:
            quote_result = await execute_request_tool(
                "get_authoritative_quote",
                latest_quote_arguments or latest_recognition.model_dump(mode="json"),
            )
            if quote_result.get("ok") is False:
                latest_quote_error = str(quote_result.get("error") or "") or None

        if (
            latest_recognition is not None
            and latest_quote is not None
            and _requires_wplus_marker(latest_recognition, latest_quote)
        ):
            return {"decision": {
                "mode": "auto",
                "actions": _wplus_quote_actions(
                    identity, latest_recognition, latest_quote, self._templates(),
                ),
                "reason": "agent_led_wplus_quote_ready",
                "reply_route": "agent", "ai_called": True,
                "order_state": _authoritative_order_state(order),
                "buyer_intent": "REQUEST_QUOTE",
                "expected_action": "ASK_SEAT_MARK",
                "selection_mode": "WPLUS_MARKED",
            }}
        if (
            latest_recognition is not None
            and latest_recognition.is_seat_selection is True
            and latest_quote is not None
            and latest_quote.quote_scope == "exact_seats"
            and _exact_quote_has_amounts(latest_quote)
        ):
            return {"decision": {
                "mode": "auto",
                "actions": _exact_quote_actions(
                    identity, latest_recognition, latest_quote,
                ),
                "reason": "agent_led_exact_quote_ready",
                "reply_route": "agent", "ai_called": True,
                "order_state": _authoritative_order_state(order),
                "buyer_intent": "REQUEST_QUOTE",
                "expected_action": "SUBMIT_ORDER",
            }}
        if quote_required and latest_quote is None and latest_recognition is not None:
            safe_reply = build_recognition_reply(
                latest_recognition,
                quote_error=latest_quote_error or "当前场次暂未取得可核验价格",
                templates=self._templates(),
            )
            return {"decision": {
                "mode": "auto", "actions": [{
                    "id": f'{identity["event_id"]}:quote-unavailable',
                    "type": "send_message", "text": safe_reply,
                    "rule_governed": True,
                    "preserve_on_new_buyer_message": True,
                }],
                "reason": "agent_led_quote_unavailable",
                "reply_route": "agent", "ai_called": True,
                "order_state": _authoritative_order_state(order),
                "expected_action": "QUOTE_FAILED_CLOSED",
            }}
        return {"decision": {
            "mode": "auto", "actions": [{
                "id": f'{identity["event_id"]}:reply',
                "type": "send_message", "text": reply.strip(),
                "rule_governed": False,
                "preserve_on_new_buyer_message": True,
            }],
            "reason": "agent_led_image_reply_ready",
            "reply_route": "agent", "ai_called": True,
            "order_state": _authoritative_order_state(order),
        }}

    async def _classify_wplus_marker_response(
        self,
        identity: Mapping[str, str],
        message: str,
        *,
        previous_image: str | None,
        messages: Sequence[object],
        pending_wplus: MovieImageInfo,
    ) -> str | None:
        """Use AI only as a bounded semantic classifier for the pending W+ lane.

        The model may classify the buyer's natural-language answer, but it
        cannot write, quote, or choose the resulting reply.  Unknown or
        unavailable classifications fail closed to the existing marker
        confirmation question.
        """
        if not self._ai_assist_enabled or self._chat is None:
            return None
        reply = getattr(self._chat, "reply", None)
        if not callable(reply):
            return None
        conversation_id = f'{identity["tenant_id"]}:{identity["shop_id"]}:{identity["chat_id"]}'
        recent_conversation: list[dict[str, object]] = []
        for item in messages[-12:]:
            if not isinstance(item, Mapping):
                continue
            direction = _text(item.get("direction")) or "unknown"
            message_type = str(item.get("messageType", item.get("message_type", "")) or "")
            content = _text(item.get("content", item.get("text"))) or ""
            urls = item.get("imageUrls", item.get("image_urls"))
            recent_conversation.append({
                "direction": direction,
                "message_type": message_type,
                "content": content[:500],
                "has_image": isinstance(urls, list) and bool(urls),
            })
        context = {
            "wplus_marker_intent_classifier": True,
            "_internal_no_persist": True,
            "buyer_message": message,
            "current_stage": "quotation",
            "flow_state": "WPLUS_MARK_CONFIRMATION_PENDING",
            "confirmation_status": "pending",
            "wplus_marker_context": {
                "marker_question_asked": True,
                "previous_buyer_image_present": previous_image is not None,
                "pending_wplus_context": {
                    "city": pending_wplus.city,
                    "cinema_name": pending_wplus.cinema_name,
                    "movie_name": pending_wplus.movie_name,
                    "date": pending_wplus.date or pending_wplus.date_text,
                    "showtime_start": pending_wplus.showtime_start,
                    "hall_name": pending_wplus.hall_name,
                    "seat_selection_type": "W+",
                },
                "recent_conversation": recent_conversation,
            },
        }
        try:
            raw = await reply(message, conversation_id, runtime_context=context)
        except Exception:
            LOGGER.warning(
                "event=wplus_marker_semantic_classifier_unavailable event_id=%s",
                identity.get("event_id"),
            )
            return None
        try:
            parsed = json.loads(str(raw or ""))
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, Mapping):
            return None
        classification = str(parsed.get("classification") or "").strip().lower()
        if not classification and isinstance(parsed.get("message"), str):
            try:
                nested = json.loads(parsed["message"])
            except (TypeError, ValueError):
                nested = None
            if isinstance(nested, Mapping):
                classification = str(nested.get("classification") or "").strip().lower()
        if classification in {"confirmed", "missing", "unclear"}:
            return classification
        return None

    async def _reply_to_message(
        self,
        envelope: Mapping[str, Any],
        identity: Mapping[str, str],
        messages: list[object],
        order: object = None,
        *,
        suppress_generic_ai: bool = False,
        generic_ai_reply_enabled: bool = True,
        automation_mode: str = "hybrid",
    ) -> dict[str, object]:
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        grounded_quote_amounts: set[int] = set()
        image_quote_targets: list[dict[str, Any]] = []
        recognition: MovieImageInfo | None = None
        quote: RealQuote | None = None
        quote_error: str | None = None

        def remember_grounded_quote(result: Mapping[str, Any]) -> None:
            quote_result = result.get("quote")
            if not isinstance(quote_result, Mapping) or result.get("ok") is False:
                return
            for field in ("unit_quote_cents", "total_quote_cents"):
                amount = quote_result.get(field)
                if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
                    grounded_quote_amounts.add(amount)

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
            direct_image_workflow = bool(
                automation_mode == "rules"
                or self._chat is None
                or not generic_ai_reply_enabled
                or suppress_generic_ai
            )
            # Image recognition and authoritative preflight are fixed business
            # steps. Agent-capable modes receive their structured results and
            # compose the buyer-facing reply; they do not decide whether the
            # backend should inspect a newly received ticket screenshot.
            agent_image_workflow = bool(
                automation_mode != "rules"
                and generic_ai_reply_enabled
                and not suppress_generic_ai
                and self._chat is not None
            )
            if agent_image_workflow:
                # W+ image transactions use the deterministic recognition and
                # quote pipeline below even in full mode. Agent availability
                # must not decide whether a buyer receives the required price,
                # marker question, or count follow-up.
                image_quote_targets = []
                fixed_recognition_result: Mapping[str, Any] = {
                    "ok": False, "error": "recognition_not_started",
                }
                try:
                    validated_image_urls = [validate_image_url(url) for url in urls]
                    validated_image_url = validated_image_urls[0]
                    human_image_urls = _human_seller_image_urls(messages)
                    recognition_arguments: dict[str, Any] = {
                        "image_urls": validated_image_urls,
                        "buyer_message": payload_content,
                    }
                    fixed_recognition_result = await self._execute_agent_tool(
                        "recognize_screenshot", recognition_arguments,
                        identity, order, envelope=envelope,
                    )
                    raw_targets = fixed_recognition_result.get("quote_targets")
                    if isinstance(raw_targets, list):
                        for target in raw_targets:
                            if not isinstance(target, Mapping):
                                continue
                            target_recognition = target.get("recognition")
                            quote_result: Mapping[str, Any] = {
                                "ok": False, "error": "quote_not_attempted",
                            }
                            parsed_target: MovieImageInfo | None = None
                            if isinstance(target_recognition, Mapping):
                                try:
                                    parsed_target = MovieImageInfo.model_validate({
                                        key: value for key, value in target_recognition.items()
                                        if key in MovieImageInfo.model_fields
                                    })
                                except Exception:
                                    parsed_target = None
                            if (
                                parsed_target is not None
                                and _recognition_ready_for_preflight(parsed_target)
                                and not target.get("conflict_fields")
                            ):
                                quote_arguments = dict(target_recognition)
                                for reference_field in (
                                    "snapshot_id", "snapshot_revision", "target_id",
                                ):
                                    if target.get(reference_field) is not None:
                                        quote_arguments[reference_field] = target.get(reference_field)
                                quote_result = await self._execute_agent_tool(
                                    "get_authoritative_quote", quote_arguments,
                                    identity, order, envelope=envelope,
                                )
                                remember_grounded_quote(quote_result)
                            image_quote_targets.append({
                                "target_id": target.get("target_id"),
                                "snapshot_id": target.get("snapshot_id"),
                                "snapshot_revision": target.get("snapshot_revision"),
                                "image_indexes": list(target.get("image_indexes") or []),
                                "conflict_fields": list(target.get("conflict_fields") or []),
                                "recognition": target_recognition,
                                "quote": (
                                    quote_result.get("quote")
                                    if quote_result.get("ok") is not False else None
                                ),
                                "quote_error": (
                                    None if quote_result.get("ok") is not False
                                    else quote_result.get("error")
                                ),
                            })

                    if len(image_quote_targets) == 1:
                        target_recognition = image_quote_targets[0].get("recognition")
                        if isinstance(target_recognition, Mapping):
                            try:
                                parsed_target = MovieImageInfo.model_validate({
                                    key: value for key, value in target_recognition.items()
                                    if key in MovieImageInfo.model_fields
                                })
                            except Exception:
                                parsed_target = None
                            target_quote = image_quote_targets[0].get("quote")
                            try:
                                parsed_quote = (
                                    RealQuote.model_validate(target_quote)
                                    if isinstance(target_quote, Mapping) else None
                                )
                            except Exception:
                                parsed_quote = None
                            if (
                                parsed_target is not None
                                and is_wplus_unselected_image(parsed_target)
                            ):
                                if parsed_quote is not None and _requires_wplus_marker(parsed_target, parsed_quote):
                                    return {"decision": {
                                        "mode": "auto",
                                        "actions": _wplus_quote_actions(
                                            identity, parsed_target, parsed_quote, self._templates(),
                                        ),
                                        "reason": "wplus_quote_ready",
                                        "reply_route": "rule",
                                        "ai_called": False,
                                        "order_state": _authoritative_order_state(order),
                                        "buyer_intent": "REQUEST_QUOTE",
                                        "expected_action": "ASK_SEAT_MARK",
                                        "selection_mode": "WPLUS_MARKED",
                                    }}
                                failure = build_recognition_reply(
                                    parsed_target,
                                    quote_error=str(image_quote_targets[0].get("quote_error") or "当前场次暂未取得可核验价格"),
                                    templates=self._templates(),
                                )
                                return {"decision": {
                                    "mode": "auto",
                                    "actions": [{
                                        "id": f'{identity["event_id"]}:quote-unavailable',
                                        "type": "send_message",
                                        "text": failure,
                                        "rule_governed": True,
                                        "preserve_on_new_buyer_message": True,
                                    }],
                                    "reason": "wplus_quote_unavailable",
                                    "reply_route": "rule",
                                    "ai_called": False,
                                    "order_state": _authoritative_order_state(order),
                                    "buyer_intent": "REQUEST_QUOTE",
                                    "expected_action": "QUOTE_FAILED_CLOSED",
                                    "selection_mode": "WPLUS_QUOTE_PENDING",
                                }}

                    def target_signature(value: Mapping[str, Any] | None) -> str:
                        if not isinstance(value, Mapping):
                            return ""
                        seats = value.get("selected_seats")
                        normalized_seats = []
                        if isinstance(seats, list):
                            normalized_seats = [
                                str(item.get("seat_number") or item.get("seat_no") or "")
                                for item in seats if isinstance(item, Mapping)
                            ]
                        return json.dumps({
                            "cinema_id": value.get("cinema_id"),
                            "movie_id": value.get("movie_id"),
                            "show_id": value.get("show_id"),
                            "showtime_start": value.get("showtime_start"),
                            "seats": normalized_seats,
                        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

                    cached_quotes = {
                        target_signature(item.get("recognition")): {
                            "ok": item.get("quote") is not None,
                            "quote": item.get("quote"),
                            **(
                                {} if item.get("quote") is not None
                                else {"error": item.get("quote_error")}
                            ),
                        }
                        for item in image_quote_targets
                        if target_signature(item.get("recognition"))
                    }

                    async def execute_request_tool(name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
                        tool_name = str(name).strip()
                        if tool_name == "recognize_screenshot":
                            requested_urls = arguments.get("image_urls")
                            if not isinstance(requested_urls, list):
                                requested_urls = [arguments.get("image_url")]
                            requested_urls = [str(value or "").strip() for value in requested_urls if str(value or "").strip()]
                            current_urls = set(validated_image_urls)
                            if requested_urls and set(requested_urls).issubset(set(human_image_urls)) and not set(requested_urls) & current_urls:
                                result = await self._execute_agent_tool(
                                    tool_name, arguments, identity, order, envelope=envelope,
                                )
                                return {**result, "context_only": True, "source": "human_seller_image"}
                            if requested_urls and not set(requested_urls).issubset(current_urls):
                                return {"ok": False, "error": "human_image_context_reference_required"}
                            return fixed_recognition_result
                        if tool_name in {
                            "quote.preflight_current", "get_quote",
                            "get_authoritative_quote", "reprice_seats",
                            "recognition.resolve_seats",
                        }:
                            cached = cached_quotes.get(target_signature(arguments))
                            if cached is not None:
                                return cached
                        result = await self._execute_agent_tool(
                            tool_name, arguments, identity, order, envelope=envelope,
                        )
                        if tool_name == "show.list" and isinstance(result, Mapping):
                            result = self._record_show_list_snapshot(identity, arguments, result)
                        remember_grounded_quote(result)
                        return result
                    def observe_agent_tool_result(
                        name: str, arguments: Mapping[str, Any], result: Mapping[str, Any],
                    ) -> None:
                        self._observe_agent_tool_result(name, arguments, result, identity)
                    runtime_context = {
                        "_agent_tool_executor": execute_request_tool,
                        "_agent_tool_result_observer": observe_agent_tool_result,
                        "tenant_id": identity["tenant_id"], "shop_id": identity["shop_id"],
                        "buyer_id": identity["buyer_id"], "chat_id": identity["chat_id"],
                        "event_id": identity["event_id"],
                        "pending_image_url": validated_image_url,
                        "pending_image_urls": validated_image_urls,
                        "human_seller_image_urls": human_image_urls,
                        "pending_image_recognition": fixed_recognition_result,
                        "pending_image_conflicts": fixed_recognition_result.get("conflict_fields") or [],
                        "image_quote_targets": image_quote_targets,
                        "current_stage": _business_stage("im.message.received", order),
                        "order_status": order_state,
                        "order_id": _pick(order, "orderId", "order_id"),
                        "allowed_query_tools": [
                            str(item.get("function", {}).get("name"))
                            for item in (getattr(self._chat, "_tool_schemas", []) or [])
                            if isinstance(item, Mapping)
                        ],
                    }
                    image_prompt = payload_content or "买家发送了图片，后端已完成固定识别和可执行的权威预报价，请根据实时上下文直接回复。"
                    reply_method = self._chat.reply
                    parameters = inspect.signature(reply_method).parameters
                    if "runtime_context" in parameters:
                        candidate = await reply_method(
                            image_prompt, conversation_id, runtime_context=runtime_context,
                        )
                    else:
                        candidate = await reply_method(image_prompt, conversation_id)
                    if not isinstance(candidate, str) or not candidate.strip():
                        raise ValueError("agent_image_reply_empty")
                    reply = candidate.strip()
                    generic_ai_reply = True
                    decision_reason = "agent_image_reply_ready"
                    recognition = None
                    quote = None
                    quote_error = None
                except Exception as error:  # noqa: BLE001 - AI boundary fails closed
                    LOGGER.warning(
                        "event=agent_image_reply_unavailable error_type=%s",
                        type(error).__name__,
                    )
                    fallback_parts: list[str] = []
                    for index, target in enumerate(image_quote_targets):
                        target_recognition = target.get("recognition")
                        if not isinstance(target_recognition, Mapping):
                            continue
                        try:
                            parsed_recognition = MovieImageInfo.model_validate({
                                key: value for key, value in target_recognition.items()
                                if key in MovieImageInfo.model_fields
                            })
                            target_quote = target.get("quote")
                            parsed_quote = (
                                RealQuote.model_validate(target_quote)
                                if isinstance(target_quote, Mapping) else None
                            )
                            rendered = build_recognition_reply(
                                parsed_recognition,
                                quote=parsed_quote,
                                quote_error=(
                                    str(target.get("quote_error") or "") or None
                                ),
                                templates=self._templates(),
                            )
                        except Exception:
                            continue
                        prefix = f"第{index + 1}张：" if len(image_quote_targets) > 1 else ""
                        fallback_parts.append(prefix + rendered)
                    if fallback_parts:
                        reply = "\n".join(fallback_parts)
                        decision_reason = "agent_image_grounded_fallback_ready"
                        generic_ai_reply = False
                        recognition = None
                        quote = None
                        quote_error = None
                    else:
                        reply = render_template(
                            self._templates().recognition_failure_other_template,
                            {"失败原因": "图片暂时无法读取或识别"},
                        )
                        decision_reason = "image_recognition_failure_reply_ready"
                        generic_ai_reply = False
                        recognition = None
                        quote = None
                        quote_error = None
            elif not self._ai_assist_enabled:
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
                        identity=identity,
                        cinema_hints=_recent_buyer_cinema_hints(messages, before_ms),
                    )
                    # The image message is the only text in this event that may
                    # declare a quantity; arbitrary history is not evidence.
                    quote = _apply_declared_ticket_count(
                        quote, _declared_ticket_count(_text(current.get("content", current.get("text"))) or ""),
                    )
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
                    if quote is not None and _requires_wplus_marker(recognition, quote):
                        return {"decision": {
                            "mode": "auto",
                            "actions": _wplus_quote_actions(
                                identity, recognition, quote, self._templates(),
                            ),
                            "reason": "wplus_quote_marker_confirmation_required",
                            "reply_route": "rule",
                            "ai_called": False,
                            "order_state": _authoritative_order_state(order),
                            "buyer_intent": "REQUEST_QUOTE",
                            "expected_action": "ASK_SEAT_MARK",
                            "selection_mode": "WPLUS_MARKED",
                        }}
                    elif is_wplus_unselected_image(recognition):
                        return {"decision": {
                            "mode": "auto",
                            "actions": [{
                                "id": f'{identity["event_id"]}:wplus-marker-required',
                                "type": "send_message",
                                "text": build_wplus_marker_confirmation_reply(templates=self._templates()),
                                "rule_governed": True,
                                "preserve_on_new_buyer_message": True,
                            }],
                            "reason": "wplus_marker_required",
                            "reply_route": "rule",
                            "ai_called": False,
                            "order_state": _authoritative_order_state(order),
                            "buyer_intent": "REQUEST_QUOTE",
                            "expected_action": "ASK_SEAT_MARK",
                            "selection_mode": "WPLUS_UNMARKED",
                        }}
                    elif (
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
            # Do not classify ordinary text with keyword/phrase rules. Fixed
            # transaction events remain rule-owned; buyer conversation belongs
            # to the Agent.
            # Fallback, refund and other transaction recovery lanes remain
            # rule-owned in every automation mode. Agent modes may explain the
            # result, but cannot replace this state-gated decision.
            fixed_switch = await self._fixed_switch_decision(identity, envelope, message)
            if fixed_switch is not None:
                return fixed_switch
            marker_confirmation = _wplus_marker_confirmation(message)
            previous_image = _immediately_previous_buyer_image(
                messages, current, _event_time_ms(envelope),
            )
            pending_wplus = self._current_wplus_marker_snapshot(identity, envelope, order)
            if (
                marker_confirmation is None
                and pending_wplus is not None
                and _wplus_marker_semantic_candidate(message)
            ):
                marker_confirmation = await self._classify_wplus_marker_response(
                    identity, message, previous_image=previous_image, messages=messages,
                    pending_wplus=pending_wplus,
                )
            if marker_confirmation and pending_wplus is not None:
                if marker_confirmation == "missing":
                    return {"decision": {
                        "mode": "auto",
                        "actions": [{
                            "id": f'{identity["event_id"]}:wplus-marker-required',
                            "type": "send_message",
                            "text": build_wplus_marker_missing_reply(templates=self._templates()),
                            "rule_governed": True,
                            "preserve_on_new_buyer_message": True,
                        }],
                        "reason": "wplus_marker_required",
                        "buyer_intent": "CONFIRM_SEATS",
                        "expected_action": "ASK_SEAT_MARK",
                        "selection_mode": "WPLUS_MARKED",
                    }}
                return {"decision": {
                    "mode": "auto",
                    "actions": [{
                        "id": f'{identity["event_id"]}:wplus-manual-fulfillment',
                        "type": "send_message",
                        "text": build_wplus_marker_confirmed_reply(templates=self._templates()),
                        "rule_governed": True,
                        "preserve_on_new_buyer_message": True,
                    }],
                    "reason": "wplus_marker_confirmed_order_guidance",
                    "buyer_intent": "CONFIRM_SEATS",
                    "expected_action": "SUBMIT_ORDER",
                    "selection_mode": "WPLUS_MARKED",
                }}
            if pending_wplus is not None:
                return {"decision": {
                    "mode": "auto",
                    "actions": [{
                        "id": f'{identity["event_id"]}:wplus-marker-confirmation',
                        "type": "send_message",
                        "text": build_wplus_marker_confirmation_reply(templates=self._templates()),
                        "rule_governed": True,
                        "preserve_on_new_buyer_message": True,
                    }],
                    "reason": "wplus_marker_confirmation_required",
                    "buyer_intent": "CONFIRM_SEATS",
                    "expected_action": "ASK_SEAT_MARK",
                    "selection_mode": "WPLUS_MARKED",
                }}
            pending = self._get_pending_candidate(identity) if automation_mode == "rules" else None
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
            templates = self._templates()
            keyword_rule = _custom_keyword_rule(message, templates) if automation_mode == "rules" else None
            declared_ticket_count = (
                _declared_ticket_count(message)
                if automation_mode == "rules" or pending_wplus is not None else None
            )
            durable_quote = self._find_quote_record(identity, envelope, order)
            if durable_quote is not None and _is_price_request(message):
                quote = _real_quote_from_record(durable_quote)
                quote_seats = _quote_record_seats(durable_quote)
                recognition = _recognition_from_quote_record(durable_quote, quote_seats)
                if quote is not None and recognition is not None:
                    reply = build_recognition_reply(
                        recognition, quote=quote, templates=self._templates(),
                    )
                    return {"decision": {
                        "mode": "auto",
                        "actions": [{
                            "id": f'{identity["event_id"]}:reply',
                            "type": "send_message",
                            "text": reply,
                            "rule_governed": True,
                            "preserve_on_new_buyer_message": True,
                        }],
                        "reason": "durable_quote_price_ready",
                        "reply_route": "rule",
                        "ai_called": False,
                        "order_state": _authoritative_order_state(order),
                    }}
            # Ordinary buyer text is Agent-owned. Seat extraction and quote
            # tools will be introduced through the provider-neutral action
            # protocol; this path must not infer a trade action locally.
            explicit_seats = (
                _explicit_seats_in_buyer_hint(message) if automation_mode == "rules" else ()
            )
            structured_request: MovieImageInfo | None = (
                _structured_ticket_request(message) if automation_mode == "rules" else None
            )
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
                        seat_request, quote = await self._quote_for_recognition(seat_request, identity)
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
            else:
                # Never route a text message by a locally recognized sentence;
                # send it to the Agent with the current quote/context facts.
                structured_request = None
            if structured_request is not None:
                try:
                    structured_request, quote = await self._quote_for_recognition(structured_request, identity)
                    quote = _apply_declared_ticket_count(quote, declared_ticket_count)
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
            elif _authoritative_order_state(order) in {"shipped", "completed"} and _is_post_order_status_intent(message):
                reply = self._templates().order_shipped_template
                decision_reason = "authoritative_shipped_status_reply_ready"
                keyword_rule = None
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            elif _authoritative_order_state(order) == "paid" and _is_post_order_status_intent(message):
                reply = self._templates().payment_success_pending_ticket_template
                decision_reason = "authoritative_paid_status_reply_ready"
                keyword_rule = None
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            # A quantity is an explicit field, not an inferred confirmation.
            # It can advance the fixed quote workflow only when a durable quote
            # exists; free-form acknowledgements never do so.
            elif isinstance(durable_quote, Mapping) and declared_ticket_count is not None:
                ticket_count = declared_ticket_count
                confirmed_quote = self._confirm_quote_record(identity, envelope, ticket_count)
                if isinstance(order, Mapping) and ticket_count is not None:
                    pricing = self._confirmed_quote_decision(identity, envelope, order)
                    if pricing is not None:
                        return self._with_keyword_actions(pricing, identity, keyword_rule)
                quantity_guidance = (
                    _quantity_order_guidance(
                        confirmed_quote if isinstance(confirmed_quote, Mapping) else durable_quote,
                        ticket_count, message, self._templates(),
                        wplus_marker_confirmed=pending_wplus is not None,
                    )
                    if ticket_count is not None else None
                )
                if quantity_guidance:
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
            elif keyword_rule is not None:
                reply = keyword_rule.reply
                decision_reason = "custom_keyword_reply_ready"
                event_time_ms = None
                prior_image = None
                is_context_clarification = False
            else:
                event_time_ms = _event_time_ms(envelope)
                is_identity_clarification = (
                    automation_mode == "rules"
                    and (_looks_like_cinema_clarification(message) or await self._is_known_city_hint(message))
                )
                prior_image = (
                    _immediately_previous_buyer_image(messages, current, event_time_ms)
                    if is_identity_clarification else None
                )
                is_context_clarification = is_identity_clarification
            if decision_reason in {
                "authoritative_paid_status_reply_ready", "authoritative_shipped_status_reply_ready",
                "order_submission_guidance_ready",
                "authoritative_paid_status_reply_ready", "authoritative_shipped_status_reply_ready",
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
                    quote = _apply_declared_ticket_count(
                        quote, _declared_ticket_count(message),
                    )
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
                    runtime_state = None
                    state_getter = getattr(self._transaction_states, "get", None)
                    if callable(state_getter):
                        try:
                            runtime_state = state_getter(**{key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")})
                        except Exception:
                            runtime_state = None
                    quote_context = durable_quote if isinstance(durable_quote, Mapping) else None
                    confirmed_facts = {}
                    missing_fields = []
                    public_quote = None
                    if quote_context:
                        confirmed_facts = {key: quote_context.get(key) for key in ("city", "cinema", "movie", "quote_date", "showtime_start", "hall", "seat_display", "ticket_count") if quote_context.get(key)}
                        missing_fields = quote_context.get("missing_fields") if isinstance(quote_context.get("missing_fields"), list) else []
                        public_quote = {key: quote_context.get(key) for key in ("quote_id", "quote_record_id", "quote_route", "quote_scope", "seat_zone_type", "unit_quote_cents", "total_quote_cents", "ticket_count", "quote_expires_at", "status") if quote_context.get(key) is not None}
                    async def execute_request_tool(name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
                        result = await self._execute_agent_tool(
                            name, arguments, identity, order, envelope=envelope,
                        )
                        if str(name).strip() == "show.list" and isinstance(result, Mapping):
                            result = self._record_show_list_snapshot(identity, arguments, result)
                        remember_grounded_quote(result)
                        return result

                    def observe_agent_tool_result(
                        name: str, arguments: Mapping[str, Any], result: Mapping[str, Any],
                    ) -> None:
                        self._observe_agent_tool_result(name, arguments, result, identity)
                    pending_agent_candidate = self._get_pending_candidate(identity)
                    pending_agent_shows = self._get_pending_shows(identity)
                    pending_agent_conflicts = tuple(
                        warning.removeprefix("conflict:")
                        for warning in (pending_agent_candidate.warnings if pending_agent_candidate is not None else [])
                        if warning.startswith("conflict:")
                        and warning.removeprefix("conflict:") in CRITICAL_IMAGE_CONFLICT_FIELDS
                    )
                    pending_resolution_targets = self._current_resolution_targets(identity)
                    runtime_context = {
                        "_agent_tool_executor": execute_request_tool,
                        "_agent_tool_result_observer": observe_agent_tool_result,
                        "tenant_id": identity["tenant_id"], "shop_id": identity["shop_id"],
                        "buyer_id": identity["buyer_id"], "chat_id": identity["chat_id"],
                        "event_id": identity["event_id"],
                        "current_stage": _business_stage("im.message.received", order),
                        "flow_state": getattr(runtime_state, "flow_state", None),
                        "order_status": _authoritative_order_state(order),
                        "quote_status": getattr(runtime_state, "quote_status", None),
                        "confirmation_status": getattr(runtime_state, "confirmation_status", None),
                        "payment_status": getattr(runtime_state, "payment_status", None),
                        "fulfillment_status": getattr(runtime_state, "fulfillment_status", None),
                        "order_id": getattr(runtime_state, "order_id", None) or _pick(order, "orderId", "order_id"),
                        "confirmed_facts": confirmed_facts,
                        "missing_fields": missing_fields or (list(getattr(runtime_state, "expected_inputs", []) or []) if runtime_state else []),
                        "current_quote": public_quote,
                        "pending_cinema_candidates": [
                            candidate.model_dump(mode="json")
                            for candidate in pending_agent_candidate.candidate_cinemas
                        ] if pending_agent_candidate is not None else [],
                        "pending_movie_candidates": [
                            candidate.model_dump(mode="json")
                            for candidate in pending_agent_candidate.candidate_movies
                        ] if pending_agent_candidate is not None else [],
                        "pending_image_conflicts": list(pending_agent_conflicts),
                        "pending_image_missing_fields": list(
                            pending_agent_candidate.missing_fields
                        ) if pending_agent_candidate is not None else [],
                        "pending_show_candidates": pending_agent_shows,
                        "pending_image_recognition": _public_recognition_payload(
                            pending_agent_candidate
                        ) if pending_agent_candidate is not None else None,
                        "pending_recognition_targets": pending_resolution_targets,
                        "allowed_query_tools": [str(item.get("function", {}).get("name")) for item in (getattr(self._chat, "_tool_schemas", []) or []) if isinstance(item, Mapping)],
                        "human_takeover_paused": bool(suppress_generic_ai),
                    }
                    reply_method = self._chat.reply
                    parameters = inspect.signature(reply_method).parameters
                    if "runtime_context" in parameters:
                        candidate = await reply_method(message, conversation_id, runtime_context=runtime_context)
                    else:
                        candidate = await reply_method(message, conversation_id)
                    assist = AiAssistResult(
                        reply_candidate=candidate,
                        confidence=0.5,
                        source_message_ids=[identity["event_id"]],
                    )
                    reply = assist.reply_candidate
                    generic_ai_reply = True
                except Exception as error:  # noqa: BLE001 - AI boundary fails closed
                    LOGGER.warning(
                        "event=chat_reply_unavailable error_type=%s",
                        type(error).__name__,
                    )
                    # A provider timeout must not turn an ordinary buyer
                    # question into a silent conversation.  Fall back only to
                    # facts already established by the workflow; never invent
                    # a price, order state, or ticket result here.
                    if pending_agent_candidate is not None:
                        reply = build_recognition_reply(
                            pending_agent_candidate,
                            templates=self._templates(),
                        )
                    else:
                        reply = self._templates().guidance_template
                    generic_ai_reply = False
                    decision_reason = "agent_text_grounded_fallback_ready"
        exact_quote_pair: tuple[MovieImageInfo, RealQuote] | None = None
        if image_workflow and recognition is not None and quote is not None:
            if quote.quote_scope == "exact_seats" and _exact_quote_has_amounts(quote):
                exact_quote_pair = (recognition, quote)
        elif image_workflow and len(image_quote_targets) == 1:
            target = image_quote_targets[0]
            target_recognition = target.get("recognition")
            target_quote = target.get("quote")
            if isinstance(target_recognition, Mapping) and isinstance(target_quote, Mapping):
                try:
                    parsed_recognition = MovieImageInfo.model_validate({
                        key: value for key, value in target_recognition.items()
                        if key in MovieImageInfo.model_fields
                    })
                    parsed_quote = RealQuote.model_validate(target_quote)
                except Exception:
                    parsed_recognition = None
                    parsed_quote = None
                if (
                    parsed_recognition is not None
                    and parsed_quote is not None
                    and parsed_quote.quote_scope == "exact_seats"
                    and _exact_quote_has_amounts(parsed_quote)
                ):
                    exact_quote_pair = (parsed_recognition, parsed_quote)
        if exact_quote_pair is not None:
            exact_recognition, exact_quote = exact_quote_pair
            return {"decision": {
                "mode": "auto",
                "actions": _exact_quote_actions(identity, exact_recognition, exact_quote),
                "reason": "exact_quote_split_messages_ready",
                "reply_route": "agent" if generic_ai_reply else "rule",
                "ai_called": generic_ai_reply,
                "order_state": _authoritative_order_state(order),
                "buyer_intent": "REQUEST_QUOTE",
                "expected_action": "SUBMIT_ORDER",
            }}
        # Generic knowledge answers may be substantially longer than a fixed
        # transaction template, but still stay within one platform message.
        if not reply or len(reply) > 3_000:
            return {"decision": {"mode": "auto", "actions": [], "reason": "safe_reply_unavailable"}}
        reply_price_claims = _price_claim_cents(reply) if generic_ai_reply else set()
        ungrounded_price_claim = bool(
            reply_price_claims and not reply_price_claims.issubset(grounded_quote_amounts)
        )
        if generic_ai_reply and (
            _has_protected_transaction_claim(reply) or ungrounded_price_claim
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
        audit = {
            "reply_route": "agent" if generic_ai_reply else "rule",
            "ai_called": generic_ai_reply,
            "order_state": _authoritative_order_state(order),
        }
        if suppress_generic_ai:
            audit["suppressed_reason"] = "human_takeover_cooldown"
        if decision_reason == "wplus_marker_confirmation_required":
            audit.update({
                "buyer_intent": "CONFIRM_SEATS",
                "expected_action": "ASK_SEAT_MARK",
                "selection_mode": "WPLUS_MARKED",
            })
        return {"decision": {"mode": "auto", "actions": actions, "reason": decision_reason, **audit}}

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
        fixed_switch = self._fixed_switch_order_created_decision(identity, order)
        if fixed_switch is not None:
            return fixed_switch
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
