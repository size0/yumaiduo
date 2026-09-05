from __future__ import annotations

from pathlib import Path


CANONICAL_FILES = [
    Path("backend/app/canonical_buyer_reply.py"),
    Path("backend/app/canonical_conversation_agent.py"),
    Path("backend/app/cinema_route_v2/service.py"),
    Path("backend/app/quote_v2/service.py"),
    Path("backend/app/recognition_v2/service.py"),
    Path("backend/app/seat_facts_v2/service.py"),
    Path("backend/app/show_resolve_v2/service.py"),
    Path("backend/app/wanda_cost_v2/service.py"),
    Path("backend/app/wanda_pricing_v2/service.py"),
]

FORBIDDEN_IMPORT_SNIPPETS = [
    "from .plugin_automation import",
    "from .chat_service import",
    "from .chat import",
    "from .service import MovieImageRecognitionService",
    "from .wanda_direct_quote import",
    "from .selected_seat_quote_service import",
    "from .liangpiao_exact_quote import",
]


def test_canonical_modules_do_not_import_legacy_owners() -> None:
    root = Path(__file__).resolve().parents[2]
    hits: list[str] = []
    for relpath in CANONICAL_FILES:
        text = (root / relpath).read_text(encoding="utf-8")
        for snippet in FORBIDDEN_IMPORT_SNIPPETS:
            if snippet in text:
                hits.append(f"{relpath}:{snippet}")
    assert hits == []
