from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import urlparse

from fastapi import HTTPException, UploadFile, status
from qcloud_cos import CosConfig, CosS3Client

from .schemas import ImageUploadResponse


MAX_IMAGE_BYTES: Final = 5 * 1024 * 1024
CLEANUP_PREFIX: Final = "wanda-vision/"
PERSISTENT_REPLY_PREFIX: Final = "wanda-replies/"
CLEANUP_MAX_AGE: Final = timedelta(minutes=30)
CLEANUP_SCAN_LIMIT: Final = 1000
IMAGE_SIGNATURES: Final = {
    "image/png": ("png", lambda content: content.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": ("jpg", lambda content: content.startswith(b"\xff\xd8\xff")),
    "image/webp": (
        "webp",
        lambda content: len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
    ),
}


def _bucket_name(bucket_url: str) -> str:
    hostname = urlparse(bucket_url).hostname
    if not hostname or ".cos." not in hostname:
        raise ValueError("COS Bucket URL 格式不正确")
    return hostname.split(".cos.", maxsplit=1)[0]


class CosStorageService:
    async def upload_image(self, image: UploadFile, settings: dict[str, object], *, persistent: bool = False) -> ImageUploadResponse:
        if not settings["bucket_url"] or not settings["secret_id"] or not settings["secret_key"]:
            raise HTTPException(status_code=503, detail="尚未完成腾讯云 COS 配置")

        content_type = image.content_type or ""
        signature = IMAGE_SIGNATURES.get(content_type)
        if signature is None:
            raise HTTPException(status_code=415, detail="只支持 PNG、JPEG 或 WEBP 图片")

        content = await image.read(MAX_IMAGE_BYTES + 1)
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="图片大小必须在 1 字节至 5MB 之间")
        extension, is_expected_type = signature
        if not is_expected_type(content):
            raise HTTPException(status_code=415, detail="图片内容与声明的格式不一致")

        object_key = self._create_object_key(extension, persistent=persistent)
        try:
            await asyncio.to_thread(self._upload, content, content_type, object_key, settings)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(status_code=502, detail="腾讯云 COS 上传失败") from error

        public_url = f"{str(settings['bucket_url']).rstrip('/')}/{object_key}"
        return ImageUploadResponse(url=public_url, object_key=object_key)

    @staticmethod
    def _create_object_key(extension: str, *, persistent: bool = False) -> str:
        now = datetime.now(UTC)
        prefix = PERSISTENT_REPLY_PREFIX if persistent else CLEANUP_PREFIX
        return f"{prefix}{now:%Y/%m/%d}/{uuid.uuid4().hex}.{extension}"

    async def cleanup_expired_images(self, settings: dict[str, object]) -> int:
        """Delete one bounded batch of expired temporary recognition images."""
        if not settings["bucket_url"] or not settings["secret_id"] or not settings["secret_key"]:
            return 0
        cutoff = datetime.now(UTC) - CLEANUP_MAX_AGE
        try:
            return await asyncio.to_thread(self._cleanup, cutoff, settings)
        except Exception:
            # Cleanup is best-effort. It must never block the recognition service.
            return 0

    @staticmethod
    def _upload(content: bytes, content_type: str, object_key: str, settings: dict[str, object]) -> None:
        client = CosStorageService._create_client(settings)
        client.put_object(
            Bucket=_bucket_name(str(settings["bucket_url"])),
            Key=object_key,
            Body=content,
            ContentType=content_type,
        )

    @staticmethod
    def _cleanup(cutoff: datetime, settings: dict[str, object]) -> int:
        client = CosStorageService._create_client(settings)
        bucket = _bucket_name(str(settings["bucket_url"]))
        response = client.list_objects(Bucket=bucket, Prefix=CLEANUP_PREFIX, MaxKeys=CLEANUP_SCAN_LIMIT)
        expired_keys = [
            item["Key"]
            for item in response.get("Contents", [])
            if item.get("Key", "").startswith(CLEANUP_PREFIX)
            and CosStorageService._is_expired(item.get("LastModified"), cutoff)
        ]
        if not expired_keys:
            return 0
        client.delete_objects(Bucket=bucket, Delete={"Object": [{"Key": key} for key in expired_keys]})
        return len(expired_keys)

    @staticmethod
    def _is_expired(last_modified: object, cutoff: datetime) -> bool:
        if not isinstance(last_modified, str):
            return False
        try:
            modified_at = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
        except ValueError:
            return False
        if modified_at.tzinfo is None:
            modified_at = modified_at.replace(tzinfo=UTC)
        return modified_at < cutoff

    @staticmethod
    def _create_client(settings: dict[str, object]) -> CosS3Client:
        config = CosConfig(
            Region=str(settings["region"]),
            SecretId=str(settings["secret_id"]),
            SecretKey=str(settings["secret_key"]),
            Scheme="https",
        )
        return CosS3Client(config)
