"""The /ws WebSocket endpoint.

Auth: the client connects, sends {"type":"auth", "token":"..."}, and the server either
answers auth_ok or closes with code 4401. A token in the URL would leak into access logs;
this keeps it in the message body.

Tenant: the token's `cid` claim decides the company. Every DB call inside the socket
opens a short-lived open_tenant_session(company_id) so RLS still applies.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select

from app.core.db import open_tenant_session
from app.core.permissions import Role
from app.core.security import decode_token
from app.modules.drivers.models import Driver
from app.modules.realtime import publisher
from app.modules.realtime.manager import manager
from app.modules.realtime.schemas import (
    AuthErr, AuthIn, AuthOk, LocationIn, PingIn, Pong,
)
from app.modules.realtime.models import DriverPosition
from app.modules.users.models import User
from app.core.geo import point

log = logging.getLogger(__name__)
router = APIRouter()

CLOSE_AUTH_FAILED = 4401
CLOSE_BAD_MESSAGE = 4400


async def _authenticate(token: str, ws: WebSocket) -> tuple[UUID, UUID, Role] | None:
    """Verify the JWT and load the user. Returns (company_id, user_id, role) or None."""
    try:
        claims = decode_token(token, "access")
    except Exception:
        await ws.send_json(AuthErr(detail="invalid token").model_dump(mode="json"))
        return None
    company_id = UUID(claims["cid"])
    user_id = UUID(claims["sub"])
    async with open_tenant_session(company_id) as db:
        user = await db.get(User, user_id)
    if user is None or not user.is_active:
        await ws.send_json(AuthErr(detail="user not found or inactive").model_dump(mode="json"))
        return None
    return company_id, user_id, user.role


async def _driver_id_for(company_id: UUID, user_id: UUID) -> UUID | None:
    async with open_tenant_session(company_id) as db:
        driver = await db.scalar(select(Driver).where(
            Driver.user_id == user_id, Driver.company_id == company_id))
    return driver.id if driver is not None else None


async def _persist_position(company_id: UUID, driver_id: UUID, msg: LocationIn) -> None:
    async with open_tenant_session(company_id) as db:
        db.add(DriverPosition(
            company_id=company_id,
            driver_id=driver_id,
            trip_id=msg.trip_id,
            location=point(msg.lat, msg.lng),
            speed_mps=msg.speed_mps,
            heading_deg=msg.heading_deg,
            accuracy_m=msg.accuracy_m,
            recorded_at=datetime.now(UTC),
        ))
        await db.commit()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    # ---- auth handshake ---------------------------------------------------------------
    try:
        first = await ws.receive_json()
    except Exception:
        await ws.close(code=CLOSE_BAD_MESSAGE)
        return
    try:
        hello = AuthIn.model_validate(first)
    except ValidationError:
        await ws.send_json(AuthErr(detail="first message must be {'type':'auth','token':'...'}").model_dump(mode="json"))
        await ws.close(code=CLOSE_BAD_MESSAGE)
        return

    auth = await _authenticate(hello.token, ws)
    if auth is None:
        await ws.close(code=CLOSE_AUTH_FAILED)
        return
    company_id, user_id, role = auth
    driver_id = await _driver_id_for(company_id, user_id) if role == Role.DRIVER else None

    key = await manager.add(company_id, user_id, role, ws)
    await ws.send_json(AuthOk(role=role, company_id=company_id).model_dump(mode="json"))

    # ---- message loop -----------------------------------------------------------------
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type") if isinstance(data, dict) else None

            if msg_type == "ping":
                await ws.send_json(Pong().model_dump(mode="json"))
                continue

            if msg_type == "location":
                if driver_id is None:
                    await ws.send_json(AuthErr(detail="only drivers may send location").model_dump(mode="json"))
                    continue
                try:
                    loc = LocationIn.model_validate(data)
                except ValidationError:
                    continue
                try:
                    await _persist_position(company_id, driver_id, loc)
                    await publisher.driver_position(company_id, driver_id, loc.lat, loc.lng, loc.trip_id)
                except Exception as exc:
                    log.warning("dropped bad location from %s: %s", driver_id, exc)
                continue

            # unknown types: ignore, per protocol
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("ws error for %s: %s", key, exc)
    finally:
        await manager.remove(key)