from __future__ import annotations

from typing import Any

import pytest

from app.models import (
    CinemaCandidate,
    MovieCandidate,
    MovieImageInfo,
    RealQuote,
    SelectedSeat,
    ShowCandidate,
)
from app.plugin_automation import PluginAutomation
from app.recognition_snapshot_store import RecognitionSnapshotStore


IDENTITY = {
    "tenant_id": "tenant-1",
    "shop_id": "shop-1",
    "buyer_id": "buyer-1",
    "chat_id": "chat-1",
    "event_id": "buyer-choice-1",
}


class _UnusedQuote:
    async def quote(self, _recognition: MovieImageInfo):
        raise AssertionError("candidate resolution must not quote")


class _CapturingQuote:
    def __init__(self) -> None:
        self.calls: list[MovieImageInfo] = []

    async def quote(self, recognition: MovieImageInfo) -> RealQuote:
        self.calls.append(recognition)
        return RealQuote(
            quote_scope="exact_seats",
            seat_zone_type="LIANGPIAO",
            price_source="liangpiao_realtime_preflight",
            total_quote_cents=5000,
            ticket_count=1,
            pricing_source="test",
        )


class _ConfirmingRecognition:
    def __init__(self, confirmed: MovieImageInfo) -> None:
        self.confirmed = confirmed
        self.calls: list[dict[str, Any]] = []

    async def confirm_recognition_candidate(
        self,
        recognition_id: str,
        cinema_id: int | None = None,
        *,
        movie_id: int | None = None,
        show_id: str | None = None,
        city_name: str | None = None,
    ) -> MovieImageInfo:
        self.calls.append({
            "recognition_id": recognition_id,
            "cinema_id": cinema_id,
            "movie_id": movie_id,
            "show_id": show_id,
            "city_name": city_name,
        })
        return self.confirmed


def _create_snapshot(
    store: RecognitionSnapshotStore,
    recognition: MovieImageInfo,
    *,
    target_id: str = "image-target-1",
    event_id: str = "image-event",
):
    return store.create(
        tenant_id=IDENTITY["tenant_id"],
        shop_id=IDENTITY["shop_id"],
        buyer_id=IDENTITY["buyer_id"],
        chat_id=IDENTITY["chat_id"],
        event_id=f"{event_id}:{target_id}",
        target_id=target_id,
        recognition=recognition,
        raw_results=recognition.raw_results,
        final_results=recognition.final_results,
        raw_response=recognition.raw_response,
    )


def _refs(snapshot) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_revision": snapshot.revision,
        "target_id": snapshot.target_id,
    }


@pytest.mark.asyncio
async def test_resolve_cinema_uses_target_snapshot_cas_and_rejects_stale_revision(tmp_path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "recognitions.sqlite3")
    pending = MovieImageInfo(
        recognition_id="recognize-cinema",
        city="上海",
        match_level="CANDIDATE",
        candidate_cinemas=[
            CinemaCandidate(cinema_id=1001, name="影院A", city_name="上海"),
            CinemaCandidate(cinema_id=1002, name="影院B", city_name="上海"),
        ],
    )
    snapshot = _create_snapshot(store, pending)
    untouched = _create_snapshot(store, pending, target_id="image-target-2")
    confirmed = pending.model_copy(update={
        "cinema_id": 1002,
        "cinema_name": "影院B",
        "candidate_cinemas": [],
        "match_level": "EXACT",
        "raw_results": {"cinema": "影院B"},
        "final_results": {"cinemaId": 1002},
    })
    recognition = _ConfirmingRecognition(confirmed)
    automation = PluginAutomation(
        recognition,
        _UnusedQuote(),
        recognition_snapshot_store=store,
    )
    pending_targets = automation._current_resolution_targets(IDENTITY)
    assert [target["target_id"] for target in pending_targets] == [
        "image-target-1",
        "image-target-2",
    ]
    assert pending_targets[0]["snapshot_id"] == snapshot.snapshot_id
    assert pending_targets[0]["snapshot_revision"] == 1
    arguments = {
        **_refs(snapshot),
        "cinema_id": 1002,
        "buyer_message": "选影院B",
    }

    resolved = await automation._execute_agent_tool(
        "resolve_cinema",
        arguments,
        IDENTITY,
        envelope={"payload": {"content": "选影院B", "messageType": 1}},
    )
    stale = await automation._execute_agent_tool(
        "resolve_cinema",
        arguments,
        {**IDENTITY, "event_id": "buyer-choice-2"},
        envelope={"payload": {"content": "选影院B", "messageType": 1}},
    )

    assert resolved["ok"] is True
    assert resolved["snapshot_revision"] == 2
    assert recognition.calls == [{
        "recognition_id": "recognize-cinema",
        "cinema_id": 1002,
        "movie_id": None,
        "show_id": None,
        "city_name": "上海",
    }]
    current = store.get_current(**{key: IDENTITY[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")}, target_id="image-target-1")
    assert current is not None
    assert current.normalized.cinema_id == 1002
    assert current.confirmation_history[-1].details["tool"] == "resolve_cinema"
    assert store.get_current(**{key: IDENTITY[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")}, target_id="image-target-2") == untouched
    assert stale == {"ok": False, "error": "recognition_snapshot_revision_conflict"}
    assert [target["target_id"] for target in automation._current_resolution_targets(IDENTITY)] == [
        "image-target-2",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "choice_key", "choice_value", "buyer_message", "pending", "confirmed"),
    [
        (
            "resolve_movie",
            "movie_id",
            22,
            "选电影B",
            MovieImageInfo(
                recognition_id="recognize-movie",
                city="上海",
                match_level="CANDIDATE",
                candidate_movies=[
                    MovieCandidate(movie_id=21, name="电影A"),
                    MovieCandidate(movie_id=22, name="电影B"),
                ],
            ),
            MovieImageInfo(
                recognition_id="recognize-movie",
                city="上海",
                movie_id=22,
                movie_name="电影B",
                match_level="EXACT",
                final_results={"movieId": 22},
            ),
        ),
        (
            "resolve_showtime",
            "show_id",
            "show-2",
            "选20:30",
            MovieImageInfo(
                recognition_id="recognize-show",
                city="上海",
                match_level="CANDIDATE",
                candidate_shows=[
                    ShowCandidate(show_id="show-1", start_time="19:30"),
                    ShowCandidate(show_id="show-2", start_time="20:30"),
                ],
            ),
            MovieImageInfo(
                recognition_id="recognize-show",
                city="上海",
                show_id="show-2",
                showtime_start="20:30",
                match_level="EXACT",
                final_results={"showId": "show-2"},
            ),
        ),
    ],
)
async def test_snapshot_candidate_resolvers_call_official_confirm_and_record_history(
    tmp_path,
    tool_name: str,
    choice_key: str,
    choice_value: object,
    buyer_message: str,
    pending: MovieImageInfo,
    confirmed: MovieImageInfo,
) -> None:
    store = RecognitionSnapshotStore(tmp_path / f"{tool_name}.sqlite3")
    snapshot = _create_snapshot(store, pending)
    recognition = _ConfirmingRecognition(confirmed)
    automation = PluginAutomation(
        recognition,
        _UnusedQuote(),
        recognition_snapshot_store=store,
    )

    result = await automation._execute_agent_tool(
        tool_name,
        {
            **_refs(snapshot),
            choice_key: choice_value,
            "buyer_message": buyer_message,
        },
        IDENTITY,
        envelope={"payload": {"content": buyer_message, "messageType": 1}},
    )

    assert result["ok"] is True
    assert result["snapshot_revision"] == 2
    current = store.get_current(**{key: IDENTITY[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")}, target_id="image-target-1")
    assert current is not None
    assert current.confirmation_history[-1].details["tool"] == tool_name
    assert recognition.calls[0][choice_key] == choice_value


@pytest.mark.asyncio
async def test_resolve_image_conflict_confirms_provider_then_cas_persists_buyer_clarification(tmp_path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "conflict.sqlite3")
    pending = MovieImageInfo(
        recognition_id="recognize-conflict",
        cinema_id=1001,
        movie_id=21,
        city="上海",
        showtime_start=None,
        match_level="NONE",
        warnings=["conflict:showtime_start"],
        missing_fields=["showtime_start"],
    )
    snapshot = _create_snapshot(store, pending)
    recognition = _ConfirmingRecognition(pending)
    automation = PluginAutomation(
        recognition,
        _UnusedQuote(),
        recognition_snapshot_store=store,
    )

    result = await automation._execute_agent_tool(
        "resolve_image_conflict",
        {
            **_refs(snapshot),
            "buyer_message": "是20:30",
            "field_updates": {"showtime_start": "20:30"},
        },
        IDENTITY,
        envelope={"payload": {"content": "是20:30", "messageType": 1}},
    )

    assert result["ok"] is True
    assert result["snapshot_revision"] == 2
    assert result["recognition"]["showtime_start"] == "20:30"
    assert recognition.calls == [{
        "recognition_id": "recognize-conflict",
        "cinema_id": 1001,
        "movie_id": 21,
        "show_id": None,
        "city_name": "上海",
    }]
    current = store.get_current(**{key: IDENTITY[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")}, target_id="image-target-1")
    assert current is not None
    assert current.confirmation_history[-1].details["field_updates"] == {"showtime_start": "20:30"}


@pytest.mark.asyncio
async def test_snapshot_resolution_rejects_non_current_buyer_message_before_provider_call(tmp_path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "message.sqlite3")
    pending = MovieImageInfo(
        recognition_id="recognize-movie",
        candidate_movies=[MovieCandidate(movie_id=22, name="电影B")],
    )
    snapshot = _create_snapshot(store, pending)
    recognition = _ConfirmingRecognition(pending)
    automation = PluginAutomation(
        recognition,
        _UnusedQuote(),
        recognition_snapshot_store=store,
    )

    result = await automation._execute_agent_tool(
        "resolve_movie",
        {
            **_refs(snapshot),
            "movie_id": 22,
            "buyer_message": "选电影B",
        },
        IDENTITY,
        envelope={"payload": {"content": "不是这个", "messageType": 1}},
    )

    assert result == {"ok": False, "error": "movie_choice_buyer_confirmation_required"}
    assert recognition.calls == []


@pytest.mark.asyncio
async def test_quote_with_snapshot_never_reads_legacy_single_slot_candidate(tmp_path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "quote-target.sqlite3")
    exact = MovieImageInfo(
        recognition_id="recognize-exact",
        is_seat_selection=True,
        cinema_id=1001,
        cinema_name="影院A",
        city="上海",
        movie_id=21,
        movie_name="电影A",
        date_text="2026-09-01",
        showtime_start="20:30",
        show_id="show-1",
        hall_name="1号厅",
        selected_seats=[SelectedSeat(
            seat_number="8排7座", row_no=8, col_no=7, seat_no="8排7座",
        )],
        selected_count_visible=1,
        match_level="EXACT",
        seat_matched=True,
        price_mismatch=False,
    )
    snapshot = _create_snapshot(store, exact)
    quote = _CapturingQuote()
    automation = PluginAutomation(
        _ConfirmingRecognition(exact),
        quote,
        recognition_snapshot_store=store,
    )
    # Simulate an old compatibility slot belonging to another image.  A
    # snapshot-scoped quote must not consult or be blocked by it.
    automation._remember_pending_candidate(IDENTITY, MovieImageInfo(
        recognition_id="legacy-other-target",
        match_level="CANDIDATE",
        candidate_cinemas=[
            CinemaCandidate(cinema_id=2001, name="影院B"),
            CinemaCandidate(cinema_id=2002, name="影院C"),
        ],
    ))

    result = await automation._execute_agent_tool(
        "get_authoritative_quote",
        _refs(snapshot),
        IDENTITY,
        envelope={"payload": {"content": "这张多少钱", "messageType": 1}},
    )

    assert result["ok"] is True
    assert len(quote.calls) == 1
    assert quote.calls[0].cinema_id == 1001
    assert quote.calls[0].show_id == "show-1"


def test_show_list_candidates_are_persisted_as_a_snapshot_target_with_refs(tmp_path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "show-list.sqlite3")
    automation = PluginAutomation(
        _UnusedQuote(),
        _UnusedQuote(),
        recognition_snapshot_store=store,
    )

    result = automation._record_show_list_snapshot(
        IDENTITY,
        {
            "cinemaId": 1001,
            "movieId": 21,
            "showDate": "2026-09-01",
        },
        {
            "ok": True,
            "shows": [
                {"showId": "show-a", "startTime": "19:30", "hallName": "1号厅"},
                {"showId": "show-b", "startTime": "20:30", "hallName": "2号厅"},
            ],
        },
    )

    assert result["ok"] is True
    assert result["snapshot_revision"] == 1
    assert result["target_id"].startswith("show-list-")
    current = store.get_current(
        tenant_id=IDENTITY["tenant_id"], shop_id=IDENTITY["shop_id"],
        buyer_id=IDENTITY["buyer_id"], chat_id=IDENTITY["chat_id"],
        target_id=result["target_id"],
    )
    assert current is not None
    assert [item.show_id for item in current.normalized.candidate_shows] == [
        "show-a", "show-b",
    ]
    assert current.normalized.cinema_id == 1001
    assert current.normalized.movie_id == 21
    assert current.normalized.date_text == "2026-09-01"
    assert "source:show.list" in current.normalized.warnings


@pytest.mark.asyncio
async def test_show_list_snapshot_targets_do_not_overwrite_each_other_on_resolution(tmp_path) -> None:
    store = RecognitionSnapshotStore(tmp_path / "show-list-isolation.sqlite3")
    automation = PluginAutomation(
        _UnusedQuote(),
        _UnusedQuote(),
        recognition_snapshot_store=store,
    )
    first = automation._record_show_list_snapshot(
        {**IDENTITY, "event_id": "show-list-event-1"},
        {"cinemaId": 1001, "movieId": 21, "showDate": "2026-09-01"},
        {"ok": True, "shows": [
            {"showId": "show-a", "startTime": "19:30", "hallName": "1号厅"},
            {"showId": "show-b", "startTime": "20:30", "hallName": "2号厅"},
        ]},
    )
    second = automation._record_show_list_snapshot(
        {**IDENTITY, "event_id": "show-list-event-2"},
        {"cinemaId": 1002, "movieId": 22, "showDate": "2026-09-02"},
        {"ok": True, "shows": [
            {"showId": "show-c", "startTime": "21:30", "hallName": "3号厅"},
            {"showId": "show-d", "startTime": "22:30", "hallName": "4号厅"},
        ]},
    )

    resolved = await automation._execute_agent_tool(
        "resolve_showtime",
        {
            "snapshot_id": first["snapshot_id"],
            "snapshot_revision": first["snapshot_revision"],
            "target_id": first["target_id"],
            "show_id": "show-b",
            "buyer_message": "选20:30",
        },
        {**IDENTITY, "event_id": "show-list-choice"},
        envelope={"payload": {"content": "选20:30", "messageType": 1}},
    )

    assert resolved["ok"] is True
    assert resolved["snapshot_revision"] == 2
    first_current = store.get_current(
        tenant_id=IDENTITY["tenant_id"], shop_id=IDENTITY["shop_id"],
        buyer_id=IDENTITY["buyer_id"], chat_id=IDENTITY["chat_id"],
        target_id=first["target_id"],
    )
    second_current = store.get_current(
        tenant_id=IDENTITY["tenant_id"], shop_id=IDENTITY["shop_id"],
        buyer_id=IDENTITY["buyer_id"], chat_id=IDENTITY["chat_id"],
        target_id=second["target_id"],
    )
    assert first_current is not None and second_current is not None
    assert first_current.normalized.show_id == "show-b"
    assert first_current.normalized.candidate_shows == []
    assert [item.show_id for item in second_current.normalized.candidate_shows] == [
        "show-c", "show-d",
    ]
    assert second_current.revision == 1
