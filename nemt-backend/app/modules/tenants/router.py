from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.core.permissions import Permission, Role
from app.core.security import hash_password
from app.modules.audit import service as audit
from app.modules.tenants.models import Company
from app.modules.tenants.schemas import CompanyCreate, CompanyOut, CompanyUpdate
from app.modules.users.models import User

router = APIRouter(prefix="/platform", tags=["platform (super admin)"])
RESERVED_SLUGS = {"platform", "admin", "api", "www", "app", "support", "status", "login"}


@router.post("/companies", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
async def create_company(
    body: CompanyCreate, request: Request,
    actor: User = Depends(require_permission(Permission.PLATFORM_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    """Onboard a new transport company together with its first Company Admin."""
    if body.slug in RESERVED_SLUGS:
        raise HTTPException(status.HTTP_409_CONFLICT, "Slug is reserved")
    company = Company(name=body.name, slug=body.slug, timezone=body.timezone)
    db.add(company)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Slug already taken") from None
    db.add(User(
        company_id=company.id, email=body.admin_email, password_hash=hash_password(body.admin_password),
        full_name=body.admin_full_name, role=Role.COMPANY_ADMIN,
    ))
    await audit.record(db, action="platform.company_created", actor=actor, company_id=company.id,
                       resource_type="company", resource_id=company.id, request=request)
    await db.commit()
    return company


@router.get("/companies", response_model=list[CompanyOut])
async def list_companies(
    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
    _: User = Depends(require_permission(Permission.PLATFORM_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    q = select(Company).where(Company.is_platform.is_(False)).order_by(Company.created_at).limit(limit).offset(offset)
    return (await db.scalars(q)).all()


@router.patch("/companies/{company_id}", response_model=CompanyOut)
async def update_company(
    company_id: UUID, body: CompanyUpdate, request: Request,
    actor: User = Depends(require_permission(Permission.PLATFORM_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    company = await db.scalar(select(Company).where(Company.id == company_id, Company.is_platform.is_(False)))
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(company, k, v)
    await audit.record(db, action="platform.company_updated", actor=actor, company_id=company.id,
                       resource_type="company", resource_id=company.id, request=request,
                       meta={"fields": sorted(changes)})
    await db.commit()
    return company