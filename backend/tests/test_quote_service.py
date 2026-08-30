from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi.testclient import TestClient

from app.config import Settings
from app.diagnostics import DiagnosticsStore
from app.errors import ProviderError
from app.main import create_app
from app.models import MovieImageInfo, PricingRulesUpdate, RealQuote
from app.settings_store import PersistentSettingsStore
from app.wanda_direct_quote import CINEMA_ORIGIN, H5_CHANNEL, MARKETING_ORIGIN, WandaDirectQuoteService


def quote_test_now() -> datetime:
    return datetime(2026, 8, 25, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


class ReversibleProtector:
    def protect(self, value: str) -> str:
        return "enc:" + value[::-1]

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")[::-1]


def test_resolved_date_accepts_month_day_with_dash_separator() -> None:
    service = WandaDirectQuoteService(
        Settings(wanda_account_pool_path="unused", wanda_cinema_cache_path="unused"),
        now_provider=lambda: datetime(2026, 8, 25, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert service._resolved_date(MovieImageInfo(date_text="周三 09-02")) == "2026-09-02"


def _direct_files(tmp_path: Path) -> tuple[Path, Path]:
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps([
        {
            "phone": "13800000439", "token": "offline-token", "status": "offline",
            "user_info": {"userIdentifier": "offline-user"},
        },
        {
            "phone": "13800009083", "token": "fixed-official-token", "status": "online",
            "user_info": {"userIdentifier": "fixed-user", "isPayMember": True, "wplusType": 1},
            "shumei_box_id": "fixed-device",
        },
    ]), encoding="utf-8")
    cache = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, "
            "cinema_name TEXT, address TEXT, search_text TEXT, raw_json TEXT, updated_at INTEGER)"
        )
        raw = {
            "storeId": "cinema-7107", "cinemaName": "昆明西山万达广场店",
            "_cityId": "city-53", "_cityName": "昆明", "address": "西山区",
        }
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cinema-7107", "city-53", "昆明", "昆明西山万达广场店", "西山区", "昆明 西山 万达", json.dumps(raw), 1),
        )
    return accounts, cache


def _showtime_response() -> dict:
    timestamp = int(datetime(2026, 8, 25, 16, 20, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    return {
        "code": 0,
        "data": {
            "bizCode": 0,
            "showtimeFilmInf": [{
                "filmName": "奥德赛",
                "showtimeFilmDateInf": [{
                    "showtimesInf": {"showtimeList": [{
                        "showtimeId": "showtime-1", "realtime": timestamp,
                        "hallName": "16号-激光IMAX-COLA厅",
                        "areaPriceList": [
                            {
                                "areaId": "35", "areaCode": "35", "areaName": "普通区",
                                "salesPrice": 7290, "settlePrice": 6190,
                            },
                            {"areaId": "36", "areaCode": "36", "areaName": "W+专享", "salesPrice": 6290},
                            {"areaId": "37", "areaCode": "37", "areaName": "W+中区", "salesPrice": 5990},
                        ],
                    }]}
                }],
            }],
        },
    }


def _seat(name: str, seat_id: str, area_id: str, row: int, column: int, *, wplus: bool) -> dict:
    return {
        "name": name, "seatId": seat_id, "status": 1, "row": row, "column": column,
        "coordx": column, "coordy": row, "payMemberSeatStatus": 1 if wplus else 0,
        "areaId": area_id,
    }


def _realtime_response() -> dict:
    return {
        "code": 0,
        "data": {
            "bizCode": 0,
            "realtimeSeats": {
                "area": [
                    {"areaId": "35", "areaName": "普通区", "seat": [
                        _seat("6排16座", "seat-exact", "35", 6, 16, wplus=False),
                    ]},
                    {"areaId": "36", "areaName": "W+专享", "seat": [
                        _seat("5排2座", "w-edge-left", "36", 5, 2, wplus=True),
                        _seat("9排14座", "w-edge-right", "36", 9, 14, wplus=True),
                    ]},
                    {"areaId": "37", "areaName": "W+中区", "seat": [
                        _seat("7排8座", "w-middle", "37", 7, 8, wplus=True),
                    ]},
                ]
            },
        },
    }


def _encrypted_offer(price_cents: int) -> str:
    groups = [{"groupName": "会员优惠", "groupType": 1, "groupItems": [{
        "name": "W+会员专享优惠", "able": True, "code": "wplus",
        "allotSeat": json.dumps({"totalPayPrice": price_cents}),
    }]}]
    raw = json.dumps(groups, ensure_ascii=False, separators=(",", ":")).encode()
    return AES.new(b"6f34faeefba8fd39", AES.MODE_ECB).encrypt(pad(raw, AES.block_size)).hex()


def _handler(
    captured: list[dict[str, object]],
    *,
    member_price_cents: int = 6190,
    release_ok: bool = True,
    release_http_errors: int = 0,
    exact_seat_available: bool = True,
    add_same_type_seat: bool = False,
    fail_first_lock: bool = False,
    lock_status_timeouts: int = 0,
    wplus_available: bool = True,
    unavailable_seat_names: set[str] | None = None,
    realtime_wplus_activity_price: int | None = 6290,
):
    state = {
        "cancelled": False,
        "release_http_errors": release_http_errors,
        "create_attempts": 0,
        "lock_status_timeouts": lock_status_timeouts,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({
            "host": request.url.host,
            "path": request.url.path,
            "token": request.headers.get("x-ry-token"),
            "channel": request.headers.get("x-ry-channel"),
            "body": request.content.decode(errors="replace"),
        })
        if request.url.path == "/showtime/by_cinema.api":
            return httpx.Response(200, json=_showtime_response())
        if request.url.path == "/order/real_time_seat.api":
            if state["cancelled"] and state["release_http_errors"]:
                state["release_http_errors"] -= 1
                return httpx.Response(503, json={"code": 503})
            response = _realtime_response()
            ordinary_seats = response["data"]["realtimeSeats"]["area"][0]["seat"]
            ordinary_seats[0]["status"] = 1 if exact_seat_available else 2
            for area in response["data"]["realtimeSeats"]["area"][1:]:
                for seat in area["seat"]:
                    seat["status"] = (
                        2 if unavailable_seat_names and seat["name"] in unavailable_seat_names
                        else 1 if wplus_available else 2
                    )
                if area["areaId"] == "36" and realtime_wplus_activity_price is not None:
                    area["wPlusActivity"] = {
                        "activityCode": "wplus-live", "activityName": "W+会员专享优惠",
                        "price": realtime_wplus_activity_price, "userLimitNum": 6,
                    }
            if add_same_type_seat:
                ordinary_seats.append(_seat("6排15座", "seat-same-type", "35", 6, 15, wplus=False))
            if state["cancelled"] and not release_ok:
                for area in response["data"]["realtimeSeats"]["area"]:
                    for seat in area["seat"]:
                        seat["status"] = 2
            return httpx.Response(200, json=response)
        if request.url.path == "/order/create_order.api":
            state["create_attempts"] += 1
            if fail_first_lock and state["create_attempts"] == 1:
                return httpx.Response(200, json={"code": 1, "data": {"bizCode": 1001}, "msg": "座位已售"})
            return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "orderId": "order-1"}})
        if request.url.path == "/order/order_status.api":
            if not state["cancelled"] and state["lock_status_timeouts"]:
                state["lock_status_timeouts"] -= 1
                raise httpx.ReadTimeout("simulated slow order status", request=request)
            status = {"orderStatus": "60", "lockSeatTime": -1} if state["cancelled"] else {"orderStatus": "40", "lockSeatTime": 120}
            return httpx.Response(200, json={"code": 0, "data": status})
        if request.url.path == "/mkt/activity/secret/list.api":
            return httpx.Response(200, json={"code": 0, "data": _encrypted_offer(member_price_cents)})
        if request.url.path == "/order/cancel.api":
            state["cancelled"] = True
            return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0}, "success": True})
        return httpx.Response(404, json={})
    return handler


@pytest.mark.asyncio
async def test_truncated_huanying_platform_name_uses_official_address_district_to_disambiguate(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        rows = [
            ("679", "city-bj", "北京", "北京寰映影城合生汇店", "北京市朝阳区西大望路甲22号院合生汇五层", "", "{}", 1),
            ("7045", "city-bj", "北京", "北京寰映影城昌平合生汇店", "北京市昌平区北清路1号院超级合生汇", "", "{}", 1),
        ]
        connection.executemany("INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    matched = await service._resolve_cinema(settings, MovieImageInfo(
        cinema_name="寰映影城（朝阳合生汇杜比影...）", movie_name="蜘蛛侠：崭新之日",
        date_text="今天 8月25日", showtime_start="10:40", selected_count_visible=0,
        confidence=0.9,
    ))
    await service.aclose()
    assert matched["cinema_id"] == "679"
    assert matched["cinema_name"] == "北京寰映影城合生汇店"


@pytest.mark.asyncio
async def test_truncated_beijing_super_heshenghui_name_matches_unique_official_cache_entry(
    tmp_path: Path,
) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany("INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("679", "city-bj", "北京", "北京寰映影城合生汇店", "北京市朝阳区西大望路甲22号院合生汇五层", "", "{}", 1),
            ("7045", "city-bj", "北京", "北京寰映影城昌平合生汇店", "北京市昌平区北清路1号院超级合生汇B2层34号寰映影城", "", "{}", 1),
        ])
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    matched = await service._resolve_cinema(settings, MovieImageInfo(
        cinema_name="寰映影城（北京超极...", city="北京", movie_name="奥德赛",
        date_text="本周六 8月29日", showtime_start="19:00",
        selected_count_visible=0, confidence=0.95,
    ))

    await service.aclose()
    assert matched["cinema_id"] == "7045"
    assert matched["cinema_name"] == "北京寰映影城昌平合生汇店"


@pytest.mark.asyncio
async def test_full_super_heshenghui_platform_name_prefers_exact_address_identity_token(
    tmp_path: Path,
) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany("INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("679", "city-bj", "北京", "北京寰映影城合生汇店", "北京市朝阳区西大望路甲22号院合生汇五层", "", "{}", 1),
            ("7045", "city-bj", "北京", "北京寰映影城昌平合生汇店", "北京市昌平区北清路1号院超极合生汇B2层34号寰映影城", "", "{}", 1),
        ])
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    recognition = MovieImageInfo.model_validate({
        "cinema_name": "寰映影城（超极合生汇激光IMAX+CINITY店）", "city": "北京",
        "movie_name": "奥德赛", "date_text": "本周六 8月29日", "showtime_start": "19:00",
        "hall_name": "IMAX激光厅-儿童需购票",
        "selected_seats": [{"seat_number": "8排13座"}, {"seat_number": "8排12座"}],
        "selected_count_visible": 2, "confidence": 0.95,
    })
    matched = await service._resolve_cinema(settings, recognition)
    completed = await service.complete_cinema(recognition)

    await service.aclose()
    assert matched["cinema_id"] == "7045"
    assert matched["cinema_name"] == "北京寰映影城昌平合生汇店"
    assert completed.cinema_name == "北京寰映影城昌平合生汇店"
    assert completed.city == "北京"


@pytest.mark.asyncio
async def test_generic_heshenghui_name_remains_ambiguous_and_fails_closed(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany("INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("679", "city-bj", "北京", "北京寰映影城合生汇店", "北京市朝阳区西大望路甲22号院合生汇五层", "", "{}", 1),
            ("7045", "city-bj", "北京", "北京寰映影城昌平合生汇店", "北京市昌平区北清路1号院超级合生汇B2层34号寰映影城", "", "{}", 1),
        ])
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    with pytest.raises(ProviderError) as error:
        await service._resolve_cinema(settings, MovieImageInfo(
            cinema_name="寰映影城（合生汇店）", city="北京", movie_name="奥德赛",
            date_text="本周六 8月29日", showtime_start="19:00", confidence=0.95,
        ))

    await service.aclose()
    assert error.value.code == "wanda_cinema_not_unique"


@pytest.mark.asyncio
async def test_specific_super_heshenghui_name_never_degrades_to_generic_only_candidate(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("679", "city-bj", "北京", "北京寰映影城合生汇店", "北京市朝阳区西大望路甲22号院合生汇五层", "", "{}", 1),
        )
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    requested_cinema_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_cinema_ids.append(request.url.params.get("cinemaId") or "")
        return httpx.Response(200, json=_showtime_response())

    service = WandaDirectQuoteService(
        settings, transport=httpx.MockTransport(handler), now_provider=quote_test_now,
    )
    recognition = MovieImageInfo(
        cinema_name="寰映影城（超极合生汇激光IMAX+CINITY店）", city="北京",
        movie_name="奥德赛", date="2026-08-29", showtime_start="19:00", confidence=0.95,
    )

    with pytest.raises(ProviderError) as error:
        await service.quote(recognition)

    await service.aclose()
    assert error.value.code == "wanda_cinema_not_found"
    assert requested_cinema_ids == []


@pytest.mark.asyncio
async def test_full_super_heshenghui_quote_keeps_strong_cinema_evidence_through_showtime_lookup(
    tmp_path: Path,
) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany("INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("679", "city-bj", "北京", "北京寰映影城合生汇店", "北京市朝阳区西大望路甲22号院合生汇五层", "", "{}", 1),
            ("7045", "city-bj", "北京", "北京寰映影城昌平合生汇店", "北京市昌平区北清路1号院超级合生汇B2层34号寰映影城", "", "{}", 1),
        ])
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(
        settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)), now_provider=quote_test_now,
    )
    showtime = _showtime_response()
    showtime_item = showtime["data"]["showtimeFilmInf"][0]["showtimeFilmDateInf"][0]["showtimesInf"]["showtimeList"][0]
    showtime_item.update({
        "realtime": int(datetime(2026, 8, 29, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000),
        "hallName": "IMAX激光厅-儿童需购票",
    })
    realtime = _realtime_response()
    realtime["data"]["realtimeSeats"]["area"][1]["seat"].extend([
        _seat("8排13座", "seat-8-13", "36", 8, 13, wplus=True),
        _seat("8排12座", "seat-8-12", "36", 8, 12, wplus=True),
    ])
    realtime["data"]["realtimeSeats"]["area"][1]["wPlusActivity"] = {
        "activityCode": "wplus-live", "activityName": "W+会员专享优惠", "price": 6290, "userLimitNum": 6,
    }
    requested_cinema_ids: list[str] = []

    async def official_get(_account, _origin, path, query, **_kwargs):
        if path == "/showtime/by_cinema.api":
            requested_cinema_ids.append(str(dict(query).get("cinemaId") or ""))
            return showtime
        if path == "/order/real_time_seat.api":
            return realtime
        raise AssertionError(f"unexpected official path: {path}")

    async def forbidden_showtime_fallback(*_args, **_kwargs):
        raise AssertionError("strong cinema evidence must not degrade to cross-cinema showtime fallback")

    service._official_get = official_get  # type: ignore[method-assign]
    service._resolve_cinema_by_showtime = forbidden_showtime_fallback  # type: ignore[method-assign]
    quote = await service.quote(MovieImageInfo.model_validate({
        "cinema_name": "寰映影城（超极合生汇激光IMAX+CINITY店）", "city": "北京",
        "movie_name": "奥德赛", "date": "2026-08-29", "showtime_start": "19:00",
        "hall_name": "IMAX激光厅-儿童需购票",
        "selected_seats": [{"seat_number": "8排13座"}, {"seat_number": "8排12座"}],
        "selected_count_visible": 2, "confidence": 0.95,
    }))

    await service.aclose()
    assert quote.matched_cinema_name == "北京寰映影城昌平合生汇店"
    assert requested_cinema_ids == ["7045"]


@pytest.mark.asyncio
async def test_missing_vision_movie_is_completed_from_unique_official_showtime(
    tmp_path: Path,
) -> None:
    accounts, cache = _direct_files(tmp_path)
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    captured: list[dict[str, object]] = []
    service = WandaDirectQuoteService(
        settings, transport=httpx.MockTransport(_handler(captured)), now_provider=quote_test_now,
    )

    quote = await service.quote(MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": None,
        "movie_name": None, "date": "2026-08-25", "showtime_start": "16:20",
        "showtime_end": "19:13", "hall_name": "16号-激光IMAX-COLA厅",
        "selected_count_visible": 0, "confidence": 0.95,
        "missing_fields": ["city", "movie_name"],
    }))

    await service.aclose()
    assert quote.matched_city_name == "昆明"
    assert quote.matched_cinema_name == "昆明西山万达广场店"
    assert quote.matched_movie_name == "奥德赛"
    assert quote.matched_showtime_start == "16:20"


@pytest.mark.asyncio
async def test_missing_movie_is_inferred_only_when_time_and_hall_select_one_official_showtime(
    tmp_path: Path,
) -> None:
    accounts, cache = _direct_files(tmp_path)
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(
        settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    payload = _showtime_response()
    second = json.loads(json.dumps(payload["data"]["showtimeFilmInf"][0]))
    second["filmName"] = "空枪"
    second_showtime = second["showtimeFilmDateInf"][0]["showtimesInf"]["showtimeList"][0]
    second_showtime["showtimeId"] = "showtime-2"
    second_showtime["hallName"] = "6号激光厅"
    payload["data"]["showtimeFilmInf"].append(second)
    recognition = MovieImageInfo(
        movie_name=None, date="2026-08-25", showtime_start="16:20",
        hall_name="6号激光厅", missing_fields=["movie_name"], confidence=0.95,
    )

    matched, movie = service._match_showtime(payload, recognition, "2026-08-25")
    assert matched["showtimeId"] == "showtime-2"
    assert movie == "空枪"

    recognition_without_hall = recognition.model_copy(update={"hall_name": None})
    with pytest.raises(ProviderError) as error:
        service._match_showtime(payload, recognition_without_hall, "2026-08-25")
    assert error.value.code == "wanda_showtime_not_unique"
    await service.aclose()


@pytest.mark.asyncio
async def test_ambiguous_city_cinemas_are_resolved_by_parallel_authoritative_showtimes(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cinema-other", "city-53", "昆明", "昆明呈贡万达广场店", "昆明市呈贡区彩云南路", "", "{}", 1),
        )
    queried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cinema_id = request.url.params.get("cinemaId") or ""
        queried.append(cinema_id)
        if cinema_id == "cinema-7107":
            return httpx.Response(200, json=_showtime_response())
        return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}})

    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(handler))
    recognition = MovieImageInfo(
        cinema_name="万达影城", city="昆明", movie_name="奥德赛",
        date_text="今天 08月25日", showtime_start="16:20",
        selected_count_visible=0, confidence=0.9,
    )

    cinema, _, showtime, movie = await service._resolve_cinema_by_showtime(
        settings, service._fixed_account(settings), recognition, "2026-08-25",
    )

    await service.aclose()
    assert set(queried) == {"cinema-7107", "cinema-other"}
    assert cinema["cinema_id"] == "cinema-7107"
    assert showtime["showtimeId"] == "showtime-1"
    assert movie == "奥德赛"


@pytest.mark.asyncio
async def test_missing_city_weak_alias_resolves_only_unique_cross_city_official_showtime(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany("INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("cinema-qj", "city-qj", "曲靖", "曲靖经开万达广场店", "曲靖市三江大道万达广场4楼", "", "{}", 1),
            ("cinema-wh", "city-wh", "武汉", "万达影城（经开万达广场IMAX激光店）", "武汉市经济技术开发区东风大道111号", "", "{}", 1),
        ])
    queried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cinema_id = request.url.params.get("cinemaId") or ""
        queried.append(cinema_id)
        if cinema_id == "cinema-qj":
            return httpx.Response(200, json=_showtime_response())
        return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}})

    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(handler))
    recognition = MovieImageInfo(
        cinema_name="万达影城（经开万达广场IMAX激光店）", city="武汉", movie_name="奥德赛",
        date_text="今天 08月25日", showtime_start="16:20", hall_name="16号-激光IMAX-COLA厅",
        selected_count_visible=0, confidence=0.95, missing_fields=["city"],
    )

    unchanged = await service.complete_cinema(recognition)
    cinema, _, showtime, _ = await service._resolve_cinema_by_showtime(
        settings, service._fixed_account(settings), recognition, "2026-08-25",
    )

    await service.aclose()
    assert unchanged.cinema_name == recognition.cinema_name
    assert set(queried) == {"cinema-qj", "cinema-wh"}
    assert cinema["cinema_name"] == "曲靖经开万达广场店"
    assert cinema["city_name"] == "曲靖"
    assert showtime["showtimeId"] == "showtime-1"


@pytest.mark.asyncio
async def test_platform_county_city_falls_back_to_official_prefecture_city_by_venue_and_showtime(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "cinema-yiwu", "city-jinhua", "金华", "义乌万达广场店",
                "义乌市新科路9号万达广场4楼", "", "{}", 1,
            ),
        )
    queried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cinema_id = request.url.params.get("cinemaId") or ""
        queried.append(cinema_id)
        if cinema_id == "cinema-yiwu":
            return httpx.Response(200, json=_showtime_response())
        return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}})

    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(handler))
    recognition = MovieImageInfo(
        cinema_name="义乌万达广场店", city="义乌", movie_name="奥德赛",
        date_text="今天 08月25日", showtime_start="16:20", hall_name="IMAX厅",
        selected_count_visible=0, confidence=0.95,
    )

    cinema, _, showtime, _ = await service._resolve_cinema_by_showtime(
        settings, service._fixed_account(settings), recognition, "2026-08-25",
    )

    await service.aclose()
    assert queried == ["cinema-yiwu"]
    assert cinema["city_name"] == "金华"
    assert cinema["cinema_name"] == "义乌万达广场店"
    assert showtime["showtimeId"] == "showtime-1"


@pytest.mark.asyncio
async def test_missing_city_short_venue_alias_is_kept_for_official_showtime_resolution(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "cinema-yx", "city-wx", "无锡", "宜兴万达广场店",
                "宜兴市宜城街道东虹路550号万达广场3楼", "", "{}", 1,
            ),
        )
    queried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cinema_id = request.url.params.get("cinemaId") or ""
        queried.append(cinema_id)
        if cinema_id == "cinema-yx":
            return httpx.Response(200, json=_showtime_response())
        return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}})

    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(handler))
    recognition = MovieImageInfo(
        cinema_name="万达影城（宜兴IMAX店）", movie_name="奥德赛",
        date_text="今天 08月25日", showtime_start="16:20", hall_name="9号IMAX厅",
        selected_count_visible=0, confidence=0.95, missing_fields=["city"],
    )

    cinema, _, showtime, _ = await service._resolve_cinema_by_showtime(
        settings, service._fixed_account(settings), recognition, "2026-08-25",
    )

    await service.aclose()
    assert queried == ["cinema-yx"]
    assert cinema["city_name"] == "无锡"
    assert cinema["cinema_name"] == "宜兴万达广场店"
    assert showtime["showtimeId"] == "showtime-1"


@pytest.mark.asyncio
async def test_missing_city_candidate_preserves_high_tech_district_as_venue_identity(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany("INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("cinema-xa", "city-xa", "西安", "西安高新万达广场店", "西安市高新区唐延路万达广场3楼", "", "{}", 1),
            ("cinema-dl", "city-dl", "大连", "大连高新万达广场店", "大连市高新园区黄浦路万达广场4楼", "", "{}", 1),
        ])
    queried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cinema_id = request.url.params.get("cinemaId") or ""
        queried.append(cinema_id)
        if cinema_id == "cinema-xa":
            return httpx.Response(200, json=_showtime_response())
        return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}})

    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(handler))
    recognition = MovieImageInfo(
        cinema_name="万达影城（高新万达广场杜比影院店）", movie_name="奥德赛",
        date_text="今天 08月25日", showtime_start="16:20", hall_name="杜比影院厅",
        selected_count_visible=0, confidence=0.95, missing_fields=["city"],
    )

    cinema, _, showtime, _ = await service._resolve_cinema_by_showtime(
        settings, service._fixed_account(settings), recognition, "2026-08-25",
    )

    await service.aclose()
    assert set(queried) == {"cinema-xa", "cinema-dl"}
    assert cinema["city_name"] == "西安"
    assert cinema["cinema_name"] == "西安高新万达广场店"
    assert showtime["showtimeId"] == "showtime-1"


@pytest.mark.asyncio
async def test_unverified_hallucinated_city_falls_back_after_direct_showtime_miss(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany("INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("cinema-qj", "city-qj", "曲靖", "曲靖经开万达广场店", "曲靖市三江大道万达广场4楼", "", "{}", 1),
            ("cinema-wh", "city-wh", "武汉", "万达影城（经开万达广场IMAX激光店）", "武汉市经济技术开发区东风大道111号", "", "{}", 1),
        ])
    captured: list[dict[str, object]] = []
    base_handler = _handler(captured, member_price_cents=5290)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/showtime/by_cinema.api"):
            cinema_id = request.url.params.get("cinemaId") or ""
            if cinema_id == "cinema-wh":
                return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}})
            if cinema_id != "cinema-qj":
                return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}})
        return base_handler(request)

    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(
        settings, transport=httpx.MockTransport(handler), now_provider=quote_test_now,
    )
    recognition = MovieImageInfo(
        cinema_name="万达影城（经开万达广场IMAX激光店）", city="武汉", movie_name="奥德赛",
        date_text="今天 08月25日", showtime_start="16:20", hall_name="16号-激光IMAX-COLA厅",
        selected_count_visible=0, confidence=0.95, missing_fields=["city"],
    )

    quote = await service.quote(recognition)

    await service.aclose()
    assert quote.matched_city_name == "曲靖"
    assert quote.matched_cinema_name == "曲靖经开万达广场店"
    assert quote.quote_scope == "area_preview"


@pytest.mark.asyncio
async def test_platform_outlet_alias_matches_official_cinema_address(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "5867", "city-bj", "北京", "北京万达影城房山店",
                "北京市房山区长阳镇首创奥特莱斯二期4层万达影城", "", "{}", 1,
            ),
        )
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    matched = await service._resolve_cinema(settings, MovieImageInfo(
        cinema_name="万达影城（房山首创奥莱激光IMAX店）", movie_name="奥德赛",
        date_text="今天 08月25日", showtime_start="19:10",
        selected_count_visible=0, confidence=0.95, missing_fields=["city"],
    ))

    await service.aclose()
    assert matched["cinema_id"] == "5867"
    assert matched["cinema_name"] == "北京万达影城房山店"


@pytest.mark.asyncio
async def test_platform_truncated_store_alias_matches_official_cinema_address(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "400", "city-cc", "长春", "长春万达影城欧亚大卖场店",
                "吉林省长春市欧亚卖场15号门四楼", "", "{}", 1,
            ),
        )
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    matched = await service._resolve_cinema(settings, MovieImageInfo(
        cinema_name="万达影城（欧亚卖场IMAX店全天...）", city="长春", movie_name="欢迎来龙餐馆",
        date_text="今天 08月25日", showtime_start="17:10", selected_count_visible=0,
        confidence=0.95, missing_fields=[],
    ))

    await service.aclose()
    assert matched["cinema_id"] == "400"
    assert matched["cinema_name"] == "长春万达影城欧亚大卖场店"


@pytest.mark.asyncio
async def test_platform_street_alias_uniquely_matches_official_cinema_address(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany("INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("324", "city-jn", "济南", "济南魏家庄万达广场店", "济南市市中区经四路5号万达广场娱乐楼5楼", "", "{}", 1),
            ("241", "city-jn", "济南", "济南高新万达广场店", "济南市高新区工业南路57号万达广场娱乐楼5层", "", "{}", 1),
        ])
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    completed = await service.complete_cinema(MovieImageInfo(
        cinema_name="济南万达影城（经四路IMAX店）", city="济南",
        movie_name="蜘蛛侠：崭新之日", date_text="今天 08月25日",
        showtime_start="19:45", selected_count_visible=0, confidence=0.95,
    ))

    await service.aclose()
    assert completed.cinema_name == "济南魏家庄万达广场店"
    assert completed.city == "济南"


@pytest.mark.asyncio
async def test_city_hint_is_accepted_only_when_it_exists_in_official_cinema_cache(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    assert await service.is_known_city_hint("昆明") is True
    assert await service.is_known_city_hint("昆明市") is True
    assert await service.is_known_city_hint("昆明西山") is True
    assert await service.canonical_city_hint("昆明西山") == "昆明"
    assert await service.is_known_city_hint("昆明万达") is True
    assert await service.canonical_city_hint("昆明万达") == "昆明"
    assert await service.is_known_city_hint("可以") is False

    await service.aclose()


@pytest.mark.asyncio
async def test_one_mall_brand_suffix_does_not_block_unique_chinese_venue_match(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "5815", "city-sz", "深圳", "深圳万达影城盐田壹海城店",
                "深圳市盐田区海山街道海景二路万科ONE MALL三楼万达影城", "", "{}", 1,
            ),
        )
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    completed = await service.complete_cinema(MovieImageInfo(
        cinema_name="万达影城（盐田壹海城ONE MALL店）", movie_name="欢迎来龙餐馆",
        date_text="今天 08月26日", showtime_start="22:10",
    ))

    await service.aclose()
    assert completed.city == "深圳"
    assert completed.cinema_name == "深圳万达影城盐田壹海城店"


@pytest.mark.asyncio
async def test_cinema_name_city_prefix_completes_missing_city_and_truncated_store(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "cinema-dq", "city-dq", "大庆", "大庆万达影城联想科技城店",
                "大庆市高新区博学大街30号联想科技城", "", "{}", 1,
            ),
        )
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    completed = await service.complete_cinema(MovieImageInfo(
        cinema_name="大庆万达影城联想科技...", movie_name="欢迎来龙餐馆",
        date_text="明天 08月27日", showtime_start="10:00", missing_fields=["city"],
    ))

    await service.aclose()
    assert completed.city == "大庆"
    assert completed.cinema_name == "大庆万达影城联想科技城店"
    assert "city" not in completed.missing_fields


@pytest.mark.asyncio
async def test_unique_short_cinema_identity_completes_truncated_wanda_name(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("256", "city-cd", "成都", "成都蜀都万达广场店", "成都市郫都区望丛东路139号万达广场4楼", "", "{}", 1),
        )
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    completed = await service.complete_cinema(MovieImageInfo(
        cinema_name="万达影城（蜀都...", movie_name="奥德赛", date_text="后天 08月26日",
        showtime_start="09:40", selected_count_visible=0, confidence=0.9,
    ))

    await service.aclose()
    assert completed.cinema_name == "成都蜀都万达广场店"
    assert completed.city == "成都"


@pytest.mark.asyncio
async def test_missing_city_truncated_name_keeps_unique_full_alias_for_showtime_verification(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("5779", "city-gg", "贵港", "贵港万达广场店", "贵港市布山大道与仙衣路交叉口贵港万达广场3楼", "", "{}", 1),
                ("681", "city-gg", "贵港", "贵港万达影城步步高店", "贵港市建设路与解放路交汇处步步高广场5楼", "", "{}", 1),
            ],
        )
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "万达影城（贵港万...", "city": None, "movie_name": "八仙！",
        "date_text": "明天 8月26日", "showtime_start": "14:05", "showtime_end": "16:29",
        "hall_name": "5号-Real3D激光厅", "selected_seats": [{"seat_number": "9排7座"}],
        "selected_count_visible": 1, "missing_fields": ["city"], "confidence": 0.95,
    })
    showtime = _showtime_response()
    item = showtime["data"]["showtimeFilmInf"][0]
    item["filmName"] = "八仙！"
    item["showtimeFilmDateInf"][0]["showtimesInf"]["showtimeList"][0].update({
        "realtime": int(datetime(2026, 8, 26, 14, 5, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000),
        "hallName": "5号-Real3D激光厅",
    })

    async def official_get(_account, _origin, _path, query, **_kwargs):
        cinema_id = dict(query).get("cinemaId")
        return showtime if cinema_id == "5779" else {"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}}

    service._official_get = official_get  # type: ignore[method-assign]
    cinema, _, matched, movie = await service._resolve_cinema_by_showtime(
        settings, {"token": "test"}, recognition, "2026-08-26",
    )

    await service.aclose()
    assert cinema["cinema_id"] == "5779"
    assert cinema["cinema_name"] == "贵港万达广场店"
    assert matched["showtimeId"] == "showtime-1"
    assert movie == "八仙！"


@pytest.mark.asyncio
async def test_chinese_truncated_format_fragment_uses_city_showtime_to_disambiguate(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.executemany(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "378", "city-wh", "芜湖", "芜湖镜湖万达广场店",
                    "芜湖市镜湖区弋江路与赭山东路交叉口万达广场", "", "{}", 1,
                ),
                (
                    "7082", "city-wh", "芜湖", "芜湖万达影城弋江万达广场店",
                    "芜湖市弋江区柏庄时代广场万达影城", "", "{}", 1,
                ),
            ],
        )
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    recognition = MovieImageInfo(
        cinema_name="万达影城 (芜湖万达广场激...", city="芜湖", movie_name="奥德赛",
        date="2026-08-28", date_text="明天 08月28日", showtime_start="12:50",
        showtime_end="15:42", hall_name="IMAX激光厅 银幕",
    )
    showtime = _showtime_response()
    item = showtime["data"]["showtimeFilmInf"][0]
    item["showtimeFilmDateInf"][0]["showtimesInf"]["showtimeList"][0].update({
        "realtime": int(datetime(2026, 8, 28, 12, 50, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000),
        "hallName": "IMAX激光厅 银幕",
    })

    async def official_get(_account, _origin, _path, query, **_kwargs):
        return showtime if dict(query).get("cinemaId") == "378" else {
            "code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []},
        }

    service._official_get = official_get  # type: ignore[method-assign]
    cinema, _, matched, movie = await service._resolve_cinema_by_showtime(
        settings, {"token": "test"}, recognition, "2026-08-28",
    )

    await service.aclose()
    assert cinema["cinema_id"] == "378"
    assert cinema["cinema_name"] == "芜湖镜湖万达广场店"
    assert matched["showtimeId"] == "showtime-1"
    assert movie == "奥德赛"


@pytest.mark.asyncio
async def test_truncated_format_fragment_does_not_block_unique_cinema_completion(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "INSERT INTO cinemas VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("590", "city-tj", "天津", "天津河东万达广场店", "天津市河东区津滨大道53号万达广场娱乐楼四层", "", "{}", 1),
        )
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    completed = await service.complete_cinema(MovieImageInfo(
        cinema_name="万达影城（河东激光 IM...", movie_name="汪汪队立大功大电影3：勇闯恐龙岛",
        date_text="今天 8月25日", showtime_start="11:55", selected_count_visible=0, confidence=0.95,
    ))

    await service.aclose()
    assert completed.cinema_name == "天津河东万达广场店"
    assert completed.city == "天津"


@pytest.mark.asyncio
async def test_unique_nearby_official_showtime_corrects_confident_vision_time_error(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    payload = _showtime_response()
    showtime = payload["data"]["showtimeFilmInf"][0]["showtimeFilmDateInf"][0]["showtimesInf"]["showtimeList"][0]
    showtime.update({
        "realtime": int(datetime(2026, 8, 27, 16, 20, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000),
        "hallName": "16号-激光IMAX-COLA厅(儿童须购票)",
        "filmList": [{"filmName": "奥德赛", "duration": 173}],
    })
    recognition = MovieImageInfo(
        city="昆明", cinema_name="昆明西山万达广场店", movie_name="奥德赛",
        date="2026-08-27", showtime_start="16:33", showtime_end="19:22",
        hall_name="16号-激光IMAX-COLA厅",
    )

    matched, movie = service._match_showtime(payload, recognition, "2026-08-27")
    await service.aclose()

    assert matched["showtimeId"] == "showtime-1"
    assert movie == "奥德赛"


@pytest.mark.asyncio
async def test_selected_regular_seat_uses_its_exact_official_area_even_when_wplus_is_available(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date_text": "2026-08-25 16:20 - 19:13", "showtime_start": "16:20", "showtime_end": "19:13",
        "hall_name": "16号-激光IMAX-COLA厅",
        "selected_seats": [{"seat_number": "6排16座", "displayed_price": 72}],
        "selected_count_visible": 1, "confidence": 0.92,
    })
    transport = httpx.MockTransport(_handler(captured))
    service = WandaDirectQuoteService(
        settings, transport=transport, now_provider=quote_test_now,
        pricing_rules=PricingRulesUpdate(enabled=True, regular_adjustment_cents=100),
    )
    quote = await service.quote(recognition)
    await service.aclose()

    assert quote.quote_scope == "exact_seats"
    assert quote.quote_date == date(2026, 8, 25)
    assert [item.seat_number for item in quote.seat_quotes] == ["6排16座"]
    assert quote.seat_zone_type == "普通区"
    assert quote.member_unit_price_cents == 6190
    assert quote.seat_type == "regular"
    assert quote.base_unit_cents == 7290
    assert quote.original_unit_price_cents == 7290
    assert quote.base_total_cents == 7290
    assert quote.price_source == "realtime_regular_area"
    assert quote.unit_quote_cents == 6290
    assert quote.ticket_count == 1
    assert quote.total_quote_cents == 6290
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]
    assert "只读" in quote.pricing_source
    assert "锁座" not in quote.pricing_source
    assert "探针" not in quote.detail
    assert all(item["token"] == "fixed-official-token" for item in captured)
    assert captured[0]["channel"] == "1_3"
    assert captured[1]["channel"] == "1_2"


@pytest.mark.asyncio
async def test_selected_ordinary_area_seat_can_disable_area_level_wplus_activity(
    tmp_path: Path,
) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured_paths: list[str] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        if request.url.path == "/showtime/by_cinema.api":
            payload = _showtime_response()
            ordinary = payload["data"]["showtimeFilmInf"][0]["showtimeFilmDateInf"][0]["showtimesInf"]["showtimeList"][0]["areaPriceList"][0]
            ordinary.update({"salesPrice": 8_890, "settlePrice": 8_090, "channelFee": 300})
            return httpx.Response(200, json=payload)
        if request.url.path == "/order/real_time_seat.api":
            payload = _realtime_response()
            payload["data"]["realtimeSeats"]["area"][0]["areaName"] = "按摩椅区"
            payload["data"]["realtimeSeats"]["area"][0]["wPlusActivity"] = {
                "activityCode": "wplus-ordinary-seat",
                "activityName": "W+会员专享优惠",
                "price": 7_676,
                "userLimitNum": 6,
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(handler),
        now_provider=quote_test_now,
        pricing_rules=PricingRulesUpdate(
            enabled=True,
            wplus_friday_member_day_enabled=False,
            regular_adjustment_cents=100,
            wplus_member_price_threshold_cents=6_000,
            wplus_adjustment_cents=290,
            rounding_increment_cents=10,
        ),
    )
    quote = await service.quote(MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [{"seat_number": "6排16座"}], "selected_count_visible": 1,
    }))
    await service.aclose()

    assert quote.quote_scope == "exact_seats"
    assert quote.seat_zone_type == "按摩椅区"
    assert quote.member_unit_price_cents == 8_090
    assert quote.original_unit_price_cents == 8_890
    assert quote.seat_quotes[0].member_price_cents == 8_090
    assert quote.seat_type == "regular"
    assert quote.price_source == "realtime_regular_area"
    assert quote.unit_quote_cents == 8_190
    assert quote.total_quote_cents == 8_190
    assert quote.channel_fee_total_cents == 300
    assert captured_paths == ["/showtime/by_cinema.api", "/order/real_time_seat.api"]


@pytest.mark.asyncio
async def test_enabled_friday_member_day_price_applies_to_area_activity_without_reclassifying_seat(
    tmp_path: Path,
) -> None:
    accounts, cache = _direct_files(tmp_path)
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/showtime/by_cinema.api":
            payload = _showtime_response()
            ordinary = payload["data"]["showtimeFilmInf"][0]["showtimeFilmDateInf"][0]["showtimesInf"]["showtimeList"][0]["areaPriceList"][0]
            ordinary.update({"salesPrice": 8_890, "settlePrice": 8_090, "channelFee": 300})
            return httpx.Response(200, json=payload)
        if request.url.path == "/order/real_time_seat.api":
            payload = _realtime_response()
            payload["data"]["realtimeSeats"]["area"][0]["areaName"] = "按摩椅区"
            payload["data"]["realtimeSeats"]["area"][0]["wPlusActivity"] = {
                "activityCode": "wplus-friday",
                "activityName": "W+周五会员日专享",
                "price": 7_676,
                "userLimitNum": 6,
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    service = WandaDirectQuoteService(
        settings, transport=httpx.MockTransport(handler), now_provider=quote_test_now,
        pricing_rules=PricingRulesUpdate(
            enabled=True, wplus_friday_member_day_enabled=True,
            regular_adjustment_cents=100, rounding_increment_cents=10,
        ),
    )
    quote = await service.quote(MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [{"seat_number": "6排16座"}], "selected_count_visible": 1,
    }))
    await service.aclose()

    assert quote.seat_zone_type == "按摩椅区"
    assert quote.seat_type == "regular"
    assert quote.member_unit_price_cents == 7_676
    assert quote.seat_quotes[0].member_price_cents == 7_676
    assert quote.unit_quote_cents == 7_780
    assert quote.total_quote_cents == 7_780


@pytest.mark.asyncio
async def test_no_selected_seat_prefers_realtime_wplus_member_activity_price(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [], "selected_count_visible": 0,
    })
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(captured, realtime_wplus_activity_price=5816)),
        now_provider=quote_test_now,
        pricing_rules=PricingRulesUpdate(enabled=True, regular_adjustment_cents=100, rounding_increment_cents=10),
    )

    quote = await service.quote(recognition)
    await service.aclose()

    assert quote.quote_scope == "area_preview"
    assert quote.member_unit_price_cents == 5816
    assert quote.base_unit_cents == 5816
    assert quote.base_total_cents is None
    assert quote.unit_quote_cents == 6000
    assert quote.total_quote_cents is None
    assert quote.needs_ticket_count is True
    assert "会员活动价" in quote.pricing_source
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_missing_realtime_wplus_activity_uses_verified_member_price_probe(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [], "selected_count_visible": 0,
    })
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(
            captured, realtime_wplus_activity_price=None, member_price_cents=5816,
        )),
        now_provider=quote_test_now,
        release_recheck_delays=(0,),
        pricing_rules=PricingRulesUpdate(enabled=True, regular_adjustment_cents=100, rounding_increment_cents=10),
    )

    quote = await service.quote(recognition)
    await service.aclose()

    assert quote.member_unit_price_cents == 5816
    assert quote.base_unit_cents == 5816
    assert quote.unit_quote_cents == 6000
    assert quote.same_type_probe_used is True
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
        "/order/create_order.api", "/order/order_status.api",
        "/mkt/activity/secret/list.api", "/order/cancel.api",
        "/order/order_status.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_member_price_probe_fails_closed_when_release_cannot_be_verified(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo(
        cinema_name="昆明西山万达广场店", city="昆明", movie_name="奥德赛",
        date="2026-08-25", showtime_start="16:20", selected_count_visible=0,
    )
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(
            captured, realtime_wplus_activity_price=None, release_ok=False,
        )),
        now_provider=quote_test_now,
        release_recheck_delays=(0, 0),
    )

    with pytest.raises(ProviderError, match="座位尚未确认恢复"):
        await service.quote(recognition)
    await service.aclose()

    paths = [item["path"] for item in captured]
    assert "/order/create_order.api" in paths
    assert "/order/cancel.api" in paths
    assert paths.count("/order/real_time_seat.api") == 3


@pytest.mark.asyncio
async def test_unavailable_selected_regular_seat_returns_same_type_reference_without_replacing_seat(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [{"seat_number": "6排16座"}],
        "selected_count_visible": 1, "confidence": 0.92,
    })
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(
            captured, exact_seat_available=False, add_same_type_seat=True,
        )),
        now_provider=quote_test_now,
    )
    with pytest.raises(ProviderError) as captured_error:
        await service.quote(recognition)
    await service.aclose()

    error = captured_error.value
    assert error.code == "wanda_selected_seat_unavailable_same_type_reference"
    assert "6排16座当前不可选" in error.message
    assert "同座位类型当前参考价：61.90一张" in error.message
    assert "按1张参考合计61.90元" in error.message
    assert "不能按原座位下单" in error.message
    assert "6排15座" not in error.message
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_unavailable_selected_wplus_seat_returns_same_wplus_type_reference(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [{"seat_number": "5排2座"}],
        "selected_count_visible": 1, "confidence": 0.92,
    })
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(
            captured, unavailable_seat_names={"5排2座"},
        )),
        now_provider=quote_test_now,
    )

    with pytest.raises(ProviderError) as captured_error:
        await service.quote(recognition)
    await service.aclose()

    error = captured_error.value
    assert error.code == "wanda_selected_seat_unavailable_same_type_reference"
    assert "5排2座当前不可选" in error.message
    assert "同座位类型当前参考价：62.90一张" in error.message
    assert "9排14座" not in error.message
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_unavailable_selected_regular_seat_does_not_fall_back_to_wplus_area(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [{"seat_number": "6排16座"}],
        "selected_count_visible": 1, "confidence": 0.92,
    })
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(captured, exact_seat_available=False)),
        now_provider=quote_test_now,
    )
    with pytest.raises(ProviderError, match="当前不可选"):
        await service.quote(recognition)
    await service.aclose()
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_selected_regular_quote_ignores_unrelated_temporary_order_failure_conditions(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [{"seat_number": "6排16座"}],
        "selected_count_visible": 1, "confidence": 0.92,
    })
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(
            captured, add_same_type_seat=True, fail_first_lock=True,
        )),
        now_provider=quote_test_now,
    )
    quote = await service.quote(recognition)
    await service.aclose()

    assert quote.quote_scope == "exact_seats"
    assert quote.unit_quote_cents == 6190
    assert quote.same_type_probe_used is False
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_no_explicit_seat_uses_read_only_wplus_area_price_without_probe(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "万达影城（昆明西山万达广场IMAX店）", "city": "昆明", "movie_name": "奥德赛",
        "date_text": "明天 (8月25日)", "showtime_start": "16:20",
        "hall_name": "99号厅（截图OCR可能有误）", "selected_count_visible": 0, "confidence": 0.82,
    })
    def fixed_now() -> datetime:
        return datetime(2026, 8, 24, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    transport = httpx.MockTransport(_handler(captured, member_price_cents=5290))
    service = WandaDirectQuoteService(settings, transport=transport, now_provider=fixed_now)
    quote = await service.quote(recognition)
    await service.aclose()

    assert quote.quote_scope == "area_preview"
    assert quote.seat_zone_type == "W+"
    assert quote.member_unit_price_cents == 6290
    assert quote.seat_type == "wplus"
    assert quote.base_unit_cents == 6290
    assert quote.base_total_cents is None
    assert quote.price_source == "realtime_wplus_area"
    assert quote.unit_quote_cents == 6290
    assert quote.total_quote_cents is None
    assert quote.needs_ticket_count is True
    assert quote.detail.startswith("已读取万达官方实时W+会员活动价")
    assert "7排8座" not in quote.detail
    assert "只读" in quote.pricing_source
    assert "锁座" not in quote.pricing_source
    assert "确认座位恢复" not in quote.detail
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_selected_regular_seat_uses_official_settle_member_price_when_wplus_is_unavailable(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [{"seat_number": "6排16座"}],
        "selected_count_visible": 1, "confidence": 0.9,
    })
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(captured, wplus_available=False)),
        now_provider=quote_test_now,
        pricing_rules=PricingRulesUpdate(enabled=True, regular_adjustment_cents=100),
    )

    quote = await service.quote(recognition)
    await service.aclose()

    assert quote.quote_scope == "exact_seats"
    assert quote.unit_quote_cents == 6290
    assert quote.total_quote_cents == 6290
    assert quote.member_unit_price_cents == 6190
    assert quote.seat_type == "regular"
    assert quote.base_unit_cents == 7290
    assert quote.base_total_cents == 7290
    assert quote.price_source == "realtime_regular_area"
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_selected_regular_quote_never_reads_temporary_order_status(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20",
        "selected_seats": [{"seat_number": "6排16座"}],
        "selected_count_visible": 1, "confidence": 0.9,
    })
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(captured, lock_status_timeouts=1)),
        order_status_timeout_seconds=0.25,
        lock_status_retry_delays=(0, 0),
        now_provider=quote_test_now,
    )
    quote = await service.quote(recognition)
    await service.aclose()

    assert quote.quote_scope == "exact_seats"
    assert quote.unit_quote_cents == 6190
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_selected_regular_quote_does_not_run_release_rechecks(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20", "hall_name": "16号-激光IMAX-COLA厅",
        "selected_seats": [{"seat_number": "6排16座"}], "selected_count_visible": 1, "confidence": 0.9,
    })
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(captured, release_http_errors=1)),
        release_recheck_delays=(0, 0),
        now_provider=quote_test_now,
    )
    quote = await service.quote(recognition)
    await service.aclose()
    assert quote.quote_scope == "exact_seats"
    assert quote.unit_quote_cents == 6190
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]


@pytest.mark.asyncio
async def test_selected_regular_quote_is_independent_of_cancel_release_state(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    captured: list[dict[str, object]] = []
    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "昆明西山万达广场店", "city": "昆明", "movie_name": "奥德赛",
        "date": "2026-08-25", "showtime_start": "16:20", "hall_name": "16号-激光IMAX-COLA厅",
        "selected_seats": [{"seat_number": "6排16座"}], "selected_count_visible": 1, "confidence": 0.9,
    })
    diagnostics = DiagnosticsStore()
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(_handler(captured, release_ok=False)),
        release_recheck_delays=(0, 0, 0),
        diagnostics=diagnostics,
        now_provider=quote_test_now,
    )
    quote = await service.quote(recognition)
    await service.aclose()
    assert quote.quote_scope == "exact_seats"
    assert quote.unit_quote_cents == 6190
    assert [item["path"] for item in captured] == [
        "/showtime/by_cinema.api", "/order/real_time_seat.api",
    ]
    assert not [
        entry for entry in diagnostics.recent(limit=50)
        if entry["event"] == "wanda_release_verification"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("date_text", "expected"),
    [
        ("今天 08月19日", "截图日期已经过期"),
        ("明天（周六）", "相对日期与星期不一致"),
    ],
)
async def test_stale_relative_dates_fail_before_any_official_request(
    tmp_path: Path,
    date_text: str,
    expected: str,
) -> None:
    accounts, cache = _direct_files(tmp_path)
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13800009083",
    )
    recognition = MovieImageInfo(
        cinema_name="昆明西山万达广场店", city="昆明", movie_name="奥德赛",
        date_text=date_text, showtime_start="16:20", confidence=0.8,
    )
    service = WandaDirectQuoteService(
        settings,
        transport=httpx.MockTransport(handler),
        now_provider=lambda: datetime(2026, 8, 25, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    with pytest.raises(ProviderError, match=expected):
        await service.quote(recognition)
    await service.aclose()
    assert called is False


@pytest.mark.asyncio
async def test_missing_fixed_account_fails_closed_without_contacting_wanda(tmp_path: Path) -> None:
    accounts, cache = _direct_files(tmp_path)
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    settings = Settings(
        wanda_account_pool_path=str(accounts), wanda_cinema_cache_path=str(cache),
        wanda_fixed_account_phone="13999999999",
    )
    service = WandaDirectQuoteService(settings, transport=httpx.MockTransport(handler))
    recognition = MovieImageInfo(
        cinema_name="昆明西山万达广场店", movie_name="奥德赛",
        date="2026-08-25", showtime_start="16:20", confidence=0.8,
    )
    with pytest.raises(ProviderError, match="固定万达W\\+账号"):
        await service.quote(recognition)
    await service.aclose()
    assert called is False


def test_image_chat_includes_direct_official_quote(tmp_path: Path) -> None:
    class StubRecognition:
        async def recognize(self, _image, _content_type, _buyer_message="", *, prior_recognitions=None):
            return MovieImageInfo(
                cinema_name="昆明西山万达广场店", movie_name="奥德赛",
                showtime_start="16:20", selected_count_visible=0, confidence=0.8,
            )

    class StubQuote:
        calls = 0

        async def quote(self, recognition: MovieImageInfo) -> RealQuote:
            self.calls += 1
            assert recognition.movie_name == "奥德赛"
            return RealQuote.model_validate({
                "quote_scope": "area_probe", "seat_zone_type": "W+",
                "unit_quote_cents": 6290, "seat_quotes": [], "needs_ticket_count": True,
                "base_unit_cents": 6290, "price_source": "realtime_wplus_area",
                "pricing_source": "万达官方实时W+区域原价（只读）", "detail": "W+专享区域只读参考价",
            })

    store = PersistentSettingsStore(
        tmp_path / "settings.json", protector=ReversibleProtector(), environment=Settings()
    )
    quote_service = StubQuote()
    client = TestClient(create_app(
        service=StubRecognition(), settings_store=store, quote_service=quote_service
    ))
    response = client.post(
        "/api/chat/image-messages",
        data={"conversation_id": "quote-chat", "message_text": "多少钱"},
        files={"image": ("seat.jpg", b"\xff\xd8\xfffixture", "image/jpeg")},
    )

    assert response.status_code == 200
    message = response.json()["message"]
    assert quote_service.calls == 1
    assert message["quote"]["unit_quote_cents"] == 6290
    assert "W+专享区官方参考价：62.90" in message["text"]
    assert "¥" not in message["text"]
    assert "锁座" not in message["text"]


@pytest.mark.asyncio
async def test_slow_showtime_request_is_hedged_without_waiting_for_the_stalled_call() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.25)
        return httpx.Response(200, json={"code": 0, "data": {"bizCode": 0, "showtimeFilmInf": []}})

    diagnostics = DiagnosticsStore()
    service = WandaDirectQuoteService(
        Settings(), transport=httpx.MockTransport(handler), diagnostics=diagnostics,
        showtime_hedge_delay_seconds=0.05,
    )
    payload = await service._official_get(
        {"token": "test-token"}, CINEMA_ORIGIN, "/showtime/by_cinema.api",
        [("cinemaId", "590"), ("showDate", "20260825"), ("json", "true")],
        channel=H5_CHANNEL, event="wanda_showtimes_response",
    )
    await service.aclose()

    assert payload["code"] == 0
    assert calls == 2
    assert any(item["event"] == "wanda_showtimes_hedged_request" for item in diagnostics.recent())


@pytest.mark.asyncio
async def test_slow_member_offer_read_is_hedged_while_probe_is_locked() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.25)
        return httpx.Response(200, json={"code": 0, "data": []})

    diagnostics = DiagnosticsStore()
    service = WandaDirectQuoteService(
        Settings(), transport=httpx.MockTransport(handler), diagnostics=diagnostics,
        showtime_hedge_delay_seconds=0.05,
    )
    payload = await service._official_app_request(
        {"token": "test-token"}, MARKETING_ORIGIN, "/mkt/activity/secret/list.api",
        method="GET", pairs=[("partition", "1-2"), ("orderId", "3"), ("did", "4")],
        event="wanda_member_offers_response",
    )
    await service.aclose()

    assert payload["code"] == 0
    assert calls == 2
    assert any(item["event"] == "wanda_member_offers_hedged_request" for item in diagnostics.recent())
