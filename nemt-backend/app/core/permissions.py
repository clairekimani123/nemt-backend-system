"""Roles and permissions (RBAC).

Roles answer "who are you?", permissions answer "what may you do?". Code checks
permissions, never roles, so adding a role later doesn't touch every endpoint.
Static for now; a `role_permissions` table can replace the dict when the admin
UI needs per-company custom roles.
"""

from enum import StrEnum


class Role(StrEnum):
    SUPER_ADMIN = "super_admin"
    COMPANY_ADMIN = "company_admin"
    DISPATCHER = "dispatcher"
    BILLING_STAFF = "billing_staff"
    FLEET_MANAGER = "fleet_manager"
    DRIVER = "driver"
    PASSENGER = "passenger"
    FACILITY_BROKER = "facility_broker"


class Permission(StrEnum):
    PLATFORM_MANAGE = "platform:manage"
    USERS_READ = "users:read"
    USERS_WRITE = "users:write"
    AUDIT_READ = "audit:read"
    SETTINGS_WRITE = "settings:write"
    PATIENTS_READ = "patients:read"
    PATIENTS_WRITE = "patients:write"
    TRIPS_READ = "trips:read"
    TRIPS_WRITE = "trips:write"
    DISPATCH = "dispatch:manage"
    DRIVERS_READ = "drivers:read"
    DRIVERS_WRITE = "drivers:write"
    VEHICLES_READ = "vehicles:read"
    VEHICLES_WRITE = "vehicles:write"
    FACILITIES_READ = "facilities:read"
    FACILITIES_WRITE = "facilities:write"
    BILLING_READ = "billing:read"
    BILLING_WRITE = "billing:write"
    REPORTS_READ = "reports:read"
    DRIVER_APP = "driver:app"  # own-trip scope enforced in services
    PASSENGER_PORTAL = "passenger:portal"  # own-trip scope enforced in services
    FACILITY_PORTAL = "facility:portal"


P = Permission
_TENANT_ALL = set(Permission) - {P.PLATFORM_MANAGE, P.DRIVER_APP, P.PASSENGER_PORTAL, P.FACILITY_PORTAL}

ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.SUPER_ADMIN: set(Permission),
    Role.COMPANY_ADMIN: _TENANT_ALL,
    Role.DISPATCHER: {
        P.PATIENTS_READ, P.PATIENTS_WRITE, P.TRIPS_READ, P.TRIPS_WRITE, P.DISPATCH,
        P.DRIVERS_READ, P.VEHICLES_READ, P.FACILITIES_READ, P.REPORTS_READ,
    },
    Role.BILLING_STAFF: {
        P.BILLING_READ, P.BILLING_WRITE, P.TRIPS_READ, P.PATIENTS_READ,
        P.FACILITIES_READ, P.REPORTS_READ,
    },
    Role.FLEET_MANAGER: {
        P.VEHICLES_READ, P.VEHICLES_WRITE, P.DRIVERS_READ, P.DRIVERS_WRITE, P.REPORTS_READ,
    },
    Role.DRIVER: {P.DRIVER_APP},
    Role.PASSENGER: {P.PASSENGER_PORTAL},
    Role.FACILITY_BROKER: {P.FACILITY_PORTAL},
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]