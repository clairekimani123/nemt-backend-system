"""Imports every model so Alembic's metadata sees ALL tables.

When you add a model in a new module: import it here AND add its name to __all__.
Being listed in __all__ is what stops the linter from deleting an 'unused' import.
(tests/test_model_registry.py fails if you forget.)
"""

from app.core.base import Base
from app.modules.audit.models import AuditLog
from app.modules.auth.models import PasswordResetToken, RefreshToken
from app.modules.billing.models import Claim, ClaimLine, ClaimPayment, RatePlan, RateRule
from app.modules.documents.models import Document
from app.modules.drivers.models import Driver
from app.modules.facilities.models import Broker, Facility
from app.modules.fleet.models import VehicleInspection, VehicleMaintenance, VehicleMileage
from app.modules.notifications.models import NotificationLog, NotificationTemplate
from app.modules.patients.models import Patient, PatientAddress
from app.modules.realtime.models import DriverPosition
from app.modules.routing.models import Route, RoutePlan, RouteStop
from app.modules.tenants.models import Company
from app.modules.trips.models import Counter, RecurringTrip, Trip, TripEvent
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

__all__ = [
    "AuditLog",
    "Base",
    "Broker",
    "Claim",
    "ClaimLine",
    "ClaimPayment",
    "Company",
    "Counter",
    "Document",
    "Driver",
    "DriverPosition",
    "Facility",
    "NotificationLog",
    "NotificationTemplate",
    "PasswordResetToken",
    "Patient",
    "PatientAddress",
    "RatePlan",
    "RateRule",
    "RecurringTrip",
    "RefreshToken",
    "Route",
    "RoutePlan",
    "RouteStop",
    "Trip",
    "TripEvent",
    "User",
    "Vehicle",
    "VehicleInspection",
    "VehicleMaintenance",
    "VehicleMileage",
]