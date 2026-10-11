from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.core.types import Email, Password


class DriverCreate(BaseModel):
    # login identity (becomes a users row with role=driver)
    email: Email
    password: Password
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    # driver profile
    employee_number: str | None = Field(default=None, max_length=32)
    license_number: str | None = Field(default=None, min_length=4, max_length=32)
    license_state: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    license_expires_on: date | None = None
    medical_cert_expires_on: date | None = None
    wheelchair_certified: bool = False
    hire_date: date | None = None
    emergency_contact_name: str | None = Field(default=None, max_length=200)
    emergency_contact_phone: str | None = Field(default=None, max_length=32)

    @field_validator("license_state")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class DriverUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    employee_number: str | None = Field(default=None, max_length=32)
    license_number: str | None = Field(default=None, min_length=4, max_length=32)
    license_state: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    license_expires_on: date | None = None
    medical_cert_expires_on: date | None = None
    wheelchair_certified: bool | None = None
    hire_date: date | None = None
    emergency_contact_name: str | None = Field(default=None, max_length=200)
    emergency_contact_phone: str | None = Field(default=None, max_length=32)


class DriverOut(BaseModel):
    id: UUID
    user_id: UUID
    email: str
    full_name: str
    phone: str | None
    is_active: bool
    employee_number: str | None
    license_last4: str | None
    license_number: str | None = None  # only for roles with drivers:write
    license_state: str | None
    license_expires_on: date | None
    medical_cert_expires_on: date | None
    wheelchair_certified: bool
    hire_date: date | None
    emergency_contact_name: str | None
    emergency_contact_phone: str | None
    archived_at: datetime | None
    created_at: datetime