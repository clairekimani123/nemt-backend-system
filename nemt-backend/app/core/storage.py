"""File-storage PORT. Dev adapter writes Fernet-encrypted files to local disk (a laptop has
no KMS). The production S3 adapter will rely on SSE-KMS + presigned URLs instead."""

import asyncio
import re
from pathlib import Path
from typing import Protocol

from app.core.config import settings
from app.core.security import decrypt_bytes, encrypt_bytes

_KEY_RE = re.compile(r"^[0-9a-f-]{36}/[0-9a-f-]{36}$")  # '<company_uuid>/<doc_uuid>' only


class Storage(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class LocalEncryptedStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key):  # blocks '../' path-traversal outright
            raise ValueError("invalid storage key")
        return self.root / f"{key}.bin"

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encrypt_bytes(data))

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        return decrypt_bytes(await asyncio.to_thread(path.read_bytes))

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, True)


def get_storage() -> Storage:
    return LocalEncryptedStorage(settings.storage_dir)