"""Profile photos: validation, storage and serving.

The bytes go through the same encrypted Storage port as documents, under a server-generated
key ('<company>/<uuid>'), so the client's filename never touches the disk and no filesystem
path ever leaves the server. Photos are served only through authenticated endpoints.
"""

from datetime import UTC, datetime

from fastapi import HTTPException, Response, UploadFile, status

from app.core.base import new_id
from app.core.config import settings
from app.core.storage import Storage
from app.modules.users.models import User

ALLOWED = ("image/jpeg", "image/png", "image/webp")


def sniff(data: bytes) -> str | None:
    """The image type from the file's real first bytes (never the client's Content-Type)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def read_image(file: UploadFile) -> tuple[bytes, str]:
    limit = settings.max_avatar_kb * 1024
    data = await file.read(limit + 1)  # never buffer more than the limit
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"Photo exceeds {settings.max_avatar_kb} KB")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file")
    kind = sniff(data)
    if kind is None or (file.content_type or "") not in ALLOWED:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only JPEG, PNG or WebP images are accepted")
    return data, kind


async def replace(storage: Storage, user: User, data: bytes, content_type: str) -> str | None:
    """Store the new photo on `user`; returns the old storage key for the caller to delete
    AFTER the database commit (so a failed commit never leaves the user without a photo)."""
    key = f"{user.company_id}/{new_id()}"
    await storage.put(key, data)
    old = user.avatar_key
    user.avatar_key, user.avatar_content_type, user.avatar_updated_at = key, content_type, datetime.now(UTC)
    return old


def clear(user: User) -> str | None:
    old = user.avatar_key
    user.avatar_key = user.avatar_content_type = user.avatar_updated_at = None  # type: ignore[assignment]
    return old


async def serve(storage: Storage, user: User) -> Response:
    if not user.avatar_key or not user.avatar_content_type:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No profile photo")
    try:
        data = await storage.get(user.avatar_key)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No profile photo") from None
    return Response(content=data, media_type=user.avatar_content_type, headers={
        "Cache-Control": "private, no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })
