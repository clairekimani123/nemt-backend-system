"""Billing request and response shapes."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.billing.models import ClaimStatus, PayerType, PaymentKind
from app.modules.billing.models import RuleKind


# ------------------------------------------------------------------ rate plans
class RatePlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    broker_id: UUID | None = None
    is_default: bool = False
    base_fare_cents: int = Field(default=0, ge=0)
    per_mile_cents: int = Field(default=0, ge=0)
    per_minute_cents: int = Field(default=0, ge=0)
    wheelchair_cents: int = Field(default=0, ge=0)
    stretcher_cents: int = Field(default=0, ge=0)
    after_hours_multiplier_bp: int = Field(default=10_000, ge=10_000)
    wait_per_minute_cents: int = Field(default=0, ge=0)
    no_show_fee_cents: int = Field(default=0, ge=0)
    late_cancel_fee_cents: int = Field(default=0, ge=0)
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def _window(self) -> "RatePlanIn":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be on or after effective_from")
        return self


class RatePlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_default: bool | None = None
    base_fare_cents: int | None = Field(default=None, ge=0)
    per_mile_cents: int | None = Field(default=None, ge=0)
    per_minute_cents: int | None = Field(default=None, ge=0)
    wheelchair_cents: int | None = Field(default=None, ge=0)
    stretcher_cents: int | None = Field(default=None, ge=0)
    after_hours_multiplier_bp: int | None = Field(default=None, ge=10_000)
    wait_per_minute_cents: int | None = Field(default=None, ge=0)
    no_show_fee_cents: int | None = Field(default=None, ge=0)
    late_cancel_fee_cents: int | None = Field(default=None, ge=0)
    effective_from: date | None = None
    effective_to: date | None = None


class RatePlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    broker_id: UUID | None
    is_default: bool
    base_fare_cents: int
    per_mile_cents: int
    per_minute_cents: int
    wheelchair_cents: int
    stretcher_cents: int
    after_hours_multiplier_bp: int
    wait_per_minute_cents: int
    no_show_fee_cents: int
    late_cancel_fee_cents: int
    effective_from: date
    effective_to: date | None
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ claims
class BillableTripOut(BaseModel):
    trip_id: UUID
    trip_ref: str
    service_date: date
    patient_first_name: str
    patient_last_name: str
    mobility: str
    broker_id: UUID | None
    authorization_number: str | None
    distance_km: float
    duration_minutes: int
    completed_at: datetime | None


class ClaimCreateFromTrip(BaseModel):
    payer_type: PayerType
    rate_plan_id: UUID | None = None       # omit to auto-pick the matching plan


class ClaimLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    line_number: int
    service_date: date
    procedure_code: str
    modifier: str | None
    units: int
    charge_cents: int
    allowed_cents: int | None


class ClaimPaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: PaymentKind
    amount_cents: int
    posted_at: datetime
    reference: str | None
    note: str | None


class ClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    claim_number: str
    status: ClaimStatus
    payer_type: PayerType
    trip_id: UUID | None
    broker_id: UUID | None
    rate_plan_id: UUID | None
    service_date: date
    place_of_service_code: str | None
    procedure_code: str | None
    diagnosis_code: str | None
    billed_cents: int
    allowed_cents: int | None
    paid_cents: int
    patient_responsibility_cents: int
    submitted_at: datetime | None
    resolved_at: datetime | None
    denial_code: str | None
    denial_note: str | None
    created_at: datetime
    updated_at: datetime


class ClaimDetailOut(ClaimOut):
    lines: list[ClaimLineOut]
    payments: list[ClaimPaymentOut]


# ------------------------------------------------------------------ lifecycle inputs
class SubmitIn(BaseModel):
    reference: str | None = Field(default=None, max_length=64)


class PostPaymentIn(BaseModel):
    kind: PaymentKind
    amount_cents: int
    reference: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    denial_code: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _sign(self) -> "PostPaymentIn":
        if self.kind == PaymentKind.PAYMENT and self.amount_cents <= 0:
            raise ValueError("payment amount must be positive")
        if self.kind in (PaymentKind.ADJUSTMENT, PaymentKind.DENIAL) and self.amount_cents > 0:
            raise ValueError("adjustment / denial amounts must be zero or negative")
        if self.kind == PaymentKind.DENIAL and not self.denial_code:
            raise ValueError("denial_code is required for a denial")
        return self


class AgingBucketOut(BaseModel):
    bucket: str            # "0-30" | "31-60" | "61-90" | "91-120" | "120+"
    count: int
    billed_cents: int
    outstanding_cents: int



class RateRuleIn(BaseModel):
    priority: int = Field(default=0, ge=-1000, le=1000)
    exclusive: bool = False
    broker_id: UUID | None = None
    procedure_code: str | None = Field(default=None, max_length=8)
    mobility: str | None = Field(default=None, max_length=32)
    weekday_mask: int | None = Field(default=None, ge=0, le=127)
    kind: RuleKind
    amount_cents: int | None = Field(default=None, ge=0)
    multiplier_bp: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _payload(self) -> "RateRuleIn":
        if self.kind == RuleKind.FLAT and self.amount_cents is None:
            raise ValueError("amount_cents is required for a flat rule")
        if self.kind == RuleKind.MULTIPLIER and self.multiplier_bp is None:
            raise ValueError("multiplier_bp is required for a multiplier rule")
        return self


class RateRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    rate_plan_id: UUID
    priority: int
    exclusive: bool
    broker_id: UUID | None
    procedure_code: str | None
    mobility: str | None
    weekday_mask: int | None
    kind: RuleKind
    amount_cents: int | None
    multiplier_bp: int | None
    created_at: datetime
    updated_at: datetime


class BatchItemOutcome(BaseModel):
    """Per-item result. `ok=false` carries a reason; `ok=true` carries the resource id."""

    index: int                      # position in the request list (0-based)
    ok: bool
    id: UUID | None = None
    error: str | None = None


class BatchOut(BaseModel):
    requested: int
    succeeded: int
    failed: int
    outcomes: list[BatchItemOutcome]


class FromTripsIn(BaseModel):
    trip_ids: list[UUID] = Field(min_length=1, max_length=200)
    payer_type: PayerType
    rate_plan_id: UUID | None = None


class BatchSubmitIn(BaseModel):
    claim_ids: list[UUID] = Field(min_length=1, max_length=200)
    reference: str | None = Field(default=None, max_length=64)


class BatchPaymentIn(BaseModel):
    claim_ids: list[UUID] = Field(min_length=1, max_length=200)
    kind: PaymentKind
    amount_cents: int
    reference: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    denial_code: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _sign(self) -> "BatchPaymentIn":
        if self.kind == PaymentKind.PAYMENT and self.amount_cents <= 0:
            raise ValueError("payment amount must be positive")
        if self.kind in (PaymentKind.ADJUSTMENT, PaymentKind.DENIAL) and self.amount_cents > 0:
            raise ValueError("adjustment / denial amounts must be zero or negative")
        if self.kind == PaymentKind.DENIAL and not self.denial_code:
            raise ValueError("denial_code is required for a denial")
        return self   