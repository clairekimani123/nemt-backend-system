"""phase 4: route plans, routes, route stops

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migrations.rls_helpers import grant, tenant_policy

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "route_plans",
        sa.Column("route_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Enum("proposed", "applied", "discarded",
                                    name="plan_status", native_enum=False,
                                    create_constraint=True, length=32), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("unassigned", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("skipped_resources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_route_plans_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_plans")),
        sa.UniqueConstraint("company_id", "id", name="uq_route_plans_company_id_id"),
    )
    op.create_index("ix_route_plans_company_date", "route_plans", ["company_id", "route_date"])
    op.create_index("ix_route_plans_company_status", "route_plans", ["company_id", "status"])
    op.create_index(op.f("ix_route_plans_company_id"), "route_plans", ["company_id"])

    op.create_table(
        "routes",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("route_date", sa.Date(), nullable=False),
        sa.Column("driver_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Enum("proposed", "published", "superseded", "discarded",
                                    name="route_status", native_enum=False,
                                    create_constraint=True, length=32), nullable=False),
        sa.Column("planned_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("drive_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distance_m", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("planned_end_at > planned_start_at", name="ck_routes_end_after_start"),
        sa.ForeignKeyConstraint(["company_id", "plan_id"], ["route_plans.company_id", "route_plans.id"],
                                name="fk_routes_company_plan", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id", "driver_id"], ["drivers.company_id", "drivers.id"],
                                name="fk_routes_company_driver"),
        sa.ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"],
                                name="fk_routes_company_vehicle"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_routes_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routes")),
        sa.UniqueConstraint("company_id", "id", name="uq_routes_company_id_id"),
    )
    op.create_index("ix_routes_company_date", "routes", ["company_id", "route_date"])
    op.create_index("ix_routes_company_status", "routes", ["company_id", "status"])
    op.create_index("ix_routes_company_driver_date", "routes", ["company_id", "driver_id", "route_date"])
    op.create_index(op.f("ix_routes_company_id"), "routes", ["company_id"])

    op.create_table(
        "route_stops",
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column("trip_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("planned_arrival_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_departure_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("wait_minutes", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("load_seats", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("load_wheelchair", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("load_stretcher", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("kind IN ('pickup', 'dropoff')", name="ck_route_stops_kind"),
        sa.CheckConstraint("sequence > 0", name="ck_route_stops_sequence_positive"),
        sa.CheckConstraint("planned_departure_at >= planned_arrival_at", name="ck_route_stops_departure_after_arrival"),
        sa.ForeignKeyConstraint(["company_id", "route_id"], ["routes.company_id", "routes.id"],
                                name="fk_route_stops_company_route", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id", "trip_id"], ["trips.company_id", "trips.id"],
                                name="fk_route_stops_company_trip"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_route_stops_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_stops")),
    )
    op.create_index("ix_route_stops_route_seq", "route_stops", ["route_id", "sequence"])
    op.create_index("ix_route_stops_company_trip", "route_stops", ["company_id", "trip_id"])
    op.create_index(op.f("ix_route_stops_company_id"), "route_stops", ["company_id"])

    # ------------------------------------------------------------------
    # Security for the Phase 4 tables
    # ------------------------------------------------------------------
    for table in ("route_plans", "routes", "route_stops"):
        tenant_policy(table)

    grant("SELECT, INSERT, UPDATE", "route_plans", "routes")
    # stops are facts, not mutable state: only INSERT and SELECT
    grant("SELECT, INSERT", "route_stops")


def downgrade() -> None:
    op.drop_index(op.f("ix_route_stops_company_id"), table_name="route_stops")
    op.drop_index("ix_route_stops_company_trip", table_name="route_stops")
    op.drop_index("ix_route_stops_route_seq", table_name="route_stops")
    op.drop_table("route_stops")

    op.drop_index(op.f("ix_routes_company_id"), table_name="routes")
    op.drop_index("ix_routes_company_driver_date", table_name="routes")
    op.drop_index("ix_routes_company_status", table_name="routes")
    op.drop_index("ix_routes_company_date", table_name="routes")
    op.drop_table("routes")

    op.drop_index(op.f("ix_route_plans_company_id"), table_name="route_plans")
    op.drop_index("ix_route_plans_company_status", table_name="route_plans")
    op.drop_index("ix_route_plans_company_date", table_name="route_plans")
    op.drop_table("route_plans")