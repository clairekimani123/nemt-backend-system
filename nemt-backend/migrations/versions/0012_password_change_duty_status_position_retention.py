"""forced password change, driver duty status, driver-position retention

Revision ID: 0012
Revises: 0011

users.must_change_password   set when an administrator gives a user a temporary password; the
                             API refuses everything except "change my password" until it is done.
drivers.duty_status          what the DRIVER says about their availability:
                               available    on duty, can take work
                               unavailable  on duty but on a break / can't take more work now
                               off_duty     not working; location sharing is refused
                             "On a ride" is not stored: it is derived from the driver's live trips.
app_purge_driver_positions   driver_positions is append-only for the runtime role (no DELETE), so
                             retention runs through this owner-defined function, nightly from Celery.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False,
                                     server_default=sa.text("false")))

    op.add_column("drivers", sa.Column(
        "duty_status",
        sa.Enum("available", "unavailable", "off_duty", name="driver_duty_status",
                native_enum=False, create_constraint=True, length=16),
        nullable=False, server_default="off_duty",
    ))
    op.add_column("drivers", sa.Column("duty_changed_at", sa.DateTime(timezone=True), nullable=True))

    # SECURITY DEFINER: the runtime role has no DELETE on driver_positions. Deletes only rows older
    # than the given number of days (at least 1), across all companies; returns how many.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_purge_driver_positions(keep_days integer) RETURNS bigint
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS
        $$
        DECLARE
            n bigint;
        BEGIN
            IF keep_days IS NULL OR keep_days < 1 THEN
                RAISE EXCEPTION 'keep_days must be at least 1';
            END IF;
            DELETE FROM driver_positions WHERE recorded_at < now() - make_interval(days => keep_days);
            GET DIAGNOSTICS n = ROW_COUNT;
            RETURN n;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION app_purge_driver_positions(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_purge_driver_positions(integer) TO nemt_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_purge_driver_positions(integer)")
    op.drop_column("drivers", "duty_changed_at")
    op.drop_column("drivers", "duty_status")
    op.drop_column("users", "must_change_password")
