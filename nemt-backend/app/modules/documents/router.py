import hashlib
import re
from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base import new_id
from app.core.config import settings
from app.core.crud import flush_or_409, get_or_404
from app.core.deps import get_current_user, get_db, require_permission
from app.core.permissions import Permission, has_permission
from app.core.phi_audit import audit_phi_read
from app.core.storage import Storage, get_storage
from app.modules.audit import service as audit
from app.modules.documents.models import DocType, Document, OwnerType
from app.modules.documents.schemas import DocumentOut
from app.modules.drivers.models import Driver
from app.modules.facilities.models import Facility
from app.modules.patients.models import Patient
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

READ = Depends(require_permission(Permission.PATIENTS_READ))
WRITE = Depends(require_permission(Permission.PATIENTS_WRITE))

router = APIRouter(prefix="/documents", tags=["documents"])

OWNER_MODEL = {OwnerType.PATIENT: Patient, OwnerType.DRIVER: Driver,
               OwnerType.VEHICLE: Vehicle, OwnerType.FACILITY: Facility}
READ_PERM = {OwnerType.PATIENT: Permission.PATIENTS_READ, OwnerType.DRIVER: Permission.DRIVERS_READ,
             OwnerType.VEHICLE: Permission.VEHICLES_READ, OwnerType.FACILITY: Permission.FACILITIES_READ}
WRITE_PERM = {OwnerType.PATIENT: Permission.PATIENTS_WRITE, OwnerType.DRIVER: Permission.DRIVERS_WRITE,
              OwnerType.VEHICLE: Permission.VEHICLES_WRITE, OwnerType.FACILITY: Permission.FACILITIES_WRITE}
# Never trust the client's Content-Type: check the file's real first bytes too.
MAGIC = {"application/pdf": b"%PDF-", "image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}


def _require(actor: User, perm: Permission) -> None:
    if not has_permission(actor.role, perm):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")


def _safe_name(name: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", (name or "file").rsplit("/", 1)[-1].rsplit("\\", 1)[-1])[:100] or "file"


@router.post("", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    request: Request,
    owner_type: OwnerType = Form(...),
    owner_id: UUID = Form(...),
    doc_type: DocType = Form(...),
    expires_on: date | None = Form(default=None),
    file: UploadFile = File(...),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
):
    _require(actor, WRITE_PERM[owner_type])
    await get_or_404(db, OWNER_MODEL[owner_type], owner_id, actor.company_id, owner_type.value.title())

    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)  # never buffer more than the limit
    if len(data) > limit:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file")
    ctype = file.content_type or ""
    if ctype not in MAGIC or not data.startswith(MAGIC[ctype]):
        raise HTTPException(415, "Only PDF, PNG or JPEG files are accepted")

    doc_id = new_id()
    key = f"{actor.company_id}/{doc_id}"  # server-generated: the client's filename never touches the disk
    doc = Document(
        id=doc_id, company_id=actor.company_id, doc_type=doc_type, filename=_safe_name(file.filename),
        content_type=ctype, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
        storage_key=key, expires_on=expires_on, uploaded_by=actor.id, **{f"{owner_type.value}_id": owner_id},
    )
    db.add(doc)
    await flush_or_409(db, "Could not save document")
    await storage.put(key, data)
    try:
        await audit.record(db, action="document.upload", actor=actor, resource_type="document",
                           resource_id=doc.id, request=request,
                           meta={"owner_type": owner_type.value, "owner_id": str(owner_id), "doc_type": doc_type.value})
        await db.commit()
    except Exception:
        await storage.delete(key)  # no orphan files if the DB write fails
        raise
    await db.refresh(doc)
    return doc


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    owner_type: OwnerType,
    owner_id: UUID,
    include_archived: bool = False,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require(actor, READ_PERM[owner_type])
    await get_or_404(db, OWNER_MODEL[owner_type], owner_id, actor.company_id, owner_type.value.title())
    stmt = select(Document).where(Document.company_id == actor.company_id,
                                  getattr(Document, f"{owner_type.value}_id") == owner_id)
    if not include_archived:
        stmt = stmt.where(Document.archived_at.is_(None))
    return (await db.scalars(stmt.order_by(Document.created_at.desc()))).all()


@router.get("/{doc_id}/download")
async def download_document(
    doc_id: UUID,
    request: Request,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
):
    """Stream the file's bytes. Every download writes two audit rows: a PHI audit
    (for HIPAA compliance — who looked at this patient's data) and a `document.download`
    action (for the operational trail — who downloaded this specific document)."""
    doc = await get_or_404(db, Document, doc_id, actor.company_id, "Document")
    _require(actor, READ_PERM[doc.owner_type])

    data = await storage.get(doc.storage_key)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found in storage")

    patient_id: UUID | None = None
    if doc.owner_type == OwnerType.PATIENT:
        patient_id = getattr(doc, "patient_id", None)

    await audit_phi_read(
        db, actor_user_id=actor.id, company_id=actor.company_id,
        resource_type="document", resource_id=doc.id,
        patient_id=patient_id, request=request,
    )
    await audit.record(db, action="document.download", actor=actor, resource_type="document",
                       resource_id=doc.id, request=request,
                       meta={"owner_type": doc.owner_type.value if hasattr(doc.owner_type, "value") else doc.owner_type,
                             "filename": doc.filename})
    await db.commit()

    return Response(
        content=data,
        media_type=doc.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{doc.filename}"',
            "Content-Security-Policy": "sandbox",
        },
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_document(document_id: UUID, request: Request, actor: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    """Archive, don't erase: the file stays in storage for the retention period."""
    doc = await get_or_404(db, Document, document_id, actor.company_id, "Document")
    _require(actor, WRITE_PERM[doc.owner_type])
    doc.archived_at = datetime.now(UTC)
    await audit.record(db, action="document.archive", actor=actor, resource_type="document",
                       resource_id=doc.id, request=request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)