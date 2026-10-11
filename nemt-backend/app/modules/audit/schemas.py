from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    id: int
    company_id: UUID | None
    actor_user_id: UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    ip: str | None
    meta: dict[str, Any] = Field(serialization_alias="metadata")
    created_at: datetime