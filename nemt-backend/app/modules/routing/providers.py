"""Travel-time providers.

Two implementations behind one interface:

  * HaversineProvider  — offline. Straight-line distance inflated by a circuity factor, divided
                         by an average speed. No network, deterministic, works in tests and on a
                         laptop with no routing server. This is the default.
  * OsrmProvider       — real road routing via an OSRM server (self-hosted; NOT the public demo,
                         because pickup/drop-off addresses are PHI — see the note in app/core/geo.py).

The rest of the code only ever sees `get_router()` and the `Matrix`/`Path`/`Point` types below.
Swapping providers is a config change (`ROUTING_BACKEND=osrm`), never a code change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Protocol

import httpx

from app.core.config import settings


# ------------------------------------------------------------------ types
@dataclass(frozen=True, slots=True)
class Point:
    lat: float
    lng: float


@dataclass(slots=True)
class Matrix:
    """Travel times and distances between every pair of points.

    `durations_s[i][j]` is the seconds needed to travel from point i to point j; `None` means
    "no road route exists" (an island, a disconnected component, an OSRM error for that pair).
    `distances_m` is in metres. Both matrices are square (n x n).
    """

    durations_s: list[list[int | None]]
    distances_m: list[list[float | None]]
    source: str


@dataclass(slots=True)
class Path:
    """A route's polyline plus its length and drive time."""

    coordinates: list[list[float]]  # GeoJSON LineString: [[lng, lat], [lng, lat], ...]
    distance_m: float
    duration_s: float
    source: str


class RoutingError(RuntimeError):
    """Raised when the routing backend cannot answer (server down, timeout, bad response)."""


class Router(Protocol):
    name: str

    async def matrix(self, points: list[Point]) -> Matrix: ...

    async def path(self, points: list[Point]) -> Path: ...


# ------------------------------------------------------------------ haversine (offline)
EARTH_M = 6_371_000.0


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lng - a.lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(h))


class HaversineProvider:
    """Offline travel estimate: distance x circuity, divided by average speed."""

    name = "haversine"

    async def matrix(self, points: list[Point]) -> Matrix:
        n = len(points)
        speed_mps = max(1.0, settings.haversine_speed_kmh * 1000 / 3600)
        dur = [[0] * n for _ in range(n)]
        dist = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                d = haversine_m(points[i], points[j]) * settings.haversine_circuity
                if d < 1.0:      # same coordinates at two indices → no travel
                    continue
                dist[i][j] = d
                dur[i][j] = max(1, math.ceil(d / speed_mps))
        return Matrix(dur, dist, self.name)

    async def path(self, points: list[Point]) -> Path:
        if len(points) < 2:
            return Path([[p.lng, p.lat] for p in points], 0.0, 0.0, self.name)
        dist = sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))
        dist *= settings.haversine_circuity
        dur = dist / max(1.0, settings.haversine_speed_kmh * 1000 / 3600)
        return Path([[p.lng, p.lat] for p in points], dist, dur, self.name)


# ------------------------------------------------------------------ OSRM (real road routing)
def _coords(pts: list[Point]) -> str:
    """OSRM wants '<lng>,<lat>;<lng>,<lat>;...' — longitude first."""
    return ";".join(f"{p.lng:.6f},{p.lat:.6f}" for p in pts)


class OsrmProvider:
    """Talks to an OSRM HTTP server. Self-hosted only; never the public demo."""

    name = "osrm"

    def __init__(self, base_url: str, profile: str = "driving", timeout_s: int = 5,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.timeout_s = timeout_s
        self._transport = transport  # tests inject httpx.MockTransport here

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport)

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise RoutingError(f"OSRM unreachable: {exc}") from None
        if r.status_code != 200:
            raise RoutingError(f"OSRM HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise RoutingError("OSRM returned non-JSON") from None
        if body.get("code") != "Ok":
            raise RoutingError(f"OSRM refused: {body.get('code')} {body.get('message', '')}".strip())
        return body

    async def matrix(self, points: list[Point]) -> Matrix:
        """Fetches the full n x n matrix, chunked so no single request exceeds the server's limit.

        When the source block and the destination block are the SAME block of coordinates, we
        send each coordinate once and index it on both axes. Only when the two blocks differ do
        we concatenate — otherwise OSRM sees 200 coordinates for a 100 x 100 request.
        """
        n = len(points)
        block = max(2, settings.osrm_max_table_size)
        dur: list[list[int | None]] = [[None] * n for _ in range(n)]
        dist: list[list[float | None]] = [[None] * n for _ in range(n)]
        async with await self._client() as client:
            for i0 in range(0, n, block):
                i1 = min(i0 + block, n)
                for j0 in range(0, n, block):
                    j1 = min(j0 + block, n)
                    src = list(range(i0, i1))
                    dst = list(range(j0, j1))
                    if src[0] == dst[0] and src[-1] == dst[-1]:
                        coords = [points[i] for i in src]
                        dst_idx = list(range(len(src)))
                    else:
                        coords = [points[i] for i in src] + [points[j] for j in dst]
                        dst_idx = list(range(len(src), len(src) + len(dst)))
                    url = f"{self.base_url}/table/v1/{self.profile}/{_coords(coords)}"
                    body = await self._get(client, url, {
                        "sources": ";".join(str(i) for i in range(len(src))),
                        "destinations": ";".join(str(i) for i in dst_idx),
                        "annotations": "duration,distance",
                    })
                    for a, seconds_row in enumerate(body["durations"]):
                        for b, seconds in enumerate(seconds_row):
                            gi, gj = src[a], dst[b]
                            if gi == gj:
                                dur[gi][gj], dist[gi][gj] = 0, 0.0
                                continue
                            dur[gi][gj] = None if seconds is None else max(1, math.ceil(seconds))
                            meters = body["distances"][a][b]
                            dist[gi][gj] = None if meters is None else float(meters)
        return Matrix(dur, dist, self.name)

    async def path(self, points: list[Point]) -> Path:
        if len(points) < 2:
            return Path([[p.lng, p.lat] for p in points], 0.0, 0.0, self.name)
        url = f"{self.base_url}/route/v1/{self.profile}/{_coords(points)}"
        async with await self._client() as client:
            body = await self._get(client, url, {"overview": "full", "geometries": "geojson"})
        route = body["routes"][0]
        return Path(
            coordinates=route["geometry"]["coordinates"],
            distance_m=float(route["distance"]),
            duration_s=float(route["duration"]),
            source=self.name,
        )


# ------------------------------------------------------------------ selection & traffic
def get_router() -> Router:
    if settings.routing_backend == "osrm":
        if settings.env == "prod" and "router.project-osrm.org" in settings.osrm_url:
            # Patient addresses are PHI. Sending them to the public OSRM demo server is a breach.
            raise RuntimeError(
                "Refusing to use the public OSRM server in production (patient addresses are PHI). "
                "Run your own OSRM instance and point OSRM_URL at it."
            )
        return OsrmProvider(settings.osrm_url, settings.osrm_profile, settings.osrm_timeout_s)
    return HaversineProvider()


def _is_rush_hour(t: time) -> bool:
    morning = time(settings.traffic_morning_start_h) <= t < time(settings.traffic_morning_end_h,
                                                                settings.traffic_morning_end_m)
    evening = time(settings.traffic_evening_start_h, settings.traffic_evening_start_m) <= t < time(
        settings.traffic_evening_end_h, settings.traffic_evening_end_m)
    return morning or evening


def traffic_factor(when: datetime) -> float:
    """Multiplier applied to drive times for trips starting near `when`.

    Returns 1.0 outside rush windows, and 1.0 whenever `traffic_rush_factor` is 1.0 (the
    default), so the system behaves identically to having no traffic model at all until you
    deliberately turn it on.
    """
    factor = settings.traffic_rush_factor
    if factor <= 1.0:
        return 1.0
    return factor if _is_rush_hour(when.timetz().replace(tzinfo=None)) else 1.0