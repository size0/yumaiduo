from datetime import date

from app.models import CinemaCandidate, MovieImageInfo
from app.pending_cinema_candidate_store import PendingCinemaCandidateStore


def _recognition() -> MovieImageInfo:
    return MovieImageInfo(
        platform="良票",
        city="厦门",
        cinema_name="万达影城（世茂海峡广场IMAX店）",
        movie_name="奥德赛",
        date=date(2026, 8, 30),
        showtime_start="10:30",
        recognition_id="r-1",
        match_level="CANDIDATE",
        candidate_cinemas=[CinemaCandidate(cinema_id=10036, name="世茂海峡广场")],
    )


def test_candidate_store_round_trips_recognition(tmp_path) -> None:
    store = PendingCinemaCandidateStore(tmp_path / "candidates.json")
    store.save("107:334:buyer:chat", _recognition())

    loaded = store.get("107:334:buyer:chat")

    assert loaded is not None
    assert loaded.recognition_id == "r-1"
    assert loaded.date == date(2026, 8, 30)
    assert loaded.candidate_cinemas[0].cinema_id == 10036


def test_candidate_store_delete_removes_context(tmp_path) -> None:
    store = PendingCinemaCandidateStore(tmp_path / "candidates.json")
    store.save("key", _recognition())
    store.delete("key")

    assert store.get("key") is None
