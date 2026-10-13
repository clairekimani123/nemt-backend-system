"""A small Dial-A-Ride solver: assign trips to vehicles and sequence their stops.

Deterministic greedy insertion with a local-improvement pass. Good enough for
realistic NEMT data (10-40 vehicles, 50-150 trips per day) with no external
dependencies and predictable behaviour in tests.

Contract:
  * Time is whole minutes since `day_start` (see the service layer: it derives
    `day_start` from the company timezone and shifts every UTC datetime by it).
  * Distance is precomputed by the caller as `travel[i][j]` (minutes between
    point i and point j). The solver never touches geography.
  * `pickup_close` is SOFT: arriving after it costs `job.pickup_late_penalty`
    minutes per minute late, added to the drive-time objective. Set the penalty
    very high to make it effectively hard.
  * `dropoff_close` (the appointment deadline) is HARD. Arriving after it makes
    the insertion infeasible.

Public surface: `Load`, `Job`, `VehicleSpec`, `solve()`. Everything else is internal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ------------------------------------------------------------------ data


@dataclass(frozen=True, slots=True)
class Load:
    seats: int = 0
    wheelchair: int = 0
    stretcher: int = 0

    def __add__(self, other: "Load") -> "Load":
        return Load(self.seats + other.seats, self.wheelchair + other.wheelchair,
                    self.stretcher + other.stretcher)

    def __sub__(self, other: "Load") -> "Load":
        return Load(self.seats - other.seats, self.wheelchair - other.wheelchair,
                    self.stretcher - other.stretcher)

    def __le__(self, other: "Load") -> bool:
        return (self.seats <= other.seats and self.wheelchair <= other.wheelchair
                and self.stretcher <= other.stretcher)

    def is_empty(self) -> bool:
        return self.seats == 0 and self.wheelchair == 0 and self.stretcher == 0


@dataclass(frozen=True, slots=True)
class Job:
    key: str
    pickup: int                         # point index
    dropoff: int                        # point index
    load: Load
    pickup_open: int                    # earliest pickup (minutes since day_start)
    pickup_close: int                   # target pickup; arriving later costs a penalty
    dropoff_close: int | None           # HARD appointment deadline, or None
    pickup_service: int                 # dwell minutes at pickup
    dropoff_service: int                # dwell minutes at drop-off
    max_ride: int                       # max minutes the passenger may sit in the vehicle
    allowed: frozenset[int]             # vehicle indices that may serve this job
    fixed_vehicle: int | None = None    # must be served by this vehicle only
    pickup_late_penalty: int = 0        # minutes of "cost" per minute arriving after pickup_close


@dataclass(frozen=True, slots=True)
class VehicleSpec:
    key: str
    capacity: Load
    shift_start: int = 0
    shift_end: int = 24 * 60


# ------------------------------------------------------------------ results


@dataclass(slots=True)
class Stop:
    job_key: str
    kind: str                           # "pickup" | "dropoff"
    point: int
    arrival: int
    departure: int
    wait: int
    load_after: Load


@dataclass(slots=True)
class VehicleRoute:
    vehicle: int                        # index into the vehicles list
    stops: list[Stop] = field(default_factory=list)
    drive_minutes: int = 0


@dataclass(slots=True)
class SolveResult:
    routes: list[VehicleRoute] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    early_dropped: dict[str, str] = field(default_factory=dict)
    total_drive_minutes: int = 0
    total_penalty: int = 0


# ------------------------------------------------------------------ internal working state


@dataclass(slots=True)
class _Seq:
    """One vehicle's ordered list of (job_key, kind) pairs, plus a cached feasibility eval."""

    vehicle: int
    spec: VehicleSpec
    entries: list[tuple[str, str]] = field(default_factory=list)


# ------------------------------------------------------------------ public API


def solve(
    travel: list[list[int]],
    jobs: list[Job],
    vehicles: list[VehicleSpec],
) -> SolveResult:
    """Assign every job to a vehicle and sequence the stops on each vehicle.

    Jobs that no vehicle can serve appear in `early_dropped` (with a specific reason)
    or in `dropped` (when the greedy pass could not place them anywhere feasible).
    """
    result = SolveResult()
    if not jobs:
        return result

    jobs_by_key = {j.key: j for j in jobs}

    # ---- 0. reject jobs nobody can serve ------------------------------------------------
    serviceable: list[Job] = []
    for job in jobs:
        reason = _impossible_reason(job, travel)
        if reason is not None:
            result.early_dropped[job.key] = reason
            continue
        serviceable.append(job)

    if not serviceable:
        return result

    # ---- 1. place fixed jobs first (their vehicle is non-negotiable) -------------------
    routes: dict[int, _Seq] = {}
    fixed_jobs: set[str] = set()

    for job in serviceable:
        if job.fixed_vehicle is None:
            continue
        if job.fixed_vehicle not in job.allowed:
            result.early_dropped[job.key] = "no_compatible_resource"
            continue
        seq = routes.setdefault(job.fixed_vehicle, _Seq(job.fixed_vehicle, vehicles[job.fixed_vehicle]))
        # Try to append pickup+dropoff at the end; try the front too. Take the cheapest feasible.
        pos = _best_append_position(seq, job, jobs_by_key, travel)
        if pos is None:
            result.early_dropped[job.key] = "appointment_unreachable"
            continue
        _splice(seq, job, pos[0], pos[1])
        fixed_jobs.add(job.key)

    # ---- 2. greedy insertion of the rest, hardest first -------------------------------
    floating = [j for j in serviceable if j.key not in fixed_jobs]
    floating.sort(key=lambda j: (j.pickup_close - j.pickup_open, -j.load.seats, j.key))

    for job in floating:
        best: tuple[int, int, int, int] | None = None   # (cost, vehicle_idx, p_pos, d_pos)
        for vi in sorted(job.allowed):
            seq = routes.setdefault(vi, _Seq(vi, vehicles[vi]))
            for p_pos, d_pos, cost in _candidate_insertions(seq, job, jobs_by_key, travel):
                key = (cost, vi, p_pos, d_pos)
                if best is None or key < best:
                    best = key
        if best is None:
            result.dropped.append(job.key)
            continue
        _, vi, p_pos, d_pos = best
        _splice(routes[vi], job, p_pos, d_pos)

    # ---- 3. local improvement: try relocating jobs to shorten the total ---------------
    _try_relocate(routes, jobs_by_key, travel)

    # ---- 4. materialise the result ----------------------------------------------------
    for vi in sorted(routes):
        seq = routes[vi]
        if not seq.entries:
            continue
        route, penalty = _materialise(seq, jobs_by_key, travel)
        result.routes.append(route)
        result.total_drive_minutes += route.drive_minutes
        result.total_penalty += penalty

    return result


# ------------------------------------------------------------------ feasibility & evaluation


def _simulate(entries: list[tuple[str, str]], jobs_by_key: dict[str, Job],
              spec: VehicleSpec, travel: list[list[int]]) -> tuple[list[Stop], int, int] | None:
    """Run the sequence forward. Returns (stops, drive_minutes, late_penalty) or None if infeasible.

    Feasibility checks:
      * every pickup precedes its own drop-off
      * no stop happens before shift_start
      * no drop-off happens after shift_end
      * for wheelchair/stretcher jobs, the vehicle has the capacity (checked via load_after)
      * load never exceeds the vehicle's capacity at any stop
      * the passenger does not ride longer than max_ride
      * the drop-off never happens after dropoff_close (appointment deadline)

    Soft constraints (penalised, not rejected):
      * arriving at a pickup after pickup_close costs `job.pickup_late_penalty` per late minute
    """
    if not entries:
        return [], 0, 0

    seen_pickups: set[str] = set()
    load = Load()
    drive = 0
    penalty = 0
    stops: list[Stop] = []

    # The vehicle "starts" at the first stop's point. There is no depot leg in this version;
    # the service layer can add one by pre-pending a synthetic pickup at a depot point.
    current_point: int | None = None
    clock = spec.shift_start

    for job_key, kind in entries:
        job = jobs_by_key[job_key]
        target = job.pickup if kind == "pickup" else job.dropoff

        if kind == "pickup":
            if job_key in seen_pickups:
                return None
            seen_pickups.add(job_key)
        else:
            if job_key not in seen_pickups:
                return None
            seen_pickups.discard(job_key)

        if current_point is not None:
            hop = travel[current_point][target]
            drive += hop
            clock += hop
        current_point = target

        if kind == "pickup":
            arrival = max(clock, job.pickup_open)
            wait = arrival - clock
            if arrival > job.pickup_close and job.pickup_late_penalty:
                penalty += (arrival - job.pickup_close) * job.pickup_late_penalty
            depart = arrival + job.pickup_service
            load = load + job.load
        else:
            arrival = clock
            wait = 0
            if arrival > clock:
                pass  # never happens: no wait at drop-off
            if job.dropoff_close is not None and arrival > job.dropoff_close:
                return None
            # ride time: pickup departure -> dropoff arrival
            pickup_entry_time = _pickup_departure(stops, job_key)
            if pickup_entry_time is None:
                return None
            if arrival - pickup_entry_time > job.max_ride:
                return None
            depart = arrival + job.dropoff_service
            load = load - job.load

        if arrival < spec.shift_start:
            return None
        if depart > spec.shift_end:
            return None
        if not (load <= spec.capacity):
            return None

        stops.append(Stop(job_key=job_key, kind=kind, point=target,
                          arrival=arrival, departure=depart, wait=wait, load_after=load))
        clock = depart

    return stops, drive, penalty


def _pickup_departure(stops: list[Stop], job_key: str) -> int | None:
    for s in stops:
        if s.job_key == job_key and s.kind == "pickup":
            return s.departure
    return None


def _objective(stops_drive: int, penalty: int) -> int:
    return stops_drive + penalty


# ------------------------------------------------------------------ insertion helpers


def _impossible_reason(job: Job, travel: list[list[int]]) -> str | None:
    """Reasons a job cannot be served by any vehicle, independent of current routes."""
    if not job.allowed:
        return "no_compatible_resource"
    hop = travel[job.pickup][job.dropoff]
    if hop >= _UNREACHABLE:
        return "unreachable_by_road"
    if job.dropoff_close is not None:
        earliest_dropoff = job.pickup_open + job.pickup_service + hop + job.dropoff_service
        if earliest_dropoff > job.dropoff_close:
            return "appointment_unreachable"
    if hop + job.pickup_service + job.dropoff_service > job.max_ride + 10**9:
        # the direct ride alone already exceeds max_ride; a detour cannot fix that
        if hop > job.max_ride:
            return "appointment_unreachable"
    return None


_UNREACHABLE = 10**6


def _candidate_insertions(seq: _Seq, job: Job, jobs_by_key: dict[str, Job],
                          travel: list[list[int]]):
    """Yield (p_pos, d_pos, cost) for every feasible insertion of `job` into `seq.entries`."""
    n = len(seq.entries)
    for p_pos in range(n + 1):
        for d_pos in range(p_pos, n + 1):
            candidate = list(seq.entries)
            candidate.insert(p_pos, (job.key, "pickup"))
            candidate.insert(d_pos + 1, (job.key, "dropoff"))
            sim = _simulate(candidate, jobs_by_key, seq.spec, travel)
            if sim is None:
                continue
            stops, drive, penalty = sim
            yield p_pos, d_pos, _objective(drive, penalty)


def _best_append_position(seq: _Seq, job: Job, jobs_by_key: dict[str, Job],
                          travel: list[list[int]]) -> tuple[int, int] | None:
    """Find the cheapest insertion for a fixed job (its vehicle is chosen, its position is not)."""
    best: tuple[int, int, int] | None = None   # (cost, p_pos, d_pos)
    for p_pos, d_pos, cost in _candidate_insertions(seq, job, jobs_by_key, travel):
        key = (cost, p_pos, d_pos)
        if best is None or key < best:
            best = key
    return (best[1], best[2]) if best else None


def _splice(seq: _Seq, job: Job, p_pos: int, d_pos: int) -> None:
    seq.entries.insert(p_pos, (job.key, "pickup"))
    seq.entries.insert(d_pos + 1, (job.key, "dropoff"))


# ------------------------------------------------------------------ local improvement


def _try_relocate(routes: dict[int, _Seq], jobs_by_key: dict[str, Job],
                  travel: list[list[int]]) -> None:
    """One pass of 'pull a job out and try to reinsert it somewhere cheaper'.

    Runs a single round; repeat calls can be added later if the objective improves enough.
    Never reduces feasibility: an improvement is only accepted when the new solution is valid.
    """
    moved = True
    while moved:
        moved = False
        for vi in sorted(routes):
            seq = routes[vi]
            for key in sorted({e[0] for e in seq.entries}):
                if jobs_by_key[key].fixed_vehicle is not None:
                    continue
                stripped = [e for e in seq.entries if e[0] != key]
                sim_now = _simulate(seq.entries, jobs_by_key, seq.spec, travel)
                sim_without = _simulate(stripped, jobs_by_key, seq.spec, travel)
                if sim_now is None or sim_without is None:
                    continue
                cost_now = _objective(sim_now[1], sim_now[2])
                cost_without = _objective(sim_without[1], sim_without[2])
                job = jobs_by_key[key]
                # Try inserting it back anywhere (including another vehicle)
                best: tuple[int, int, int, int] | None = None
                for wj in sorted(job.allowed):
                    other = routes.get(wj)
                    if other is None:
                        continue
                    base = other.entries if wj != vi else stripped
                    for p_pos, d_pos, cost in _candidate_insertions(
                            _Seq(other.vehicle, other.spec, base), job, jobs_by_key, travel):
                        k = (cost, wj, p_pos, d_pos)
                        if best is None or k < best:
                            best = k
                if best is None:
                    continue
                _, wj, p_pos, d_pos = best
                new_cost = best[0]
                old_total = cost_now
                new_total = cost_without + new_cost if wj == vi else cost_now + new_cost
                if wj != vi and new_total < old_total:
                    # remove from vi, add to wj
                    seq.entries = stripped
                    _splice(routes[wj], job, p_pos, d_pos)
                    moved = True
                elif wj == vi and new_cost < cost_now:
                    seq.entries = stripped
                    _splice(seq, job, p_pos, d_pos)
                    moved = True


# ------------------------------------------------------------------ materialisation


def _materialise(seq: _Seq, jobs_by_key: dict[str, Job],
                 travel: list[list[int]]) -> tuple[VehicleRoute, int]:
    sim = _simulate(seq.entries, jobs_by_key, seq.spec, travel)
    assert sim is not None
    stops, drive, penalty = sim
    return VehicleRoute(vehicle=seq.vehicle, stops=stops, drive_minutes=drive), penalty