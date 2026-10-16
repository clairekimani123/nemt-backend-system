"""phase 6: notification templates and log

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migrations.rls_helpers import grant, tenant_policy

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_templates",
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.Enum("email", "sms", "push", name="notification_channel",
                                     native_enum=False, create_constraint=True, length=32), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False, server_default="en"),
        sa.Column("subject", sa.String(length=200), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_notification_templates_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_templates")),
        sa.UniqueConstraint("company_id", "id", name="uq_notification_templates_company_id_id"),
        sa.UniqueConstraint("company_id", "event", "channel", "language", name="uq_notification_templates_key"),
    )
    op.create_index("ix_notification_templates_company_event", "notification_templates",
                    ["company_id", "event"])
    op.create_index(op.f("ix_notification_templates_company_id"), "notification_templates",
                    ["company_id"])

    op.create_table(
        "notification_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.Enum("email", "sms", "push", name="notification_log_channel",
                                     native_enum=False, create_constraint=True, length=32), nullable=False),
        sa.Column("recipient_user_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_address", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=True),
        sa.Column("body_preview", sa.String(length=200), nullable=True),
        sa.Column("status", sa.Enum("queued", "sent", "failed", "skipped",
                                    name="notification_delivery_status", native_enum=False,
                                    create_constraint=True, length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("attempts >= 1", name="ck_notification_logs_attempts_positive"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_notification_logs_company_id_companies"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_logs")),
    )
    op.create_index("ix_notification_logs_company_created", "notification_logs",
                    ["company_id", "created_at"])
    op.create_index("ix_notification_logs_recipient", "notification_logs",
                    ["company_id", "recipient_user_id"])
    op.create_index(op.f("ix_notification_logs_company_id"), "notification_logs", ["company_id"])

    tenant_policy("notification_templates")
    tenant_policy("notification_logs")

    grant("SELECT, INSERT, UPDATE", "notification_templates")
    grant("SELECT, INSERT", "notification_logs")     # append-only log


        # A minimal default: "your driver is on the way" in English email.
    # Every other template is created per-tenant from the admin UI.
    op.execute("""
        INSERT INTO notification_templates
        (id, company_id, event, channel, language, subject, body, is_active, created_at, updated_at)
        SELECT gen_random_uuid(), c.id, 'trip.en_route.patient', 'email', 'en',
               'Your ride is on the way',
               E'Hi {{patient_first_name}},\\n\\nYour driver is on the way to {{pickup_line1}}.\\n\\nTrip: {{trip_ref}}\\n\\nThank you.',
               true, now(), now()
        FROM companies c
        WHERE c.is_platform = false
    """)


def downgrade() -> None:
    op.drop_index(op.f("ix_notification_logs_company_id"), table_name="notification_logs")
    op.drop_index("ix_notification_logs_recipient", table_name="notification_logs")
    op.drop_index("ix_notification_logs_company_created", table_name="notification_logs")
    op.drop_table("notification_logs")

    op.drop_index(op.f("ix_notification_templates_company_id"), table_name="notification_templates")
    op.drop_index("ix_notification_templates_company_event", table_name="notification_templates")
    op.drop_table("notification_templates")