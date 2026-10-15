"""phase 5: driver positions

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa
import geoalchemy2  # noqa: F401

from migrations.rls_helpers import grant, tenant_policy

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "driver_positions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("driver_id", sa.Uuid(), nullable=False),
        sa.Column("trip_id", sa.Uuid(), nullable=True),
        sa.Column("location", geoalchemy2.types.Geography(
        geometry_type="POINT", srid=4326, spatial_index=False,
        from_text="ST_GeogFromText", name="geography"), nullable=False),
        sa.Column("latitude", sa.Float(), sa.Computed("ST_Y(location::geometry)", persisted=True), nullable=True),
        sa.Column("longitude", sa.Float(), sa.Computed("ST_X(location::geometry)", persisted=True), nullable=True),
        sa.Column("speed_mps", sa.Float(), nullable=True),
        sa.Column("heading_deg", sa.Float(), nullable=True),
        sa.Column("accuracy_m", sa.Float(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["company_id", "driver_id"], ["drivers.company_id", "drivers.id"],
        name="fk_driver_positions_company_driver", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id", "trip_id"], ["trips.company_id", "trips.id"],
        name="fk_driver_positions_company_trip"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
        name=op.f("fk_driver_positions_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_driver_positions")),
    )
    op.create_index("ix_driver_positions_driver_time", "driver_positions", ["driver_id", "recorded_at"])
    op.create_index("ix_driver_positions_trip_time", "driver_positions", ["trip_id", "recorded_at"],
    postgresql_where=sa.text("trip_id IS NOT NULL"))
    op.create_index(op.f("ix_driver_positions_company_id"), "driver_positions", ["company_id"])

    tenant_policy("driver_positions")
    grant("SELECT, INSERT", "driver_positions")   # append-only: no UPDATE, no DELETE


def downgrade() -> None:
    op.drop_index(op.f("ix_driver_positions_company_id"), table_name="driver_positions")
    op.drop_index("ix_driver_positions_trip_time", table_name="driver_positions",
    postgresql_where=sa.text("trip_id IS NOT NULL"))
    op.drop_index("ix_driver_positions_driver_time", table_name="driver_positions")
    op.drop_table("driver_positions")