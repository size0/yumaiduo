from __future__ import annotations

DEPENDENCY_GRAPH: dict[str, tuple[str, ...]] = {
    "cinema": ("cinema_id", "show", "seat_facts", "cost", "pricing", "quote_record"),
    "movie": ("movie_id", "show", "seat_facts", "cost", "pricing", "quote_record"),
    "date": ("show", "seat_facts", "cost", "pricing", "quote_record"),
    "show": ("show_id", "seat_facts", "cost", "pricing", "quote_record"),
    "seat": ("cost", "pricing", "quote_record"),
    "ticket_count": ("pricing", "quote_record"),
}


def invalidated_fields(changed_field: str) -> tuple[str, ...]:
    return DEPENDENCY_GRAPH.get(changed_field, ())
