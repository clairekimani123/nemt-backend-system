"""Super Admin platform tools: oversight across companies, Super Admin accounts, and the
full system reset that returns the installation to its first-run state.

All routes require `platform:manage` (Super Admin only). A Super Admin's DB session runs in
platform (bypass) context, set in core/deps.get_current_user from the DB row, not the token.
"""

import asyncio
import logging
import re
import shutil
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import is_lock_conflict
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission, Role
from app.core.security import hash_password, verify_password
from app.core.types import Email, Password
from app.modules.audit import service as audit
from app.modules.audit.models import AuditLog
from app.modules.auth.service import get_user_by_email, revoke_user_sessions
from app.modules.drivers.models import Driver
from app.modules.patients.models import Patient
from app.modules.realtime.manager import manager
from app.modules.tenants.models import Company
from app.modules.trips.models import Trip
from app.modules.users.models import User
from app.modules.users.schemas import UserOut
from app.modules.vehicles.models import Vehicle

log = logging.getLogger(__name__)
router = APIRouter(prefix="/platform", tags=["platform (super admin)"])
SUPER = Depends(require_permission(Permission.PLATFORM_MANAGE))

RESET_PHRASE = "RESET SYSTEM"
RESET_ATTEMPTS = 3  # retries when the reset loses a deadlock against in-flight requests
# Roles a Super Admin may give to someone inside a company. super_admin lives on the platform only.
TENANT_ROLES = set(Role) - {Role.SUPER_ADMIN}


# ======================================================================= overview
class CompanyStats(BaseModel):
    id: UUID
    name: str
    slug: str
    is_active: bool
    users_by_role: dict[str, int]
    patients: int
    drivers: int
    vehicles: int
    trips: int


class OverviewOut(BaseModel):
    companies: int
    active_companies: int
    users_by_role: dict[str, int]
    patients: int
    drivers: int
    vehicles: int
    trips: int
    per_company: list[CompanyStats]


async def _counts_by_company(db: AsyncSession, model) -> dict[UUID, int]:  # type: ignore[no-untyped-def]
    rows = (await db.execute(select(model.company_id, func.count()).group_by(model.company_id))).all()
    return {cid: n for cid, n in rows}


@router.get("/overview", response_model=OverviewOut)
async def overview(_: User = SUPER, db: AsyncSession = Depends(get_db)):
    companies = (await db.scalars(select(Company).where(Company.is_platform.is_(False)).order_by(Company.name))).all()
    roles = (await db.execute(select(User.company_id, User.role, func.count()).group_by(User.company_id, User.role))).all()
    patients = await _counts_by_company(db, Patient)
    drivers = await _counts_by_company(db, Driver)
    vehicles = await _counts_by_company(db, Vehicle)
    trips = await _counts_by_company(db, Trip)
    total_roles: dict[str, int] = {}
    per: dict[UUID, dict[str, int]] = {}
    for cid, role, n in roles:
        total_roles[role.value] = total_roles.get(role.value, 0) + n
        per.setdefault(cid, {})[role.value] = n
    return OverviewOut(
        companies=len(companies), active_companies=sum(c.is_active for c in companies), users_by_role=total_roles,
        patients=sum(patients.values()), drivers=sum(drivers.values()), vehicles=sum(vehicles.values()),
        trips=sum(trips.values()),
        per_company=[CompanyStats(id=c.id, name=c.name, slug=c.slug, is_active=c.is_active,
                                  users_by_role=per.get(c.id, {}), patients=patients.get(c.id, 0),
                                  drivers=drivers.get(c.id, 0), vehicles=vehicles.get(c.id, 0),
                                  trips=trips.get(c.id, 0)) for c in companies],
    )


# ======================================================================= company users
class CompanyAdminIn(BaseModel):
    email: Email
    password: Password
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)


class PlatformUserUpdate(BaseModel):
    is_active: bool | None = None
    role: Role | None = None


async def _company(db: AsyncSession, company_id: UUID) -> Company:
    company = await db.scalar(select(Company).where(Company.id == company_id, Company.is_platform.is_(False)))
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company


@router.get("/companies/{company_id}/users", response_model=list[UserOut])
async def company_users(company_id: UUID, role: Role | None = None, _: User = SUPER,
                        db: AsyncSession = Depends(get_db)):
    await _company(db, company_id)
    q = select(User).where(User.company_id == company_id)
    if role:
        q = q.where(User.role == role)
    return (await db.scalars(q.order_by(User.role, User.full_name).limit(500))).all()


@router.post("/companies/{company_id}/admins", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def add_company_admin(company_id: UUID, body: CompanyAdminIn, request: Request, actor: User = SUPER,
                            db: AsyncSession = Depends(get_db)):
    email = body.email
    await _company(db, company_id)
    if await get_user_by_email(db, company_id, email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use in that company")
    user = User(company_id=company_id, email=email, password_hash=hash_password(body.password),
                full_name=body.full_name, phone=body.phone, role=Role.COMPANY_ADMIN)
    db.add(user)
    await db.flush()
    await audit.record(db, action="platform.company_admin_created", actor=actor, company_id=company_id,
                       resource_type="user", resource_id=user.id, request=request)
    await db.commit()
    return user


@router.patch("/companies/{company_id}/users/{user_id}", response_model=UserOut)
async def update_company_user(company_id: UUID, user_id: UUID, body: PlatformUserUpdate, request: Request,
                              actor: User = SUPER, db: AsyncSession = Depends(get_db)):
    await _company(db, company_id)
    target = await db.scalar(select(User).where(User.id == user_id, User.company_id == company_id))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    changes = body.model_dump(exclude_unset=True)
    if "role" in changes and changes["role"] not in TENANT_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot assign this role inside a company")
    for k, v in changes.items():
        setattr(target, k, v)
    if changes.get("is_active") is False:
        await revoke_user_sessions(db, target.id)
    await audit.record(db, action="platform.user_updated", actor=actor, company_id=company_id,
                       resource_type="user", resource_id=target.id, request=request,
                       meta={"fields": sorted(changes)})
    await db.commit()
    return target


# ======================================================================= super admins
@router.get("/super-admins", response_model=list[UserOut])
async def list_super_admins(actor: User = SUPER, db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(User).where(User.company_id == actor.company_id, User.role == Role.SUPER_ADMIN)
                             .order_by(User.created_at))).all()


@router.post("/super-admins", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def add_super_admin(body: CompanyAdminIn, request: Request, actor: User = SUPER,
                          db: AsyncSession = Depends(get_db)):
    email = body.email
    if await get_user_by_email(db, actor.company_id, email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use")
    user = User(company_id=actor.company_id, email=email, password_hash=hash_password(body.password),
                full_name=body.full_name, phone=body.phone, role=Role.SUPER_ADMIN)
    db.add(user)
    await db.flush()
    await audit.record(db, action="platform.super_admin_created", actor=actor, resource_type="user",
                       resource_id=user.id, request=request)
    await db.commit()
    return user


@router.patch("/super-admins/{user_id}", response_model=UserOut)
async def set_super_admin_active(user_id: UUID, body: PlatformUserUpdate, request: Request, actor: User = SUPER,
                                 db: AsyncSession = Depends(get_db)):
    if body.role is not None or body.is_active is None:
        raise HTTPException(422, "Only is_active can be changed here")
    if user_id == actor.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot deactivate yourself")
    target = await db.scalar(select(User).where(User.id == user_id, User.company_id == actor.company_id,
                                                User.role == Role.SUPER_ADMIN))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    target.is_active = body.is_active
    if not body.is_active:
        await revoke_user_sessions(db, target.id)
    await audit.record(db, action="platform.super_admin_updated", actor=actor, resource_type="user",
                       resource_id=target.id, request=request, meta={"is_active": body.is_active})
    await db.commit()
    return target


# ======================================================================= system reset
class ResetPreviewOut(BaseModel):
    enabled: bool
    confirm_phrase: str = RESET_PHRASE
    will_delete: dict[str, int]
    will_preserve: dict[str, int]


class ResetIn(BaseModel):
    confirm: str
    password: str = Field(max_length=128)


class ResetOut(BaseModel):
    deleted: dict[str, int]
    needs_setup: bool = True


@router.get("/system/reset-preview", response_model=ResetPreviewOut)
async def reset_preview(_: User = SUPER, db: AsyncSession = Depends(get_db)):
    """What a reset would remove, so the confirmation dialog can show real numbers."""
    tables = (await db.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        " AND tablename NOT IN ('alembic_version', 'spatial_ref_sys', 'audit_logs', 'companies')"
        " ORDER BY tablename"))).scalars().all()
    counts: dict[str, int] = {}
    for t in tables:  # names come from the catalog, not from the request
        counts[t] = await db.scalar(text(f'SELECT count(*) FROM "{t}"')) or 0
    counts["companies"] = await db.scalar(select(func.count()).select_from(Company)
                                          .where(Company.is_platform.is_(False))) or 0
    preserved = {"audit_logs": await db.scalar(select(func.count()).select_from(AuditLog)) or 0,
                 "platform_company": 1}
    return ResetPreviewOut(enabled=settings.system_reset_enabled, will_delete=counts, will_preserve=preserved)


def _wipe_document_files() -> None:
    """Stored document files belong to the cleared companies. Best effort, after the commit."""
    root = Path(settings.storage_dir).resolve()
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.is_dir() and re.fullmatch(r"[0-9a-f-]{36}", child.name):
            shutil.rmtree(child, ignore_errors=True)


@router.post("/system/reset", response_model=ResetOut)
async def system_reset(body: ResetIn, request: Request, actor: User = SUPER, db: AsyncSession = Depends(get_db)):
    """Return the installation to its first-run state. Irreversible.

    Clears every operational table (all users, including Super Admins, every company except the
    platform row, patients, drivers, vehicles, trips, documents, sessions...) in ONE transaction.
    Keeps the schema, the platform row and the audit log. Afterwards the first Super Admin is
    created again through /setup (or the CLI bootstrap)."""
    if not settings.system_reset_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "System reset is disabled on this server")
    if body.confirm != RESET_PHRASE:
        raise HTTPException(422, f"Type '{RESET_PHRASE}' exactly to confirm")
    if not verify_password(actor.password_hash, body.password):  # fresh re-authentication
        await audit.record(db, action="platform.system_reset_denied", actor=actor, request=request,
                           meta={"reason": "bad_password"})
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password is incorrect")

    actor_id, platform_id = actor.id, actor.company_id
    for attempt in range(1, RESET_ATTEMPTS + 1):
        try:
            deleted = (await db.execute(text("SELECT app_system_reset()"))).scalar_one()
            break
        except DBAPIError as exc:
            # The rollback undoes the whole attempt; the next transaction gets the platform context
            # again from the session (core/db after_begin), so a retry starts from a clean slate.
            await db.rollback()
            if is_lock_conflict(exc) and attempt < RESET_ATTEMPTS:
                # Requests still running against the tables being cleared: Postgres picked the
                # reset as the deadlock victim. Let them finish, then try again.
                await asyncio.sleep(0.25 * attempt)
                continue
            log.error("system reset failed and was rolled back: %s", exc.orig.__class__.__name__)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "Reset could not complete and nothing was changed. Try again.") from None
    db.expunge_all()  # the actor's row is gone; don't let the ORM try to touch it
    db.add(AuditLog(company_id=platform_id, actor_user_id=actor_id, action="platform.system_reset",
                    resource_type="system", ip=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent", "")[:300],
                    meta={"deleted": deleted}))
    await db.commit()

    closed = await manager.close_all()                  # live sockets of deleted users
    await asyncio.to_thread(_wipe_document_files)
    log.warning("SYSTEM RESET by %s: %s (closed %d sockets)", actor_id, deleted, closed)
    return ResetOut(deleted=deleted)
