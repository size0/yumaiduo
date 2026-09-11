from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
from pathlib import Path

from .config import Settings
from .errors import RecognitionError
from .service import MovieImageRecognitionService


async def recognize_file(path: Path) -> int:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        result = await MovieImageRecognitionService(Settings.from_env()).recognize(
            path.read_bytes(), content_type,
        )
    except (OSError, RecognitionError) as error:
        message = error.message if isinstance(error, RecognitionError) else str(error)
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "data": result.model_dump(mode="json")}, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="识别电影票或影院选座截图")
    parser.add_argument("image", type=Path, help="本地 JPG、PNG 或 WebP 图片路径")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(recognize_file(args.image)))


if __name__ == "__main__":
    main()
