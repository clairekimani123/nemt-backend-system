"""profile photos, vehicle colour, passenger acceptance of an assigned driver

Revision ID: 0010
Revises: 0009

users.avatar_*      a user's own profile photo. The bytes live in encrypted storage under a
                    server-generated key; the client never learns the key or a file path.
vehicles.color      lets a passenger recognise their ride at the kerb (plate + colour + model).
trips.passenger_*   the passenger's answer to the driver dispatch assigned:
                      NULL      no answer needed (patient has no portal login, or not assigned)
                      pending   assigned, waiting for the passenger
                      accepted  passenger accepted this driver
                      declined  passenger declined; the trip is back in the unassigned lane
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_key", sa.String(length=80), nullable=True))
    op.add_column("users", sa.Column("avatar_content_type", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("avatar_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "avatar_fields_together", "users",
        "(avatar_key IS NULL) = (avatar_content_type IS NULL)",
    )

    op.add_column("vehicles", sa.Column("color", sa.String(length=30), nullable=True))

    op.add_column("trips", sa.Column(
        "passenger_response",
        sa.Enum("pending", "accepted", "declined", name="trip_passenger_response",
                native_enum=False, create_constraint=True, length=16),
        nullable=True,
    ))
    op.add_column("trips", sa.Column("passenger_responded_at", sa.DateTime(timezone=True), nullable=True))
    # pending/accepted always belong to a live assignment; declined means the driver was released
    op.create_check_constraint(
        "passenger_response_matches_assignment", "trips",
        "passenger_response IS NULL OR (passenger_response = 'declined') = (driver_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("passenger_response_matches_assignment", "trips", type_="check")
    op.drop_column("trips", "passenger_responded_at")
    op.drop_column("trips", "passenger_response")
    op.drop_column("vehicles", "color")
    op.drop_constraint("avatar_fields_together", "users", type_="check")
    op.drop_column("users", "avatar_updated_at")
    op.drop_column("users", "avatar_content_type")
    op.drop_column("users", "avatar_key")
