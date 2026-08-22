from app.cinema_match_experiment import CinemaCandidate, match_cinema_by_title


CANDIDATES = [
    CinemaCandidate("1", "莆田万达广场店", "莆田"),
    CinemaCandidate("2", "莆田涵江万达广场店", "莆田"),
    CinemaCandidate("3", "武汉汉街万达广场店", "武汉"),
    CinemaCandidate("4", "武汉经开万达广场店", "武汉"),
]


def test_missing_city_fails_closed_before_title_scoring() -> None:
    result = match_cinema_by_title("万达影城（莆田万达广场激光IMAX店）", None, CANDIDATES)
    assert result.status == "needs_city"
    assert result.matched is None


def test_character_coverage_matches_a_truncated_mobile_title_inside_explicit_city() -> None:
    result = match_cinema_by_title("万达影城（莆田万达广场激光 IMAX...", "莆田", CANDIDATES)
    assert result.status == "matched"
    assert result.matched is not None
    assert result.matched.candidate.cinema_id == "1"
    assert result.matched.score >= 0.9


def test_more_matching_branch_characters_win_without_token_search() -> None:
    result = match_cinema_by_title("万达影城（武汉汉街IMAX激光店）", "武汉", CANDIDATES)
    assert result.status == "matched"
    assert result.matched is not None
    assert result.matched.candidate.cinema_id == "3"


def test_generic_same_city_title_stays_ambiguous() -> None:
    result = match_cinema_by_title("万达影城IMAX店", "武汉", CANDIDATES)
    assert result.status in {"ambiguous", "not_matched"}
    assert result.matched is None
