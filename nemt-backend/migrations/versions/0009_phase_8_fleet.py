"""phase 8: fleet maintenance, inspections, mileage

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

from migrations.rls_helpers import grant, tenant_policy

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vehicle_maintenance",
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Enum("oil_change", "tire_rotation", "tire_replacement", "brake_service",
                                  "transmission", "battery", "ac_service", "lift_inspection",
                                  "ramp_inspection", "general_repair", "other",
                                  name="maintenance_kind", native_enum=False,
                                  create_constraint=True, length=32), nullable=False),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("odometer_km", sa.Integer(), nullable=True),
        sa.Column("cost_cents", sa.Integer(), nullable=True),
        sa.Column("vendor", sa.String(length=200), nullable=True),
        sa.Column("invoice_number", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("next_due_date", sa.Date(), nullable=True),
        sa.Column("next_due_km", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("cost_cents IS NULL OR cost_cents >= 0", name="ck_maint_cost_positive"),
        sa.CheckConstraint("odometer_km IS NULL OR odometer_km >= 0", name="ck_maint_odo_positive"),
        sa.ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"],
                                name="fk_vehicle_maintenance_company_vehicle", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_vehicle_maintenance_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vehicle_maintenance")),
        sa.UniqueConstraint("company_id", "id", name="uq_vehicle_maintenance_company_id_id"),
    )
    op.create_index("ix_maint_company_vehicle_date", "vehicle_maintenance",
                    ["company_id", "vehicle_id", "service_date"])
    op.create_index("ix_maint_company_next_due", "vehicle_maintenance", ["company_id", "next_due_date"])
    op.create_index(op.f("ix_vehicle_maintenance_company_id"), "vehicle_maintenance", ["company_id"])

    op.create_table(
        "vehicle_inspections",
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Enum("state_annual", "insurance_annual", "internal_monthly",
                                  "accessibility", "other",
                                  name="inspection_kind", native_enum=False,
                                  create_constraint=True, length=32), nullable=False),
        sa.Column("inspected_on", sa.Date(), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=False),
        sa.Column("result", sa.Enum("pass", "fail", name="inspection_result",
                                    native_enum=False, create_constraint=True, length=32), nullable=False),
        sa.Column("inspector_name", sa.String(length=200), nullable=True),
        sa.Column("inspector_org", sa.String(length=200), nullable=True),
        sa.Column("certificate_number", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("expires_on >= inspected_on", name="ck_insp_expires_after"),
        sa.ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"],
                                name="fk_vehicle_inspections_company_vehicle", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_vehicle_inspections_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vehicle_inspections")),
        sa.UniqueConstraint("company_id", "id", name="uq_vehicle_inspections_company_id_id"),
    )
    op.create_index("ix_insp_company_vehicle_date", "vehicle_inspections",
                    ["company_id", "vehicle_id", "inspected_on"])
    op.create_index("ix_insp_company_expires", "vehicle_inspections", ["company_id", "expires_on"])
    op.create_index(op.f("ix_vehicle_inspections_company_id"), "vehicle_inspections", ["company_id"])

    op.create_table(
        "vehicle_mileage",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("reading_km", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("reading_km >= 0", name="ck_mileage_reading_positive"),
        sa.ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"],
                                name="fk_vehicle_mileage_company_vehicle", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_vehicle_mileage_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vehicle_mileage")),
    )
    op.create_index("ix_mileage_company_vehicle_time", "vehicle_mileage",
                    ["company_id", "vehicle_id", "recorded_at"])
    op.create_index(op.f("ix_vehicle_mileage_company_id"), "vehicle_mileage", ["company_id"])

    for table in ("vehicle_maintenance", "vehicle_inspections", "vehicle_mileage"):
        tenant_policy(table)
        grant("SELECT, INSERT", table)          # append-only across the board
    # Only maintenance and inspections are ever corrected — allow UPDATE on those,
    # but keep mileage purely append-only (a wrong reading is corrected by a new one).
    grant("UPDATE", "vehicle_maintenance", "vehicle_inspections")


def downgrade() -> None:
    op.drop_index(op.f("ix_vehicle_mileage_company_id"), table_name="vehicle_mileage")
    op.drop_index("ix_mileage_company_vehicle_time", table_name="vehicle_mileage")
    op.drop_table("vehicle_mileage")

    op.drop_index(op.f("ix_vehicle_inspections_company_id"), table_name="vehicle_inspections")
    op.drop_index("ix_insp_company_expires", table_name="vehicle_inspections")
    op.drop_index("ix_insp_company_vehicle_date", table_name="vehicle_inspections")
    op.drop_table("vehicle_inspections")

    op.drop_index(op.f("ix_vehicle_maintenance_company_id"), table_name="vehicle_maintenance")
    op.drop_index("ix_maint_company_next_due", table_name="vehicle_maintenance")
    op.drop_index("ix_maint_company_vehicle_date", table_name="vehicle_maintenance")
    op.drop_table("vehicle_maintenance")