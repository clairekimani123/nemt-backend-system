"""PHI read audit.

Call `await audit_phi_read(db, actor, resource_type, resource_id, patient_id, request)`
after any endpoint returns data that includes patient identifiers or medical details.

The row lands in `audit_logs` with `action="phi.read"`. Every read is a row. This is what
answers the HIPAA auditor's question: "who looked at patient X's record last month?"
"""
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit import service as audit


async def audit_phi_read(
    db: AsyncSession,
    *,
    actor_user_id: UUID,
    company_id: UUID,
    resource_type: str,
    resource_id: UUID,
    patient_id: UUID | None,
    request: Request,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write one `phi.read` row per successful read of patient-adjacent data.

    Called by endpoints after they have fetched the data, before they return it. The
    audit writes into the same `audit_logs` table as everything else, so queries like
    "show me every read of patient X last week" work with plain SQL.
    """
    meta: dict[str, Any] = {"resource_type": resource_type, "resource_id": str(resource_id)}
    if patient_id is not None:
        meta["patient_id"] = str(patient_id)
    if extra:
        meta.update(extra)
    await audit.record(
    db,
    action="phi.read",
    actor_user_id=actor_user_id,
    company_id=company_id,
    resource_type=resource_type,
    resource_id=str(resource_id),
    request=request,
    meta=meta,
)