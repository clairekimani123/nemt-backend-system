from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.permissions import Role
from app.core.types import Email, Password


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    company_id: UUID
    email: str
    full_name: str
    phone: str | None
    role: Role
    is_active: bool
    mfa_enabled: bool
    last_login_at: datetime | None
    created_at: datetime


class UserCreate(BaseModel):
    email: Email
    password: Password
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    role: Role


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    role: Role | None = None
    is_active: bool | None = None