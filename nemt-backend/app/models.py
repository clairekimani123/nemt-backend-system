"""Imports every model so Alembic's metadata sees ALL tables.

If you add a model in a new module, add it here AND to __all__ (listing it in __all__
is what stops the linter from deleting an 'unused' import).
"""

from app.core.base import Base
from app.modules.audit.models import AuditLog
from app.modules.auth.models import PasswordResetToken, RefreshToken
from app.modules.documents.models import Document
from app.modules.drivers.models import Driver
from app.modules.facilities.models import Broker, Facility
from app.modules.patients.models import Patient, PatientAddress
from app.modules.tenants.models import Company
from app.modules.users.models import User
from app.modules.vehicles.models import Vehicle

__all__ = [
    "AuditLog",
    "Base",
    "Broker",
    "Company",
    "Document",
    "Driver",
    "Facility",
    "PasswordResetToken",
    "Patient",
    "PatientAddress",
    "RefreshToken",
    "User",
    "Vehicle",
]