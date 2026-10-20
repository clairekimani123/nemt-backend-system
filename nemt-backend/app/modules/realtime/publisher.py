"""Public helpers other modules call to publish events. Kept import-light so the trips
module doesn't drag the WebSocket stack into its imports unless it needs it."""

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from app.core.permissions import Role
from app.modules.realtime.manager import manager
from app.modules.realtime.schemas import (
    AssignmentAnsweredEvent,
    DriverDutyEvent,
    DriverPositionEvent,
    PatientCreatedEvent,
    TripAssignedEvent,
    TripChangedEvent,
    TripCreatedEvent,
    TripStatusEvent,
)
from app.modules.trips.state import TripStatus

DISPATCH_ROLES = {Role.SUPER_ADMIN, Role.COMPANY_ADMIN, Role.DISPATCHER}


async def trip_status_changed(company_id: UUID, trip_id: UUID, status: TripStatus,
                              driver_id: UUID | None, vehicle_id: UUID | None) -> None:
    await manager.broadcast(
        company_id,
        TripStatusEvent(trip_id=trip_id, status=status, driver_id=driver_id,
                        vehicle_id=vehicle_id, at=datetime.now(UTC)),
        roles=DISPATCH_ROLES,
    )


async def trip_assigned(company_id: UUID, trip_id: UUID, driver_id: UUID,
                        vehicle_id: UUID, driver_user_id: UUID) -> None:
    event = TripAssignedEvent(trip_id=trip_id, driver_id=driver_id, vehicle_id=vehicle_id,
                              at=datetime.now(UTC))
    # to every dispatcher in the company
    await manager.broadcast(company_id, event, roles=DISPATCH_ROLES)
    # ...and to the driver it's for
    await manager.to_user(company_id, driver_user_id, event)


async def trip_changed_for_users(company_id: UUID, user_ids: Iterable[UUID | None], trip_id: UUID,
                                 change: str) -> None:
    """'Your trip changed, refetch it' to the driver and/or passenger involved (ids only, no PHI)."""
    ids = {u for u in user_ids if u is not None}
    if ids:
        await manager.broadcast(company_id, TripChangedEvent(trip_id=trip_id, change=change, at=datetime.now(UTC)),
                                user_ids=ids)


async def driver_duty_changed(company_id: UUID, driver_id: UUID, duty_status: str) -> None:
    await manager.broadcast(company_id, DriverDutyEvent(driver_id=driver_id, duty_status=duty_status,
                                                        at=datetime.now(UTC)), roles=DISPATCH_ROLES)


async def assignment_answered(company_id: UUID, trip_id: UUID, driver_id: UUID, *, accepted: bool,
                              driver_user_id: UUID | None) -> None:
    event = AssignmentAnsweredEvent(trip_id=trip_id, driver_id=driver_id, accepted=accepted, at=datetime.now(UTC))
    await manager.broadcast(company_id, event, roles=DISPATCH_ROLES)
    if driver_user_id is not None:
        await manager.to_user(company_id, driver_user_id, event)


async def driver_position(company_id: UUID, driver_id: UUID, lat: float, lng: float,
                          trip_id: UUID | None) -> None:
    await manager.broadcast(
        company_id,
        DriverPositionEvent(driver_id=driver_id, lat=lat, lng=lng, trip_id=trip_id,
                            at=datetime.now(UTC)),
        roles=DISPATCH_ROLES,
    )

async def trip_created(company_id: UUID, trip_ids: list[UUID], source: str) -> None:
    for trip_id in trip_ids:
        await manager.broadcast(
            company_id, TripCreatedEvent(trip_id=trip_id, source=source, at=datetime.now(UTC)),
            roles=DISPATCH_ROLES,
        )


async def patient_created(company_id: UUID, patient_id: UUID, *, self_registered: bool) -> None:
    await manager.broadcast(
        company_id,
        PatientCreatedEvent(patient_id=patient_id, self_registered=self_registered, at=datetime.now(UTC)),
        roles=DISPATCH_ROLES,
    )
