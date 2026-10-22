from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.address import apply_address
from app.core.crud import Page, flush_or_409, get_or_404
from app.core.deps import get_db, require_permission
from app.core.permissions import Permission, Role, has_permission
from app.core.phi_audit import audit_phi_read
from app.core.security import blind_index
from app.modules.audit import service as audit
from app.modules.patients import service
from app.modules.patients.models import MobilityType, Patient, PatientAddress
from app.modules.patients.schemas import (
    PatientAddressCreate,
    PatientAddressOut,
    PatientAddressUpdate,
    PatientCreate,
    PatientOut,
    PatientUpdate,
)
from app.modules.users.models import User

router = APIRouter(prefix="/patients", tags=["patients (PHI)"])
READ = Depends(require_permission(Permission.PATIENTS_READ))
WRITE = Depends(require_permission(Permission.PATIENTS_WRITE))


def _sensitive(user: User) -> bool:
    return has_permission(user.role, Permission.PATIENTS_SENSITIVE)


async def _own_patient(db: AsyncSession, user: User, patient_id: UUID) -> Patient:
    return await get_or_404(db, Patient, patient_id, user.company_id, "Patient")


@router.post("", response_model=PatientOut, status_code=status.HTTP_201_CREATED)
async def create_patient(body: PatientCreate, request: Request, actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    data = body.model_dump(exclude={"home_address"})
    if body.user_id:  # a portal login may only be linked to a PASSENGER account of this company
        u = await get_or_404(db, User, body.user_id, actor.company_id, "User")
        if u.role != Role.PASSENGER:
            raise HTTPException(422, "user_id must be a passenger account")
    patient = Patient(company_id=actor.company_id)
    service.apply_fields(patient, data)
    db.add(patient)
    await flush_or_409(db, "A patient with this Medicaid ID (or portal user) already exists")
    if body.home_address:
        addr = PatientAddress(company_id=actor.company_id, patient_id=patient.id, label="home", is_primary=True)
        await apply_address(addr, body.home_address)
        db.add(addr)
    await audit.record(db, action="patient.create", actor=actor, resource_type="patient",
                       resource_id=patient.id, request=request)
    await db.commit()
    await db.refresh(patient)
    return service.to_out(patient, _sensitive(actor))


@router.get("", response_model=list[PatientOut])
async def list_patients(
    request: Request,
    q: str | None = Query(default=None, min_length=2, max_length=100, description="name contains"),
    medicaid_id: str | None = Query(default=None, min_length=6, max_length=20),
    mobility: MobilityType | None = None,
    include_archived: bool = False,
    page: Page = Depends(),
    actor: User = READ,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Patient).where(Patient.company_id == actor.company_id)
    if not include_archived:
        stmt = stmt.where(Patient.archived_at.is_(None))
    if q:
        stmt = stmt.where(func.lower(Patient.first_name + " " + Patient.last_name).contains(q.lower(), autoescape=True))
    if medicaid_id:  # equality via blind index; the ID itself is never compared in SQL
        stmt = stmt.where(Patient.medicaid_id_hash == blind_index(medicaid_id))
    if mobility:
        stmt = stmt.where(Patient.mobility == mobility)
    rows = (await db.scalars(stmt.order_by(Patient.last_name, Patient.first_name, Patient.id)
                             .limit(page.limit).offset(page.offset))).all()
    # PHI access is logged. The search text itself can be PHI, so we log only that a filter was used.
    await audit.record(db, action="patient.list", actor=actor, resource_type="patient", request=request,
                       meta={"count": len(rows), "filtered": bool(q or medicaid_id or mobility)})
    await db.commit()
    sens = _sensitive(actor)
    return [service.to_out(p, sens) for p in rows]


@router.get("/{patient_id}", response_model=PatientOut)
async def get_patient(patient_id: UUID, request: Request, actor: User = READ, db: AsyncSession = Depends(get_db)):
    patient = await _own_patient(db, actor, patient_id)
    await audit.record(db, action="patient.read", actor=actor, resource_type="patient",
                       resource_id=patient.id, request=request, meta={"sensitive_view": _sensitive(actor)})
    await db.commit()
    await audit_phi_read(
    db, actor_user_id=actor.id, company_id=actor.company_id,
    resource_type="patient", resource_id=patient.id,
    patient_id=patient.id, request=request,
)
    return service.to_out(patient, _sensitive(actor))


@router.patch("/{patient_id}", response_model=PatientOut)
async def update_patient(patient_id: UUID, body: PatientUpdate, request: Request, actor: User = WRITE,
                         db: AsyncSession = Depends(get_db)):
    patient = await _own_patient(db, actor, patient_id)
    changes = body.model_dump(exclude_unset=True)
    if {"date_of_birth", "medicaid_id", "notes"} & changes.keys() and not _sensitive(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to change sensitive fields")
    service.apply_fields(patient, changes)
    await flush_or_409(db, "A patient with this Medicaid ID already exists")
    await audit.record(db, action="patient.update", actor=actor, resource_type="patient",
                       resource_id=patient.id, request=request, meta={"fields": sorted(changes)})
    await db.commit()
    await db.refresh(patient)
    return service.to_out(patient, _sensitive(actor))


@router.delete("/{patient_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_patient(patient_id: UUID, request: Request, actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    """Soft delete: the record is retained (HIPAA retention); it just disappears from normal lists."""
    patient = await _own_patient(db, actor, patient_id)
    patient.archived_at = datetime.now(UTC)
    await audit.record(db, action="patient.archive", actor=actor, resource_type="patient",
                       resource_id=patient.id, request=request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------- addresses ----------------
@router.get("/{patient_id}/addresses", response_model=list[PatientAddressOut])
async def list_addresses(patient_id: UUID, request: Request, actor: User = READ, db: AsyncSession = Depends(get_db)):
    patient = await _own_patient(db, actor, patient_id)
    rows = (await db.scalars(select(PatientAddress).where(
        PatientAddress.patient_id == patient.id, PatientAddress.company_id == actor.company_id)
        .order_by(PatientAddress.is_primary.desc(), PatientAddress.label))).all()
    await audit.record(db, action="patient.addresses_read", actor=actor, resource_type="patient",
                       resource_id=patient.id, request=request)
    await db.commit()
    await audit_phi_read(
    db, actor_user_id=actor.id, company_id=actor.company_id,
    resource_type="patient", resource_id=patient.id,
    patient_id=patient.id, request=request,
)
    return rows


async def _clear_primary(db: AsyncSession, patient_id: UUID, except_id: UUID | None = None) -> None:
    stmt = update(PatientAddress).where(PatientAddress.patient_id == patient_id, PatientAddress.is_primary.is_(True))
    if except_id:
        stmt = stmt.where(PatientAddress.id != except_id)
    await db.execute(stmt.values(is_primary=False))


@router.post("/{patient_id}/addresses", response_model=PatientAddressOut, status_code=status.HTTP_201_CREATED)
async def add_address(patient_id: UUID, body: PatientAddressCreate, request: Request, actor: User = WRITE,
                      db: AsyncSession = Depends(get_db)):
    patient = await _own_patient(db, actor, patient_id)
    addr = PatientAddress(company_id=actor.company_id, patient_id=patient.id, label=body.label,
                          is_primary=body.is_primary, access_notes=body.access_notes)
    await apply_address(addr, body)
    if body.is_primary:
        await _clear_primary(db, patient.id)
    db.add(addr)
    await flush_or_409(db, "Could not save address")
    await audit.record(db, action="patient.address_add", actor=actor, resource_type="patient",
                       resource_id=patient.id, request=request, meta={"address_id": str(addr.id)})
    await db.commit()
    await db.refresh(addr)
    return addr


@router.patch("/{patient_id}/addresses/{address_id}", response_model=PatientAddressOut)
async def update_address(patient_id: UUID, address_id: UUID, body: PatientAddressUpdate, request: Request,
                         actor: User = WRITE, db: AsyncSession = Depends(get_db)):
    patient = await _own_patient(db, actor, patient_id)
    addr = await db.scalar(select(PatientAddress).where(
        PatientAddress.id == address_id, PatientAddress.patient_id == patient.id,
        PatientAddress.company_id == actor.company_id))
    if addr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Address not found")
    changes = body.model_dump(exclude_unset=True, exclude={"address"})
    if changes.get("is_primary"):
        await _clear_primary(db, patient.id, except_id=addr.id)
    for k, v in changes.items():
        setattr(addr, k, v)
    if body.address:
        await apply_address(addr, body.address)
    await flush_or_409(db, "Could not save address")
    await audit.record(db, action="patient.address_update", actor=actor, resource_type="patient",
                       resource_id=patient.id, request=request, meta={"address_id": str(addr.id)})
    await db.commit()
    await db.refresh(addr)
    return addr


@router.delete("/{patient_id}/addresses/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(patient_id: UUID, address_id: UUID, request: Request, actor: User = WRITE,
                         db: AsyncSession = Depends(get_db)):
    patient = await _own_patient(db, actor, patient_id)
    addr = await db.scalar(select(PatientAddress).where(
        PatientAddress.id == address_id, PatientAddress.patient_id == patient.id,
        PatientAddress.company_id == actor.company_id))
    if addr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Address not found")
    await db.delete(addr)
    await audit.record(db, action="patient.address_delete", actor=actor, resource_type="patient",
                       resource_id=patient.id, request=request, meta={"address_id": str(address_id)})
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)