"""phase 7.2: rate rules

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

from migrations.rls_helpers import grant, tenant_policy

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_rules",
        sa.Column("rate_plan_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exclusive", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("broker_id", sa.Uuid(), nullable=True),
        sa.Column("procedure_code", sa.String(length=8), nullable=True),
        sa.Column("mobility", sa.String(length=32), nullable=True),
        sa.Column("weekday_mask", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Enum("flat", "multiplier", name="rate_rule_kind",
                                  native_enum=False, create_constraint=True, length=32), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("multiplier_bp", sa.Integer(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(kind = 'flat' AND amount_cents IS NOT NULL) OR "
                           "(kind = 'multiplier' AND multiplier_bp IS NOT NULL)",
                           name="ck_rate_rules_payload_matches_kind"),
        sa.CheckConstraint("amount_cents IS NULL OR amount_cents >= 0",
                           name="ck_rate_rules_amount_positive"),
        sa.CheckConstraint("multiplier_bp IS NULL OR multiplier_bp >= 0",
                           name="ck_rate_rules_multiplier_positive"),
        sa.CheckConstraint("weekday_mask IS NULL OR (weekday_mask BETWEEN 0 AND 127)",
                           name="ck_rate_rules_weekday_range"),
        sa.ForeignKeyConstraint(["company_id", "rate_plan_id"], ["rate_plans.company_id", "rate_plans.id"],
                                name="fk_rate_rules_company_rate_plan", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id", "broker_id"], ["brokers.company_id", "brokers.id"],
                                name="fk_rate_rules_company_broker"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_rate_rules_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_rules")),
        sa.UniqueConstraint("company_id", "id", name="uq_rate_rules_company_id_id"),
    )
    op.create_index("ix_rate_rules_plan_priority", "rate_rules", ["rate_plan_id", "priority"])
    op.create_index(op.f("ix_rate_rules_company_id"), "rate_rules", ["company_id"])

    tenant_policy("rate_rules")
    grant("SELECT, INSERT, UPDATE, DELETE", "rate_rules")


def downgrade() -> None:
    op.drop_index(op.f("ix_rate_rules_company_id"), table_name="rate_rules")
    op.drop_index("ix_rate_rules_plan_priority", table_name="rate_rules")
    op.drop_table("rate_rules")