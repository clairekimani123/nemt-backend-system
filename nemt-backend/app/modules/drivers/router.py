from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud import Page, flush_or_409
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission, Role, has_permission
from app.core.security import decrypt_field, encrypt_field, hash_password
from app.modules.audit import service as audit
from app.modules.auth.service import get_user_by_email, revoke_user_sessions
from app.modules.drivers.models import Driver
from app.modules.drivers.schemas import DriverCreate, DriverOut, DriverUpdate
from app.modules.users.models import User

router = APIRouter(prefix="/drivers", tags=["drivers"])
READ = Depends(require_permission(Permission.DRIVERS_READ))
WRITE = Depends(require_permission(Permission.DRIVERS_WRITE))

_JOIN = and_(User.id == Driver.user_id, User.company_id == Driver.company_id)


def to_out(d: Driver, u: User, actor: User) -> DriverOut:
    out = DriverOut(
        id=d.id, user_id=u.id, email=u.email, full_name=u.full_name, phone=u.phone, is_active=u.is_active,
        employee_number=d.employee_number, license_last4=d.license_last4, license_state=d.license_state,
        license_expires_on=d.license_expires_on, medical_cert_expires_on=d.medical_cert_expires_on,
        wheelchair_certified=d.wheelchair_certified, hire_date=d.hire_date,
        emergency_contact_name=d.emergency_contact_name, emergency_contact_phone=d.emergency_contact_phone,
        archived_at=d.archived_at, created_at=d.created_at,
    )
    if d.license_number_enc and has_permission(actor.role, Permission.DRIVERS_WRITE):
        out.license_number = decrypt_field(d.license_number_enc)
    return out


async def _load(db: AsyncSession, actor: User, driver_id: UUID) -> tuple[Driver, User]:
    row = (await db.execute(select(Driver, User).join(User, _JOIN).where(
        Driver.id == driver_id, Driver.company_id == actor.company_id))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Driver not found")
    return row[0], row[1]


def _set_license(d: Driver, number: str | None) -> None:
    d.license_number_enc = encrypt_field(number) if number else None
    d.license_last4 = number[-4:] if number else None


@router.post("", response_model=DriverOut, status_code=status.HTTP_201_CREATED)
async def create_driver(body: DriverCreate, request: Request, actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    """Creates the login (users, role=driver) and the driver profile in ONE transaction."""
    if await get_user_by_email(db, actor.company_id, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use")
    user = User(company_id=actor.company_id, email=body.email, password_hash=hash_password(body.password),
                full_name=body.full_name, phone=body.phone, role=Role.DRIVER)
    db.add(user)
    await flush_or_409(db, "Email already in use")
    profile = body.model_dump(exclude={"email", "password", "full_name", "phone", "license_number"})
    driver = Driver(company_id=actor.company_id, user_id=user.id, **profile)
    _set_license(driver, body.license_number)
    db.add(driver)
    await flush_or_409(db, "Employee number already in use")
    await audit.record(db, action="driver.create", actor=actor, resource_type="driver",
                       resource_id=driver.id, request=request)
    await db.commit()
    await db.refresh(driver)
    return to_out(driver, user, actor)


@router.get("", response_model=list[DriverOut])
async def list_drivers(
    q: str | None = Query(default=None, min_length=2, max_length=100),
    wheelchair_certified: bool | None = None,
    include_archived: bool = False,
    page: Page = Depends(),
    actor: User = READ,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Driver, User).join(User, _JOIN).where(Driver.company_id == actor.company_id)
    if not include_archived:
        stmt = stmt.where(Driver.archived_at.is_(None))
    if q:
        stmt = stmt.where(func.lower(User.full_name).contains(q.lower(), autoescape=True))
    if wheelchair_certified is not None:
        stmt = stmt.where(Driver.wheelchair_certified.is_(wheelchair_certified))
    rows = (await db.execute(stmt.order_by(User.full_name, Driver.id).limit(page.limit).offset(page.offset))).all()
    return [to_out(d, u, actor) for d, u in rows]


@router.get("/{driver_id}", response_model=DriverOut)
async def get_driver(driver_id: UUID, actor: User = READ, db: AsyncSession = Depends(get_db)):
    d, u = await _load(db, actor, driver_id)
    return to_out(d, u, actor)


@router.patch("/{driver_id}", response_model=DriverOut)
async def update_driver(driver_id: UUID, body: DriverUpdate, request: Request, actor: User = WRITE,
                        db: AsyncSession = Depends(get_db)):
    d, u = await _load(db, actor, driver_id)
    changes = body.model_dump(exclude_unset=True)
    for key in ("full_name", "phone"):  # these live on the user row
        if key in changes:
            if key == "full_name" and changes[key] is None:
                raise HTTPException(422, "full_name cannot be null")
            setattr(u, key, changes.pop(key))
    if "license_number" in changes:
        _set_license(d, changes.pop("license_number"))
    if changes.get("wheelchair_certified", True) is None:
        raise HTTPException(422, "wheelchair_certified cannot be null")
    for k, v in changes.items():
        setattr(d, k, v)
    await flush_or_409(db, "Employee number already in use")
    await audit.record(db, action="driver.update", actor=actor, resource_type="driver", resource_id=d.id,
                       request=request, meta={"fields": sorted(body.model_dump(exclude_unset=True))})
    await db.commit()
    await db.refresh(d)
    return to_out(d, u, actor)


@router.delete("/{driver_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_driver(driver_id: UUID, request: Request, actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    """Offboarding: archive the profile, disable the login, and kill every active session."""
    d, u = await _load(db, actor, driver_id)
    d.archived_at, u.is_active = datetime.now(UTC), False
    await revoke_user_sessions(db, u.id)
    await audit.record(db, action="driver.archive", actor=actor, resource_type="driver",
                       resource_id=d.id, request=request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)