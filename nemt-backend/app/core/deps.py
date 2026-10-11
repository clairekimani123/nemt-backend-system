"""Request-scoped dependencies: tenant session, current user, RBAC guard."""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import apply_tenant, open_tenant_session, resolve_company_id
from app.core.permissions import Permission, Role, has_permission
from app.core.security import decode_token
from app.modules.users.models import User

_bearer = HTTPBearer(auto_error=False)


# ---- pre-auth routes (login, register, forgot-password): tenant comes from a header ----
async def get_tenant_db(
    x_tenant: str = Header(..., description="Company slug, e.g. 'acme-transport'"),
) -> AsyncIterator[AsyncSession]:
    company_id = await resolve_company_id(x_tenant)
    if company_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown company")
    async with open_tenant_session(company_id) as session:
        yield session


# ---- authenticated routes: tenant comes from the verified JWT ----
class Principal(BaseModel):
    user_id: UUID
    company_id: UUID


async def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if creds is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"}
        )
    claims = decode_token(creds.credentials, "access")
    return Principal(user_id=UUID(claims["sub"]), company_id=UUID(claims["cid"]))


async def get_db(principal: Principal = Depends(get_principal)) -> AsyncIterator[AsyncSession]:
    async with open_tenant_session(principal.company_id) as session:
        yield session


async def get_current_user(
    principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)
) -> User:
    user = await db.get(User, principal.user_id)  # RLS-scoped to the token's tenant
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    if user.role == Role.SUPER_ADMIN:
        # Decided from the DB row, not the token, so demotion takes effect immediately.
        await apply_tenant(db, user.company_id, bypass=True)
    return user


def require_permission(permission: Permission):  # type: ignore[no-untyped-def]
    async def guard(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user.role, permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return user

    return guard
