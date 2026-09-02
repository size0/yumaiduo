from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from decimal import Decimal
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .config import Settings
from .diagnostics import DiagnosticsStore
from .errors import ProviderError
from .models import MovieImageInfo, PricingRulesUpdate, RealQuote, RealSeatQuote
from .reply_template_store import ReplyTemplates, render_template
from .observability import LOGGER
from .probe.policy import ProbePolicy


CINEMA_ORIGIN: Final = "https://cinema-api-prd-mx.wandafilm.com"
FRONT_ORIGIN: Final = "https://front-gateway-c.wandafilm.com"
MARKETING_ORIGIN: Final = "https://mkt-activity-api-prd-mx.wandafilm.com"
ALLOWED_HOSTS: Final = frozenset({
    "cinema-api-prd-mx.wandafilm.com",
    "front-gateway-c.wandafilm.com",
    "mkt-activity-api-prd-mx.wandafilm.com",
})
SALE_SUBJECT_CODE: Final = "Wanda"
H5_CHANNEL: Final = "1_3"
APP_CHANNEL: Final = "1_2"
H5_CLIENT_KEY: Final = "B3AA12B0145E1982F282BEDD8A3305B89A9811280C0B8CC3A6A60D81022E4903"
APP_CLIENT_KEY: Final = "B6C1D9E2F8G7H5J3K4L0MNPQRSTUVWXYZABCDEFGHIJKLMNOPQRSTUVWXYZ5678A"
APP_VERSION: Final = "9.3.6"
APP_AES_KEY: Final = b"6f34faeefba8fd39"


class WandaDirectQuoteService:
    """Fast Wanda pricing with realtime W+ activity prices and a verified probe fallback."""

    def __init__(
        self,
        settings: Settings | Callable[[], Settings],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        diagnostics: DiagnosticsStore | None = None,
        now_provider: Callable[[], datetime] | None = None,
        release_recheck_delays: Sequence[float] = (0, 0.5, 1.5, 5, 10, 20),
        order_status_timeout_seconds: float = 1.5,
        lock_status_retry_delays: Sequence[float] = (0, 0.25, 0.75),
        showtime_hedge_delay_seconds: float = 1.5,
        pricing_rules: PricingRulesUpdate | Callable[[], PricingRulesUpdate] | None = None,
        reply_templates: Callable[[], ReplyTemplates] | None = None,
    ) -> None:
        self._settings_provider = settings if callable(settings) else lambda: settings
        self._pricing_rules_provider = (
            pricing_rules if callable(pricing_rules)
            else lambda: pricing_rules or PricingRulesUpdate()
        )
        self._reply_templates_provider = reply_templates
        initial = self._settings_provider()
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(initial.wanda_request_timeout_seconds, connect=4.0),
            follow_redirects=False,
        )
        self._diagnostics = diagnostics or DiagnosticsStore()
        self._now_provider = now_provider or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        delays = tuple(float(value) for value in release_recheck_delays)
        if (
            not delays or delays[0] != 0
            or any(value < 0 or value > 30 for value in delays)
            or sum(delays) > 60
        ):
            raise ValueError("release recheck delays must begin with zero and total at most 60 seconds")
        self._release_recheck_delays = delays
        status_timeout = float(order_status_timeout_seconds)
        status_delays = tuple(float(value) for value in lock_status_retry_delays)
        if not 0.25 <= status_timeout <= 5:
            raise ValueError("order status timeout must be between 0.25 and 5 seconds")
        if not status_delays or status_delays[0] != 0 or any(value < 0 or value > 2 for value in status_delays):
            raise ValueError("lock status retry delays must begin with zero and stay between 0 and 2 seconds")
        hedge_delay = float(showtime_hedge_delay_seconds)
        if not 0.05 <= hedge_delay <= 5:
            raise ValueError("showtime hedge delay must be between 0.05 and 5 seconds")
        self._order_status_timeout_seconds = status_timeout
        self._lock_status_retry_delays = status_delays
        self._showtime_hedge_delay_seconds = hedge_delay
        self._selected_phone = initial.wanda_fixed_account_phone.strip()
        self._showtime_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_wplus_seats(
        self,
        recognition: MovieImageInfo,
        *,
        row_no: int | None = None,
        seat_preference: str | None = None,
        wanda_cinema_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Read the live Wanda W+ seat map for a buyer's row preference.

        This is deliberately separate from ``quote``: an unmarked W+ image is
        a seat-area consultation, so we must show currently available member
        seats before asking the buyer whether the seats were marked. No price,
        lock, order, or selected-seat quote is produced here.
        """
        settings = self._settings_provider()
        account = self._fixed_account(settings)
        quote_date = self._resolved_date(recognition)
        if not quote_date or not recognition.showtime_start:
            raise ProviderError(
                "wanda_seat_identity_incomplete",
                "查询W+座位需要完整的日期和开场时间。",
            )
        self._validate_quote_datetime(recognition, quote_date)
        cached_showtimes: dict[str, Mapping[str, Any]] = {}
        city_is_unverified = not recognition.city or "city" in recognition.missing_fields
        try:
            cinema = await self._resolve_cinema(
                settings, recognition, preferred_cinema_id=wanda_cinema_id,
            )
        except ProviderError as error:
            if error.code not in {"wanda_cinema_not_found", "wanda_cinema_not_unique"}:
                raise
            cinema, showtime_payload, showtime, _ = await self._resolve_cinema_by_showtime(
                settings, account, recognition, quote_date, cached_showtimes=cached_showtimes,
            )
        else:
            cinema_id = str(cinema["cinema_id"])
            showtime_payload = await self._official_get(
                account,
                CINEMA_ORIGIN,
                "/showtime/by_cinema.api",
                [("cinemaId", cinema_id), ("showDate", quote_date.replace("-", "")), ("json", "true")],
                channel=H5_CHANNEL,
                event="wanda_showtimes_response",
            )
            cached_showtimes[cinema_id] = showtime_payload
            if int(cinema.get("match_score") or 0) < 90:
                cinema, showtime_payload, showtime, _ = await self._resolve_cinema_by_showtime(
                    settings, account, recognition, quote_date, cached_showtimes=cached_showtimes,
                )
            else:
                try:
                    showtime, _ = self._match_showtime(showtime_payload, recognition, quote_date)
                except ProviderError:
                    if not city_is_unverified:
                        raise
                    cinema, showtime_payload, showtime, _ = await self._resolve_cinema_by_showtime(
                        settings, account, recognition, quote_date, cached_showtimes=cached_showtimes,
                    )
        showtime_id = str(showtime.get("showtimeId") or showtime.get("id") or "").strip()
        if not showtime_id:
            raise ProviderError("wanda_showtime_invalid", "万达官方场次缺少场次 ID。")
        realtime_payload = await self._official_get(
            account,
            FRONT_ORIGIN,
            "/order/real_time_seat.api",
            [("dId", showtime_id)],
            channel=APP_CHANNEL,
            event="wanda_realtime_seats_response",
        )
        seats = self._seat_facts(realtime_payload, self._area_prices(showtime))
        requested_row = row_no
        if requested_row is None and seat_preference:
            match = re.search(r"(?:第\s*)?(\d{1,2})\s*排", str(seat_preference))
            if match:
                requested_row = int(match.group(1))
        available = [
            {
                "seat_number": str(seat["label"]),
                "row_no": int(seat["row"]),
                "col_no": int(seat["column"]),
                "status": "AVAILABLE",
                "seat_zone_type": "W+",
            }
            for seat in seats
            if seat.get("available") is True
            and seat.get("wplus") is True
            and (requested_row is None or int(seat["row"]) == requested_row)
        ]
        available.sort(key=lambda seat: (seat["row_no"], seat["col_no"]))
        return {
            "ok": True,
            "cinema_id": str(cinema["cinema_id"]),
            "show_id": showtime_id,
            "date": quote_date,
            "showtime_start": recognition.showtime_start,
            "seat_zone_type": "W+",
            "requested_row": requested_row,
            "seat_preference": str(seat_preference or "").strip() or None,
            "available_seats": available[:100],
            "available_count": len(available),
            "buyer_guidance": (
                f"已查询到当前第{requested_row}排可售W+座位："
                + "、".join(seat["seat_number"] for seat in available[:100])
                + "。这些位置仅用于核实价格和人工处理；已知张数不要重复询问。"
                if available and requested_row is not None
                else (
                    "已查询到当前可售W+座位，但没有匹配到该排；请买家重新说明排数或在截图上标记位置。"
                    if requested_row is not None
                    else "已查询当前可售W+座位，请买家确认位置和张数。"
                )
            ),
        }

    async def quote(
        self, recognition: MovieImageInfo, *, wanda_cinema_id: str | None = None,
    ) -> RealQuote:
        started = time.perf_counter()
        settings = self._settings_provider()
        rules = PricingRulesUpdate.model_validate(self._pricing_rules_provider())
        account = self._fixed_account(settings)
        quote_date = self._resolved_date(recognition)
        if not quote_date or not recognition.showtime_start:
            raise ProviderError(
                "wanda_quote_identity_incomplete",
                "直接查询万达需要完整的日期和开场时间。",
            )
        self._validate_quote_datetime(recognition, quote_date)
        cached_showtimes: dict[str, Mapping[str, Any]] = {}
        city_is_unverified = not recognition.city or "city" in recognition.missing_fields
        try:
            cinema = await self._resolve_cinema(
                settings, recognition, preferred_cinema_id=wanda_cinema_id,
            )
        except ProviderError as error:
            if error.code not in {"wanda_cinema_not_found", "wanda_cinema_not_unique"}:
                raise
            cinema, showtime_payload, showtime, official_movie = await self._resolve_cinema_by_showtime(
                settings, account, recognition, quote_date, cached_showtimes=cached_showtimes,
            )
        else:
            cinema_id = str(cinema["cinema_id"])
            showtime_payload = await self._official_get(
                account,
                CINEMA_ORIGIN,
                "/showtime/by_cinema.api",
                [("cinemaId", cinema_id), ("showDate", quote_date.replace("-", "")), ("json", "true")],
                channel=H5_CHANNEL,
                event="wanda_showtimes_response",
            )
            cached_showtimes[cinema_id] = showtime_payload
            if int(cinema.get("match_score") or 0) < 90:
                cinema, showtime_payload, showtime, official_movie = await self._resolve_cinema_by_showtime(
                    settings, account, recognition, quote_date, cached_showtimes=cached_showtimes,
                )
            else:
                try:
                    showtime, official_movie = self._match_showtime(showtime_payload, recognition, quote_date)
                except ProviderError:
                    if not city_is_unverified:
                        raise
                    cinema, showtime_payload, showtime, official_movie = await self._resolve_cinema_by_showtime(
                        settings, account, recognition, quote_date, cached_showtimes=cached_showtimes,
                    )
        cinema_id = str(cinema["cinema_id"])
        showtime_id = str(showtime.get("showtimeId") or showtime.get("id") or "").strip()
        if not showtime_id:
            raise ProviderError("wanda_showtime_invalid", "万达官方场次缺少场次 ID。")
        realtime_payload = await self._official_get(
            account,
            FRONT_ORIGIN,
            "/order/real_time_seat.api",
            [("dId", showtime_id)],
            channel=APP_CHANNEL,
            event="wanda_realtime_seats_response",
        )
        area_prices = self._area_prices(showtime)
        seats = self._seat_facts(realtime_payload, area_prices)
        vip_pricing = self._is_vip_showtime(showtime)
        ticket_count = len(recognition.selected_seats) if recognition.selected_seats else None
        if recognition.selected_seats:
            # An explicit X排Y座 is authoritative. Quote that seat's official
            # area and never replace a regular/other-area selection with an
            # unrelated W+ area merely because the showtime also has W+ seats.
            selected = self._selected_seat_facts(recognition, seats)
            unavailable = [str(seat["label"]) for seat in selected if not seat["available"]]
            if unavailable:
                same_type_reference = await self._unavailable_same_type_reference(
                    selected, unavailable, account=account, cinema_id=cinema_id,
                    showtime_id=showtime_id, seats=seats, vip_pricing=vip_pricing,
                )
                if same_type_reference is not None:
                    raise ProviderError(
                        "wanda_selected_seat_unavailable_same_type_reference",
                        same_type_reference,
                    )
                raise ProviderError(
                    "wanda_selected_seat_unavailable",
                    f"{'、'.join(unavailable)}当前不可选，请刷新选座截图后重试。",
                )
            member_prices: dict[tuple[str, int, int], int | None] = {}
            for seat in selected:
                key = (str(seat["area_id"]), int(seat["price"]), int(seat["channel_fee"]))
                if key in member_prices:
                    continue
                uses_wplus_pricing = bool(seat.get("wplus_pricing_eligible", seat.get("wplus")))
                if vip_pricing:
                    # A VIP hall has no ordinary member price in this API. It
                    # therefore enters the universal Wanda rule at a 100%
                    # discount ratio (official salesPrice as the reference).
                    member_price = None
                elif uses_wplus_pricing and rules.wplus_friday_member_day_enabled:
                    member_price = self._positive_int(seat.get("wplus_member_price"))
                elif rules.wplus_friday_member_day_enabled:
                    member_price = self._positive_int(seat.get("friday_member_price"))
                    if member_price is None:
                        member_price = self._positive_int(seat.get("regular_member_price"))
                else:
                    member_price = self._positive_int(seat.get("regular_member_price"))
                if member_price is None and uses_wplus_pricing and not vip_pricing:
                    member_price = await self._probe_member_price(
                        account,
                        cinema_id=cinema_id,
                        showtime_id=showtime_id,
                        seat=seat,
                    )
                member_prices[key] = member_price
            if not vip_pricing and any(value is None for value in member_prices.values()):
                raise ProviderError(
                    "wanda_regular_member_price_unavailable",
                    "万达官方场次未返回所选座位的实时会员价，无法安全计算报价。",
                )
            result = self._exact_quote(
                selected, member_prices, cinema, official_movie, vip_pricing=vip_pricing,
            )
        else:
            wplus_area = self._available_wplus_area_reference(area_prices, seats)
            if wplus_area is None:
                raise ProviderError(
                    "wanda_wplus_seat_unavailable",
                    "当前场次会员区域无可选座位",
                )
            member_price = (
                wplus_area.get("member_price")
                if rules.wplus_friday_member_day_enabled
                else wplus_area.get("regular_member_price")
            )
            probe_used = False
            if member_price is None and rules.wplus_friday_member_day_enabled:
                probe_seat = self._wplus_probe_seat(wplus_area, seats)
                member_price = await self._probe_member_price(
                    account,
                    cinema_id=cinema_id,
                    showtime_id=showtime_id,
                    seat=probe_seat,
                )
                probe_used = True
            result = self._middle_wplus_quote(
                wplus_area, member_price, cinema, official_movie,
                ticket_count=ticket_count, probe_used=probe_used,
            )
        matched_date, matched_start = self._showtime_start(showtime)
        matched_end = self._showtime_end(showtime, matched_date, matched_start)
        result = result.model_copy(update={
            "quote_date": date.fromisoformat(quote_date),
            "matched_movie_name": official_movie or None,
            "matched_showtime_start": matched_start or None,
            "matched_showtime_end": matched_end or None,
            "matched_hall_name": str(showtime.get("hallName") or "").strip() or None,
        })
        elapsed = round((time.perf_counter() - started) * 1000)
        if result.pricing_rule_version:
            self._diagnostics.add(
                "pricing_rules_applied",
                rule_version=result.pricing_rule_version,
                quote_scope=result.quote_scope,
            )
        self._diagnostics.add(
            "wanda_direct_quote_completed",
            duration_ms=elapsed,
            scope=result.quote_scope,
            matched_cinema_name=result.matched_cinema_name,
            showtime_id=showtime_id,
        )
        LOGGER.info(
            "event=wanda_direct_quote_completed scope=%s duration_ms=%d",
            result.quote_scope,
            elapsed,
        )
        return result.model_copy(update={"timings_ms": {"total": elapsed}})

    def _fixed_account(self, settings: Settings) -> dict[str, Any]:
        path = Path(settings.wanda_account_pool_path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            raise ProviderError(
                "wanda_account_pool_unavailable",
                "无法读取万达登录账号，请先在票务系统登录固定账号。",
            ) from None
        accounts = payload.get("accounts") if isinstance(payload, Mapping) else payload
        if not isinstance(accounts, list):
            raise ProviderError("wanda_account_pool_invalid", "万达账号文件格式无效。")
        candidates = [
            item for item in accounts
            if isinstance(item, dict)
            and str(item.get("status") or "").lower() == "online"
            and str(item.get("token") or "").strip()
            and str(item.get("phone") or "").strip()
            and self._account_has_wplus(item)
        ]
        wanted = settings.wanda_fixed_account_phone.strip() or self._selected_phone
        if wanted:
            candidates = [item for item in candidates if str(item.get("phone") or "").strip() == wanted]
        if not candidates:
            raise ProviderError(
                "wanda_fixed_account_unavailable",
                "固定万达W+账号未登录、会员已失效或账号不可用，请先在票务系统检查。",
            )
        account = candidates[0]
        self._selected_phone = str(account["phone"])
        return account

    @staticmethod
    def _account_has_wplus(account: Mapping[str, Any]) -> bool:
        user_info = account.get("user_info") if isinstance(account.get("user_info"), Mapping) else {}
        try:
            wplus_type = int(user_info.get("wplusType"))
        except (TypeError, ValueError):
            wplus_type = 0
        active_flag = (
            user_info.get("isPayMember") is True
            or wplus_type > 0
            or account.get("is_wplus") is True
            or str(account.get("account_type") or "").lower() == "wplus"
        )
        if not active_flag:
            return False
        expiry_text = str(account.get("wplus_end_date") or user_info.get("payMemberStr") or "")
        match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", expiry_text)
        if match:
            try:
                expiry = date(*(int(value) for value in match.groups()))
                if expiry < datetime.now(ZoneInfo("Asia/Shanghai")).date():
                    return False
            except ValueError:
                pass
        return True

    async def canonical_city_hint(self, value: str) -> str | None:
        """Resolve an official city prefix from a city, district, or city+venue clarification."""
        wanted = self._normalize_name(value).removesuffix("市")
        if len(wanted) < 2:
            return None
        cache_path = Path(self._settings_provider().wanda_cinema_cache_path)
        try:
            with sqlite3.connect(f"file:{cache_path.as_posix()}?mode=ro", uri=True, timeout=3) as connection:
                cities = [str(row[0] or "").strip() for row in connection.execute("SELECT DISTINCT city_name FROM cinemas")]
        except sqlite3.Error:
            return None
        exact = [city for city in cities if self._normalize_name(city).removesuffix("市") == wanted]
        if len(exact) == 1:
            return exact[0]
        venue_hint = any(marker in wanted for marker in ("万达", "影城", "影院", "广场", "店"))
        max_suffix_length = 30 if venue_hint else 5
        prefixed = [
            city for city in cities
            if wanted.startswith(self._normalize_name(city).removesuffix("市"))
            and 1 <= len(wanted) - len(self._normalize_name(city).removesuffix("市")) <= max_suffix_length
        ]
        return prefixed[0] if len(prefixed) == 1 else None

    async def is_known_city_hint(self, value: str) -> bool:
        """Accept only city clarifications that uniquely map to the official cinema cache."""
        return await self.canonical_city_hint(value) is not None

    async def quote_mapped(
        self, recognition: MovieImageInfo, *, wanda_cinema_id: str,
    ) -> RealQuote:
        """Quote using an explicit local Wanda ID from the cinema route mapping."""
        return await self.quote(recognition, wanda_cinema_id=wanda_cinema_id)

    async def match_cached_cinema(self, recognition: MovieImageInfo) -> dict[str, Any] | None:
        """Return a unique match in the local Wanda capability catalog, if any."""
        try:
            return await self._resolve_cinema(self._settings_provider(), recognition)
        except ProviderError as error:
            if error.code in {"wanda_cinema_not_found", "wanda_cinema_not_unique"}:
                return None
            raise

    async def complete_cinema(self, recognition: MovieImageInfo) -> MovieImageInfo:
        """Complete a uniquely matched truncated cinema name from the official cache."""
        candidate = recognition
        if not recognition.city or "city" in recognition.missing_fields:
            inferred_city = await self.canonical_city_hint(recognition.cinema_name or "")
            if inferred_city is not None:
                candidate = recognition.model_copy(update={
                    "city": inferred_city,
                    "missing_fields": [field for field in recognition.missing_fields if field != "city"],
                })
            elif "city" in recognition.missing_fields:
                return recognition
        matched = await self._resolve_cinema(self._settings_provider(), candidate)
        return candidate.model_copy(update={
            "cinema_name": matched["cinema_name"],
            "city": candidate.city or matched["city_name"],
        })

    async def _resolve_cinema(
        self, settings: Settings, recognition: MovieImageInfo,
        *, preferred_cinema_id: str | None = None,
    ) -> dict[str, Any]:
        wanted = self._normalize_name(recognition.cinema_name or "")
        wanted_alias = self._cinema_match_key(recognition.cinema_name or "")
        if not wanted:
            raise ProviderError("wanda_cinema_missing", "直接查询万达需要完整影院名称。")
        cache_path = Path(settings.wanda_cinema_cache_path)
        try:
            with sqlite3.connect(f"file:{cache_path.as_posix()}?mode=ro", uri=True, timeout=3) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT cinema_id, city_name, cinema_name, address FROM cinemas"
                ).fetchall()
        except sqlite3.Error:
            raise ProviderError(
                "wanda_cinema_cache_unavailable",
                "无法读取万达官方影院缓存，请先在票务系统刷新影院列表。",
            ) from None
        preferred = str(preferred_cinema_id or "").strip()
        if preferred:
            matches = [row for row in rows if str(row["cinema_id"] or "").strip() == preferred]
            if len(matches) != 1:
                raise ProviderError("wanda_cinema_mapping_not_found", "本地万达影院映射不存在或已失效。")
            row = matches[0]
            return {
                "cinema_id": str(row["cinema_id"]),
                "cinema_name": str(row["cinema_name"]),
                "city_name": str(row["city_name"]),
                "match_score": 100,
            }

        wanted_city = self._normalize_name(recognition.city or "")
        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            cinema_name = str(row["cinema_name"] or "")
            actual = self._normalize_name(cinema_name)
            actual_alias = self._cinema_match_key(cinema_name)
            # Some Liangpiao canonical names omit the city prefix while the
            # local Wanda cache includes it (e.g. “万达寰映影城（振华广场杜比
            # 影院店）” vs “呼和浩特寰映影城振华广场店”). Use the recognized
            # city as additional geography when comparing both aliases.
            row_city = str(row["city_name"] or "")
            city_context = row_city if any("\u4e00" <= char <= "\u9fff" for char in row_city) else (recognition.city or "")
            candidate_wanted_alias = self._cinema_candidate_match_key(
                recognition.cinema_name or "", city_context, str(row["address"] or "")
            )
            candidate_actual_alias = self._cinema_candidate_match_key(
                cinema_name, city_context, str(row["address"] or "")
            )
            address_identity_tokens = self._cinema_address_identity_tokens(str(row["address"] or ""))
            city = self._normalize_name(str(row["city_name"] or ""))
            # Synthetic cache fixtures and some provider records use opaque
            # city codes (for example ``city-bj``); they are not comparable to
            # a recognized Chinese city name and must not reject an otherwise
            # exact cinema match.
            if (
                wanted_city and city
                and any("\u4e00" <= char <= "\u9fff" for char in city)
                and wanted_city not in city and city not in wanted_city
            ):
                continue
            if actual == wanted:
                score = 100
            elif actual_alias and actual_alias == wanted_alias:
                score = 95
            elif candidate_actual_alias and candidate_actual_alias == candidate_wanted_alias:
                score = 90
            elif (
                candidate_wanted_alias in address_identity_tokens
                and self._is_distinctive_venue_identity(candidate_wanted_alias)
            ):
                # An exact venue identity such as “超极合生汇” is more
                # specific than a shorter overlapping identity like “合生汇”.
                # Treat it as strong cinema evidence; quote() still verifies
                # the movie, date and time inside this exact cinema.
                score = 90
            elif any(
                self._address_identity_matches_query(candidate_wanted_alias, token)
                for token in address_identity_tokens
            ):
                score = 85
            elif min(len(actual), len(wanted)) >= 4 and (actual in wanted or wanted in actual):
                score = 70
            elif min(len(actual_alias), len(wanted_alias)) >= 4 and (
                actual_alias in wanted_alias or wanted_alias in actual_alias
            ):
                score = 65
            elif (
                len(wanted_alias) >= 2
                and wanted_alias not in {"万达", "广场", "影城", "影院", "电影", "中心", "国际"}
                and wanted_alias in actual_alias
            ):
                score = 55
            else:
                score = 0
            if score:
                scored.append((score, row))
        if not scored:
            raise ProviderError("wanda_cinema_not_found", "影院无法在万达官方影院缓存中匹配。")
        highest = max(score for score, _ in scored)
        winners = [row for score, row in scored if score == highest]
        unique_ids = {str(row["cinema_id"]) for row in winners}
        if len(unique_ids) != 1:
            raise ProviderError("wanda_cinema_not_unique", "影院名称匹配到多家门店，请补充城市或完整店名。")
        row = winners[0]
        return {
            "cinema_id": str(row["cinema_id"]),
            "cinema_name": str(row["cinema_name"]),
            "city_name": str(row["city_name"]),
            "match_score": highest,
        }

    async def _resolve_cinema_by_showtime(
        self,
        settings: Settings,
        account: Mapping[str, Any],
        recognition: MovieImageInfo,
        quote_date: str,
        *,
        cached_showtimes: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], Mapping[str, Any], dict[str, Any], str]:
        """Resolve a weak cinema alias by uniquely matching authoritative showtimes in parallel."""
        wanted_city = self._normalize_name(recognition.city or "").removesuffix("市")
        has_verified_city = len(wanted_city) >= 2 and "city" not in recognition.missing_fields
        cache_path = Path(settings.wanda_cinema_cache_path)
        try:
            with sqlite3.connect(f"file:{cache_path.as_posix()}?mode=ro", uri=True, timeout=3) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT cinema_id, city_name, cinema_name, address FROM cinemas"
                ).fetchall()
        except sqlite3.Error:
            raise ProviderError(
                "wanda_cinema_cache_unavailable",
                "无法读取万达官方影院缓存，请先在票务系统刷新影院列表。",
            ) from None
        city_rows = [
            row for row in rows
            if has_verified_city
            and self._normalize_name(str(row["city_name"] or "")).removesuffix("市") == wanted_city
        ]
        # Platforms often display a county-level venue as the city (for example
        # 义乌), while Wanda's official cache groups it under a prefecture-level
        # city (for example 金华). A city absent from the official cache is not an
        # authoritative constraint; keep the venue alias and use bounded
        # official-showtime verification across matching cinemas instead.
        if has_verified_city and not city_rows:
            has_verified_city = False

        wanted_alias = self._cinema_match_key(recognition.cinema_name or "")
        if has_verified_city:
            wanted_alias = wanted_alias.replace(wanted_city, "")
        generic_aliases = {"", "万达", "万达广场", "广场", "影城", "影院", "电影"}
        signaled: list[sqlite3.Row] = []
        for row in city_rows if has_verified_city else rows:
            cinema_name = str(row["cinema_name"] or "")
            actual_alias = self._cinema_cross_city_match_key(
                cinema_name, str(row["city_name"] or "")
            )
            actual_full_alias = self._cinema_match_key(cinema_name)
            street_match = any(
                self._address_identity_matches_query(wanted_alias, token)
                for token in self._cinema_address_identity_tokens(str(row["address"] or ""))
            )
            name_overlap = bool(
                wanted_alias not in generic_aliases
                and actual_alias not in generic_aliases
                and min(len(wanted_alias), len(actual_alias)) >= 2
                and wanted_alias in actual_alias
            )
            full_name_overlap = bool(
                not has_verified_city
                and wanted_alias not in generic_aliases
                and actual_full_alias not in generic_aliases
                and min(len(wanted_alias), len(actual_full_alias)) >= 2
                and wanted_alias in actual_full_alias
            )
            if street_match or name_overlap or full_name_overlap:
                signaled.append(row)
        candidates = signaled or (city_rows if wanted_alias in generic_aliases else [])
        if not candidates:
            raise ProviderError(
                "wanda_cinema_not_found",
                "缺少城市且影院别名无法形成有限候选，不能安全跨城市核对场次。",
            )
        if len(candidates) > 12:
            raise ProviderError(
                "wanda_cinema_candidate_set_too_large",
                "影院别名候选过多，无法安全并发核对场次；请补充完整影院名称。",
            )

        semaphore = asyncio.Semaphore(3)
        cached = dict(cached_showtimes or {})

        async def inspect(row: sqlite3.Row) -> tuple[dict[str, Any], Mapping[str, Any], dict[str, Any], str] | None:
            cinema_id = str(row["cinema_id"])
            try:
                async with semaphore:
                    payload = cached.get(cinema_id)
                    if payload is None:
                        payload = await self._official_get(
                            account,
                            CINEMA_ORIGIN,
                            "/showtime/by_cinema.api",
                            [("cinemaId", cinema_id), ("showDate", quote_date.replace("-", "")), ("json", "true")],
                            channel=H5_CHANNEL,
                            event="wanda_candidate_showtimes_response",
                        )
                showtime, official_movie = self._match_showtime(payload, recognition, quote_date)
            except ProviderError:
                return None
            return ({
                "cinema_id": cinema_id,
                "cinema_name": str(row["cinema_name"]),
                "city_name": str(row["city_name"]),
                "match_score": 0,
            }, payload, showtime, official_movie)

        resolved = [item for item in await asyncio.gather(*(inspect(row) for row in candidates)) if item is not None]
        unique = {item[0]["cinema_id"]: item for item in resolved}
        self._diagnostics.add(
            "wanda_cinema_candidates_checked",
            candidate_count=len(candidates),
            showtime_match_count=len(unique),
        )
        if len(unique) == 1:
            result = next(iter(unique.values()))
            self._diagnostics.add(
                "wanda_cinema_resolved_by_showtime",
                matched_cinema_name=result[0]["cinema_name"],
            )
            return result
        if not unique:
            raise ProviderError(
                "wanda_cinema_showtime_not_found",
                "候选影院均没有唯一匹配截图中的影片、日期和开场时间。",
            )
        raise ProviderError(
            "wanda_cinema_showtime_not_unique",
            "多家候选影院都有相同影片和场次，无法安全确定影院；请补充完整影院名称。",
        )

    async def _official_get(
        self,
        account: Mapping[str, Any],
        origin: str,
        path: str,
        pairs: list[tuple[str, Any]],
        *,
        channel: str,
        event: str,
    ) -> Mapping[str, Any]:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or path not in {
                "/showtime/by_cinema.api", "/order/real_time_seat.api"
            }
        ):
            raise ProviderError("wanda_official_origin_forbidden", "万达官方接口地址未通过安全校验。")
        query = urlencode(pairs)
        target = f"{path}?{query}" if query else path
        timestamp = int(time.time() * 1000)
        client_key = APP_CLIENT_KEY if channel == APP_CHANNEL else H5_CLIENT_KEY
        check = hashlib.md5(
            f"{SALE_SUBJECT_CODE}{channel}{client_key}{timestamp}{target}".encode()
        ).hexdigest()
        token = str(account.get("token") or "").strip()
        user_info = account.get("user_info") if isinstance(account.get("user_info"), Mapping) else {}
        user_identifier = str(user_info.get("userIdentifier") or "").strip()
        shumei = str(account.get("shumei_box_id") or "").strip()
        if channel == APP_CHANNEL:
            mx: dict[str, Any] = {
                "ver": APP_VERSION, "sCode": SALE_SUBJECT_CODE, "_mi_": token,
                "width": 1080, "json": True, "cCode": channel, "check": check,
                "ts": timestamp, "height": 2244, "appId": 2, "model": "meizu 17",
                "systemVersion": "11",
            }
            if shumei:
                mx["ShumeiBoxId"] = shumei
            headers = {
                "User-Agent": "okhttp/4.12.0", "X-RY-VERSION": APP_VERSION,
                "X-RY-MODEL": "meizu 17", "X-RY-SYSTEM-VER": "11",
            }
        else:
            mx = {
                "ver": "7.0.0", "sCode": SALE_SUBJECT_CODE, "_mi_": token,
                "width": 1280, "json": "true", "cCode": channel, "check": check,
                "ts": timestamp, "heigth": 720, "appId": 3,
                "model": "iPhone8,1", "systemVersion": "15.8.8",
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_8 like Mac OS X) AppleWebKit/605.1.15",
                "Origin": "https://m.wandacinemas.com", "Referer": "https://m.wandacinemas.com/",
            }
        headers.update({
            "Content-Type": "application/x-www-form-urlencoded",
            "MX-API": json.dumps(mx, separators=(",", ":")),
            "X-RY-CHECK": check,
            "X-RY-CHANNEL": channel,
            "X-RY-TIMESTAMP": str(timestamp),
            "X-RY-TOKEN": token,
        })
        if user_identifier:
            headers["X-RY-USER"] = user_identifier
        if shumei and channel == APP_CHANNEL:
            headers["ShumeiBoxId"] = shumei
        started = time.perf_counter()

        async def fetch_payload() -> Any:
            response = await self._client.get(f"{origin}{target}", headers=headers)
            response.raise_for_status()
            return response.json()

        tasks: set[asyncio.Task[Any]] = {asyncio.create_task(fetch_payload())}
        hedged = False
        errors: list[BaseException] = []
        try:
            if path == "/showtime/by_cinema.api":
                done, _ = await asyncio.wait(tasks, timeout=self._showtime_hedge_delay_seconds)
                if not done:
                    tasks.add(asyncio.create_task(fetch_payload()))
                    hedged = True
            while tasks:
                done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        payload = task.result()
                    except BaseException as error:
                        errors.append(error)
                        continue
                    for pending in tasks:
                        pending.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks.clear()
                    break
                else:
                    continue
                break
            else:
                raise errors[-1]
        except httpx.TimeoutException as error:
            raise ProviderError("wanda_official_timeout", "万达官方接口响应超时。") from error
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise ProviderError("wanda_official_unavailable", "暂时无法连接万达官方接口。") from error
        finally:
            for pending in tasks:
                pending.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        if hedged:
            self._diagnostics.add("wanda_showtimes_hedged_request")
        if not isinstance(payload, Mapping):
            raise ProviderError("wanda_official_response_invalid", "万达官方接口返回格式无效。")
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        self._diagnostics.add(event, duration_ms=duration_ms, response=payload)
        code = payload.get("code")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        biz_code = data.get("bizCode")
        if code not in (0, "0") or biz_code not in (None, 0, "0"):
            message = str(payload.get("msg") or payload.get("message") or data.get("bizMsg") or "")
            if any(marker in message for marker in ("登录", "token", "Token", "认证")) or code in (401, "401"):
                raise ProviderError("wanda_account_login_expired", "固定万达账号登录已失效，请重新登录。")
            raise ProviderError("wanda_official_business_failed", "万达官方接口未返回成功结果。")
        return payload

    async def _probe_member_price(
        self,
        account: Mapping[str, Any],
        *,
        cinema_id: str,
        showtime_id: str,
        seat: Mapping[str, Any],
    ) -> int:
        if not ProbePolicy.context_allows_active_probe():
            raise ProviderError("QUOTE_REQUIRES_ACTIVE_PROBE", "实时会员成本缺失，需要独立 Active Probe。")
        seat_id = str(seat.get("seat_id") or "").strip()
        area_id = str(seat.get("area_id") or "").strip()
        original_price = int(seat.get("price") or 0)
        channel_fee = int(seat.get("channel_fee") or 0)
        if not seat_id or not area_id or original_price <= 0:
            raise ProviderError("wanda_member_probe_invalid", "实时座位缺少锁座核价所需字段。")
        lock = self._showtime_locks.setdefault(showtime_id, asyncio.Lock())
        async with lock:
            order_id = ""
            probe_error: BaseException | None = None
            member_price: int | None = None
            released = False
            try:
                created = await self._official_app_request(
                    account,
                    FRONT_ORIGIN,
                    "/order/create_order.api",
                    method="POST",
                    pairs=[
                        ("retailerCode", "MX"),
                        ("mobile", str(account.get("phone") or "")),
                        ("seatId", f"{seat_id},{original_price},{channel_fee},0"),
                        ("totalPrice", original_price),
                        ("dId", showtime_id),
                    ],
                    sign_encoded=True,
                    event="wanda_temporary_order_response",
                )
                data = created.get("data") if isinstance(created.get("data"), Mapping) else {}
                order_id = str(data.get("orderId") or created.get("orderId") or "").strip()
                if not order_id:
                    raise ProviderError("wanda_temporary_lock_failed", "万达临时锁座失败，未生成订单。")
                if created.get("code") not in (0, "0") or data.get("bizCode") not in (0, "0"):
                    raise ProviderError("wanda_temporary_lock_state_unknown", "万达临时锁座状态不明确。")
                locked = False
                for attempt, delay in enumerate(self._lock_status_retry_delays, start=1):
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        status_payload = await self._official_app_request(
                            account,
                            FRONT_ORIGIN,
                            "/order/order_status.api",
                            method="POST",
                            pairs=[("json", "true"), ("orderId", order_id)],
                            event="wanda_temporary_order_status_response",
                            timeout_seconds=self._order_status_timeout_seconds,
                        )
                    except ProviderError as error:
                        if error.code not in {"wanda_official_timeout", "wanda_official_unavailable"}:
                            raise
                        self._diagnostics.add(
                            "wanda_order_status_short_retry",
                            phase="lock",
                            attempt=attempt,
                            code=error.code,
                            timeout_ms=round(self._order_status_timeout_seconds * 1000),
                        )
                        continue
                    status_data = status_payload.get("data") if isinstance(status_payload.get("data"), Mapping) else {}
                    try:
                        lock_time = int(status_data.get("lockSeatTime"))
                    except (TypeError, ValueError):
                        lock_time = -1
                    locked = str(status_data.get("orderStatus") or "") == "40" and lock_time >= 0
                    if locked:
                        break
                if not locked:
                    raise ProviderError("wanda_temporary_lock_state_unknown", "万达未确认临时锁座状态。")
                offers = await self._official_app_request(
                    account,
                    MARKETING_ORIGIN,
                    "/mkt/activity/secret/list.api",
                    method="GET",
                    pairs=[
                        ("partition", f"{area_id}-{seat_id}"),
                        ("orderId", order_id),
                        ("did", showtime_id),
                    ],
                    event="wanda_member_offers_response",
                )
                member_price = self._wplus_offer_price(offers)
            except BaseException as error:
                probe_error = error
            finally:
                if order_id:
                    try:
                        released = await asyncio.shield(
                            self._cancel_and_verify_release(account, order_id, showtime_id, seat_id)
                        )
                    except BaseException:
                        released = False
            if order_id and not released:
                raise ProviderError(
                    "wanda_temporary_lock_release_unverified",
                    "临时锁座已尝试取消，但座位尚未确认恢复；本次不返回报价。",
                )
            if probe_error is not None:
                raise probe_error
            if member_price is None:
                raise ProviderError("wanda_wplus_offer_unavailable", "未找到唯一可用的W+会员专享优惠价。")
            return member_price

    async def _cancel_and_verify_release(
        self,
        account: Mapping[str, Any],
        order_id: str,
        showtime_id: str,
        seat_id: str,
    ) -> bool:
        cancelled = await self._official_app_request(
            account,
            FRONT_ORIGIN,
            "/order/cancel.api",
            method="POST",
            pairs=[("orderId", order_id)],
            event="wanda_temporary_cancel_response",
        )
        cancel_data = cancelled.get("data") if isinstance(cancelled.get("data"), Mapping) else {}
        if cancelled.get("code") not in (0, "0", None) or cancel_data.get("bizCode") not in (0, "0", None):
            return False
        order_cancelled = False
        seat_released = False
        for index, delay in enumerate(self._release_recheck_delays):
            if index and delay:
                await asyncio.sleep(delay)
            if not order_cancelled:
                try:
                    status_payload = await self._official_app_request(
                        account,
                        FRONT_ORIGIN,
                        "/order/order_status.api",
                        method="POST",
                        pairs=[("json", "true"), ("orderId", order_id)],
                        event="wanda_cancelled_order_status_response",
                        timeout_seconds=self._order_status_timeout_seconds,
                    )
                    status_data = status_payload.get("data") if isinstance(status_payload.get("data"), Mapping) else {}
                    try:
                        lock_time = int(status_data.get("lockSeatTime"))
                    except (TypeError, ValueError):
                        lock_time = 0
                    order_cancelled = (
                        str(status_data.get("orderStatus") or "") == "60"
                        and lock_time == -1
                    )
                except ProviderError as error:
                    self._diagnostics.add(
                        "wanda_cancel_status_recheck_failed",
                        code=error.code,
                        attempt=index + 1,
                    )
            try:
                realtime = await self._official_get(
                    account,
                    FRONT_ORIGIN,
                    "/order/real_time_seat.api",
                    [("dId", showtime_id)],
                    channel=APP_CHANNEL,
                    event="wanda_release_recheck_response",
                )
                seat_released = self._seat_id_available(realtime, seat_id)
            except ProviderError as error:
                self._diagnostics.add(
                    "wanda_release_recheck_failed",
                    code=error.code,
                    attempt=index + 1,
                )
            self._diagnostics.add(
                "wanda_release_verification",
                attempt=index + 1,
                order_cancelled=order_cancelled,
                seat_released=seat_released,
            )
            if order_cancelled and seat_released:
                return True
        return False

    async def _official_app_request(
        self,
        account: Mapping[str, Any],
        origin: str,
        path: str,
        *,
        method: str,
        pairs: list[tuple[str, Any]],
        event: str,
        sign_encoded: bool = False,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        parsed = urlsplit(origin)
        allowed_paths = {
            "/order/create_order.api", "/order/order_status.api", "/order/cancel.api",
            "/mkt/activity/secret/list.api",
        }
        if (
            parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or path not in allowed_paths
            or method not in {"GET", "POST"}
        ):
            raise ProviderError("wanda_official_origin_forbidden", "万达官方接口地址未通过安全校验。")
        timestamp = int(time.time() * 1000)
        content: str | None = None
        if method == "GET":
            query = urlencode(pairs)
            target = f"{path}?{query}" if query else path
            sign_target = target
        else:
            raw_body = "&".join(f"{key}={value}" for key, value in pairs)
            encoded_body = "&".join(f"{key}={self._lowercase_quote(str(value))}" for key, value in pairs)
            sign_body = encoded_body if sign_encoded else raw_body
            sign_target = path + sign_body
            target = path
            content = encoded_body
        check = hashlib.md5(
            f"{SALE_SUBJECT_CODE}{APP_CHANNEL}{APP_CLIENT_KEY}{timestamp}{sign_target}".encode()
        ).hexdigest()
        token = str(account.get("token") or "").strip()
        user_info = account.get("user_info") if isinstance(account.get("user_info"), Mapping) else {}
        shumei = str(account.get("shumei_box_id") or "").strip()
        mx: dict[str, Any] = {
            "ver": APP_VERSION, "sCode": SALE_SUBJECT_CODE, "_mi_": token,
            "width": 1080, "json": True, "cCode": APP_CHANNEL, "check": check,
            "ts": timestamp, "height": 2244, "appId": 2, "model": "meizu 17",
            "systemVersion": "11",
        }
        if shumei:
            mx["ShumeiBoxId"] = shumei
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "okhttp/4.12.0",
            "MX-API": json.dumps(mx, separators=(",", ":")),
            "X-RY-CHECK": check, "X-RY-CHANNEL": APP_CHANNEL,
            "X-RY-TIMESTAMP": str(timestamp), "X-RY-TOKEN": token,
            "X-RY-VERSION": APP_VERSION, "X-RY-MODEL": "meizu 17",
            "X-RY-SYSTEM-VER": "11",
        }
        user_identifier = str(user_info.get("userIdentifier") or "").strip()
        if user_identifier:
            headers["X-RY-USER"] = user_identifier
        if shumei:
            headers["ShumeiBoxId"] = shumei
        started = time.perf_counter()
        request_options: dict[str, Any] = {}
        if timeout_seconds is not None:
            request_options["timeout"] = timeout_seconds

        async def fetch_payload() -> Any:
            response = await self._client.request(
                method, f"{origin}{target}", content=content, headers=headers,
                **request_options,
            )
            response.raise_for_status()
            return response.json()

        tasks: set[asyncio.Task[Any]] = {asyncio.create_task(fetch_payload())}
        hedged = False
        errors: list[BaseException] = []
        try:
            if method == "GET" and path == "/mkt/activity/secret/list.api":
                done, _ = await asyncio.wait(tasks, timeout=self._showtime_hedge_delay_seconds)
                if not done:
                    tasks.add(asyncio.create_task(fetch_payload()))
                    hedged = True
            while tasks:
                done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        payload = task.result()
                    except BaseException as error:
                        errors.append(error)
                        continue
                    for pending in tasks:
                        pending.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks.clear()
                    break
                else:
                    continue
                break
            else:
                raise errors[-1]
        except httpx.TimeoutException as error:
            raise ProviderError("wanda_official_timeout", "万达官方接口响应超时。") from error
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise ProviderError("wanda_official_unavailable", "暂时无法连接万达官方接口。") from error
        finally:
            for pending in tasks:
                pending.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        if hedged:
            self._diagnostics.add("wanda_member_offers_hedged_request")
        if not isinstance(payload, Mapping):
            raise ProviderError("wanda_official_response_invalid", "万达官方接口返回格式无效。")
        self._diagnostics.add(
            event,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            response=payload,
        )
        return payload

    @classmethod
    def _wplus_offer_price(cls, payload: Mapping[str, Any]) -> int:
        raw_data = payload.get("data")
        decoded: Any = raw_data
        if isinstance(raw_data, str):
            decoded = cls._decrypt_offer_data(raw_data)
        groups = decoded if isinstance(decoded, list) else decoded.get("res", decoded) if isinstance(decoded, Mapping) else []
        candidates: list[int] = []
        for group in groups if isinstance(groups, list) else []:
            items = group.get("groupItems") if isinstance(group, Mapping) else None
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, Mapping) or item.get("able") is not True:
                    continue
                if "W+会员专享" not in str(item.get("name") or ""):
                    continue
                allot = item.get("allotSeat") or item.get("allot_seat")
                if isinstance(allot, str):
                    try:
                        allot = json.loads(allot)
                    except ValueError:
                        allot = None
                if isinstance(allot, Mapping):
                    value = cls._positive_int(allot.get("totalPayPrice"))
                    if value is not None:
                        candidates.append(value)
        unique = set(candidates)
        if len(unique) != 1:
            raise ProviderError("wanda_wplus_offer_unavailable", "未找到唯一可用的W+会员专享优惠价。")
        return next(iter(unique))

    @staticmethod
    def _decrypt_offer_data(value: str) -> Any:
        try:
            encrypted = bytes.fromhex(value)
        except ValueError:
            return None
        for key in (APP_CLIENT_KEY[:16].encode(), APP_AES_KEY):
            try:
                decoded = AES.new(key, AES.MODE_ECB).decrypt(encrypted)
                try:
                    decoded = unpad(decoded, AES.block_size)
                except ValueError:
                    decoded = decoded.rstrip(b"\x00")
                return json.loads(decoded.decode("utf-8"))
            except Exception:
                continue
        return None

    @staticmethod
    def _seat_id_available(payload: Mapping[str, Any], expected: str) -> bool:
        found = False

        def visit(value: Any) -> None:
            nonlocal found
            if found:
                return
            if isinstance(value, Mapping):
                seat_id = str(value.get("seatId") or value.get("seat_id") or "")
                if seat_id == expected and value.get("status") in (1, "1", "可选"):
                    found = True
                    return
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for nested in value:
                    visit(nested)

        visit(payload)
        return found

    @staticmethod
    def _lowercase_quote(value: str) -> str:
        encoded = quote(value, safe="!*'()")
        return re.sub(r"%([0-9A-Fa-f]{2})", lambda item: "%" + item.group(1).lower(), encoded)

    def _match_showtime(
        self,
        payload: Mapping[str, Any],
        recognition: MovieImageInfo,
        quote_date: str,
    ) -> tuple[dict[str, Any], str]:
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        films = data.get("showtimeFilmInf") if isinstance(data, Mapping) else None
        if not isinstance(films, list):
            raise ProviderError("wanda_showtime_response_invalid", "万达官方场次数据格式无效。")
        wanted_movie = self._normalize_name(recognition.movie_name or "")
        wanted_time = recognition.showtime_start or ""
        movie_matches = 0
        date_matches = 0
        time_matches: dict[str, tuple[dict[str, Any], str]] = {}
        near_time_matches: dict[str, tuple[dict[str, Any], str]] = {}
        summaries: list[dict[str, str]] = []
        for film in films:
            if not isinstance(film, Mapping):
                continue
            dates = film.get("showtimeFilmDateInf") or []
            for date_info in dates if isinstance(dates, list) else []:
                if not isinstance(date_info, Mapping):
                    continue
                showtimes_info = date_info.get("showtimesInf")
                showtimes = showtimes_info.get("showtimeList") if isinstance(showtimes_info, Mapping) else []
                for raw in showtimes if isinstance(showtimes, list) else []:
                    if not isinstance(raw, Mapping):
                        continue
                    item = dict(raw)
                    movie_name = self._showtime_movie_name(film, item)
                    actual_movie = self._normalize_name(movie_name)
                    actual_date, actual_time = self._showtime_start(item)
                    if len(summaries) < 30:
                        summaries.append({
                            "movie": movie_name,
                            "date": actual_date,
                            "time": actual_time,
                            "hall": str(item.get("hallName") or ""),
                        })
                    title_matches = bool(
                        actual_movie
                        and (
                            not wanted_movie
                            or actual_movie == wanted_movie
                            or (
                                min(len(actual_movie), len(wanted_movie)) >= 2
                                and (wanted_movie in actual_movie or actual_movie in wanted_movie)
                            )
                        )
                    )
                    if not title_matches:
                        continue
                    movie_matches += 1
                    if actual_date != quote_date:
                        continue
                    date_matches += 1
                    identity = str(item.get("showtimeId") or item.get("id") or "")
                    if not identity:
                        continue
                    if actual_time == wanted_time:
                        time_matches[identity] = (item, movie_name)
                        continue
                    start_delta = self._clock_delta_minutes(actual_time, wanted_time)
                    actual_end = self._showtime_end(item, actual_date, actual_time)
                    end_delta = self._clock_delta_minutes(actual_end, recognition.showtime_end or "")
                    hall_matches = bool(
                        recognition.hall_name
                        and self._hall_matches(recognition.hall_name, str(item.get("hallName") or ""))
                    )
                    if (
                        hall_matches and start_delta is not None and start_delta <= 15
                        and (
                            not recognition.showtime_end
                            or (end_delta is not None and end_delta <= 15)
                        )
                    ):
                        near_time_matches[identity] = (item, movie_name)
        if not time_matches and len(near_time_matches) == 1:
            time_matches = near_time_matches
            corrected = next(iter(near_time_matches.values()))[0]
            self._diagnostics.add(
                "wanda_showtime_vision_time_corrected",
                recognized_start=wanted_time,
                official_start=self._showtime_start(corrected)[1],
                recognized_end=recognition.showtime_end,
                official_end=self._showtime_end(
                    corrected, *self._showtime_start(corrected),
                ),
            )
        if not time_matches:
            self._diagnostics.add(
                "wanda_showtime_match_failed",
                requested={
                    "movie": recognition.movie_name,
                    "date": quote_date,
                    "time": wanted_time,
                    "hall": recognition.hall_name,
                },
                movie_matches=movie_matches,
                date_matches=date_matches,
                official_candidates=summaries,
            )
            if wanted_movie and movie_matches == 0:
                raise ProviderError(
                    "wanda_movie_not_found",
                    "万达官方场次中没有识别出的影片名称，请检查影片OCR或发送更完整截图。",
                )
            if date_matches == 0:
                raise ProviderError(
                    "wanda_showdate_not_found",
                    "影片存在，但万达官方没有截图日期对应的场次；截图可能已过期。",
                )
            raise ProviderError(
                "wanda_showtime_start_not_found",
                "影片和日期存在，但万达官方没有截图中的开场时间；请刷新场次截图。",
            )
        if len(time_matches) == 1:
            return next(iter(time_matches.values()))
        hall_matches = {
            identity: candidate
            for identity, candidate in time_matches.items()
            if recognition.hall_name
            and self._hall_matches(recognition.hall_name, str(candidate[0].get("hallName") or ""))
        }
        if len(hall_matches) == 1:
            return next(iter(hall_matches.values()))
        raise ProviderError(
            "wanda_showtime_not_unique",
            "同一影院存在多个相同影片、日期和时间的场次，请提供准确影厅。",
        )

    @staticmethod
    def _showtime_movie_name(film: Mapping[str, Any], showtime: Mapping[str, Any]) -> str:
        film_list = showtime.get("filmList") or []
        nested = film_list[0].get("filmName") if isinstance(film_list, list) and film_list and isinstance(film_list[0], Mapping) else ""
        return str(nested or film.get("filmName") or film.get("nameCN") or film.get("name") or "")

    @staticmethod
    def _showtime_start(showtime: Mapping[str, Any]) -> tuple[str, str]:
        value = showtime.get("realtime") or showtime.get("showtime") or 0
        if isinstance(value, (int, float)) and value > 1_000_000_000:
            moment = datetime.fromtimestamp(value / 1000, ZoneInfo("Asia/Shanghai"))
            return moment.date().isoformat(), moment.strftime("%H:%M")
        text = str(value or "")
        date_match = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", text)
        time_match = re.search(r"(\d{1,2}:\d{2})", text)
        return (
            "-".join(date_match.groups()) if date_match else "",
            time_match.group(1) if time_match else "",
        )

    @staticmethod
    def _clock_delta_minutes(left: str, right: str) -> int | None:
        try:
            left_hour, left_minute = (int(value) for value in left.split(":", 1))
            right_hour, right_minute = (int(value) for value in right.split(":", 1))
        except (AttributeError, TypeError, ValueError):
            return None
        return abs((left_hour * 60 + left_minute) - (right_hour * 60 + right_minute))

    @staticmethod
    def _showtime_end(showtime: Mapping[str, Any], actual_date: str, actual_time: str) -> str:
        film_list = showtime.get("filmList") or []
        film = film_list[0] if isinstance(film_list, list) and film_list and isinstance(film_list[0], Mapping) else {}
        try:
            duration = int(film.get("duration") or 0)
            start = datetime.fromisoformat(f"{actual_date}T{actual_time}:00")
        except (TypeError, ValueError):
            return ""
        if duration <= 0 or duration > 600:
            return ""
        return (start + timedelta(minutes=duration)).strftime("%H:%M")

    @staticmethod
    def _is_vip_showtime(showtime: Mapping[str, Any]) -> bool:
        values = (
            showtime.get("hallType"), showtime.get("wandaSign"), showtime.get("hallName"),
        )
        return any(
            "VIP" in unicodedata.normalize("NFKC", str(value or "")).upper()
            or "贵宾厅" in str(value or "")
            for value in values
        )

    @staticmethod
    def _hall_matches(wanted: str, actual: str) -> bool:
        wanted_key = wanted.upper().replace(" ", "")
        actual_key = actual.upper().replace(" ", "")
        number = re.search(r"(\d+)号?", wanted_key)
        if number and re.search(r"(\d+)号?", actual_key):
            return number.group(1) == re.search(r"(\d+)号?", actual_key).group(1)
        premium = ("IMAX", "CINITY", "PRIME", "XLAND", "4DX", "CGS")
        return wanted_key in actual_key or actual_key in wanted_key or any(tag in wanted_key and tag in actual_key for tag in premium)

    @staticmethod
    def _area_prices(showtime: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        entries = showtime.get("areaPriceList") or []
        for item in entries if isinstance(entries, list) else []:
            if not isinstance(item, Mapping):
                continue
            for value in (item.get("areaId"), item.get("areaCode"), item.get("code")):
                key = str(value or "").strip()
                if key:
                    result[key] = dict(item)
        return result

    def _seat_facts(
        self,
        payload: Mapping[str, Any],
        area_prices: Mapping[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        realtime = data.get("realtimeSeats") if isinstance(data, Mapping) else None
        areas = realtime.get("area") if isinstance(realtime, Mapping) else None
        if not isinstance(areas, list):
            raise ProviderError("wanda_seat_response_invalid", "万达官方实时座位数据格式无效。")
        facts: list[dict[str, Any]] = []
        for area in areas:
            if not isinstance(area, Mapping):
                continue
            area_id = str(area.get("areaId") or area.get("areaCode") or "").strip()
            price_entry = area_prices.get(area_id, {})
            area_name = str(area.get("areaName") or price_entry.get("areaName") or "")
            price = self._positive_int(price_entry.get("salesPrice"))
            wplus_activity = area.get("wPlusActivity") if isinstance(area.get("wPlusActivity"), Mapping) else {}
            wplus_member_price = self._positive_int(wplus_activity.get("price"))
            if price is not None and wplus_member_price is not None and wplus_member_price > price:
                wplus_member_price = None
            channel_fee = self._positive_int(price_entry.get("channelFee"), allow_zero=True) or 0
            raw_seats = area.get("seat") or area.get("seats") or []
            for seat in raw_seats if isinstance(raw_seats, list) else []:
                if not isinstance(seat, Mapping):
                    continue
                label = str(seat.get("name") or seat.get("seatName") or "").replace(" ", "")
                seat_area_id = str(seat.get("areaId") or area_id).strip()
                seat_price_entry = area_prices.get(seat_area_id, price_entry)
                seat_price = self._positive_int(seat_price_entry.get("salesPrice")) or price
                regular_member_price = self._positive_int(seat_price_entry.get("settlePrice"))
                if (
                    seat_price is not None and regular_member_price is not None
                    and regular_member_price > seat_price
                ):
                    regular_member_price = None
                seat_area_name = str(seat.get("areaName") or area_name or seat_price_entry.get("areaName") or "")
                # The threshold and W+ formula apply only to a physical W+ seat.
                # Wanda may attach an area-level W+ activity to ordinary or
                # preferred areas as well; that activity must not reclassify
                # those seats as W+ for customer pricing.
                physical_wplus = (
                    seat.get("payMemberSeatStatus") in (1, "1", True)
                    or "W+" in seat_area_name.upper()
                )
                seat_wplus_member_price = wplus_member_price if physical_wplus else None
                if (
                    seat_price is not None and seat_wplus_member_price is not None
                    and seat_wplus_member_price > seat_price
                ):
                    seat_wplus_member_price = None
                # The Friday activity may be attached to a non-W+ area (as in
                # Wanda's Friday member-day promotion). Keep it as a separate
                # eligible member price without changing the physical seat
                # type or enabling the W+ threshold formula.
                friday_member_price = wplus_member_price
                wplus_pricing_eligible = physical_wplus
                row, column = self._seat_coordinate(seat, label)
                facts.append({
                    "seat_id": str(seat.get("seatId") or seat.get("id") or ""),
                    "label": label,
                    "area_id": seat_area_id,
                    "area_name": seat_area_name,
                    "seat_type": str(
                        seat.get("seatTypeCode") or seat.get("seatType")
                        or seat.get("seatKind") or ""
                    ).strip(),
                    "price": seat_price,
                    "channel_fee": channel_fee,
                    "available": seat.get("status") in (1, "1", "可选"),
                    "wplus": physical_wplus,
                    "wplus_pricing_eligible": wplus_pricing_eligible,
                    "wplus_member_price": seat_wplus_member_price,
                    "friday_member_price": friday_member_price,
                    "regular_member_price": regular_member_price,
                    "row": row,
                    "column": column,
                })
        return facts

    @staticmethod
    def _selected_seat_facts(
        recognition: MovieImageInfo,
        seats: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for wanted in recognition.selected_seats:
            label = wanted.seat_number.replace(" ", "")
            matches = [seat for seat in seats if seat["label"] == label]
            if len(matches) != 1:
                raise ProviderError("wanda_selected_seat_not_found", f"万达实时座位图中无法唯一匹配{label}。")
            seat = matches[0]
            if not seat["price"]:
                raise ProviderError("wanda_seat_price_missing", f"万达官方未返回{label}的区域价格。")
            selected.append(seat)
        return selected

    async def _unavailable_same_type_reference(
        self,
        selected: list[dict[str, Any]],
        unavailable: list[str],
        *,
        account: Mapping[str, str],
        cinema_id: str,
        showtime_id: str,
        seats: list[dict[str, Any]],
        vip_pricing: bool = False,
    ) -> str | None:
        type_keys = {self._seat_type_key(seat) for seat in selected}
        if len(type_keys) != 1:
            return None
        candidates = self._same_type_probe_candidates(selected[0], seats)
        if not candidates:
            return None
        reference = candidates[0]
        uses_wplus_pricing = bool(reference.get("wplus_pricing_eligible", reference.get("wplus")))
        rules = PricingRulesUpdate.model_validate(self._pricing_rules_provider())
        if vip_pricing:
            unit_quote = self._vip_priced_unit(int(reference["price"]), rules)
        else:
            member_price = self._positive_int(
                reference.get("wplus_member_price")
                if uses_wplus_pricing else reference.get("regular_member_price")
            )
            if member_price is None:
                member_price = await self._probe_member_price(
                    account,
                    cinema_id=cinema_id,
                    showtime_id=showtime_id,
                    seat=reference,
                )
            unit_quote = self._priced_unit(
                original_price=int(reference["price"]),
                member_price=member_price,
                is_wplus=uses_wplus_pricing,
                rules=rules,
            )
        ticket_count = len(selected)
        total_quote = unit_quote * ticket_count
        template = (
            self._reply_templates_provider().same_type_unavailable_template
            if self._reply_templates_provider is not None else
            "{不可选座位}不可选，同类型参考价{同类型参考价}元/张"
            "（{张数}张约{同类型参考总价}元）。请换座后发最新截图。"
        )
        return render_template(template, {
            "不可选座位": "、".join(unavailable),
            "同类型参考价": f"{unit_quote / 100:.2f}",
            "张数": ticket_count,
            "同类型参考总价": f"{total_quote / 100:.2f}",
        })

    @classmethod
    def _same_type_probe_candidates(
        cls,
        reference: Mapping[str, Any],
        seats: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        type_key = cls._seat_type_key(reference)
        candidates = [
            seat for seat in seats
            if seat.get("available")
            and seat.get("seat_id")
            and seat.get("price")
            and cls._seat_type_key(seat) == type_key
        ]
        reference_row = reference.get("row")
        reference_column = reference.get("column")

        def distance(seat: Mapping[str, Any]) -> tuple[float, str]:
            if (
                reference_row is not None and reference_column is not None
                and seat.get("row") is not None and seat.get("column") is not None
            ):
                value = abs(int(seat["row"]) - int(reference_row)) + abs(
                    int(seat["column"]) - int(reference_column)
                )
            else:
                value = float("inf")
            return value, str(seat.get("seat_id") or "")

        candidates.sort(key=distance)
        return candidates

    @classmethod
    def _seat_type_key(cls, seat: Mapping[str, Any]) -> tuple[str, str, bool, str, int, int]:
        return (
            str(seat.get("area_id") or "").strip(),
            cls._normalize_name(str(seat.get("area_name") or "")),
            bool(seat.get("wplus_pricing_eligible", seat.get("wplus"))),
            str(seat.get("seat_type") or "").strip().lower(),
            int(seat.get("price") or 0),
            int(seat.get("channel_fee") or 0),
        )

    @staticmethod
    def _pricing_rule_version(rules: PricingRulesUpdate) -> str:
        digest = hashlib.sha256(
            json.dumps(rules.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]
        return f"pricing-{digest}"

    @staticmethod
    def _priced_unit(
        *,
        original_price: int,
        member_price: int | None,
        is_wplus: bool,
        rules: PricingRulesUpdate,
    ) -> int:
        if original_price <= 0:
            raise ValueError("authoritative_original_price_required")
        if not rules.enabled:
            return member_price if member_price is not None and member_price > 0 else original_price
        if member_price is None or member_price <= 0:
            raise ValueError("authoritative_member_price_required")
        if rules.wanda_rules:
            # The configured Wanda bands replace the legacy +1/-2.90 policy
            # for every Wanda seat type, not only W+ seats. The discount rate
            # uses the official member price and official original price.
            adjustment = WandaDirectQuoteService._dynamic_wanda_adjustment(
                original_price, member_price, rules.wanda_rules,
            )
            raw = member_price + adjustment
        elif is_wplus:
            raw = (
                max(original_price + rules.wplus_adjustment_cents, member_price)
                if member_price <= rules.wplus_member_price_threshold_cents
                else member_price
            )
        else:
            raw = member_price + rules.regular_adjustment_cents
        increment = rules.rounding_increment_cents
        lower = ((member_price + increment - 1) // increment) * increment
        upper = (original_price // increment) * increment
        if lower > upper:
            raise ValueError("pricing_member_floor_exceeds_original_cap")
        rounded = max(increment, ((raw + increment // 2) // increment) * increment)
        return min(max(rounded, lower), upper)

    @staticmethod
    def _vip_priced_unit(original_price: int, rules: PricingRulesUpdate) -> int:
        if original_price <= 0:
            raise ValueError("authoritative_original_price_required")
        if original_price <= rules.vip_discount_threshold_cents:
            raw = original_price - rules.vip_low_price_discount_cents
        else:
            raw = (original_price * rules.vip_high_price_discount_percent + 50) // 100
        increment = rules.rounding_increment_cents
        rounded = ((raw + increment // 2) // increment) * increment
        return min(max(rounded, increment), original_price)

    @staticmethod
    def _dynamic_wanda_adjustment(original_price: int, member_price: int, bands: Sequence[Any]) -> int:
        discount_percent = Decimal(member_price * 100) / Decimal(original_price)
        for index, band in enumerate(bands):
            minimum = Decimal(str(band.min_discount_percent))
            maximum = Decimal(str(band.max_discount_percent))
            is_last = index == len(bands) - 1
            if minimum <= discount_percent < maximum or (is_last and discount_percent <= maximum):
                return int(band.fixed_adjustment_cents)
        raise ValueError("pricing_discount_band_not_covered")

    def _exact_quote(
        self,
        selected_facts: list[dict[str, Any]],
        member_prices: Mapping[tuple[str, int, int], int | None],
        cinema: Mapping[str, str],
        official_movie: str,
        *,
        same_type_probe_used: bool = False,
        vip_pricing: bool = False,
    ) -> RealQuote:
        rules = PricingRulesUpdate.model_validate(self._pricing_rules_provider())
        selected: list[RealSeatQuote] = []
        zones: set[str] = set()
        for seat in selected_facts:
            key = (str(seat["area_id"]), int(seat["price"]), int(seat["channel_fee"]))
            member_price = member_prices.get(key)
            zone = "W+" if seat["wplus"] else seat["area_name"] or "普通"
            zones.add(zone)
            selected.append(RealSeatQuote(
                seat_number=str(seat["label"]),
                seat_zone_type=zone,
                original_price_cents=int(seat["price"]),
                member_price_cents=None if vip_pricing else member_price,
                channel_fee_cents=int(seat["channel_fee"]),
                unit_quote_cents=(
                    self._vip_priced_unit(int(seat["price"]), rules)
                    if vip_pricing else self._priced_unit(
                        original_price=int(seat["price"]),
                        member_price=member_price,
                        is_wplus=bool(seat.get("wplus_pricing_eligible", seat.get("wplus"))),
                        rules=rules,
                    )
                ),
            ))
        member_values = {item.member_price_cents for item in selected}
        base_values = {item.original_price_cents for item in selected}
        seat_types = {
            "wplus" if seat.get("wplus_pricing_eligible", seat.get("wplus")) else "regular"
            for seat in selected_facts
        }
        seat_type = next(iter(seat_types)) if len(seat_types) == 1 else "mixed"
        return RealQuote(
            quote_scope="exact_seats",
            seat_zone_type=next(iter(zones)) if len(zones) == 1 else "混合区域",
            member_unit_price_cents=next(iter(member_values)) if len(member_values) == 1 else None,
            original_unit_price_cents=next(iter(base_values)) if len(base_values) == 1 else None,
            seat_type=seat_type,
            base_unit_cents=next(iter(base_values)) if len(base_values) == 1 else None,
            base_total_cents=sum(item.original_price_cents for item in selected),
            price_source=(
                "realtime_vip_area" if vip_pricing else (
                    "realtime_regular_area" if seat_type == "regular" else (
                        "realtime_wplus_area" if seat_type == "wplus" else "realtime_mixed_area"
                    )
                )
            ),
            unit_quote_cents=selected[0].unit_quote_cents if len({item.unit_quote_cents for item in selected}) == 1 else None,
            total_quote_cents=sum(item.unit_quote_cents for item in selected),
            channel_fee_total_cents=sum(item.channel_fee_cents for item in selected),
            seat_quotes=selected,
            ticket_count=len(selected),
            needs_ticket_count=False,
            same_type_probe_used=same_type_probe_used,
            pricing_source=(
                "万达官方VIP厅实时销售价 + VIP厅报价规则（只读）"
                if vip_pricing else (
                    "万达官方实时座位原价（W+区域优先）+ 后台报价规则（只读）"
                    if rules.enabled else "万达官方实时座位原价（W+区域优先，只读）"
                )
            ),
            pricing_rule_version=self._pricing_rule_version(rules) if rules.enabled else None,
            detail=(
                f"已通过万达官方场次和实时座位图读取区域原价，全程只读、未锁座；影片：{official_movie}"
                + (
                    "；已应用VIP厅报价规则"
                    if vip_pricing else ("；最终展示金额已应用后台确定性报价规则" if rules.enabled else "")
                )
            ),
            matched_cinema_name=cinema["cinema_name"],
            matched_city_name=cinema.get("city_name"),
        )

    @classmethod
    def _available_wplus_area_reference(
        cls,
        area_prices: Mapping[str, Mapping[str, Any]],
        seats: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        unique: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        for entry in area_prices.values():
            area_id = str(entry.get("areaId") or "").strip()
            area_code = str(entry.get("areaCode") or entry.get("code") or "").strip()
            area_name = str(entry.get("areaName") or "").strip()
            price = cls._positive_int(entry.get("salesPrice"))
            if price is None or not (area_code == "36" or "W+" in area_name.upper()):
                continue
            candidate = {
                "area_id": area_id or area_code,
                "area_code": area_code,
                "area_name": area_name or "W+专享",
                "price": price,
                "regular_member_price": cls._positive_int(entry.get("settlePrice")),
                "channel_fee": cls._positive_int(entry.get("channelFee"), allow_zero=True) or 0,
            }
            unique[(area_id, area_code, area_name, price)] = candidate
        available: list[dict[str, Any]] = []
        for candidate in unique.values():
            identifiers = {candidate["area_id"], candidate["area_code"]} - {""}
            matching_seats = [
                seat for seat in seats
                if seat["available"] and str(seat.get("area_id") or "") in identifiers
            ]
            if matching_seats:
                member_prices = {
                    int(seat["wplus_member_price"])
                    for seat in matching_seats if seat.get("wplus_member_price") is not None
                }
                if len(member_prices) > 1:
                    raise ProviderError(
                        "wanda_wplus_member_price_ambiguous",
                        "当前场次W+专享区域返回多个不同会员活动价，无法唯一确定参考价。",
                    )
                candidate["member_price"] = next(iter(member_prices)) if member_prices else None
                available.append(candidate)
        if not available:
            return None
        preferred = [item for item in available if item["area_code"] == "36"] or available
        prices = {int(item["price"]) for item in preferred}
        if len(prices) != 1:
            raise ProviderError(
                "wanda_wplus_area_price_ambiguous",
                "当前场次存在多个不同价格的W+专享区域，无法唯一确定参考价。",
            )
        preferred.sort(key=lambda item: (str(item["area_code"]), str(item["area_id"]), str(item["area_name"])))
        return preferred[0]

    @classmethod
    def _wplus_probe_seat(
        cls, area: Mapping[str, Any], seats: list[dict[str, Any]],
    ) -> dict[str, Any]:
        identifiers = {
            str(area.get("area_id") or "").strip(),
            str(area.get("area_code") or "").strip(),
        } - {""}
        candidates = [
            seat for seat in seats
            if seat.get("available") and seat.get("seat_id") and seat.get("price")
            and str(seat.get("area_id") or "") in identifiers
        ]
        if not candidates:
            raise ProviderError(
                "wanda_member_probe_seat_unavailable",
                "W+区域未找到可用于会员价探针核验的实时可售座位。",
            )
        rows = [int(seat["row"]) for seat in candidates if seat.get("row") is not None]
        columns = [int(seat["column"]) for seat in candidates if seat.get("column") is not None]
        center_row = (min(rows) + max(rows)) / 2 if rows else None
        center_column = (min(columns) + max(columns)) / 2 if columns else None

        def rank(seat: Mapping[str, Any]) -> tuple[float, str]:
            if center_row is None or center_column is None or seat.get("row") is None or seat.get("column") is None:
                return float("inf"), str(seat.get("seat_id") or "")
            distance = abs(int(seat["row"]) - center_row) + abs(int(seat["column"]) - center_column)
            return distance, str(seat.get("seat_id") or "")

        candidates.sort(key=rank)
        return candidates[0]

    def _middle_wplus_quote(
        self,
        probe: Mapping[str, Any],
        member_price: int | None,
        cinema: Mapping[str, str],
        official_movie: str,
        *,
        ticket_count: int | None = None,
        probe_used: bool = False,
    ) -> RealQuote:
        rules = PricingRulesUpdate.model_validate(self._pricing_rules_provider())
        base_price = int(member_price) if member_price is not None else int(probe["price"])
        unit_quote = self._priced_unit(
            original_price=int(probe["price"]), member_price=member_price,
            is_wplus=True, rules=rules,
        )
        return RealQuote(
            quote_scope="area_preview",
            seat_zone_type="W+",
            member_unit_price_cents=member_price,
            original_unit_price_cents=int(probe["price"]),
            seat_type="wplus",
            base_unit_cents=base_price,
            base_total_cents=base_price * ticket_count if ticket_count else None,
            price_source="realtime_wplus_area",
            unit_quote_cents=unit_quote,
            total_quote_cents=unit_quote * ticket_count if ticket_count else None,
            channel_fee_total_cents=int(probe.get("channel_fee") or 0) * ticket_count if ticket_count else None,
            seat_quotes=[],
            ticket_count=ticket_count,
            needs_ticket_count=ticket_count is None,
            same_type_probe_used=probe_used,
            pricing_source=(
                "万达官方W+会员专享优惠 + 后台报价规则（探针已释放）"
                if probe_used and rules.enabled
                else "万达官方W+会员专享优惠（探针已释放）"
                if probe_used
                else "万达官方实时W+会员活动价 + 后台报价规则（只读）"
                if member_price is not None and rules.enabled
                else "万达官方实时W+会员活动价（只读）"
                if member_price is not None
                else "万达官方实时W+区域原价 + 后台报价规则（只读）"
                if rules.enabled else "万达官方实时W+区域原价（只读）"
            ),
            pricing_rule_version=self._pricing_rule_version(rules) if rules.enabled else None,
            detail=(
                (
                    "实时座位未返回W+会员活动价，已通过同区域可售座位探针读取会员专享价，"
                    "并确认临时订单已取消、座位已恢复可售；"
                    if probe_used
                    else "已读取万达官方实时W+会员活动价，并确认该区域仍有可售座位；全程只读、未选择或锁定具体座位；"
                )
                + f"影片：{official_movie}"
                + ("；最终展示金额已应用后台确定性报价规则" if rules.enabled else "")
            ),
            matched_cinema_name=cinema["cinema_name"],
            matched_city_name=cinema.get("city_name"),
        )

    def _validate_quote_datetime(self, recognition: MovieImageInfo, quote_date: str) -> None:
        now = self._now_provider().astimezone(ZoneInfo("Asia/Shanghai"))
        try:
            candidate = date.fromisoformat(quote_date)
        except ValueError:
            raise ProviderError("wanda_quote_date_invalid", "截图日期格式无效，请发送最新截图。") from None
        if candidate < now.date():
            raise ProviderError(
                "wanda_screenshot_date_expired",
                "截图日期已经过期，请发送当前仍可购买场次的最新截图。",
            )
        text = recognition.date_text or ""
        has_explicit_month_day = bool(re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*日", text))
        if not has_explicit_month_day:
            weekday_match = re.search(r"(?:周|星期)([一二三四五六日天])", text)
            if weekday_match:
                expected = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[weekday_match.group(1)]
                if candidate.weekday() != expected:
                    raise ProviderError(
                        "wanda_relative_date_conflict",
                        "截图中的相对日期与星期不一致，可能是旧截图；请发送带月日的最新场次截图。",
                    )
        if candidate == now.date() and recognition.showtime_start:
            try:
                hour, minute = (int(value) for value in recognition.showtime_start.split(":", 1))
                start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if start < now - timedelta(minutes=5):
                    raise ProviderError(
                        "wanda_showtime_expired",
                        "截图中的开场时间已经过去，请发送当前可购买场次的最新截图。",
                    )
            except ValueError:
                pass

    def _resolved_date(self, recognition: MovieImageInfo) -> str | None:
        if recognition.date is not None:
            return recognition.date.isoformat()
        text = recognition.date_text or ""
        now = self._now_provider().astimezone(ZoneInfo("Asia/Shanghai"))
        full_date = re.search(
            r"(20\d{2})\s*(?:[-/.]|年)\s*(\d{1,2})\s*(?:[-/.]|月)\s*(\d{1,2})\s*日?",
            text,
        )
        if full_date:
            try:
                return date(*(int(value) for value in full_date.groups())).isoformat()
            except ValueError:
                return None
        match = re.search(r"(?:^|\D)(\d{1,2})\s*[-/.]\s*(\d{1,2})(?:日)?", text)
        if match:
            try:
                candidate = date(now.year, int(match.group(1)), int(match.group(2)))
                if candidate < now.date():
                    next_year = date(now.year + 1, candidate.month, candidate.day)
                    if next_year - now.date() <= timedelta(days=31):
                        candidate = next_year
                return candidate.isoformat()
            except ValueError:
                return None
        match = re.search(r"(?:^|\D)(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if match:
            try:
                candidate = date(now.year, int(match.group(1)), int(match.group(2)))
                if candidate < now.date():
                    next_year = date(now.year + 1, candidate.month, candidate.day)
                    if next_year - now.date() <= timedelta(days=31):
                        candidate = next_year
                return candidate.isoformat()
            except ValueError:
                return None
        for label, offset in (("今天", 0), ("明天", 1), ("后天", 2)):
            if label in text:
                return (now.date() + timedelta(days=offset)).isoformat()
        return None

    @staticmethod
    def _seat_coordinate(seat: Mapping[str, Any], label: str) -> tuple[int | None, int | None]:
        match = re.fullmatch(r"(\d{1,2})排(\d{1,3})座", label)
        if match:
            return int(match.group(1)), int(match.group(2))
        try:
            row = int(seat.get("row") or seat.get("coordy"))
            column = int(seat.get("column") or seat.get("coordx"))
            return row, column
        except (TypeError, ValueError):
            return None, None

    @staticmethod
    def _positive_int(*values: Any, allow_zero: bool = False) -> int | None:
        for value in values:
            try:
                result = int(value)
            except (TypeError, ValueError):
                continue
            if result > 0 or (allow_zero and result == 0):
                return result
        return None

    @classmethod
    def _cinema_match_key(cls, value: str) -> str:
        """Normalize buyer-platform branding without dropping the venue identity."""
        normalized = cls._normalize_name(value)
        for generic in (
            "万达寰映影城", "万达寰映影院", "万达寰时影城", "万达寰時影城",
            "万达方米影城", "万达方米影院",
            "万达影城", "万达影院", "万达电影", "寰映影城", "寰映影院",
            "寰时影城", "寰時影城", "寰时影院", "寰時影院", "方米影城", "方米影院",
        ):
            normalized = normalized.replace(generic, "")
        # Platform titles sometimes append the English mall brand to the same
        # Chinese venue identity (for example “盐田壹海城ONE MALL”). Wanda's
        # official cache keeps only the Chinese venue name.
        normalized = normalized.replace("onemall", "")
        for format_name in (
            "laserimax", "imax", "prime", "cinity", "xland", "cola",
            "杜比影院", "杜比影", "杜比", "激光厅", "激光", "巨幕",
        ):
            normalized = normalized.replace(format_name, "")
        normalized = normalized.replace("特许", "")
        raw_value = unicodedata.normalize("NFKC", value).strip().lower()
        if re.search(r"(?:\.{2,}|…)[)）】\]}]*$", raw_value):
            partial_formats = {
                format_name[:length]
                for format_name in ("laserimax", "imax", "prime", "cinity", "xland", "cola", "激光")
                for length in range(1, len(format_name))
            }
            for fragment in sorted(partial_formats, key=len, reverse=True):
                if normalized.endswith(fragment):
                    normalized = normalized[: -len(fragment)]
                    break
        while normalized.endswith(("影城", "影院", "店")):
            for suffix in ("影城", "影院", "店"):
                if normalized.endswith(suffix):
                    normalized = normalized[: -len(suffix)]
                    break
        return normalized

    @classmethod
    def _cinema_address_identity_tokens(cls, address: str) -> set[str]:
        """Extract street and distinctive venue identities from an official address."""
        normalized = unicodedata.normalize("NFKC", address).lower()
        tokens: set[str] = set()
        for segment in re.split(r"[省市区县]", normalized):
            tokens.update(
                cls._normalize_name(match)
                for match in re.findall(r"([\u4e00-\u9fff0-9]{2,12}?(?:大道|路|街))", segment)
            )
        for segment in re.split(r"[省市区县镇乡]|街道", normalized):
            for match in re.findall(r"([\u4e00-\u9fff0-9]{2,10}?(?:奥特莱斯|奥莱|大卖场|卖场))", segment):
                token = cls._normalize_name(match)
                tokens.add(token)
                if token.endswith("奥特莱斯"):
                    tokens.add(token.removesuffix("奥特莱斯") + "奥莱")
                if token.endswith("大卖场"):
                    tokens.add(token.removesuffix("大卖场") + "卖场")
        for segment in re.split(r"[省市区县镇乡路街号院层楼]|街道", normalized):
            tokens.update(
                cls._normalize_name(match)
                for match in re.findall(
                    r"([\u4e00-\u9fff0-9]{0,8}(?:合生汇|大悦城|万象汇|万象城|印象城|吾悦广场))",
                    segment,
                )
            )
        bounded_tokens = {token for token in tokens if len(token) >= 3}
        # The venue is marketed with both “超极合生汇” and “超级合生汇”
        # spellings. Keep the alias bounded to this proper venue identity.
        for token in tuple(bounded_tokens):
            if "超级合生汇" in token:
                bounded_tokens.add(token.replace("超级合生汇", "超极合生汇"))
            if "超极合生汇" in token:
                bounded_tokens.add(token.replace("超极合生汇", "超级合生汇"))
        return bounded_tokens

    @staticmethod
    def _is_distinctive_venue_identity(value: str) -> bool:
        """Require a qualifier before a bounded commercial-complex suffix."""
        venue_suffixes = ("合生汇", "大悦城", "万象汇", "万象城", "印象城", "吾悦广场")
        return any(value.endswith(suffix) and len(value) > len(suffix) for suffix in venue_suffixes)

    @classmethod
    def _address_identity_matches_query(cls, wanted: str, token: str) -> bool:
        """Allow truncation/noise without degrading a specific venue to its generic suffix."""
        if len(wanted) < 2:
            return False
        if wanted in token:
            return True
        if token not in wanted:
            return False
        return not (
            cls._is_distinctive_venue_identity(wanted)
            and not cls._is_distinctive_venue_identity(token)
        )

    @classmethod
    def _cinema_candidate_match_key(cls, value: str, city_name: str, address: str) -> str:
        """Remove geography verified by a candidate when scoring a direct cinema match."""
        key = cls._cinema_match_key(value)
        geography = {cls._normalize_name(city_name)}
        normalized_address = unicodedata.normalize("NFKC", address)
        geography.update(
            cls._normalize_name(match)
            for match in re.findall(r"([^省市区县]{2,8})(?:区|县)", normalized_address)
        )
        for token in sorted((item for item in geography if item), key=len, reverse=True):
            key = key.replace(token, "")
        return key

    @classmethod
    def _cinema_cross_city_match_key(cls, value: str, city_name: str) -> str:
        """Remove only city names; 高新、经开 and similar venue identities must survive."""
        key = cls._cinema_match_key(value)
        city = cls._normalize_name(city_name)
        if city:
            key = key.replace(city, "")
        return key

    @staticmethod
    def _normalize_name(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).lower()
        return "".join(char for char in normalized if char.isalnum() or "\u4e00" <= char <= "\u9fff")
