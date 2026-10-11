"""The trip life-cycle as a STATE MACHINE.

Analogy: a board game. At any moment a trip sits on exactly one square (its status) and the
rules say which squares it may move to next. Anything not listed is an illegal move, and
the code refuses it, so a trip can never jump from 'scheduled' straight to 'completed'.
"""

from enum import StrEnum


class TripStatus(StrEnum):
    SCHEDULED = "scheduled"                    # booked, no driver yet
    ASSIGNED = "assigned"                      # driver + vehicle chosen
    EN_ROUTE_TO_PICKUP = "en_route_to_pickup"
    ARRIVED_AT_PICKUP = "arrived_at_pickup"
    PASSENGER_ON_BOARD = "passenger_on_board"
    EN_ROUTE_TO_DROPOFF = "en_route_to_dropoff"
    ARRIVED_AT_DROPOFF = "arrived_at_dropoff"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


S = TripStatus

TRANSITIONS: dict[TripStatus, set[TripStatus]] = {
    S.SCHEDULED: {S.ASSIGNED, S.CANCELLED},
    S.ASSIGNED: {S.SCHEDULED, S.EN_ROUTE_TO_PICKUP, S.CANCELLED},
    S.EN_ROUTE_TO_PICKUP: {S.ASSIGNED, S.ARRIVED_AT_PICKUP, S.CANCELLED},
    S.ARRIVED_AT_PICKUP: {S.PASSENGER_ON_BOARD, S.NO_SHOW, S.CANCELLED},
    S.PASSENGER_ON_BOARD: {S.EN_ROUTE_TO_DROPOFF},
    S.EN_ROUTE_TO_DROPOFF: {S.ARRIVED_AT_DROPOFF},
    S.ARRIVED_AT_DROPOFF: {S.COMPLETED},
    S.COMPLETED: set(),
    S.CANCELLED: set(),
    S.NO_SHOW: set(),
}

TERMINAL = {S.COMPLETED, S.CANCELLED, S.NO_SHOW}
OCCUPYING = {S.ASSIGNED, S.EN_ROUTE_TO_PICKUP, S.ARRIVED_AT_PICKUP, S.PASSENGER_ON_BOARD,
             S.EN_ROUTE_TO_DROPOFF, S.ARRIVED_AT_DROPOFF}          # a driver + vehicle are busy
NON_TERMINAL = OCCUPYING | {S.SCHEDULED}
PRE_PICKUP = {S.SCHEDULED, S.ASSIGNED, S.EN_ROUTE_TO_PICKUP}        # candidates for "late"
EDITABLE = {S.SCHEDULED, S.ASSIGNED}
# What a DRIVER may do from the phone. Assigning/cancelling stays with dispatch.
DRIVER_TARGETS = {S.EN_ROUTE_TO_PICKUP, S.ARRIVED_AT_PICKUP, S.PASSENGER_ON_BOARD,
                  S.EN_ROUTE_TO_DROPOFF, S.ARRIVED_AT_DROPOFF, S.COMPLETED, S.NO_SHOW}
# Reachable through the status endpoint. SCHEDULED/ASSIGNED have their own assign/unassign calls.
STATUS_ENDPOINT_TARGETS = set(S) - {S.SCHEDULED, S.ASSIGNED}


def can_transition(current: TripStatus, target: TripStatus) -> bool:
    return target in TRANSITIONS[current]


class Leg(StrEnum):
    OUTBOUND = "outbound"
    RETURN = "return"


class TripSource(StrEnum):
    DISPATCHER = "dispatcher"
    RECURRING = "recurring"
    PASSENGER_PORTAL = "passenger_portal"
    BROKER_IMPORT = "broker_import"


class CancelReason(StrEnum):
    PATIENT_CANCELLED = "patient_cancelled"
    FACILITY_CANCELLED = "facility_cancelled"
    PATIENT_HOSPITALIZED = "patient_hospitalized"
    NO_DRIVER_AVAILABLE = "no_driver_available"
    VEHICLE_BREAKDOWN = "vehicle_breakdown"
    WEATHER = "weather"
    DUPLICATE_BOOKING = "duplicate_booking"
    OTHER = "other"