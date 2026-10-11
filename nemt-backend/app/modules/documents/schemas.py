from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.documents.models import DocType, OwnerType


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    owner_type: OwnerType
    owner_id: UUID
    doc_type: DocType
    filename: str
    content_type: str
    size_bytes: int
    expires_on: date | None
    uploaded_by: UUID | None
    created_at: datetime
    archived_at: datetime | None