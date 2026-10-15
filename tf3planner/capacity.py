"""Capacity layer: loading time, terminal (bay/platform) occupancy, road link and single-track
rail capacity, maintenance coverage, and a post-processor for economics.evaluate_link results.

economics.evaluate_link sizes the fleet to the volume but treats roads, truck bays and single
track as unlimited, so it can plan hundreds of trucks on one road. `apply_capacity` takes its
LinkEval and returns the adjustments the planner needs: extra infrastructure capex (terminals,
passing loops / second track, maintenance buildings), extra vehicles, an effective speed factor
and a feasibility flag with a reason. All functions are pure (no module state is mutated).

Time base: game seconds; one game year = game.year_ms / 1000 = 1461 s (docs/mechanics.md).
Capacities are therefore "per game year" (a 1900 train makes < 1 round trip per year on 12 km).

Models and assumptions (docs/mechanics.md §4-7, docs/real_logistics.md §3, 4.3, 10.1):

1. Loading (mechanics §4.2, open question Q5 - NOT measured in game yet):
       rate [units/s per loading vehicle] = vehicle.loadSpeed * cargo.loadSpeedFactor
                                            * terminal_mod * stock_mod
       terminal_mod = 2 (specialized platform, matching class) | 1 (universal) | 0.25 (wrong class
                      on a specialized platform);  stock_mod = 2 for a specialized warehouse
       Rail wagons load in parallel (one wagon per platform position); the part of a train longer
       than the platform loads at 0.25x ("overlength"), which then dictates the dwell.
       t_load = (capacity / loading_units) / rate * LOAD_CALIBRATION
   cargo.loadSpeedFactor = 0.0625 for every cargo (a 6-unit horse cart: 96 s). economics.py,
   tours.py and the dossier use the same function (economics_load_time reproduces the
   economics trip, so the running part of ev.trip_time_s is recovered exactly).
   LOAD_CALIBRATION (or the `load_calibration` argument) rescales once Q5 is measured.
2. Terminals = parallel servers (real_logistics §10.1): occupancy at one line end
       rho = n_vehicles * dwell_end / trip_time, terminals = ceil(rho / max_occ), max_occ 0.7.
   Waiting for a full load is NOT counted: with FULL_LOAD the vehicle at the bay leaves when the
   stock delivered enough, so waiting consumes slack of one bay, not throughput (§10.1 reading).
3. Road (real_logistics §4.3): one lane per direction, car-following capacity
       q_cap = junction_factor * v / (L_veh + S0 + v * T)       [vehicles/s per direction]
   flow per direction q = n / trip_time; v/c = q / q_cap; BPR speed factor
       sf = 1 / (1 + 0.15 * sensitivity * (v/c)^4)
   with `sensitivity` = baseConfig trafficSpeedSensitivityScale (in the game it scales the town
   traffic rating; using it as the congestion-delay weight is an ASSUMPTION). Feasible if
   v/c <= 0.85, truck bays <= MAX_ROAD_TERMINALS, station entrances <= MAX_ROAD_ENTRANCES and
   n <= MAX_ROAD_VEHICLES (practical cap: queues at stations / depots spill onto the road).
4. Rail single track with passing loops (UIC 405/406, mechanics §6): critical section s = L/(k+1),
       t_s = s / v_eff, c = train_len / v_eff + SWITCH_S + v_eff / (2 * ACCEL)
       trains_per_year_each_dir = u_max * Y / (2 * (t_s + c))
   Platoons (TF3 path signals; "several signals on a loop let several trains wait"): m trains
   of one direction follow on block signals, cycle = 2 * (t_s + c + (m - 1) * h), capacity
   m * u_max * Y / cycle, loops m trains long. Tried for m = 1..3, cheapest layout wins.
   Every section holds one platoon at a time (path-signal reservation), so additionally
   k + 1 >= trains simultaneously running / m. Loops need s >= MIN_LOOP_SPACING_M; when no k works
   the single track saturates and a second track is needed:
       double track, block B: h = (B + train_len) / v_eff + T_CLEAR, cap = u_max * Y / h.
   Loop cost = (m * train_len + LOOP_MARGIN_M + 2 * SWITCH_ZONE_M) * track cost_per_m (+ signals).
   Crossing waits slow trains: sf = 1 / (1 + 0.5 * u^2) single, 1 / (1 + 0.15 * u^4) double.
5. Maintenance (mechanics §5): pool = vehicles a building maintains; ASSUMED to count assigned
   vehicles (conservative). One depot is needed to buy vehicles; the rest of the pool comes from
   the cheaper option (maintenance buildings usually). coverage="full" also covers the whole route
   with sites every 2 * radius (vehicles never leave a maintenance area).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

from . import facilities
from .economics import (DETOUR_DEFAULT, STOP_DELAY_S, VEHICLE_LENGTH_M, Endpoint, LinkEval,
                        evaluate_link)
from .model import Game, Way
from .vehicles import Vehicle, available

# --------------------------------------------------------------------------- constants

YEAR_S_DEFAULT = 1461.0
CARGO_LOAD_FACTOR_DEFAULT = 0.0625     # every cargo in the base game (mechanics §4.2)
SPECIAL_PLATFORM_MOD = 2.0
SPECIAL_STOCK_MOD = 2.0
INCOMPATIBILITY_MOD = 0.25             # wrong class on a specialized platform
OVERLENGTH_MOD = 0.25                  # wagons beyond the platform end
LOAD_CALIBRATION = 1.0                 # multiply modelled load times (set after Q5 measurement)
MAX_PLATFORM_M = 840.0                 # 21 x 40 m segments

# terminal approach + clearing time per visit, s (ASSUMPTION, not in scripts)
MANOEUVRE_S = {"road": 6.0, "tram": 6.0, "rail": 20.0, "water": 40.0}
MAX_OCC = 0.7                          # planned terminal / line occupancy (UIC 406, §10.1)

# road
ROAD_VEHICLE_LENGTH_M = 10.0           # ASSUMPTION (horse cart ~6 m, 1970s+ trucks 10-16 m)
ROAD_S0_M = 3.0                        # standstill gap
ROAD_T_HEADWAY_S = 1.5                 # time gap of the car-following model
ROAD_JUNCTION_FACTOR = 0.6             # junctions, town streets, station turns on the route
ROAD_MAX_VC = 0.85
ROAD_BPR_ALPHA, ROAD_BPR_BETA = 0.15, 4.0
STATION_ENTRY_S = 4.0                  # one vehicle passing the entrance/exit gate, s
MAX_ROAD_TERMINALS = 8                 # truck bays per station (template 1/2/4, extendable)
MAX_ROAD_ENTRANCES = 4                 # entrance+exit pairs per station
MAX_ROAD_VEHICLES = 150                # practical cap per road line (ASSUMPTION)

# rail
SPEED_EFF = 0.85                       # same running-speed factor as economics.py
SWITCH_S = 15.0                        # route setting / signal reaction at a loop
ACCEL = 0.25                           # m/s^2, time lost decelerating+accelerating = v / (2 a)
MIN_LOOP_SPACING_M = 1000.0
LOOP_MARGIN_M = 40.0                   # loop longer than the train by one segment
SWITCH_ZONE_M = 40.0                   # turnout + signal zone at each loop end
SIGNAL_COST = 0.0                      # signals are not in the export; set when known
SIGNAL_SPACING_M = 300.0               # block length on double track (signals are cheap, place densely)
T_CLEAR_S = 10.0
RAIL_ALPHA_SINGLE, RAIL_BETA_SINGLE = 0.5, 2.0    # crossing waits on single track (heuristic)
RAIL_ALPHA_DOUBLE, RAIL_BETA_DOUBLE = 0.15, 4.0

# maintenance: mode -> (depot pool, depot radius, maintenance building pool, radius)
MAINT_POOL = {"road": (12, 400.0, 16, 600.0), "tram": (12, 400.0, 16, 600.0),
              "rail": (6, 800.0, 12, 800.0), "water": (8, 1000.0, 12, 1400.0)}
MAINT_FALLBACK = {"road": (60_000.0, 10_000.0), "tram": (60_000.0, 10_000.0),
                  "rail": (240_000.0, 40_000.0), "water": (240_000.0, 40_000.0)}
MAINT_FILE = {"road": "road_maint_station.con", "tram": "road_maint_station.con",
              "rail": "rail_maint_station.con", "water": "water_maint_station.con"}
MAX_HARBOR_TERMINALS = 4

CRF_RATE, CRF_YEARS = 0.10, 20         # capital charge for scoring (same as the 8.2 sweep)


def crf(rate: float = CRF_RATE, years: int = CRF_YEARS) -> float:
    """Capital recovery factor: annual payment per unit of capital."""
    return rate / (1 - (1 + rate) ** -years)


# --------------------------------------------------------------------------- 1. loading

def loading_time_s(consist_capacity: float, vehicle_load_speed: float,
                   cargo_load_speed_factor: float = CARGO_LOAD_FACTOR_DEFAULT,
                   platform_specialized: bool = False, warehouse_specialized: bool = False, *,
                   n_units: int = 1, class_mismatch: bool = False, overlength_fraction: float = 0.0,
                   calibration: float = LOAD_CALIBRATION) -> float:
    """Seconds to fill (or empty) a consist at one terminal.

    consist_capacity: units of the whole consist; n_units: vehicles loading in parallel (wagons);
    vehicle_load_speed: per-vehicle `loadSpeed` from the model; platform_specialized: the
    terminal is specialized for the cargo class (x2), or with class_mismatch=True for another
    class (x0.25); warehouse_specialized: loading from a specialized warehouse stock (x2);
    overlength_fraction > 0: part of the train is off the platform and loads at x0.25.
    """
    if consist_capacity <= 0:
        return 0.0
    term = 1.0
    if platform_specialized:
        term = INCOMPATIBILITY_MOD if class_mismatch else SPECIAL_PLATFORM_MOD
    stock = SPECIAL_STOCK_MOD if warehouse_specialized else 1.0
    rate = max(vehicle_load_speed, 1e-6) * max(cargo_load_speed_factor, 1e-6) * term * stock
    if overlength_fraction > 0:
        rate *= OVERLENGTH_MOD
    per_unit = consist_capacity / max(1, n_units)
    return per_unit / rate * calibration


def economics_load_time(game: Game, cargo_key: str, consist) -> float:
    """Load time exactly as economics.evaluate_link prices it (loading_time_s with the cargo's
    loadSpeedFactor, wagons in parallel, universal platform, default calibration)."""
    n_units = sum(k for v, k in consist.units if getattr(v, "capacity", None)) or 1
    return loading_time_s(consist.capacity, consist.load_speed, cargo_load_factor(game, cargo_key),
                          n_units=n_units, calibration=1.0)


def cargo_load_factor(game: Game, cargo_key: str) -> float:
    """cargo.loadSpeedFactor from the export (0.0625 for all base cargos, 1.0 passengers).
    Memoized per game (the export's cargo table does not change during a run)."""
    cache = getattr(game, "_cargo_load_factor_cache", None)
    if cache is None:
        cache = {}
        try:
            game._cargo_load_factor_cache = cache
        except Exception:                 # pragma: no cover
            return _cargo_load_factor(game, cargo_key)
    f = cache.get(cargo_key)
    if f is None:
        f = cache[cargo_key] = _cargo_load_factor(game, cargo_key)
    return f


def _cargo_load_factor(game: Game, cargo_key: str) -> float:
    for c in game.raw.get("cargoTypes", []):
        if str(c.get("name", "")).rsplit("/", 1)[-1].removesuffix(".cargo") == cargo_key:
            f = (c.get("data") or {}).get("loadSpeedFactor")
            if f:
                return float(f)
            return 1.0 if c.get("isPassenger") else CARGO_LOAD_FACTOR_DEFAULT
    return CARGO_LOAD_FACTOR_DEFAULT


# --------------------------------------------------------------------------- 2. terminals

def terminal_occupancy(n_vehicles: float, trip_time_s: float, dwell_s: float) -> float:
    """Fraction of time one terminal is busy at a line end: each of n vehicles visits the end
    once per round trip and holds a terminal for dwell_s (load + stop delays + manoeuvre)."""
    if trip_time_s <= 0:
        return math.inf
    return n_vehicles * dwell_s / trip_time_s


def terminals_needed(occupancy: float, max_occ: float = MAX_OCC) -> int:
    """Parallel terminals so that each runs at most max_occ busy."""
    if not math.isfinite(occupancy):
        return 10 ** 6
    return max(1, math.ceil(occupancy / max_occ - 1e-9))


# --------------------------------------------------------------------------- 3. road

def road_lane_capacity(speed_mps: float, vehicle_length_m: float = ROAD_VEHICLE_LENGTH_M,
                       year_s: float = YEAR_S_DEFAULT, junction_factor: float = ROAD_JUNCTION_FACTOR) -> float:
    """Vehicles per game year one lane (one direction) can pass."""
    v = max(speed_mps, 0.5)
    q = v / (vehicle_length_m + ROAD_S0_M + v * ROAD_T_HEADWAY_S)
    return q * junction_factor * year_s


def road_speed_factor(vc_ratio: float, sensitivity: float = 1.0) -> float:
    """BPR travel-time curve as a speed multiplier (1 = free flow)."""
    x = max(0.0, vc_ratio)
    return 1.0 / (1.0 + ROAD_BPR_ALPHA * sensitivity * x ** ROAD_BPR_BETA)


@dataclass
class RoadCheck:
    ok: bool
    reason: str
    flow_per_year: float          # vehicles per direction per game year
    capacity_per_year: float
    vc: float
    speed_factor: float
    terminals: int                # truck bays needed per station (0 if dwell unknown)
    entrances: int                # entrance/exit pairs needed per station
    jam_spacing_ok: bool


def road_link_ok(n_vehicles: int, trip_time_s: float, route_m: float, speed_mps: float = 8.0, *,
                 dwell_s: float | None = None, vehicle_length_m: float = ROAD_VEHICLE_LENGTH_M,
                 year_s: float = YEAR_S_DEFAULT, sensitivity: float = 1.0, max_vc: float = ROAD_MAX_VC,
                 max_occ: float = MAX_OCC, max_terminals: int = MAX_ROAD_TERMINALS,
                 max_entrances: int = MAX_ROAD_ENTRANCES, max_vehicles: int = MAX_ROAD_VEHICLES) -> RoadCheck:
    """Can a road line with n vehicles of round-trip time trip_time_s run on one 2-lane road
    of route_m and its two truck stations? dwell_s = bay occupancy per visit (load + delays)."""
    if trip_time_s <= 0:
        return RoadCheck(False, "нулевое время рейса", math.inf, 0, math.inf, 0, 0, 0, False)
    flow = n_vehicles * year_s / trip_time_s
    cap = road_lane_capacity(speed_mps, vehicle_length_m, year_s)
    vc = flow / cap if cap > 0 else math.inf
    sf = road_speed_factor(vc, sensitivity)
    spacing_ok = (2 * route_m / max(n_vehicles, 1)) >= vehicle_length_m + ROAD_S0_M
    terms = terminals_needed(terminal_occupancy(n_vehicles, trip_time_s, dwell_s), max_occ) if dwell_s else 0
    ents = terminals_needed(terminal_occupancy(n_vehicles, trip_time_s, STATION_ENTRY_S), max_occ)
    reasons = []
    if n_vehicles > max_vehicles:
        reasons.append(f"{n_vehicles} грузовиков на линии > {max_vehicles}")
    if vc > max_vc:
        reasons.append(f"загрузка дороги {vc:.0%} > {max_vc:.0%}")
    if not spacing_ok:
        reasons.append("плотность машин выше пробочной")
    if terms > max_terminals:
        reasons.append(f"нужно {terms} терминалов на станции > {max_terminals}")
    if ents > max_entrances:
        reasons.append(f"нужно {ents} въездов на станцию > {max_entrances}")
    return RoadCheck(not reasons, "; ".join(reasons), flow, cap, vc, sf, terms, ents, spacing_ok)


# --------------------------------------------------------------------------- 4. rail

def crossing_margin_s(v_mps: float, train_len_m: float) -> float:
    """Extra time per section passage: train clears the loop, route setting, stop/start loss."""
    ve = max(v_mps * SPEED_EFF, 0.5)
    return train_len_m / ve + SWITCH_S + ve / (2 * ACCEL)


def follow_headway_s(v_mps: float, train_len_m: float = 200.0, signal_spacing_m: float = SIGNAL_SPACING_M) -> float:
    """Minimum time between two trains in the same direction on block signals."""
    ve = max(v_mps * SPEED_EFF, 0.5)
    return (signal_spacing_m + train_len_m) / ve + T_CLEAR_S


def single_track_capacity(L_m: float, v_mps: float, n_loops: int = 0, train_len_m: float = 200.0,
                          u_max: float = MAX_OCC, year_s: float = YEAR_S_DEFAULT, platoon: int = 1) -> float:
    """Trains per game year in EACH direction on a single track of length L with n_loops evenly
    spaced passing loops. Directions alternate through the critical section; platoon > 1 lets
    that many same-direction trains follow each other on intermediate block signals (the loops
    must then hold `platoon` trains)."""
    ve = max(v_mps * SPEED_EFF, 0.5)
    s = L_m / (n_loops + 1)
    m = max(1, platoon)
    cycle = 2 * (s / ve + crossing_margin_s(v_mps, train_len_m) + (m - 1) * follow_headway_s(v_mps, train_len_m))
    return m * u_max * year_s / cycle


def max_loops(L_m: float, min_spacing_m: float = MIN_LOOP_SPACING_M) -> int:
    return max(0, math.floor(L_m / min_spacing_m) - 1)


def single_track_limit(L_m: float, v_mps: float, train_len_m: float = 200.0, u_max: float = MAX_OCC,
                       year_s: float = YEAR_S_DEFAULT, min_spacing_m: float = MIN_LOOP_SPACING_M,
                       platoon: int = 1) -> float:
    """Saturation capacity of a single track: loops at the minimum practical spacing."""
    return single_track_capacity(L_m, v_mps, max_loops(L_m, min_spacing_m), train_len_m, u_max, year_s, platoon)


def loops_needed(trains_per_year: float, L_m: float, v_mps: float, train_len_m: float = 200.0,
                 u_max: float = MAX_OCC, year_s: float = YEAR_S_DEFAULT,
                 min_spacing_m: float = MIN_LOOP_SPACING_M, trains_running: int = 1, platoon: int = 1) -> int | None:
    """Fewest passing loops so a single track carries trains_per_year each way; None when the
    single track saturates (needs a second track). trains_running = trains simultaneously on the
    line; each section holds at most one platoon."""
    m = max(1, platoon)
    k_max = max_loops(L_m, min_spacing_m)
    k_min = max(0, math.ceil(trains_running / m) - 1)
    if k_min > k_max:
        return None
    ve = max(v_mps * SPEED_EFF, 0.5)
    budget = (m * u_max * year_s / (2 * max(trains_per_year, 1e-9)) - crossing_margin_s(v_mps, train_len_m)
              - (m - 1) * follow_headway_s(v_mps, train_len_m))
    if budget <= 0:
        return None
    k = max(k_min, math.ceil(L_m / (budget * ve) - 1e-9) - 1)
    return k if k <= k_max else None


def double_track_capacity(v_mps: float, train_len_m: float = 200.0, signal_spacing_m: float = SIGNAL_SPACING_M,
                          u_max: float = MAX_OCC, year_s: float = YEAR_S_DEFAULT) -> float:
    """Trains per game year per direction on double track with block signals."""
    return u_max * year_s / follow_headway_s(v_mps, train_len_m, signal_spacing_m)


def loop_cost(cost_per_m: float, train_len_m: float = 200.0, maintenance_per_m: float = 0.0,
              platoon: int = 1) -> tuple[float, float]:
    """(capex, upkeep/yr) of one passing loop: a parallel track `platoon` trains long plus one
    segment of margin and two turnout zones ("2 extra segments + switches"), priced per metre of
    the line's track type, plus signals."""
    m = max(1, platoon)
    length = m * train_len_m + LOOP_MARGIN_M + 2 * SWITCH_ZONE_M
    return length * cost_per_m + (2 + 2 * m) * SIGNAL_COST, length * maintenance_per_m


def double_track_threshold(L_m: float, v_mps: float, train_len_m: float = 200.0, cost_per_m: float = 90.0,
                           u_max: float = MAX_OCC, year_s: float = YEAR_S_DEFAULT,
                           min_spacing_m: float = MIN_LOOP_SPACING_M, platoons=(1, 2, 3)) -> float:
    """Trains per year each way above which a second track is required or cheaper than loops
    (best platoon size; capacity at the most loops whose total cost stays below a second track)."""
    second = L_m * cost_per_m
    k_max = max_loops(L_m, min_spacing_m)
    best = 0.0
    for m in platoons:
        per_loop, _ = loop_cost(cost_per_m, train_len_m, platoon=m)
        k_cost = int(second // per_loop) if per_loop > 0 else k_max
        best = max(best, single_track_capacity(L_m, v_mps, min(k_max, k_cost), train_len_m, u_max, year_s, m))
    return best


@dataclass
class RailPlan:
    kind: str                      # "single" | "single+loops" | "double" | "infeasible"
    loops: int
    capex: float
    maintenance: float
    capacity: float                # trains/yr each way of the chosen layout
    utilization: float
    speed_factor: float
    reason: str = ""
    platoon: int = 1


def rail_line_plan(trains_per_year: float, L_m: float, v_mps: float, train_len_m: float, cost_per_m: float,
                   maintenance_per_m: float = 0.0, n_trains: int = 1, running_share: float = 1.0,
                   second_track_cost: float | None = None, u_max: float = MAX_OCC,
                   year_s: float = YEAR_S_DEFAULT, platoons=(1, 2, 3), horizon_years: float = 5.0) -> RailPlan:
    """Cheapest track layout for the line: plain single track, single + loops (platoons of 1..3
    trains), or double track; cost = capex + horizon_years * upkeep."""
    if n_trains <= 1:
        cap = single_track_capacity(L_m, v_mps, 0, train_len_m, 1.0, year_s)
        return RailPlan("single", 0, 0.0, 0.0, cap, trains_per_year / cap if cap else 0, 1.0)
    running = max(1, math.ceil(n_trains * min(1.0, running_share)))
    sec_cost = second_track_cost if second_track_cost is not None else L_m * cost_per_m
    sec_maint = L_m * maintenance_per_m
    options: list[RailPlan] = []
    for m in platoons:
        k = loops_needed(trains_per_year, L_m, v_mps, train_len_m, u_max, year_s,
                         trains_running=running, platoon=m)
        if k is None or (m > 1 and k == 0):
            continue
        lc, lm = loop_cost(cost_per_m, train_len_m, maintenance_per_m, m)
        cap = single_track_capacity(L_m, v_mps, k, train_len_m, u_max, year_s, m)
        u = trains_per_year / cap * u_max
        # crossing waits; forming a platoon adds waiting too (heuristic)
        sf = 1.0 / (1.0 + RAIL_ALPHA_SINGLE * u ** RAIL_BETA_SINGLE * (1 + 0.25 * (m - 1)))
        options.append(RailPlan("single+loops" if k else "single", k, k * lc, k * lm, cap, u, sf, "", m))
    dbl_cap = double_track_capacity(v_mps, train_len_m, u_max=u_max, year_s=year_s)
    if trains_per_year <= dbl_cap:
        u = trains_per_year / dbl_cap * u_max
        sf = 1.0 / (1.0 + RAIL_ALPHA_DOUBLE * u ** RAIL_BETA_DOUBLE)
        options.append(RailPlan("double", 0, sec_cost, sec_maint, dbl_cap, u, sf))
    if options:
        return min(options, key=lambda o: o.capex + horizon_years * o.maintenance)
    return RailPlan("infeasible", 0, sec_cost, sec_maint, dbl_cap, trains_per_year / dbl_cap * u_max,
                    0.5, f"{trains_per_year:.0f} поездов/год > {dbl_cap:.0f} даже на двух путях")


# --------------------------------------------------------------------------- 5. maintenance

@dataclass
class MaintPlan:
    mode: str
    depots: int
    maint_buildings: int
    sites: int                     # places along the route that need coverage
    capex: float
    upkeep: float
    label: str
    pool: int = 0


def maint_building_price(cat: facilities.Catalogue, mode: str, year: int) -> tuple[float, float]:
    """Same price as facilities.depot / the dossier (script price x1.5). The export's menu
    attribute of road_maint_station.con (180 000) is the era-C template value, not the price."""
    return facilities.maint_building(cat, mode, year)


def maintenance_plan(cat: facilities.Catalogue, mode: str, year: int, n_vehicles: int, route_m: float,
                     coverage: str = "ends", horizon_years: float = 5.0) -> MaintPlan:
    """Depots + maintenance buildings for one line: one depot to buy vehicles, the remaining pool
    from the cheapest building type (cost + horizon_years of upkeep). coverage="full" also places
    a site every 2 * radius along the route."""
    dpool, drad, mpool, mrad = MAINT_POOL.get(mode, MAINT_POOL["road"])
    dc, du = facilities.depot_price(cat, mode, year)
    mc, mu = maint_building_price(cat, mode, year)
    sites = 1 if coverage == "ends" else max(1, math.ceil(route_m / (2 * max(mrad, drad))))
    rest = max(0, n_vehicles - dpool)
    options = []
    # (a) extra pool from maintenance buildings
    m = max(math.ceil(rest / mpool), sites - 1)
    options.append((1, m))
    # (b) extra pool from more depots
    d = max(1 + math.ceil(rest / dpool), sites)
    options.append((d, 0))
    best = min(options, key=lambda o: o[0] * (dc + horizon_years * du) + o[1] * (mc + horizon_years * mu))
    nd, nm = best
    name = {"road": "автодепо", "tram": "трамв. депо", "rail": "ж/д депо", "water": "верфь"}.get(mode, "депо")
    label = f"{nd}× {name}" + (f" + {nm}× станция ТО" if nm else "")
    return MaintPlan(mode, nd, nm, sites, nd * dc + nm * mc, nd * du + nm * mu, label, nd * dpool + nm * mpool)


# --------------------------------------------------------------------------- 6. apply to a LinkEval

def _way_for(game: Game, mode: str, year: int, route) -> Way | None:
    key = getattr(route, "way_key", None)
    if key:
        for w in game.ways:
            if w.key == key:
                return w
    if mode not in ("rail", "road"):
        return None
    from .routing import default_way      # local import: routing pulls scipy
    return default_way(game, mode, year)


def _route_len(ev: LinkEval, route) -> float:
    if route is None:
        return ev.route_m
    if isinstance(route, (int, float)):
        return float(route)
    return float(getattr(route, "length_m", ev.route_m))


def _special_premium(cat: facilities.Catalogue, mode: str, year: int, cls: str, segs: int,
                     terminals: int) -> tuple[float, float]:
    """Extra cost of specialized cargo platforms over universal ones for one station."""
    if mode == "rail":
        era = "era_a" if year < 1950 else "era_b" if year < 1990 else "era_c"
        u, um = cat.price(year, "rail_platform", "modular_station/platform_cargo_era", ".module")
        s, sm = cat.price(year, "rail_platform_special", f"platform_cargo_{era}_{cls}.module")
        return terminals * segs * (s - u), terminals * segs * (sm - um)
    if mode in ("road", "tram"):
        u, um = cat.price(year, "road_platform", "modular_street_station/cargo_platform.module")
        s, sm = cat.price(year, "road_platform_special", f"modular_street_station/cargo_platform_{cls}.module")
        k = terminals * facilities.ROAD_PLATFORMS_PER_TERMINAL
        return k * (s - u), k * (sm - um)
    return 0.0, 0.0


def _harbor_pier_cost(cat: facilities.Catalogue, year: int) -> tuple[float, float]:
    """Marginal cost of one extra pier: (max - min) / 3 of the small harbor metacon (1 -> 4)."""
    for e in cat.metas:
        if "harbor_modular_era" in e["name"] and "small" in e["name"] and facilities._available(e, year):
            at = (e.get("description") or {}).get("attributes") or {}
            c, m = at.get("cost") or [0, 0], at.get("maintenanceCost") or [0, 0]
            if len(c) > 1 and c[1] > c[0]:
                return (c[1] - c[0]) / 3, (m[1] - m[0]) / 3 if len(m) > 1 else 0.0
    return 100_000.0, 16_000.0


def _evaluate_variant(ev: LinkEval, game: Game, cat: facilities.Catalogue, route, specialized: bool,
                      u_max: float, max_occ: float, load_calibration: float, coverage: str,
                      sensitivity: float, max_road_vehicles: int, max_road_terminals: int) -> dict:
    c = ev.consist
    mode, year = ev.mode, ev.year
    year_s = game.year_ms / 1000.0
    cargo = game.cargo_by_key[ev.cargo]
    f = cargo_load_factor(game, ev.cargo)
    units = sum(k for _, k in c.units)
    loading_units = c.units[-1][1] if (mode == "rail" and len(c.units) > 1) else 1
    train_len = units * VEHICLE_LENGTH_M if mode == "rail" else ROAD_VEHICLE_LENGTH_M
    overlength = max(0.0, train_len - MAX_PLATFORM_M) / train_len if mode == "rail" else 0.0
    cls = facilities._special(cargo.classes)
    notes: list[str] = []

    load_t = loading_time_s(c.capacity, c.load_speed, f, specialized, False, n_units=loading_units,
                            overlength_fraction=overlength, calibration=load_calibration)
    # economics.py trip: 2 * running + 2 * load + 2 * STOP_DELAY_S with its own load model
    load_e = economics_load_time(game, ev.cargo, c)
    running = max(0.0, ev.trip_time_s - 2 * load_e - 2 * STOP_DELAY_S)
    route_m = _route_len(ev, route)
    if route_m and abs(route_m - ev.route_m) > 1 and ev.route_m > 0:
        running *= route_m / ev.route_m
    way = _way_for(game, mode, year, route)
    if way and way.speed > 0 and c.speed > way.speed:
        running *= c.speed / way.speed
        notes.append(f"скорость ограничена путём {way.key}: {way.speed * 3.6:.0f} км/ч")
    man = MANOEUVRE_S.get(mode, 10.0)
    dwell = load_t + STOP_DELAY_S + man

    sf, n, trip = 1.0, ev.n_consists, ev.trip_time_s
    road: RoadCheck | None = None
    rail: RailPlan | None = None
    cpm = way.cost_per_m if way else 0.0
    mpm = way.maintenance_per_m if way else 0.0
    second = None
    if mode == "rail":
        rb = getattr(route, "build_cost", None)
        second = float(rb) if rb else (ev.route_build if ev.route_build > 0 else route_m * cpm)
    for _ in range(30):
        trip = running / sf + 2 * load_t + 2 * STOP_DELAY_S + 2 * man
        per = c.capacity * year_s / trip
        n = max(1, math.ceil(ev.volume / per - 1e-9))
        if mode in ("road", "tram"):
            road = road_link_ok(n, trip, route_m, c.speed, dwell_s=dwell, year_s=year_s, sensitivity=sensitivity,
                                max_occ=max_occ, max_terminals=max_road_terminals, max_vehicles=max_road_vehicles)
            new_sf = road.speed_factor
        elif mode == "rail":
            tpy = n * year_s / trip
            rail = rail_line_plan(tpy, route_m, min(c.speed, way.speed) if way and way.speed else c.speed,
                                  train_len, cpm, mpm, n, running / sf / trip if trip else 1.0, second,
                                  u_max, year_s)
            new_sf = rail.speed_factor
        else:
            new_sf = 1.0
        if abs(new_sf - sf) < 1e-4:
            sf = new_sf
            break
        sf = 0.5 * (sf + new_sf)              # damped fixed point
    trip = running / sf + 2 * load_t + 2 * STOP_DELAY_S + 2 * man

    feasible, reasons = True, []
    occ = terminal_occupancy(n, trip, dwell)
    terms = terminals_needed(occ, max_occ)
    occ_e = ev.n_consists * (load_e + STOP_DELAY_S) / ev.trip_time_s if ev.trip_time_s else 1.0
    terms_e = max(1, math.ceil(occ_e / 0.7))

    # stations at both ends
    extra_st, extra_st_m = 0.0, 0.0
    station_name = ev.station_name
    if mode in ("rail", "road", "tram"):
        st = facilities.station(cat, mode, year, cargo.classes, units * VEHICLE_LENGTH_M, terms)
        station_name = st.name
        st_e = facilities.station(cat, mode, year, cargo.classes, units * VEHICLE_LENGTH_M, terms_e)
        extra_st = 2 * (st.cost - st_e.cost)
        extra_st_m = 2 * (st.maintenance - st_e.maintenance)
        if specialized and cls:
            segs = math.ceil(min(units * VEHICLE_LENGTH_M, MAX_PLATFORM_M) / facilities.SEGMENT_M) if mode == "rail" else 1
            pc, pm = _special_premium(cat, mode, year, cls, segs, terms)
            extra_st += 2 * pc
            extra_st_m += 2 * pm
    elif mode == "water":
        if terms > MAX_HARBOR_TERMINALS:
            feasible = False
            reasons.append(f"нужно {terms} причалов > {MAX_HARBOR_TERMINALS}")
        pc, pm = _harbor_pier_cost(cat, year)
        extra_st = 2 * max(0, terms - 1) * pc
        extra_st_m = 2 * max(0, terms - 1) * pm
    if mode == "rail" and overlength > 0:
        notes.append(f"поезд {train_len:.0f} м длиннее платформы {MAX_PLATFORM_M:.0f} м")

    # line infrastructure
    extra_track, extra_track_m = 0.0, 0.0
    if road is not None and not road.ok:
        feasible = False
        reasons.append(road.reason)
    if road is not None and road.entrances > 1:
        e, em = facilities.road_entrance(cat, year)          # one entrance_exit module per gate
        extra_st += 2 * (road.entrances - 1) * e
        extra_st_m += 2 * (road.entrances - 1) * em
    if rail is not None:
        extra_track, extra_track_m = rail.capex, rail.maintenance
        if rail.kind == "infeasible":
            feasible = False
            reasons.append(rail.reason)
        elif rail.kind == "single+loops":
            notes.append(f"однопутка + {rail.loops} разъезд(ов)"
                         + (f", пакеты по {rail.platoon} поезда" if rail.platoon > 1 else ""))
        elif rail.kind == "double":
            notes.append("нужен второй путь")

    # maintenance vs the depots economics.py already priced
    mp = maintenance_plan(cat, mode, year, n, route_m, coverage)
    dp_e = facilities.depot(cat, mode, year, ev.n_consists)
    extra_dep = mp.capex - ev.depot_cost
    extra_dep_m = mp.upkeep - dp_e.maintenance

    dn = n - ev.n_consists
    extra_veh = dn * c.price
    extra_run = dn * c.running_cost
    extra_infra = extra_st + extra_track + extra_dep
    extra_maint = extra_st_m + extra_track_m + extra_dep_m
    capex = ev.capex + extra_veh + extra_infra
    profit = ev.profit - extra_run - extra_maint

    # largest volume this consist type could move within the hard limits (road / water)
    per = c.capacity * year_s / trip
    max_vol = math.inf
    if mode in ("road", "tram"):
        trips_bays = max_road_terminals * max_occ * year_s / dwell
        trips_gate = MAX_ROAD_ENTRANCES * max_occ * year_s / STATION_ENTRY_S
        trips_road = ROAD_MAX_VC * road_lane_capacity(c.speed, year_s=year_s)
        max_vol = min(trips_bays, trips_gate, trips_road) * c.capacity
        max_vol = min(max_vol, max_road_vehicles * per)
    elif mode == "water":
        max_vol = MAX_HARBOR_TERMINALS * max_occ * year_s / dwell * c.capacity
    elif mode == "rail":
        v_line = min(c.speed, way.speed) if way and way.speed else c.speed
        max_vol = double_track_capacity(v_line, train_len, u_max=u_max, year_s=year_s) * c.capacity

    return {
        "feasible": feasible,
        "reason": "; ".join(reasons),
        "platform": "specialized" if specialized else "universal",
        "speed_factor": sf,
        "load_time_s": load_t,
        "dwell_s": dwell,
        "trip_time_s": trip,
        "n_vehicles": n,
        "extra_vehicles": dn,
        "extra_vehicle_capex": extra_veh,
        "extra_running": extra_run,
        "terminals": terms,
        "terminal_occupancy": occ,
        "extra_station_capex": extra_st,
        "extra_station_maint": extra_st_m,
        "track": rail.__dict__ if rail else None,
        "road": road.__dict__ if road else None,
        "extra_track_capex": extra_track,
        "extra_track_maint": extra_track_m,
        "maintenance_plan": mp.__dict__,
        "station_name": station_name,
        "depot_name": facilities.depot_name(mode, mp.maint_buildings, mp.depots),
        "extra_depot_capex": extra_dep,
        "extra_depot_maint": extra_dep_m,
        "extra_infra_capex": extra_infra,
        "extra_maintenance": extra_maint,
        "capex": capex,
        "profit": profit,
        "score": profit - capex * crf(),
        "max_volume": max_vol,
        "notes": notes,
    }


def apply_capacity(ev: LinkEval, route=None, game: Game | None = None,
                   catalogue: facilities.Catalogue | None = None, *, platform: str = "auto",
                   u_max: float = MAX_OCC, max_occ: float = MAX_OCC, load_calibration: float = LOAD_CALIBRATION,
                   coverage: str = "ends", sensitivity: float | None = None,
                   max_road_vehicles: int = MAX_ROAD_VEHICLES,
                   max_road_terminals: int = MAX_ROAD_TERMINALS) -> dict:
    """Capacity adjustments for an evaluate_link result.

    route: routing.Route, a route length in m, or None (ev.route_m). Returns a dict with
    feasible/reason, speed_factor, n_vehicles, terminals, extra_* capex and upkeep (deltas versus
    what evaluate_link already priced; depot delta can be negative), capex/profit/score after
    the adjustments and notes. platform: "universal" | "specialized" | "auto" (cheaper by score).
    """
    if game is None:
        raise ValueError("game is required")
    if ev is None or ev.consist is None:
        return {"feasible": False, "reason": "нет состава", "speed_factor": 1.0, "extra_infra_capex": 0.0,
                "extra_maintenance": 0.0, "notes": []}
    cat = catalogue or getattr(game, "_catalogue", None) or facilities.Catalogue(game)
    if sensitivity is None:
        sensitivity = float(((game.raw.get("baseConfig") or {}).get("advancedOptions") or {})
                            .get("trafficSpeedSensitivityScale", 1.0) or 1.0)
    has_special = facilities._special(game.cargo_by_key[ev.cargo].classes) is not None \
        and ev.mode in ("rail", "road", "tram")
    if platform == "specialized" and has_special:
        variants = [True]
    elif platform == "auto" and has_special:
        variants = [False, True]
    else:
        variants = [False]
    res = [_evaluate_variant(ev, game, cat, route, s, u_max, max_occ, load_calibration, coverage,
                             sensitivity, max_road_vehicles, max_road_terminals) for s in variants]
    return max(res, key=lambda r: (r["feasible"], r["score"]))


def apply_to(ev: LinkEval, adj: dict) -> LinkEval:
    """Copy of ev with the capacity adjustments folded in (fleet, trip, capex, upkeep, notes)."""
    if not adj.get("n_vehicles"):
        return ev
    notes = list(ev.notes) + list(adj.get("notes", []))
    if not adj["feasible"]:
        notes.append("НЕ ПРОХОДИТ ПО ПРОПУСКНОЙ СПОСОБНОСТИ: " + adj["reason"])
    names = {k: adj[k] for k in ("station_name", "depot_name") if adj.get(k)}
    return replace(ev, n_consists=adj["n_vehicles"], trip_time_s=adj["trip_time_s"], **names,
                   vehicle_capex=ev.vehicle_capex + adj["extra_vehicle_capex"],
                   infra_capex=ev.infra_capex + adj["extra_infra_capex"],
                   running=ev.running + adj["extra_running"],
                   maintenance=ev.maintenance + adj["extra_maintenance"], notes=notes)


def best_with_capacity(game: Game, vehicles: list[Vehicle], cargo, src: Endpoint, dst: Endpoint, mode: str,
                       volume: float, year: int, route_m: float | None = None, route=None, **kw):
    """Like evaluate_link but compares every vehicle type AFTER capacity limits (economics picks
    the best type assuming unlimited roads, which favours tiny trucks). Returns (ev, adj) of the
    best feasible option, else the best infeasible one, or (None, None)."""
    avail = available(vehicles, year)
    if mode == "rail":
        locos = [v for v in avail if v.carrier == "rail" and v.role == "loco"]
        wagons = [v for v in avail if v.carrier == "rail" and v.role == "wagon" and v.carries(cargo.key)]
        groups = [[l, w] for l in locos for w in wagons]
    else:
        carrier = {"road": "road", "water": "water", "tram": "tram"}.get(mode, mode)
        groups = [[v] for v in avail if v.carrier == carrier and v.carries(cargo.key)]
    best = None
    for g in groups:
        ev = evaluate_link(game, g, cargo, src, dst, mode, volume, year, route_m=route_m)
        if ev is None:
            continue
        adj = apply_capacity(ev, route if route is not None else route_m, game, **kw)
        key = (adj["feasible"], adj["score"])
        if best is None or key > best[0]:
            best = (key, ev, adj)
    return (best[1], best[2]) if best else (None, None)


# --------------------------------------------------------------------------- 7. break-even table

VOLUMES = (50, 100, 300, 1000, 3000)
DISTANCES_KM = (3, 6, 12, 24, 48)
ERAS = (1900, 1925, 1950, 1975, 2000)


def _raw_score(ev: LinkEval | None) -> float:
    return ev.profit - ev.capex * crf() if ev else -math.inf


def breakeven_table(game: Game, vehicles: list[Vehicle], year: int, cargo_key: str = "coal",
                    volumes=VOLUMES, distances_km=DISTANCES_KM, detour: float = 1.25, **kw) -> list[dict]:
    """Road vs rail per (distance, volume) with capacity limits applied, industry -> industry
    (no delivery-time limit). Score = annual profit - capex * CRF(10 %, 20 y), in money/yr."""
    cargo = game.cargo_by_key[cargo_key]
    rows = []
    for d in distances_km:
        src = Endpoint("industry", -1, "src", 0.0, 0.0, 0.0)
        dst = Endpoint("industry", -2, "dst", d * 1000.0, 0.0, 0.0)
        route_m = d * 1000.0 * detour
        for vol in volumes:
            row = {"dist_km": d, "volume": vol}
            for mode in ("road", "rail"):
                raw = evaluate_link(game, vehicles, cargo, src, dst, mode, vol, year, route_m=route_m)
                ev, adj = best_with_capacity(game, vehicles, cargo, src, dst, mode, vol, year, route_m=route_m, **kw)
                row[mode] = {
                    "raw_score": _raw_score(raw),
                    "raw_n": raw.n_consists if raw else 0,
                    "raw_vehicle": raw.consist.label if raw else "",
                    "feasible": bool(adj and adj["feasible"]),
                    "score": adj["score"] if adj else -math.inf,
                    "n": adj["n_vehicles"] if adj else 0,
                    "vehicle": ev.consist.label if ev else "",
                    "reason": adj["reason"] if adj else "нет техники",
                    "terminals": adj["terminals"] if adj else 0,
                    "track": (adj["track"] or {}).get("kind") if adj else None,
                    "loops": (adj["track"] or {}).get("loops") if adj else None,
                    "platform": adj["platform"] if adj else "",
                }
            r, t = row["road"], row["rail"]
            if r["feasible"] and t["feasible"]:
                row["winner"] = "road" if r["score"] > t["score"] else "rail"
            elif r["feasible"] or t["feasible"]:
                row["winner"] = "road" if r["feasible"] else "rail"
            else:
                row["winner"] = "none"
            row["raw_winner"] = "road" if r["raw_score"] > t["raw_score"] else "rail"
            rows.append(row)
    return rows


def _fmt_m(x: float) -> str:
    if not math.isfinite(x):
        return "—"
    return f"{x / 1e6:+.2f}"


def render_breakeven_md(tables: dict[int, list[dict]],
                        sensitivity: dict[str, dict[int, list[dict]]] | None = None) -> str:
    """Markdown tables per era; `sensitivity` maps a caption to alternative tables (winner only)."""
    out = []
    for year, rows in tables.items():
        out.append(f"### {year}\n")
        dists = sorted({r["dist_km"] for r in rows})
        vols = sorted({r["volume"] for r in rows})
        out.append("Winner with capacity limits (raw model without limits in brackets):\n")
        out.append("| km \\ u/yr | " + " | ".join(str(v) for v in vols) + " |")
        out.append("|---|" + "---|" * len(vols))
        for d in dists:
            cells = []
            for v in vols:
                r = next(x for x in rows if x["dist_km"] == d and x["volume"] == v)
                w = {"road": "**road**", "rail": "**rail**", "none": "none"}[r["winner"]]
                cells.append(f"{w} ({r['raw_winner']})")
            out.append(f"| {d} | " + " | ".join(cells) + " |")
        out.append("")
        out.append("Score M$/yr (profit − capex·CRF) road / rail, `x` = infeasible; fleet road / rail:\n")
        out.append("| km \\ u/yr | " + " | ".join(str(v) for v in vols) + " |")
        out.append("|---|" + "---|" * len(vols))
        for d in dists:
            cells = []
            for v in vols:
                r = next(x for x in rows if x["dist_km"] == d and x["volume"] == v)
                a, b = r["road"], r["rail"]
                sa = _fmt_m(a["score"]) + ("" if a["feasible"] else "x")
                sb = _fmt_m(b["score"]) + ("" if b["feasible"] else "x")
                trk = {"single": "", "single+loops": f",{b['loops']}L", "double": ",2T"}.get(b["track"] or "", "")
                cells.append(f"{sa} / {sb} ({a['n']}T/{b['n']}tr{trk})")
            out.append(f"| {d} | " + " | ".join(cells) + " |")
        out.append("")
        reasons = sorted({re.sub(r"[\d.%]+", "N", part.strip()) for r in rows
                          if not r["road"]["feasible"] for part in r["road"]["reason"].split(";") if part.strip()})
        if reasons:
            out.append("Road infeasibility causes: " + "; ".join(reasons) + ".\n")
        vs = sorted({r["road"]["vehicle"] for r in rows if r["road"]["vehicle"]})
        vr = sorted({r["rail"]["vehicle"].split(" + ")[0] for r in rows if r["rail"]["vehicle"]})
        out.append(f"Vehicles chosen — road: {', '.join(vs)}; rail locos: {', '.join(vr)}.\n")
        for caption, alt in (sensitivity or {}).items():
            if year not in alt:
                continue
            fr = alt[year]
            out.append(f"Sensitivity — {caption}:\n")
            out.append("| km \\ u/yr | " + " | ".join(str(v) for v in vols) + " |")
            out.append("|---|" + "---|" * len(vols))
            for d in dists:
                cells = [next(x for x in fr if x["dist_km"] == d and x["volume"] == v)["winner"] for v in vols]
                out.append(f"| {d} | " + " | ".join(cells) + " |")
            out.append("")
    return "\n".join(out)


def breakeven_summary(rows: list[dict]) -> dict:
    """Smallest volume per distance at which rail wins (None if never), for a lookup table."""
    res = {}
    for d in sorted({r["dist_km"] for r in rows}):
        vs = sorted((r for r in rows if r["dist_km"] == d), key=lambda r: r["volume"])
        res[d] = next((r["volume"] for r in vs if r["winner"] == "rail"), None)
    return res


if __name__ == "__main__":
    import sys
    from .model import Game as _G
    from .vehicles import load_vehicles
    g = _G.load()
    vs = load_vehicles(g)
    tables, fast, nocap = {}, {}, {}
    for y in ERAS:
        tables[y] = breakeven_table(g, vs, y)
        fast[y] = breakeven_table(g, vs, y, load_calibration=CARGO_LOAD_FACTOR_DEFAULT)
        nocap[y] = breakeven_table(g, vs, y, max_road_vehicles=10 ** 6)
        print(y, "rail from (km: u/yr):", breakeven_summary(tables[y]),
              "| fast load:", breakeven_summary(fast[y]), "| no fleet cap:", breakeven_summary(nocap[y]),
              file=sys.stderr)
    print(render_breakeven_md(tables, {
        "loading 16× faster (no `loadSpeedFactor`, the economics.py model)": fast,
        f"no {MAX_ROAD_VEHICLES}-truck fleet cap (bays, road v/c and gates only)": nocap}))
