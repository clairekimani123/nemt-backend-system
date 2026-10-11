"""Geo helpers + the geocoder PORT.

HIPAA warning: a patient's address is PHI. Never send it to a free/public geocoder
(e.g. the public Nominatim server). Production adapters must be self-hosted
(Nominatim/Pelias) or covered by a BAA. Until then: `none` (client supplies lat/lng)
or `fake` (deterministic coordinates for dev and tests).
"""

import hashlib
from typing import Protocol

from geoalchemy2 import WKTElement

from app.core.config import settings


def point(lat: float, lng: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)  # WKT is x y = lng lat


class Geocoder(Protocol):
    async def geocode(self, address: str) -> tuple[float, float] | None: ...


class NullGeocoder:
    async def geocode(self, address: str) -> tuple[float, float] | None:
        return None


class FakeGeocoder:
    """Deterministic pseudo-coordinates around Washington DC. Dev/test only."""

    async def geocode(self, address: str) -> tuple[float, float] | None:
        h = hashlib.sha256(address.lower().encode()).digest()
        lat = 38.80 + int.from_bytes(h[:4], "big") / 2**32 * 0.20
        lng = -77.20 + int.from_bytes(h[4:8], "big") / 2**32 * 0.30
        return round(lat, 6), round(lng, 6)


def get_geocoder() -> Geocoder:
    return FakeGeocoder() if settings.geocoder_backend == "fake" else NullGeocoder()