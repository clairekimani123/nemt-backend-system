"""First-run setup.

GET  /setup/status       public: is this a fresh installation (no Super Admin yet)?
POST /setup/super-admin  create the first Super Admin. Requires BOTH:
                           - a fresh installation (no active Super Admin), and
                           - the SETUP_TOKEN configured on the server (read from the server's
                             environment by whoever installs the system).
                         Without SETUP_TOKEN, setup is CLI-only (python -m app.scripts.bootstrap).
"""

import hmac

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.types import Email, Password
from app.modules.setup import service

router = APIRouter(prefix="/setup", tags=["setup"])


class SetupStatusOut(BaseModel):
    needs_setup: bool
    web_setup_enabled: bool  # false -> use the CLI bootstrap
    platform_slug: str = service.PLATFORM_SLUG


class SetupIn(BaseModel):
    setup_token: str = Field(min_length=1, max_length=200)
    email: Email
    password: Password
    full_name: str = Field(min_length=1, max_length=200)


class SetupOut(BaseModel):
    email: str
    platform_slug: str = service.PLATFORM_SLUG


def _web_setup_enabled() -> bool:
    return settings.setup_token is not None and len(settings.setup_token.get_secret_value()) >= 16


@router.get("/status", response_model=SetupStatusOut)
async def setup_status():
    return SetupStatusOut(needs_setup=await service.super_admin_count() == 0,
                          web_setup_enabled=_web_setup_enabled())


@router.post("/super-admin", response_model=SetupOut, status_code=status.HTTP_201_CREATED)
async def create_super_admin(body: SetupIn, request: Request):
    if not _web_setup_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Web setup is disabled on this server. Run: python -m app.scripts.bootstrap --email <you>")
    expected = settings.setup_token.get_secret_value()  # type: ignore[union-attr]
    if not hmac.compare_digest(body.setup_token.encode(), expected.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid setup token")
    user = await service.create_first_super_admin(body.email, body.password, " ".join(body.full_name.split()),
                                                  via="web", require_empty=True)
    if user is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This system is already set up")
    return SetupOut(email=user.email)
