"""In-process pub/sub for the WebSocket bus.

Keyed by (company_id, role) so a broadcast to `dispatchers` in company A never reaches
company B or any driver. If you ever run multiple uvicorn workers, swap `broadcast` for a
Redis pub/sub or Postgres LISTEN/NOTIFY — the interface stays the same.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from fastapi import WebSocket
from pydantic import BaseModel

from app.core.permissions import Role

log = logging.getLogger(__name__)

# Every socket gets its own key, so ONE user may have several (two tabs, phone + laptop) and each
# tab keeps its own feed. Each socket remembers the login session (refresh-token family) it
# authenticated with, so signing that session out closes exactly its sockets.
SocketKey = tuple[UUID, UUID]  # (company_id, connection id)


@dataclass(frozen=True)
class Conn:
    ws: WebSocket
    role: Role
    user_id: UUID
    session_id: UUID


class ConnectionManager:
    def __init__(self) -> None:
        self._conns: dict[SocketKey, Conn] = {}
        self._by_company: dict[UUID, set[SocketKey]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def add(self, company_id: UUID, user_id: UUID, role: Role, ws: WebSocket, session_id: UUID) -> SocketKey:
        key: SocketKey = (company_id, uuid4())
        async with self._lock:
            self._conns[key] = Conn(ws, role, user_id, session_id)
            self._by_company[company_id].add(key)
        return key

    async def remove(self, key: SocketKey) -> None:
        async with self._lock:
            self._conns.pop(key, None)
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
            conn = self._conns.get(key)
            if conn is None:
                continue
            if roles is not None and conn.role not in roles:
                continue
            if user_ids is not None and conn.user_id not in user_ids:
                continue
            try:
                await conn.ws.send_json(payload)
                sent += 1
            except Exception as exc:                        # a closed socket must never break the fan-out
                log.warning("drop send to %s: %s", key, exc)
                await self.remove(key)
        return sent

    async def to_user(self, company_id: UUID, user_id: UUID, message: BaseModel) -> int:
        return await self.broadcast(company_id, message, user_ids={user_id})

    async def _close(self, match, code: int) -> int:  # type: ignore[no-untyped-def]
        async with self._lock:
            keys = [k for k, c in self._conns.items() if match(k, c)]
            conns = [self._conns.pop(k) for k in keys]
            for k in keys:
                self._by_company.get(k[0], set()).discard(k)
        for c in conns:
            try:
                await c.ws.close(code=code)
            except Exception:
                pass
        return len(conns)

    async def close_session(self, company_id: UUID, session_id: UUID, code: int = 4401) -> int:
        """Sign-out of one login session: close the sockets that authenticated with it."""
        return await self._close(lambda k, c: k[0] == company_id and c.session_id == session_id, code)

    async def close_user(self, company_id: UUID, user_id: UUID, code: int = 4401) -> int:
        """All of a user's sessions were revoked (password reset, deactivation)."""
        return await self._close(lambda k, c: k[0] == company_id and c.user_id == user_id, code)

    async def close_all(self, code: int = 4401) -> int:
        """Disconnect every socket (used by the system reset: old sessions must not linger)."""
        n = await self._close(lambda k, c: True, code)
        async with self._lock:
            self._by_company.clear()
        return n

    def stats(self) -> dict[str, Any]:
        return {"sockets": len(self._conns), "companies": len(self._by_company)}


# one manager per process
manager = ConnectionManager()