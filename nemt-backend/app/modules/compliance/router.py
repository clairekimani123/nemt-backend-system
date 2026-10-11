from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.core.permissions import Permission
from app.modules.documents.models import Document
from app.modules.drivers.models import Driver
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

router = APIRouter(prefix="/compliance", tags=["compliance"])


class ExpiringItem(BaseModel):
    kind: str  # driver_license | driver_medical_cert | vehicle_registration | vehicle_insurance | document
    owner_type: str
    owner_id: UUID
    label: str
    expires_on: date
    days_left: int  # negative = already expired


@router.get("/expirations", response_model=list[ExpiringItem])
async def expirations(
    days: int = Query(default=30, ge=0, le=365),
    actor: User = Depends(require_permission(Permission.COMPLIANCE_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Everything expired or expiring within `days`. The Notifications phase will run this
    nightly from Celery and alert the fleet manager; the query is already here."""
    today = datetime.now(UTC).date()
    cutoff = today + timedelta(days=days)
    cid = actor.company_id
    items: list[ExpiringItem] = []

    def add(kind: str, otype: str, oid: UUID, label: str, on: date) -> None:
        items.append(ExpiringItem(kind=kind, owner_type=otype, owner_id=oid, label=label,
                                  expires_on=on, days_left=(on - today).days))

    drivers = (await db.execute(
        select(Driver, User).join(User, and_(User.id == Driver.user_id, User.company_id == Driver.company_id))
        .where(Driver.company_id == cid, Driver.archived_at.is_(None)))).all()
    for d, u in drivers:
        if d.license_expires_on and d.license_expires_on <= cutoff:
            add("driver_license", "driver", d.id, u.full_name, d.license_expires_on)
        if d.medical_cert_expires_on and d.medical_cert_expires_on <= cutoff:
            add("driver_medical_cert", "driver", d.id, u.full_name, d.medical_cert_expires_on)

    for v in (await db.scalars(select(Vehicle).where(Vehicle.company_id == cid, Vehicle.archived_at.is_(None)))).all():
        if v.registration_expires_on and v.registration_expires_on <= cutoff:
            add("vehicle_registration", "vehicle", v.id, f"Unit {v.unit_number}", v.registration_expires_on)
        if v.insurance_expires_on and v.insurance_expires_on <= cutoff:
            add("vehicle_insurance", "vehicle", v.id, f"Unit {v.unit_number}", v.insurance_expires_on)

    docs = (await db.scalars(select(Document).where(
        Document.company_id == cid, Document.archived_at.is_(None),
        Document.expires_on.is_not(None), Document.expires_on <= cutoff))).all()
    for doc in docs:
        add("document", doc.owner_type.value, doc.owner_id, f"{doc.doc_type.value}: {doc.filename}", doc.expires_on)

    return sorted(items, key=lambda i: i.expires_on)