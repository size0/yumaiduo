from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..models import MovieImageInfo
from ..probe.policy import disable_active_probe
from .observations import Observation
from .registry import ToolDefinition, ToolRegistry


_READ_ONLY_NAMES = (
    "recognize_screenshot", "cinema.list", "movie.resolve", "show.list",
    "show.detail", "seat.list", "quote.preview", "order.current",
    "conversation.current",
)


_FORBIDDEN_KEYS = {"raw_response", "raw_results", "final_results", "token", "access_token", "cookie", "csrf", "csrf_token", "app_secret"}


def _clean(value: object, *, limit: int = 100) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _clean(item, limit=limit)
            for key, item in list(value.items())[:limit]
            if str(key).lower() not in _FORBIDDEN_KEYS
        }
    if isinstance(value, list):
        return [_clean(item, limit=limit) for item in value[:limit]]
    if isinstance(value, tuple):
        return [_clean(item, limit=limit) for item in value[:limit]]
    if hasattr(value, "model_dump"):
        return _clean(value.model_dump(mode="json"), limit=limit)
    return value


def _public_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _clean(item) for key, item in value.items() if str(key).lower() not in _FORBIDDEN_KEYS}


def _provider_items(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("items", "list", "records", "cinemas", "movies", "shows", "seats", "data"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, Mapping)][:100]
        if isinstance(candidate, Mapping):
            nested = _provider_items(candidate)
            if nested:
                return nested
    return []


def _canonical_candidate(item: Mapping[str, Any], label: str) -> dict[str, Any]:
    aliases = {
        "cinema": {"id": ("cinemaId", "cinema_id", "id"), "name": ("cinemaName", "cinema_name", "name"), "city": ("cityName", "city_name", "city"), "address": ("address", "cinemaAddress", "cinema_address")},
        "movie": {"id": ("movieId", "movie_id", "id"), "name": ("movieName", "movie_name", "filmName", "name")},
        "show": {"id": ("showId", "show_id", "id"), "cinema_id": ("cinemaId", "cinema_id"), "movie_id": ("movieId", "movie_id"), "movie_name": ("movieName", "movie_name"), "hall_name": ("hallName", "hall_name", "hall"), "start_time": ("startTime", "start_time", "showtime")},
        "seat": {"seat_number": ("seatName", "seat_name", "seatNumber", "seat_number", "label"), "seat_no": ("seatNo", "seat_no"), "area_id": ("areaId", "area_id"), "status": ("status", "seatStatus", "seat_status")},
    }
    mapping = aliases.get(label, {})
    result: dict[str, Any] = {}
    for canonical, keys in mapping.items():
        for key in keys:
            if item.get(key) is not None and str(item.get(key)).strip() != "":
                result[canonical] = _clean(item.get(key))
                break
    return result or _public_mapping(item)


def _observation_from_provider(code: str, value: object, *, label: str) -> Observation:
    if not isinstance(value, Mapping):
        return Observation.warning("provider_response_invalid", message=f"{label}返回格式无效。")
    public = _public_mapping(value)
    items = _provider_items(value)
    if not items:
        nested = value.get("data")
        if isinstance(nested, Mapping) and nested:
            items = [nested]
    canonical_label = "cinema" if label == "影院" else "movie" if label == "影片" else "show" if label.startswith("场次") else "seat" if label == "座位" else "order"
    candidates = [_canonical_candidate(item, canonical_label) for item in items]
    return Observation.success(code, facts={"items": candidates, "provider_summary": public, "count": len(candidates)}, candidates=candidates, message=f"已查询{label}。")


def _recognition_observation(recognition: MovieImageInfo) -> Observation:
    facts = recognition.model_dump(mode="json", exclude={"raw_results", "final_results", "raw_response"})
    candidates = [item.model_dump(mode="json") for item in recognition.candidate_cinemas]
    candidates.extend(item.model_dump(mode="json") for item in recognition.candidate_movies)
    candidates.extend(item.model_dump(mode="json") for item in recognition.candidate_shows)
    missing = tuple(recognition.missing_fields)
    if recognition.recognition_blocker:
        return Observation.warning(
            "recognition_incomplete", facts=facts, missing_fields=missing,
            message=f"截图识别存在阻塞：{recognition.recognition_blocker}。",
        )
    return Observation.success("recognition_ready", facts=facts, candidates=candidates, message="截图识别完成。")


def _quote_observation(quote: object, *, quote_id: str, expires_at: str) -> Observation:
    if not hasattr(quote, "model_dump"):
        return Observation.warning("quote_response_invalid", message="报价返回格式无效。")
    data = quote.model_dump(mode="json")
    total = data.get("total_quote_cents")
    count = data.get("ticket_count")
    if total is None or count is None:
        return Observation.warning("quote_incomplete", facts=data, missing_fields=("total_price_cents", "quantity"), message="报价缺少总价或票数。")
    return Observation.success(
        "quote_ready",
        facts={
            "quote_id": quote_id,
            "provider": data.get("price_source") or data.get("pricing_source") or "wanda",
            "quantity": count,
            "unit_price_cents": data.get("unit_quote_cents"),
            "total_price_cents": total,
            "currency": "CNY",
            "expires_at": data.get("quote_expires_at") or data.get("expires_at") or expires_at,
            "quote": data,
        },
        message="已取得权威实时报价。",
    )


def build_read_only_registry(
    *,
    recognition_service: object | None = None,
    quote_service: object | None = None,
    provider_client: object | None = None,
    recent_messages: Callable[[], list[Mapping[str, Any]]] | None = None,
    quote_recorder: Callable[[Mapping[str, Any]], object] | None = None,
    current_order_provider: Callable[[Mapping[str, str]], object] | None = None,
    route_resolver: object | None = None,
) -> ToolRegistry:
    """Build the canonical read-only tool registry from existing services."""
    registry = ToolRegistry(read_only=True)

    async def recognize(arguments: dict[str, Any]) -> Observation:
        if recognition_service is None:
            return Observation.warning("recognition_unavailable", message="截图识别服务暂时不可用。")
        url = str(arguments.get("image_url") or "").strip()
        if not url:
            urls = arguments.get("image_urls")
            url = str(urls[0] if isinstance(urls, list) and urls else "").strip()
        method = getattr(recognition_service, "recognize_from_url", None)
        if not url or not callable(method):
            return Observation.warning("image_required", missing_fields=("image_url",), message="请提供有效截图。")
        try:
            result = await method(url, buyer_message=str(arguments.get("buyer_message") or "")[:2000])
        except Exception:
            return Observation.warning("recognition_failed", message="截图暂时无法识别，请稍后重试或转人工确认。")
        return _recognition_observation(result) if isinstance(result, MovieImageInfo) else Observation.warning("recognition_response_invalid")

    async def provider_read(arguments: dict[str, Any], method_name: str, label: str) -> Observation:
        method = getattr(provider_client, method_name, None) if provider_client is not None else None
        if not callable(method):
            return Observation.warning("provider_read_unavailable", message=f"{label}查询服务暂时不可用。")
        try:
            result = await method(**arguments)
        except Exception:
            return Observation.warning("provider_read_failed", message=f"{label}暂时查询失败，请稍后重试。")
        return _observation_from_provider(f"{label}_ready", result, label=label)

    async def cinema_list(arguments: dict[str, Any]) -> Observation:
        return await provider_read(arguments, "cinema_list", "影院")

    async def movie_resolve(arguments: dict[str, Any]) -> Observation:
        return await provider_read(arguments, "movie_list", "影片")

    async def show_list(arguments: dict[str, Any]) -> Observation:
        return await provider_read(arguments, "show_list", "场次")

    async def show_detail(arguments: dict[str, Any]) -> Observation:
        return await provider_read(arguments, "show_detail", "场次详情")

    async def seat_list(arguments: dict[str, Any]) -> Observation:
        return await provider_read(arguments, "seat_list", "座位")

    async def quote_preview(arguments: dict[str, Any], identity: Mapping[str, str]) -> Observation:
        if quote_service is None or not callable(getattr(quote_service, "quote", None)):
            return Observation.warning("quote_unavailable", message="实时报价服务暂时不可用，请人工确认。")
        try:
            fields = {key: value for key, value in arguments.items() if key in MovieImageInfo.model_fields}
            if isinstance(fields.get("selected_seats"), list) and "selected_count_visible" not in fields:
                fields["selected_count_visible"] = len(fields["selected_seats"])
            recognition = MovieImageInfo.model_validate(fields)
            quote_target = recognition
            preferred_cinema_id = None
            if route_resolver is not None and callable(getattr(route_resolver, "resolve", None)):
                route = await route_resolver.resolve(recognition)
                if getattr(route, "route", "UNKNOWN") != "WANDA_SELF":
                    return Observation.warning(
                        "quote_route_unavailable",
                        message="当前影院不是可用的万达自营报价场次，需人工确认。",
                    )
                quote_target = getattr(route, "recognition", recognition)
                preferred_cinema_id = getattr(route, "wanda_cinema_id", None)
            mapped_quote = getattr(quote_service, "quote_mapped", None)
            # quote.preview is read-only: it may never fall through to the
            # legacy WandaDirectQuoteService temporary-order probe.
            with disable_active_probe():
                quote = await (
                    mapped_quote(quote_target, wanda_cinema_id=preferred_cinema_id)
                    if preferred_cinema_id and callable(mapped_quote)
                    else quote_service.quote(quote_target)
                )
        except Exception as error:
            if getattr(error, "code", "") == "QUOTE_REQUIRES_ACTIVE_PROBE":
                return Observation.warning(
                    "QUOTE_REQUIRES_ACTIVE_PROBE",
                    message="实时会员成本缺失，需要由确定性的 ProbeCoordinator 处理。",
                )
            return Observation.warning("quote_failed", message="这个场次暂时没有查到可用价格，需要人工确认。")
        data = quote.model_dump(mode="json") if hasattr(quote, "model_dump") else {}
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=15)
        quote_id = f"agent-quote-{uuid4().hex}"
        effective_expires_at = str(data.get("quote_expires_at") or data.get("expires_at") or expires_at.isoformat())
        facts = _quote_observation(quote, quote_id=quote_id, expires_at=effective_expires_at)
        if quote_recorder is not None:
            try:
                quote_facts = facts.facts
                quote_recorder({
                    "record_id": quote_id,
                    "quote_id": quote_id,
                    "tenant_id": identity.get("tenant_id"), "shop_id": identity.get("shop_id"),
                    "buyer_id": identity.get("buyer_id"), "chat_id": identity.get("chat_id"),
                    "event_id": identity.get("event_id"), "trace_id": identity.get("trace_id"),
                    "provider": quote_facts.get("provider"), "quote_route": "wanda_self",
                    "quantity": quote_facts.get("quantity"),
                    "unit_price_cents": quote_facts.get("unit_price_cents"),
                    "total_quote_cents": quote_facts.get("total_price_cents"),
                    "currency": "CNY", "created_at": now.isoformat(),
                    "quote_expires_at": effective_expires_at,
                    "provider_trace_reference": data.get("provider_quote_id") or data.get("provider_quote_hash"),
                    "snapshot_reference": data.get("provider_quote_hash"),
                    "status": "succeeded", "delivery_state": "pending",
                })
            except Exception:
                pass
        return facts

    async def order_current(_arguments: dict[str, Any], identity: Mapping[str, str]) -> Observation:
        if current_order_provider is None:
            return Observation.warning("order_not_bound", message="当前会话没有绑定订单。")
        try:
            result = current_order_provider(identity)
            if hasattr(result, "__await__"):
                result = await result
        except Exception:
            return Observation.warning("order_read_failed", message="当前订单暂时无法读取，请稍后重试。")
        return _observation_from_provider("order_ready", result, label="订单")

    async def conversation_current(
        _arguments: dict[str, Any], _identity: Mapping[str, str], context: Mapping[str, Any],
    ) -> Observation:
        if recent_messages is not None:
            messages = recent_messages()
        else:
            conversation = context.get("conversation")
            messages = conversation.get("recent_messages", []) if isinstance(conversation, Mapping) else []
        return Observation.success("conversation_ready", facts={"messages": _clean(messages)}, message="已读取当前会话最近消息。")

    definitions = {
        "recognize_screenshot": ("识别买家截图中的影院、影片、场次和座位。", {"type": "object", "properties": {"image_url": {"type": "string"}, "image_urls": {"type": "array", "items": {"type": "string"}}, "buyer_message": {"type": "string"}}}, recognize),
        "cinema.list": ("查询影院候选。", {"type": "object", "additionalProperties": False, "properties": {"cityCode": {"type": "string"}, "cityName": {"type": "string"}, "keyword": {"type": "string"}}}, cinema_list),
        "movie.resolve": ("查询并解析影片候选。", {"type": "object", "additionalProperties": False, "properties": {"city_code": {"type": "string"}, "keyword": {"type": "string"}}}, movie_resolve),
        "show.list": ("查询影院影片的实时场次。", {"type": "object", "additionalProperties": False, "properties": {"cinemaId": {"type": "integer"}, "movieId": {"type": "integer"}, "showDate": {"type": "string"}}}, show_list),
        "show.detail": ("查询单个场次详情。", {"type": "object", "additionalProperties": False, "properties": {"showId": {"type": "string"}, "cinemaId": {"type": "integer"}}}, show_detail),
        "seat.list": ("查询实时座位，不锁座。", {"type": "object", "additionalProperties": False, "properties": {"showId": {"type": "string"}, "cinemaId": {"type": "integer"}, "row_no": {"type": "integer"}, "seat_preference": {"type": "string"}}}, seat_list),
        "quote.preview": ("获取权威实时报价，不下单、不改价。", {"type": "object", "additionalProperties": False, "properties": {"cinema_id": {"type": "integer"}, "cinema_name": {"type": "string"}, "movie_name": {"type": "string"}, "date": {"type": "string"}, "showtime_start": {"type": "string"}, "hall_name": {"type": "string"}, "selected_seats": {"type": "array", "items": {"type": "object"}}}}, quote_preview),
        "order.current": ("读取当前绑定订单状态。", {"type": "object", "additionalProperties": False, "properties": {"order_no": {"type": "string"}}}, order_current),
        "conversation.current": ("读取当前会话最近消息。", {"type": "object", "additionalProperties": False, "properties": {}}, conversation_current),
    }
    for name in _READ_ONLY_NAMES:
        description, schema, handler = definitions[name]
        registry.register(ToolDefinition(name=name, description=description, input_schema=schema, handler=handler, risk_level="read", scope="request"))
    return registry
