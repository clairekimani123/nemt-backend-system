"""Billing: rate plans, claims, payments, and claim lines.

A rate plan says what a company charges. A claim is one billable unit (usually one trip)
submitted to one payer. Payments and denials are rows against a claim, never overwrites —
so the full history of how a claim resolved is always reconstructible.
"""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKeyConstraint, Identity, Index,
    Integer, SmallInteger, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, IdMixin, TenantMixin, TimestampMixin, str_enum


class PayerType(StrEnum):
    MEDICAID = "medicaid"
    MEDICARE = "medicare"
    PRIVATE_INSURANCE = "private_insurance"
    FACILITY = "facility"
    PATIENT = "patient"
    BROKER = "broker"


class ClaimStatus(StrEnum):
    DRAFT = "draft"                 # created, not yet submitted
    SUBMITTED = "submitted"         # sent to the payer
    PAID = "paid"                   # paid in full
    PARTIAL = "partial"             # paid less than billed
    DENIED = "denied"               # rejected outright
    WRITTEN_OFF = "written_off"     # company decided not to pursue
    VOID = "void"                   # created in error


class PaymentKind(StrEnum):
    PAYMENT = "payment"
    ADJUSTMENT = "adjustment"       # contractual write-down
    DENIAL = "denial"


class RatePlan(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "rate_plans"

    name: Mapped[str] = mapped_column(String(120))
    broker_id: Mapped[UUID | None]  # UUID | None — left untyped so we don't import Broker here
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    base_fare_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    per_mile_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    per_minute_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    wheelchair_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stretcher_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    after_hours_multiplier_bp: Mapped[int] = mapped_column(
        Integer, default=10_000, server_default="10000",
        doc="Basis points; 10000 = 1.0x, 15000 = 1.5x")

    wait_per_minute_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    no_show_fee_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    late_cancel_fee_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_rate_plans_company_id_id"),
        ForeignKeyConstraint(["company_id", "broker_id"], ["brokers.company_id", "brokers.id"],
                             name="fk_rate_plans_company_broker"),
        CheckConstraint("base_fare_cents >= 0", name="ck_rate_plans_base_positive"),
        CheckConstraint("after_hours_multiplier_bp >= 10000", name="ck_rate_plans_multiplier_ge_1x"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                        name="ck_rate_plans_window"),
        Index("ix_rate_plans_company_broker", "company_id", "broker_id"),
        Index("ix_rate_plans_company_effective", "company_id", "effective_from"),
    )


class Claim(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "claims"

    trip_id: Mapped[UUID | None]
    broker_id: Mapped[UUID | None]
    rate_plan_id: Mapped[UUID | None]

    claim_number: Mapped[str] = mapped_column(String(32))     # human-readable, unique per company
    status: Mapped[ClaimStatus] = mapped_column(
        str_enum(ClaimStatus, "claim_status"), default=ClaimStatus.DRAFT)
    payer_type: Mapped[PayerType] = mapped_column(str_enum(PayerType, "claim_payer_type"))

    service_date: Mapped[date] = mapped_column(Date)
    place_of_service_code: Mapped[str | None] = mapped_column(String(4))
    procedure_code: Mapped[str | None] = mapped_column(String(8))
    diagnosis_code: Mapped[str | None] = mapped_column(String(16))

    billed_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    allowed_cents: Mapped[int | None] = mapped_column(Integer)
    paid_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    patient_responsibility_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    denial_code: Mapped[str | None] = mapped_column(String(16))
    denial_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_claims_company_id_id"),
        UniqueConstraint("company_id", "claim_number", name="uq_claims_company_number"),
        ForeignKeyConstraint(["company_id", "trip_id"], ["trips.company_id", "trips.id"],
                             name="fk_claims_company_trip"),
        ForeignKeyConstraint(["company_id", "broker_id"], ["brokers.company_id", "brokers.id"],
                             name="fk_claims_company_broker"),
        ForeignKeyConstraint(["company_id", "rate_plan_id"], ["rate_plans.company_id", "rate_plans.id"],
                             name="fk_claims_company_rate_plan"),
        CheckConstraint("billed_cents >= 0", name="ck_claims_billed_positive"),
        CheckConstraint("paid_cents >= 0", name="ck_claims_paid_positive"),
        Index("ix_claims_company_status", "company_id", "status"),
        Index("ix_claims_company_service", "company_id", "service_date"),
        Index("ix_claims_company_submitted", "company_id", "submitted_at"),
    )


class ClaimPayment(TenantMixin, Base):
    """Every money event against a claim. Append-only: no updates, no deletes."""

    __tablename__ = "claim_payments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    claim_id: Mapped[UUID]
    kind: Mapped[PaymentKind] = mapped_column(str_enum(PaymentKind, "claim_payment_kind"))
    amount_cents: Mapped[int] = mapped_column(Integer)      # signed: negative for denials/adjustments
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reference: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "claim_id"], ["claims.company_id", "claims.id"],
                             name="fk_claim_payments_company_claim", ondelete="CASCADE"),
        Index("ix_claim_payments_claim", "claim_id", "posted_at"),
    )


class ClaimLine(IdMixin, TenantMixin, Base):
    """For CMS-1500/837P. Most claims have one line today; the schema allows more."""

    __tablename__ = "claim_lines"

    claim_id: Mapped[UUID]
    line_number: Mapped[int] = mapped_column(SmallInteger)
    service_date: Mapped[date] = mapped_column(Date)
    procedure_code: Mapped[str] = mapped_column(String(8))
    modifier: Mapped[str | None] = mapped_column(String(8))
    units: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    charge_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    allowed_cents: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("company_id", "claim_id", "line_number",
                         name="uq_claim_lines_company_claim_line"),
        ForeignKeyConstraint(["company_id", "claim_id"], ["claims.company_id", "claims.id"],
                             name="fk_claim_lines_company_claim", ondelete="CASCADE"),
        CheckConstraint("line_number > 0", name="ck_claim_lines_number_positive"),
        CheckConstraint("units > 0", name="ck_claim_lines_units_positive"),
    )