"""phase 7: rate plans, claims, payments, claim lines

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migrations.rls_helpers import grant, tenant_policy

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_plans",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("broker_id", sa.Uuid(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("base_fare_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("per_mile_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("per_minute_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wheelchair_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stretcher_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("after_hours_multiplier_bp", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column("wait_per_minute_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("no_show_fee_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("late_cancel_fee_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("base_fare_cents >= 0", name="ck_rate_plans_base_positive"),
        sa.CheckConstraint("after_hours_multiplier_bp >= 10000", name="ck_rate_plans_multiplier_ge_1x"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                           name="ck_rate_plans_window"),
        sa.ForeignKeyConstraint(["company_id", "broker_id"], ["brokers.company_id", "brokers.id"],
                                name="fk_rate_plans_company_broker"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_rate_plans_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_plans")),
        sa.UniqueConstraint("company_id", "id", name="uq_rate_plans_company_id_id"),
    )
    op.create_index("ix_rate_plans_company_broker", "rate_plans", ["company_id", "broker_id"])
    op.create_index("ix_rate_plans_company_effective", "rate_plans", ["company_id", "effective_from"])
    op.create_index(op.f("ix_rate_plans_company_id"), "rate_plans", ["company_id"])

    op.create_table(
        "claims",
        sa.Column("trip_id", sa.Uuid(), nullable=True),
        sa.Column("broker_id", sa.Uuid(), nullable=True),
        sa.Column("rate_plan_id", sa.Uuid(), nullable=True),
        sa.Column("claim_number", sa.String(length=32), nullable=False),
        sa.Column("status", sa.Enum("draft", "submitted", "paid", "partial", "denied", "written_off", "void",
                                    name="claim_status", native_enum=False, create_constraint=True, length=16),
                  nullable=False),
        sa.Column("payer_type", sa.Enum("medicaid", "medicare", "private_insurance", "facility", "patient", "broker",
                                        name="claim_payer_type", native_enum=False, create_constraint=True, length=24),
                  nullable=False),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("place_of_service_code", sa.String(length=4), nullable=True),
        sa.Column("procedure_code", sa.String(length=8), nullable=True),
        sa.Column("diagnosis_code", sa.String(length=16), nullable=True),
        sa.Column("billed_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allowed_cents", sa.Integer(), nullable=True),
        sa.Column("paid_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("patient_responsibility_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denial_code", sa.String(length=16), nullable=True),
        sa.Column("denial_note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("billed_cents >= 0", name="ck_claims_billed_positive"),
        sa.CheckConstraint("paid_cents >= 0", name="ck_claims_paid_positive"),
        sa.ForeignKeyConstraint(["company_id", "trip_id"], ["trips.company_id", "trips.id"],
                                name="fk_claims_company_trip"),
        sa.ForeignKeyConstraint(["company_id", "broker_id"], ["brokers.company_id", "brokers.id"],
                                name="fk_claims_company_broker"),
        sa.ForeignKeyConstraint(["company_id", "rate_plan_id"], ["rate_plans.company_id", "rate_plans.id"],
                                name="fk_claims_company_rate_plan"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_claims_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
        sa.UniqueConstraint("company_id", "id", name="uq_claims_company_id_id"),
        sa.UniqueConstraint("company_id", "claim_number", name="uq_claims_company_number"),
    )
    op.create_index("ix_claims_company_status", "claims", ["company_id", "status"])
    op.create_index("ix_claims_company_service", "claims", ["company_id", "service_date"])
    op.create_index("ix_claims_company_submitted", "claims", ["company_id", "submitted_at"])
    op.create_index(op.f("ix_claims_company_id"), "claims", ["company_id"])

    op.create_table(
        "claim_payments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Enum("payment", "adjustment", "denial", name="claim_payment_kind",
                                  native_enum=False, create_constraint=True, length=16), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reference", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["company_id", "claim_id"], ["claims.company_id", "claims.id"],
                                name="fk_claim_payments_company_claim", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_claim_payments_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_payments")),
    )
    op.create_index("ix_claim_payments_claim", "claim_payments", ["claim_id", "posted_at"])
    op.create_index(op.f("ix_claim_payments_company_id"), "claim_payments", ["company_id"])

    op.create_table(
        "claim_lines",
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("line_number", sa.SmallInteger(), nullable=False),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("procedure_code", sa.String(length=8), nullable=False),
        sa.Column("modifier", sa.String(length=8), nullable=True),
        sa.Column("units", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("charge_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allowed_cents", sa.Integer(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("line_number > 0", name="ck_claim_lines_number_positive"),
        sa.CheckConstraint("units > 0", name="ck_claim_lines_units_positive"),
        sa.ForeignKeyConstraint(["company_id", "claim_id"], ["claims.company_id", "claims.id"],
                                name="fk_claim_lines_company_claim", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_claim_lines_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_lines")),
        sa.UniqueConstraint("company_id", "claim_id", "line_number",
                            name="uq_claim_lines_company_claim_line"),
    )
    op.create_index(op.f("ix_claim_lines_company_id"), "claim_lines", ["company_id"])

    for table in ("rate_plans", "claims"):
        tenant_policy(table)
    tenant_policy("claim_lines")
    tenant_policy("claim_payments")

    grant("SELECT, INSERT, UPDATE", "rate_plans", "claims")
    grant("SELECT, INSERT", "claim_payments", "claim_lines")   # append-only