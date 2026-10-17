import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InvalidRequestError
from sqlalchemy.orm.exc import ObjectDeletedError

from app.core.config import settings
from app.core.db import SessionLocal, engine, is_lock_conflict
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.compliance.router import router as compliance_router
from app.modules.documents.router import router as documents_router
from app.modules.drivers.router import router as drivers_router
from app.modules.facilities.router import brokers_router
from app.modules.facilities.router import router as facilities_router
from app.modules.passenger.router import router as passenger_router
from app.modules.patients.router import router as patients_router
from app.modules.realtime.ws import router as realtime_router
from app.modules.routing.driver_route import router as driver_route_router
from app.modules.routing.router import router as routing_router
from app.modules.setup.router import router as setup_router
from app.modules.stats.router import router as stats_router
from app.modules.tenants.router import router as platform_router
from app.modules.tenants.system import router as platform_system_router
from app.modules.trips.driver_router import router as driver_app_router
from app.modules.trips.recurring_router import router as recurring_router
from app.modules.trips.router import board_router
from app.modules.trips.router import router as trips_router
from app.modules.users.router import router as users_router
from app.modules.vehicles.router import router as vehicles_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="MediRoutes API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "X-Tenant"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"  # responses may contain PHI: never cache
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(InvalidRequestError)
async def vanished_row(_: Request, exc: InvalidRequestError):
    # A write committed, then its row was gone before it could be read back: a system reset ran in
    # between (it TRUNCATEs every operational table). Say so instead of failing with a bare 500.
    if isinstance(exc, ObjectDeletedError) or "Could not refresh instance" in str(exc):
        return JSONResponse(status_code=409, content={
            "detail": "This record no longer exists. It may have been removed by a system reset."})
    log.error("unhandled ORM error: %s", exc.__class__.__name__)  # class only: messages may hold PHI
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.exception_handler(DBAPIError)
async def database_error(_: Request, exc: DBAPIError):
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if is_lock_conflict(exc):  # e.g. lost a deadlock against a running system reset; nothing was saved
        return JSONResponse(status_code=503, headers={"Retry-After": "2"}, content={
            "detail": "The system is busy and your change was not saved. Please try again."})
    log.error("unhandled database error: %s (sqlstate %s)", exc.orig.__class__.__name__, sqlstate)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}


@app.get("/health/ready", tags=["ops"])
async def ready():
    async with SessionLocal() as s:
        await s.execute(text("SELECT 1"))
    return {"status": "ready"}


for r in (auth_router, users_router, audit_router, platform_router, patients_router, drivers_router,
          vehicles_router, facilities_router, brokers_router, documents_router, compliance_router, trips_router, board_router, recurring_router,
          driver_app_router,  routing_router, driver_route_router, realtime_router, passenger_router,
          setup_router, stats_router, platform_system_router):
    app.include_router(r)