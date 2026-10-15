from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.compliance.router import router as compliance_router
from app.modules.documents.router import router as documents_router
from app.modules.drivers.router import router as drivers_router
from app.modules.facilities.router import brokers_router
from app.modules.facilities.router import router as facilities_router
from app.modules.patients.router import router as patients_router
from app.modules.tenants.router import router as platform_router
from app.modules.trips.driver_router import router as driver_app_router
from app.modules.trips.recurring_router import router as recurring_router
from app.modules.trips.router import board_router
from app.modules.trips.router import router as trips_router
from app.modules.users.router import router as users_router
from app.modules.vehicles.router import router as vehicles_router
from app.modules.routing.driver_route import router as driver_route_router
from app.modules.routing.router import router as routing_router
from app.modules.realtime.ws import router as realtime_router


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
          driver_app_router,  routing_router, driver_route_router, realtime_router):
    app.include_router(r)