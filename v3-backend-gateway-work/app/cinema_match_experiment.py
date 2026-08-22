"""Offline-only cinema title matcher used for A/B evaluation.

This module never calls Wanda, changes production matching, or performs a
transaction.  It compares a recognized title against every official cinema in
one buyer-confirmed city and fails closed when the score or margin is weak.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_FORMAT_NOISE = re.compile(r"(?i)i-?max|prime|xland|cinity|cgs|4dx|gt|luxe|dolby")
_TEXT_NOISE = re.compile(r"(?:双)?激光|巨幕|杜比(?:影院|影厅)?|儿童需购票|万达(?:电影城|影城|电影)|电影院|电影城|影城|影院")
_PUNCTUATION = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_COMMON = frozenset("万达广场店城影院电影")


@dataclass(frozen=True)
class CinemaCandidate:
    cinema_id: str
    cinema_name: str
    city_name: str


@dataclass(frozen=True)
class ScoredCinema:
    candidate: CinemaCandidate
    score: float
    matched_characters: int
    query_coverage: float
    candidate_coverage: float


@dataclass(frozen=True)
class ExperimentalCinemaMatch:
    status: str
    matched: ScoredCinema | None
    alternatives: tuple[ScoredCinema, ...]
    reason: str


def _key(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = _FORMAT_NOISE.sub("", text)
    text = _TEXT_NOISE.sub("", text)
    text = _PUNCTUATION.sub("", text)
    return re.sub(r"店$", "", text)


def _lcs_length(left: str, right: str) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for left_character in left:
        current = [0]
        for index, right_character in enumerate(right, start=1):
            current.append(previous[index - 1] + 1 if left_character == right_character else max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def _bigrams(value: str) -> set[str]:
    return {value[index:index + 2] for index in range(max(0, len(value) - 1))}


def _score(raw_title: str, candidate: CinemaCandidate) -> ScoredCinema:
    query = _key(raw_title)
    official = _key(candidate.cinema_name)
    lcs = _lcs_length(query, official)
    query_coverage = lcs / len(query) if query else 0.0
    candidate_coverage = lcs / len(official) if official else 0.0
    query_bigrams = _bigrams(query)
    official_bigrams = _bigrams(official)
    union = query_bigrams | official_bigrams
    bigram_score = len(query_bigrams & official_bigrams) / len(union) if union else 0.0
    exact_bonus = 0.08 if query and official and (query == official or query in official or official in query) else 0.0
    score = min(1.0, 0.55 * query_coverage + 0.25 * candidate_coverage + 0.20 * bigram_score + exact_bonus)
    distinctive = sum(1 for character in query if character not in _COMMON and character in official)
    return ScoredCinema(candidate, round(score, 4), distinctive, round(query_coverage, 4), round(candidate_coverage, 4))


def match_cinema_by_title(
    raw_title: str | None,
    city: str | None,
    candidates: Iterable[CinemaCandidate],
    *,
    minimum_score: float = 0.72,
    minimum_margin: float = 0.08,
) -> ExperimentalCinemaMatch:
    if not str(city or "").strip():
        return ExperimentalCinemaMatch("needs_city", None, (), "explicit_city_required")
    query = _key(raw_title)
    if len(query) < 2:
        return ExperimentalCinemaMatch("not_matched", None, (), "cinema_title_missing")
    city_key = _key(city)
    city_candidates = [candidate for candidate in candidates if city_key and city_key in _key(candidate.city_name)]
    if not city_candidates:
        return ExperimentalCinemaMatch("not_matched", None, (), "city_not_in_catalog")
    ranked = tuple(sorted((_score(raw_title or "", candidate) for candidate in city_candidates), key=lambda item: (-item.score, -item.matched_characters, item.candidate.cinema_id)))
    top = ranked[0]
    second_score = ranked[1].score if len(ranked) > 1 else 0.0
    if top.score < minimum_score or top.matched_characters < 2:
        return ExperimentalCinemaMatch("not_matched", None, ranked[:3], "score_below_threshold")
    if len(ranked) > 1 and top.score - second_score < minimum_margin:
        return ExperimentalCinemaMatch("ambiguous", None, ranked[:3], "score_margin_too_small")
    return ExperimentalCinemaMatch("matched", top, ranked[:3], "unique_high_score")


def load_official_cinemas(path: str | Path) -> list[CinemaCandidate]:
    with sqlite3.connect(f"file:{Path(path)}?mode=ro&immutable=1", uri=True) as connection:
        rows = connection.execute("SELECT cinema_id, cinema_name, city_name FROM cinemas").fetchall()
    return [CinemaCandidate(str(cinema_id), str(cinema_name), str(city_name)) for cinema_id, cinema_name, city_name in rows]
