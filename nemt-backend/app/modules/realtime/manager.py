"""In-process pub/sub for the WebSocket bus.

Keyed by (company_id, role) so a broadcast to `dispatchers` in company A never reaches
company B or any driver. If you ever run multiple uvicorn workers, swap `broadcast` for a
Redis pub/sub or Postgres LISTEN/NOTIFY — the interface stays the same.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import WebSocket
from pydantic import BaseModel

from app.core.permissions import Role

log = logging.getLogger(__name__)

# A socket is identified by (company_id, user_id). It carries a role for fan-out.
SocketKey = tuple[UUID, UUID]


class ConnectionManager:
    def __init__(self) -> None:
        # (company_id, user_id) → (websocket, role, user_id)
        self._by_user: dict[SocketKey, tuple[WebSocket, Role, UUID]] = {}
        # company_id → set of SocketKeys
        self._by_company: dict[UUID, set[SocketKey]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def add(self, company_id: UUID, user_id: UUID, role: Role, ws: WebSocket) -> SocketKey:
        key: SocketKey = (company_id, user_id)
        async with self._lock:
            # If this user already has a socket, replace it (a driver reconnecting from a
            # new phone should not leave a ghost socket behind).
            old = self._by_user.get(key)
            if old is not None:
                try:
                    await old[0].close(code=1000)
                except Exception:
                    pass
            self._by_user[key] = (ws, role, user_id)
            self._by_company[company_id].add(key)
        return key

    async def remove(self, key: SocketKey) -> None:
        async with self._lock:
            self._by_user.pop(key, None)
            company_id = key[0]
            self._by_company.get(company_id, set()).discard(key)
            if not self._by_company.get(company_id):
                self._by_company.pop(company_id, None)

    async def broadcast(self, company_id: UUID, message: BaseModel, *, roles: set[Role] | None = None,
                        user_ids: set[UUID] | None = None) -> int:
        """Send `message` to every socket in `company_id` whose role is in `roles` and/or whose
        user id is in `user_ids`. Passing None for a filter means 'any'.

        Returns the number of sockets the message was sent to (or attempted — dropped sends are
        logged and skipped, not raised)."""
        payload = message.model_dump(mode="json")
        async with self._lock:
            keys = list(self._by_company.get(company_id, ()))
        sent = 0
        for key in keys:
            entry = self._by_user.get(key)
            if entry is None:
                continue
            ws, role, uid = entry
            if roles is not None and role not in roles:
                continue
            if user_ids is not None and uid not in user_ids:
                continue
            try:
                await ws.send_json(payload)
                sent += 1
            except Exception as exc:                        # a closed socket must never break the fan-out
                log.warning("drop send to %s: %s", key, exc)
                await self.remove(key)
        return sent

    async def to_user(self, company_id: UUID, user_id: UUID, message: BaseModel) -> int:
        return await self.broadcast(company_id, message, user_ids={user_id})

    def stats(self) -> dict[str, Any]:
        return {"sockets": len(self._by_user), "companies": len(self._by_company)}


# one manager per process
manager = ConnectionManager()