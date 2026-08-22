"""Compare current recorded cinema outcomes with the offline title matcher.

Input is a JSON array of sanitized image samples. No image, buyer identifier,
network request, seat lookup, or transaction is read or performed.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.cinema_match_experiment import CinemaCandidate, load_official_cinemas, match_cinema_by_title


def _normalized(value: Any) -> str:
    return "".join(character.lower() for character in str(value or "") if character.isalnum())


def _official_for_name(name: str, cinemas: list[CinemaCandidate]) -> CinemaCandidate | None:
    wanted = _normalized(name)
    exact = [cinema for cinema in cinemas if _normalized(cinema.cinema_name) == wanted]
    if len(exact) == 1:
        return exact[0]
    contained = [cinema for cinema in cinemas if wanted and (wanted in _normalized(cinema.cinema_name) or _normalized(cinema.cinema_name) in wanted)]
    return contained[0] if len(contained) == 1 else None


def evaluate(samples: list[dict[str, Any]], cinemas: list[CinemaCandidate]) -> dict[str, Any]:
    rows = []
    for sample in samples:
        recognition = sample.get("recognition") or sample.get("requested_match") or {}
        current = sample.get("current") or {}
        current_match = sample.get("current_match") or {}
        expected_name = str(current.get("matched_cinema") or current_match.get("cinema") or "").strip()
        expected = _official_for_name(expected_name, cinemas) if expected_name else None
        supplied_city = str(recognition.get("city") or sample.get("supplemented_city") or "").strip()
        buyer_result = match_cinema_by_title(recognition.get("cinema"), supplied_city, cinemas)
        after_city_result = match_cinema_by_title(recognition.get("cinema"), expected.city_name if expected else supplied_city, cinemas)
        rows.append({
            "sample_id": sample.get("sample_id"),
            "has_ground_truth": expected is not None,
            "city_initially_available": bool(supplied_city),
            "current_failure_code": current.get("failure_code") or sample.get("failure_code"),
            "buyer_city_status": buyer_result.status,
            "after_city_status": after_city_result.status,
            "after_city_agrees": bool(expected and after_city_result.matched and after_city_result.matched.candidate.cinema_id == expected.cinema_id),
            "after_city_candidate": after_city_result.matched.candidate.cinema_name if after_city_result.matched else None,
            "after_city_score": after_city_result.matched.score if after_city_result.matched else None,
        })
    ground_truth = [row for row in rows if row["has_ground_truth"]]
    agreements = sum(row["after_city_agrees"] for row in ground_truth)
    return {
        "sample_count": len(rows),
        "ground_truth_sample_count": len(ground_truth),
        "initial_city_available_count": sum(row["city_initially_available"] for row in rows),
        "initial_outcomes": dict(Counter(row["buyer_city_status"] for row in rows)),
        "after_city_outcomes": dict(Counter(row["after_city_status"] for row in ground_truth)),
        "after_city_agreement_count": agreements,
        "after_city_agreement_rate": round(agreements / len(ground_truth) * 100, 1) if ground_truth else None,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(json.loads(args.samples.read_text(encoding="utf-8")), load_official_cinemas(args.catalog))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
