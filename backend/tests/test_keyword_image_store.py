from pathlib import Path

import pytest

from app.keyword_image_store import KeywordImageStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"protected:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("protected:")


def test_keyword_image_store_is_encrypted_tenant_scoped_and_content_addressed(tmp_path: Path) -> None:
    store = KeywordImageStore(tmp_path / "images", protector=PlainProtector())
    image = b"\x89PNG\r\n\x1a\n" + b"safe-image"

    first = store.save("tenant-a", image, "image/png", "../../reply.png")
    second = store.save("tenant-a", image, "image/png", "reply-again.png")

    assert first["asset_id"] == second["asset_id"]
    assert first["filename"].endswith(".png")
    loaded = store.get("tenant-a", first["asset_id"])
    assert loaded["data"] == image
    assert not store.exists("tenant-b", first["asset_id"])
    raw = next((tmp_path / "images").rglob("*.json")).read_text(encoding="utf-8")
    assert "safe-image" not in raw
    assert "../" not in raw


@pytest.mark.parametrize(
    ("data", "content_type", "error"),
    [
        (b"not-an-image", "image/png", "keyword_image_type_invalid"),
        (b"\x89PNG\r\n\x1a\nbody", "image/jpeg", "keyword_image_type_invalid"),
        (b"", "image/png", "keyword_image_size_invalid"),
    ],
)
def test_keyword_image_store_rejects_invalid_uploads(
    tmp_path: Path, data: bytes, content_type: str, error: str,
) -> None:
    store = KeywordImageStore(tmp_path / "images", protector=PlainProtector())

    with pytest.raises(ValueError, match=error):
        store.save("tenant-a", data, content_type, "reply.png")
