"""The routing providers in isolation: haversine math, the OSRM HTTP adapter, traffic windows."""

from datetime import datetime

import httpx
import pytest

from app.core.config import settings
from app.modules.routing.providers import (
    HaversineProvider,
    OsrmProvider,
    Point,
    RoutingError,
    get_router,
    haversine_m,
    traffic_factor,
)

DC, NORTH = Point(38.90, -77.00), Point(38.95, -77.00)  # ~5.5 km apart on the same meridian


# ------------------------------------------------------------------ haversine
async def test_haversine_matrix_is_sensible_and_symmetric():
    m = await HaversineProvider().matrix([DC, NORTH, DC])
    assert m.durations_s[0][0] == 0 and m.durations_s[0][2] == 0     # a place is 0 s from itself
    assert m.durations_s[0][1] == m.durations_s[1][0]                 # symmetric
    assert 5400 < haversine_m(DC, NORTH) < 5700                       # 0.05 deg of latitude ~ 5.56 km
    assert round(m.distances_m[0][1] / 1000, 2) == round(haversine_m(DC, NORTH) * settings.haversine_circuity / 1000, 2)
    expected_s = m.distances_m[0][1] / 1000 / settings.haversine_speed_kmh * 3600
    assert abs(m.durations_s[0][1] - round(expected_s)) <= 1          # ceil rounding tolerance
    assert m.source == "haversine"


async def test_haversine_path_returns_the_stops_in_geojson_order():
    p = await HaversineProvider().path([DC, NORTH])
    assert p.coordinates == [[-77.0, 38.9], [-77.0, 38.95]]           # [lng, lat] order
    assert p.distance_m > 0 and p.duration_s > 0


# ------------------------------------------------------------------ traffic windows
@pytest.mark.parametrize("when,expected_rush", [
    ("2026-10-12 08:00", True),      # Monday morning rush
    ("2026-10-12 09:29", True),      # still inside [07:00, 09:30)
    ("2026-10-12 09:30", False),     # exactly at the end — window is half-open
    ("2026-10-12 12:00", False),     # midday
    ("2026-10-12 15:30", True),      # evening rush starts
    ("2026-10-12 18:29", True),
    ("2026-10-12 18:30", False),     # evening window ends
    ("2026-10-12 03:00", False),     # dead of night
])
def test_traffic_factor(monkeypatch, when, expected_rush):
    monkeypatch.setattr(settings, "traffic_rush_factor", 1.5)
    assert traffic_factor(datetime.fromisoformat(when)) == (1.5 if expected_rush else 1.0)


def test_traffic_is_off_by_default():
    """The default factor must be 1.0 — otherwise tests would vary with the wall clock."""
    assert settings.traffic_rush_factor == 1.0


# ------------------------------------------------------------------ OSRM adapter
def fake_osrm(calls: list[httpx.Request], *, fail: bool = False):
    """Answers like a real OSRM /table and /route server, but with made-up numbers."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if fail:
            return httpx.Response(500, text="boom")
        parts = request.url.path.split("/")             # /table/v1/driving/<coords>
        coords = [tuple(map(float, c.split(","))) for c in parts[4].split(";")]
        if parts[1] == "route":
            return httpx.Response(200, json={
                "code": "Ok",
                "routes": [{
                    "distance": 1234.5,
                    "duration": 99.0,
                    "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]},
                }],
            })
        q = request.url.params
        src = [int(i) for i in q["sources"].split(";")]
        dst = [int(i) for i in q["destinations"].split(";")]
        assert q["annotations"] == "duration,distance"
        # simple made-up numbers: |latitude difference| * 1000 s and * 5000 m
        dur = [[abs(coords[i][1] - coords[j][1]) * 1000 for j in dst] for i in src]
        dist = [[abs(coords[i][1] - coords[j][1]) * 5000 for j in dst] for i in src]
        if len(coords) == 3:                             # make one pair unreachable
            dur[0][2] = None
        return httpx.Response(200, json={"code": "Ok", "durations": dur, "distances": dist})

    return handler


def provider(handler) -> OsrmProvider:
    return OsrmProvider("http://osrm.test:5000/", "driving", 5, transport=httpx.MockTransport(handler))


async def test_osrm_builds_requests_the_way_osrm_expects_and_parses_the_answer():
    calls: list[httpx.Request] = []
    pts = [Point(38.90, -77.00), Point(38.95, -77.10), Point(39.00, -77.20)]
    m = await provider(fake_osrm(calls)).matrix(pts)
    assert len(calls) == 1
    req = calls[0]
    # OSRM wants 'lng,lat' — our Point is lat,lng, so the URL shows them swapped
    assert req.url.path == "/table/v1/driving/-77.000000,38.900000;-77.100000,38.950000;-77.200000,39.000000"
    # Same block → coordinates sent ONCE, indices reused on both axes
    assert req.url.params["sources"] == "0;1;2"
    assert req.url.params["destinations"] == "0;1;2"
    assert m.durations_s[0][1] == pytest.approx(50, abs=1)   # fake server: 0.05° lat * 1000 s, ±1 for fp
    assert m.durations_s[1][0] == pytest.approx(50, abs=1)   # symmetric answer from the fake
    assert m.durations_s[0][0] == 0 and m.distances_m[2][2] == 0
    assert m.durations_s[0][2] is None                       # fake marked this pair unreachable
    assert m.source == "osrm"


async def test_osrm_matrix_is_fetched_in_blocks_when_it_is_big():
    calls: list[httpx.Request] = []
    pts = [Point(38.0 + i * 0.001, -77.0) for i in range(120)]
    m = await provider(fake_osrm(calls)).matrix(pts)
    # 120 coords, 100 max per call → ceil(120/100) = 2 blocks per axis → 2 x 2 = 4 requests
    assert len(calls) == 4
    for r in calls:
        n_src = len(r.url.params["sources"].split(";"))
        n_dst = len(r.url.params["destinations"].split(";"))
        n_coords = len(r.url.path.split("/")[4].split(";"))
        assert n_src * n_dst <= 100 * 100                       # each sub-table fits the server limit
        # Same block on both axes → coords sent once; different blocks → sources + destinations
        assert n_coords in (max(n_src, n_dst), n_src + n_dst)
    for i, j in ((0, 119), (119, 0), (60, 61), (49, 50), (100, 3)):
        assert m.durations_s[i][j] == pytest.approx(abs(pts[i].lat - pts[j].lat) * 1000, abs=1)


async def test_osrm_path_returns_geometry():
    path = await provider(fake_osrm([])).path([DC, NORTH])
    assert path.distance_m == 1234.5 and path.duration_s == 99.0
    assert path.coordinates == [[-77.0, 38.9], [-77.0, 38.95]]


@pytest.mark.parametrize("make", [
    lambda: provider(fake_osrm([], fail=True)),                                  # HTTP 500
    lambda: provider(lambda r: httpx.Response(200, json={"code": "NoSegment", "message": "no road"})),
    lambda: provider(lambda r: httpx.Response(200, text="<html>not json</html>")),
    lambda: provider(lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow"))),  # server down
])
async def test_osrm_problems_become_one_clear_error(make):
    with pytest.raises(RoutingError):
        await make().matrix([DC, NORTH])


# ------------------------------------------------------------------ selection & guard
def test_provider_selection_and_production_guard(monkeypatch):
    assert isinstance(get_router(), HaversineProvider)
    monkeypatch.setattr(settings, "routing_backend", "osrm")
    monkeypatch.setattr(settings, "osrm_url", "http://localhost:5000")
    assert isinstance(get_router(), OsrmProvider)
    # The public demo server must never be used in production
    monkeypatch.setattr(settings, "osrm_url", "https://router.project-osrm.org")
    monkeypatch.setattr(settings, "env", "prod")
    with pytest.raises(RuntimeError, match="public OSRM"):
        get_router()
    monkeypatch.setattr(settings, "env", "dev")
    assert isinstance(get_router(), OsrmProvider)