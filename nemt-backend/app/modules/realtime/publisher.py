"""Public helpers other modules call to publish events. Kept import-light so the trips
module doesn't drag the WebSocket stack into its imports unless it needs it."""

from datetime import UTC, datetime
from uuid import UUID

from app.core.permissions import Role
from app.modules.realtime.manager import manager
from app.modules.realtime.schemas import DriverPositionEvent, TripAssignedEvent, TripStatusEvent
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


async def driver_position(company_id: UUID, driver_id: UUID, lat: float, lng: float,
                          trip_id: UUID | None) -> None:
    await manager.broadcast(
        company_id,
        DriverPositionEvent(driver_id=driver_id, lat=lat, lng=lng, trip_id=trip_id,
                            at=datetime.now(UTC)),
        roles=DISPATCH_ROLES,
    )