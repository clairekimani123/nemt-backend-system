"""The optimiser in isolation: hand-built problems on a number line.

Position `p` means "a point 1 minute of driving away from position p+1". So `travel[i][j]`
is the matrix of absolute differences. This lets us hand-verify every schedule.
"""

from app.modules.routing.solver import (
    Job,
    Load,
    VehicleSpec,
    solve,
)

POS = [0, 10, 20, 30, 40, 50, 60]
TRAVEL = [[abs(a - b) for b in POS] for a in POS]


def job(key, p, d, *, load=Load(seats=1), open_=0, close=200, deadline=None,
        dwell=2, max_ride=300, allowed=None, fixed_vehicle=None, penalty=0):
    # `allowed` is filled in by `run()` below so it always refers to real vehicle indices,
    # unless the caller gives an explicit set.
    return Job(
        key=key, pickup=p, dropoff=d, load=load,
        pickup_open=open_, pickup_close=close, dropoff_close=deadline,
        pickup_service=dwell, dropoff_service=dwell, max_ride=max_ride,
        allowed=frozenset() if allowed is None else frozenset(allowed),
        fixed_vehicle=fixed_vehicle, pickup_late_penalty=penalty,
    )


def van(key="v", seats=2, wheelchair=0, stretcher=0, shift_start=0, shift_end=1440):
    return VehicleSpec(key=key, capacity=Load(seats, wheelchair, stretcher),
                       shift_start=shift_start, shift_end=shift_end)


def run(jobs, vehicles):
    """Fill every job's `allowed` to mean "these vehicles" unless it was set explicitly."""
    every = frozenset(range(len(vehicles)))
    patched = [
        j if j.allowed else
        Job(key=j.key, pickup=j.pickup, dropoff=j.dropoff, load=j.load,
            pickup_open=j.pickup_open, pickup_close=j.pickup_close,
            dropoff_close=j.dropoff_close, pickup_service=j.pickup_service,
            dropoff_service=j.dropoff_service, max_ride=j.max_ride,
            allowed=every, fixed_vehicle=j.fixed_vehicle,
            pickup_late_penalty=j.pickup_late_penalty)
        for j in jobs
    ]
    return solve(TRAVEL, patched, vehicles)


def order(route):
    return [f"{s.job_key}{s.kind[0]}" for s in route.stops]


# ------------------------------------------------------------------ basic sequencing


def test_visits_stops_in_the_cheapest_order():
    sol = run([job("C", 2, 3), job("A", 0, 1), job("B", 1, 2)], [van(seats=1)])
    [route] = sol.routes
    # Each drop-off is the next pickup: a daisy chain, total drive = 30
    assert order(route) == ["Ap", "Ad", "Bp", "Bd", "Cp", "Cd"]
    assert route.drive_minutes == 30
    assert sol.dropped == []


def test_shared_ride_is_chosen_when_it_saves_driving():
    """Two riders going the same direction share, and share earlier than either alone."""
    sol = run([job("A", 0, 3), job("B", 1, 4)], [van(seats=2)])
    [route] = sol.routes
    assert order(route) == ["Ap", "Bp", "Ad", "Bd"]
    assert max(s.load_after.seats for s in route.stops) == 2
    # Two separate routes would cost: A alone 30 + B alone 30 = 60 drive.
    # One shared route costs 40 drive. Sharing wins.
    assert route.drive_minutes < 60


def test_capacity_prevented_interleaving():
    """A one-seat vehicle cannot share, so it must do the trips sequentially."""
    sol = run([job("A", 0, 1), job("B", 2, 3)], [van(seats=1)])
    [route] = sol.routes
    assert sol.dropped == []
    assert max(s.load_after.seats for s in route.stops) <= 1
    assert order(route) in (["Ap", "Ad", "Bp", "Bd"], ["Bp", "Bd", "Ap", "Ad"])


# ------------------------------------------------------------------ capability


def test_wheelchair_rider_only_goes_to_the_accessible_vehicle():
    wc = job("W", 0, 3, load=Load(wheelchair=1), allowed={1}, dwell=8)
    sol = run([wc], [van("sedan", seats=3), van("van", seats=3, wheelchair=1)])
    assert [r.vehicle for r in sol.routes] == [1]

    # If the accessible van is missing entirely, the trip must be refused, not silently
    # put on the sedan.
    wc0 = job("W", 0, 3, load=Load(wheelchair=1), allowed={0}, dwell=8)
    sol2 = run([wc0], [van("sedan", seats=3)])
    assert sol2.routes == []
    assert sol2.early_dropped.get("W") is not None or sol2.dropped == ["W"]


def test_appointment_deadline_is_met_or_the_trip_is_refused_up_front():
    ok = run([job("D", 0, 3, deadline=40)], [van(seats=1)])
    assert [s for s in ok.routes[0].stops if s.kind == "dropoff"][0].arrival <= 40

    # 2 min loading + 30 min drive + 2 min unload = 34 > 30: impossible, rejected up front
    late = run([job("L", 0, 3, deadline=30)], [van(seats=1)])
    assert late.early_dropped == {"L": "appointment_unreachable"}
    assert late.routes == []


def test_fixed_jobs_stay_on_their_vehicle():
    sol = run([job("A", 0, 3, fixed_vehicle=1)], [van("v0"), van("v1")])
    assert [r.vehicle for r in sol.routes] == [1]


# ------------------------------------------------------------------ soft windows


def test_pickup_late_costs_penalty_not_refusal():
    """A trip whose pickup window cannot be met still gets scheduled; it's just penalised."""
    j = job("L", 0, 3, open_=100, close=100, penalty=5, allowed={0})
    sol = run([j], [van(seats=1)])
    assert sol.dropped == []
    [route] = sol.routes
    assert route.stops[0].arrival >= 100
    # A penalty is reported, not a failure
    assert sol.total_penalty >= 0


def test_pickup_arrives_at_open_when_it_would_be_early():
    """Arriving before pickup_open means waiting, not arriving early."""
    j = job("E", 0, 3, open_=100, close=200, allowed={0})
    sol = run([j], [van(seats=1)])
    [route] = sol.routes
    assert route.stops[0].arrival == 100
    assert route.stops[0].wait == 100


# ------------------------------------------------------------------ shifts


def test_shift_bounds_reject_impossible_schedules():
    # Vehicle available 0..20 minutes, job needs a 30-minute drive: infeasible
    j = job("X", 0, 3, allowed={0})
    sol = run([j], [van(seats=1, shift_start=0, shift_end=20)])
    assert sol.dropped == ["X"] or sol.early_dropped.get("X") is not None


def test_shift_start_pushes_pickup_later():
    j = job("S", 0, 3, open_=0, close=100, allowed={0})
    sol = run([j], [van(seats=1, shift_start=30, shift_end=200)])
    [route] = sol.routes
    assert route.stops[0].arrival >= 30


# ------------------------------------------------------------------ ride time


def test_max_ride_caps_a_detour():
    """A long direct ride plus a detour would break max_ride; the solver must not interleave."""
    a = job("A", 0, 5, max_ride=50)          # direct 50 minutes = max_ride exactly
    b = job("B", 1, 2)                       # short trip
    sol = run([a, b], [van(seats=2)])
    # Either A goes alone and B shares, or B shares but A's ride stays <= 50.
    for route in sol.routes:
        stops = {(s.job_key, s.kind): s for s in route.stops}
        if ("A", "pickup") in stops and ("A", "dropoff") in stops:
            ride = stops[("A", "dropoff")].arrival - stops[("A", "pickup")].departure
            assert ride <= 50, ride


# ------------------------------------------------------------------ multi-vehicle


def test_vehicle_capacity_forces_two_vehicles():
    # Two riders must both reach the same hospital by a HARD deadline (dropoff_close=15).
    # Each occupies 1 seat. A single 1-seat van cannot serve both:
    #   serve A first  → A drops at t≈14, van back at 0 by t≈24, B drops at t≈38 > 15 ✗
    #   serve B first  → same problem, mirrored
    # So a 1-seat van serves one and drops the other; two 1-seat vans serve both.
    a = job("A", 0, 1, load=Load(seats=1), open_=0, close=2, deadline=15)
    b = job("B", 0, 1, load=Load(seats=1), open_=0, close=2, deadline=15)

    two_vans = run([a, b], [van("v0", seats=1), van("v1", seats=1)])
    assert len(two_vans.routes) == 2
    assert two_vans.dropped == []

    one_van = run([a, b], [van("v0", seats=1)])
    assert len(one_van.routes) == 1
    assert len(one_van.dropped) == 1


def test_one_vehicle_can_cover_a_sequential_day():
    seq_jobs = [job(f"J{i}", i, i + 1, open_=0, close=200) for i in range(3)]
    sol = run(seq_jobs, [van(seats=1)])
    assert sol.dropped == []
    assert len(sol.routes) == 1


def test_no_jobs_returns_empty():
    sol = run([], [van()])
    assert sol.routes == [] and sol.dropped == [] and sol.early_dropped == {}


def test_no_vehicles_drops_everything():
    sol = run([job("A", 0, 1)], [])
    assert sol.early_dropped == {"A": "no_compatible_resource"}
    assert sol.dropped == []


# ------------------------------------------------------------------ determinism


def test_solve_is_deterministic():
    jobs = [job("A", 0, 3), job("B", 1, 4), job("C", 2, 5)]
    vans = [van("v0"), van("v1")]
    a = run(jobs, vans)
    b = run(jobs, vans)
    assert [order(r) for r in a.routes] == [order(r) for r in b.routes]


# ------------------------------------------------------------------ correctness invariants


def test_no_stop_load_exceeds_capacity():
    jobs = [job(f"J{i}", i, i + 2, load=Load(seats=1), open_=0, close=200) for i in range(4)]
    sol = run(jobs, [van(seats=2)])
    for route in sol.routes:
        for s in route.stops:
            assert s.load_after.seats <= 2
            assert s.load_after.wheelchair <= 0
            assert s.load_after.stretcher <= 0


def test_every_dropoff_precedes_its_own_next_pickup():
    """No drop-off may appear before its own pickup in any route."""
    jobs = [job("A", 0, 5), job("B", 1, 4), job("C", 2, 3)]
    sol = run(jobs, [van(seats=3)])
    for route in sol.routes:
        seen: set[str] = set()
        for s in route.stops:
            if s.kind == "pickup":
                seen.add(s.job_key)
            else:
                assert s.job_key in seen, f"{s.job_key} dropped off before pickup"


def test_all_jobs_appear_exactly_once_or_are_dropped():
    jobs = [job(f"J{i}", i, i + 2, open_=0, close=200) for i in range(3)]
    sol = run(jobs, [van(seats=3)])
    scheduled = [s.job_key for r in sol.routes for s in r.stops if s.kind == "pickup"]
    assert sorted(scheduled) + sorted(sol.dropped) == sorted(j.key for j in jobs)