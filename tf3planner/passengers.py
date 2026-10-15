"""Passenger and worker transport: demand model and ProjectCandidate generators.

Game facts used (docs/mechanics.md sections 3, 8, 9, 11; verified from game scripts):
- Ticket per passenger and leg = 3.609 $/m x straight-line 3D distance (+8x climb) x (0.5 + priceFactor)
  x rank multiplier (`Game.income_per_unit`). Speed and comfort do not change the ticket.
- Residents cycle home -> shop (commercial) -> home -> work (industrial land use or an industry) -> home,
  and like to shop or work in neighbouring towns. Population = residential capacity.
- Industries have 20 work slots (destinationWeight 2) reachable only by public transport. >= 10 distinct
  residents arriving by line within 2 years give a production boost x(1.5 + U[0,1)) per industry.
- Town XP: 5 * 2^tier per resident arriving by line at a town building (tier 0 same town, 1 neighbour, 2 other).
- One game year = yearDurationMs of simulation (1461 s here); vehicles and pedestrians move on that clock.
- Catchment radii: small road stop 160 m, modular road station 175 m, rail passenger main building 150 m.

Model (ASSUMPTIONS, all exposed in PassengerParams so they can be calibrated from in-game line statistics):

1. Trip generation. Each resident makes TRIPS_PER_RESIDENT_YEAR one-way legs per game year (all modes).
   Trip generation is engine-side (open question Q10). The default 2.0 is a deliberately conservative guess:
   agents walk on the simulation clock (~1.4 m/s), so one 1461 s year is ~2 km of walking. A 4-leg cycle
   (home-shop-home-work-home) with ~300-500 m legs plus access walks already takes most of a year before any
   dwell time at the destination; with dwell, ~2 legs/year remain. Trips scale with the year length
   (slower calendar = more sim seconds per year = more trips per year).
2. Destination choice: singly-constrained gravity model. Shopping legs are attracted by commercial capacity,
   work legs by industrial capacity plus industries (20 slots x weight 2), with impedance exp(-d / D),
   D = DISTANCE_DECAY_M, cut at max_trip_m. Intra-town legs use the mean distance between two random
   points of the town disc (0.905 R). Town radius R = town_radius_k * sqrt(total land-use capacity)
   (~250-450 m for 1900 towns of 400-1600 capacity; TF towns are about one 1900-era house per 4 people).
3. Mode capture (share of legs that use the line): a power-logit on travel times,
   share = 1 / (1 + (t_pt / t_alt)^k), k = mode_exponent, where t_pt = access walk + headway/2 + ride
   (+ transfer) and t_alt = walking (detour 1.3, 1.4 m/s) or, for the car-owning share of residents
   (rises from 0 in 1900 to ~50 % by 1980, TF2-like ASSUMPTION), the faster of walking and driving.
   Workers to industries cannot walk or drive (capture 1 once a line exists).
4. Coverage: only residents/destinations inside stop catchments can use a line. Coverage of a town by n stops
   of radius r: 1 - exp(-n r^2 / R^2) (random overlap). Intra-town trips need both ends covered (cov^2).
   Intercity lines see the station catchment at each end, or the feeder coverage (x transfer_factor)
   when the town already has a bus/tram loop (built_stations contains (town, 'town_loop')).
5. Frequency: Mohring square-root rule h* = sqrt(2 T c / (Q v_w)) (T cycle time, c vehicle cost per second,
   Q passengers per second, v_w ticket revenue lost per passenger-second of waiting, from the capture curve)
   gives the starting fleet; then the fleet size is optimised exactly over a small range with the capture
   model (the discrete version of the same trade-off), together with the vehicle model and stop count.
6. Capacity: vehicles x capacity x runs/year x load factor; intra-town loops reuse seats (turnover =
   loop length / mean ride length, capped by the number of stops).

Keys used in ProjectCandidate (shared with the planner):
- pair_keys (min_node, max_node, 'road'|'rail') for routes actually built (towns are pre-connected by
  roads on this map, enforceMainConnections = true, so intercity bus routes are free by default).
- station_keys (node, 'road_pax' | 'tram_pax' | 'rail_pax'): passenger stops/stations (separate from cargo
  terminals in TF3); (town, 'town_loop') marks a built intra-town loop = feeder network for intercity lines
  (intra-town candidates are not generated again for that town).
- Bundles (kind 'passenger', mode 'multi'): intercity line + the missing town loops at both ends, evaluated
  together, because the loops are what make intercity lines pay (coverage) while a loop alone often does
  not; `legs` lists the parts. Standalone loops/intercity lines are generated as well.
- boosts {(industry_node, 'workers'): factor} for worker lines that reach >= 10 distinct workers / 2 years.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Callable

import numpy as np

from . import facilities
from .candidates import ProjectCandidate, VehicleLine
from .economics import MAX_OCCUPANCY, STOP_DELAY_S, VEHICLE_LENGTH_M
from .model import Game
from .vehicles import Vehicle, available

# --------------------------------------------------------------------------- calibration parameters

REF_YEAR_S = 1461.0                 # year length the trip rate refers to (yearDurationMs 1461000)
TRIPS_PER_RESIDENT_YEAR = 2.0       # one-way legs per resident per (reference) year, all modes [Q10]
DISTANCE_DECAY_M = 2500.0           # gravity impedance exp(-d / D)
MEAN_DISC_DISTANCE = 128.0 / (45.0 * math.pi)   # mean distance of two random points in a unit disc


@dataclass
class PassengerParams:
    # trip generation and destination choice
    trips_per_resident_year: float = TRIPS_PER_RESIDENT_YEAR
    ref_year_s: float = REF_YEAR_S
    shop_share: float = 0.5                 # half the legs are shopping-related, half work-related
    distance_decay_m: float = DISTANCE_DECAY_M
    max_trip_m: float = 15000.0
    industry_slots: int = 20                # industrialCapacity of every industry
    industry_dest_weight: float = 2.0       # destinationWeight of industry work slots
    include_industry_work: bool = True      # potential demand assumes industries are reachable by line
    town_radius_k: float = 10.0             # R = k * sqrt(res + com + ind capacity), metres
    # mode choice
    walk_speed: float = 1.4                 # m/s
    walk_detour: float = 1.3
    car_speed: float = 8.0                  # m/s, door to door in town traffic
    car_overhead_s: float = 60.0
    car_share: tuple = ((1900, 0.0), (1915, 0.0), (1930, 0.15), (1960, 0.35), (1980, 0.5), (2100, 0.5))
    mode_exponent: float = 4.0
    access_fraction: float = 0.5            # mean access walk = fraction x catchment radius (each end)
    ride_detour_town: float = 1.5           # in-vehicle distance / straight distance on a town loop
    transfer_factor: float = 0.85           # share of feeder-covered riders accepting a transfer
    transfer_wait_s: float = 60.0
    feeder_coverage: float = 0.7            # town coverage of an existing bus/tram network
    # facilities
    stop_catchment_m: float = 160.0         # small road stop
    rail_catchment_m: float = 150.0         # rail passenger main building 1
    # operations
    load_factor_town: float = 0.5           # mean occupancy achievable on a town loop (peaks, direction)
    load_factor_intercity: float = 0.75
    rank_multiplier: float = 1.0            # ticketPriceMultiplier(rank); 1.0 at rank 1
    amort_years: float = 10.0               # option ranking inside a generator: profit - capex / amort
    max_road_vehicles: int = 30
    max_trains: int = 8
    max_wagons: int = 8
    max_intra_stops: int = 10
    min_intra_stops: int = 2
    tram_track_share: float = 0.2           # tram lane upgrade = +20 % of the town street cost
    tram_electric_share: float = 0.4        # electric tram lane = +40 %
    town_new_links: bool = True             # loops may add new street links where routing.py prices demolition
    # line selection
    intercity_max_m: float = 12000.0
    intercity_neighbours: int = 6           # per town, nearest towns considered for intercity lines
    town_roads_exist: bool = True           # enforceMainConnections: the game links towns by road
    use_streets: bool = True                # stops/loops/lines on the export v6 street network
    min_volume: float = 1.0                 # drop candidates carrying less (passengers/year)
    # workers
    worker_range_m: float = 4000.0
    worker_towns_per_industry: int = 2
    min_workers: int = 10
    worker_window_years: float = 2.0
    worker_boost_factor: float = 2.0        # expected 1.5 + U[0,1); planner observed x2.0..2.31
    # reference service for demand_matrix(captured=True)
    ref_pt_speed: float = 4.5               # m/s
    ref_headway_s: float = 240.0


# --------------------------------------------------------------------------- towns

@dataclass
class TownInfo:
    index: int              # node index (towns first)
    id: int
    name: str
    x: float
    y: float
    z: float
    residents: float
    commercial: float
    industrial: float
    radius: float

    @property
    def capacity(self) -> float:
        return self.residents + self.commercial + self.industrial


def _current_capacities(game: Game) -> dict[int, tuple[float, float, float]]:
    """Current land-use capacities from the export (capacityUsage), falling back to initial ones."""
    out = {}
    for raw in game.raw.get("towns", []):
        cu = raw.get("capacityUsage")
        if isinstance(cu, list) and len(cu) >= 3 and all(isinstance(c, dict) for c in cu[:3]):
            out[raw["id"]] = tuple(float(c.get("capacity", 0) or 0) for c in cu[:3])
    return out


def town_infos(game: Game, params: PassengerParams | None = None) -> list[TownInfo]:
    p = params or PassengerParams()
    cur = _current_capacities(game)
    out = []
    for k, t in enumerate(game.towns):
        r, c, i = cur.get(t.id) or (t.capacities.get("residential", 0), t.capacities.get("commercial", 0),
                                    t.capacities.get("industrial", 0))
        radius = p.town_radius_k * math.sqrt(max(r + c + i, 1.0))
        out.append(TownInfo(k, t.id, t.name, t.x, t.y, t.z, float(r), float(c), float(i), radius))
    return out


def intra_distance_pdf(radius: float, n: int = 32) -> tuple[np.ndarray, np.ndarray]:
    """Distances and probability weights of the distance between two random points in a disc."""
    u = (np.arange(n) + 0.5) / n                     # d / (2R) on a midpoint grid
    d = 2 * radius * u
    w = (2 * d / radius ** 2) * (2 / math.pi) * (np.arccos(u) - u * np.sqrt(1 - u * u))
    w = w / w.sum()
    return d, w


def coverage(n_stops: float, catchment: float, radius: float) -> float:
    """Share of a town disc inside n randomly overlapping catchment circles."""
    return 1.0 - math.exp(-n_stops * catchment ** 2 / max(radius, 1.0) ** 2)


# --------------------------------------------------------------------------- mode choice

def car_share(year: int, p: PassengerParams) -> float:
    pts = sorted(p.car_share)
    ys = [a for a, _ in pts]
    vs = [b for _, b in pts]
    return float(np.interp(year, ys, vs))


def _share(t_pt, t_alt, k: float):
    return 1.0 / (1.0 + np.power(np.maximum(t_pt, 1e-6) / np.maximum(t_alt, 1e-6), k))


def capture(t_pt, d_straight, year: int, p: PassengerParams):
    """Share of legs of straight length d that choose public transport taking t_pt seconds."""
    d = np.asarray(d_straight, dtype=float)
    t_walk = d * p.walk_detour / p.walk_speed
    t_car = d * p.walk_detour / p.car_speed + p.car_overhead_s
    cs = car_share(year, p)
    s_walk = _share(t_pt, t_walk, p.mode_exponent)
    s_car = _share(t_pt, np.minimum(t_walk, t_car), p.mode_exponent)
    return (1 - cs) * s_walk + cs * s_car


# --------------------------------------------------------------------------- demand

def _year_scale(game: Game, p: PassengerParams) -> float:
    return (game.year_ms / 1000.0) / p.ref_year_s


def gravity(towns: list[TownInfo], dest_xyz: np.ndarray, dest_work: np.ndarray, p: PassengerParams,
            year_scale: float = 1.0) -> np.ndarray:
    """Legs/year by residents of each town (rows) between home and each destination (columns).

    Destinations are the towns (same order, first len(towns) columns) followed by extra work-only
    destinations (industries) given by dest_xyz[len(towns):] and their work weights dest_work.
    Both directions (outbound and return) are counted in M[i, j].
    """
    nt = len(towns)
    o = np.array([[t.x, t.y, t.z] for t in towns])
    dx = o[:, None, :] - dest_xyz[None, :, :]
    d = np.sqrt((dx ** 2).sum(-1))
    for t in towns:
        d[t.index, t.index] = MEAN_DISC_DISTANCE * t.radius
    f = np.exp(-d / p.distance_decay_m) * (d <= p.max_trip_m)
    shop = np.zeros(dest_xyz.shape[0])
    shop[:nt] = [t.commercial for t in towns]
    work = np.asarray(dest_work, dtype=float).copy()
    work[:nt] = [t.industrial for t in towns]
    ws = f * shop[None, :]
    ww = f * work[None, :]
    ps = ws / np.maximum(ws.sum(1, keepdims=True), 1e-12)
    pw = ww / np.maximum(ww.sum(1, keepdims=True), 1e-12)
    legs = np.array([t.residents for t in towns]) * p.trips_per_resident_year * year_scale
    return legs[:, None] * (p.shop_share * ps + (1 - p.shop_share) * pw)


def demand_matrix(game: Game, params: PassengerParams | None = None, year: int | None = None,
                  captured: bool = False, nodes: list | None = None):
    """Annual passenger legs between towns (diagonal = intra-town) and from towns to industries.

    Returns (nodes, M) with nodes = routing.network_nodes(game) order (towns first, then industries) and
    M[i, j] = legs/year made by residents of town i between home i and destination j, both directions.
    Directional flow i -> j between two towns = (M[i, j] + M[j, i]) / 2. Rows of industries are zero.
    captured=False: all-mode (potential) legs; captured=True: legs a reference public-transport service
    (ref_pt_speed, ref_headway_s, full catchment coverage) would carry.
    """
    p = params or PassengerParams()
    year = game.year if year is None else year
    if nodes is None:
        from .routing import network_nodes
        nodes = network_nodes(game)
    towns = town_infos(game, p)
    nt = len(towns)
    xyz = np.array([[n.x, n.y, n.z] for n in nodes], dtype=float)
    for t in towns:
        xyz[t.index] = (t.x, t.y, t.z)
    work = np.zeros(len(nodes))
    if p.include_industry_work:
        work[nt:] = [0.0 if getattr(n, "water_only", False) else p.industry_slots * p.industry_dest_weight
                     for n in nodes[nt:]]
    g = gravity(towns, xyz, work, p, _year_scale(game, p))
    M = np.zeros((len(nodes), len(nodes)))
    M[:nt, :] = g
    if captured:
        acc = 2 * p.access_fraction * p.stop_catchment_m * p.walk_detour / p.walk_speed
        for t in towns:
            d, w = intra_distance_pdf(t.radius)
            t_pt = acc + p.ref_headway_s / 2 + d * p.ride_detour_town / p.ref_pt_speed
            M[t.index, t.index] *= float((w * capture(t_pt, d, year, p)).sum())
        o = xyz[:nt]
        dist = np.sqrt(((o[:, None, :] - xyz[None, :nt, :]) ** 2).sum(-1))
        t_pt = acc + p.ref_headway_s / 2 + dist * 1.2 / p.ref_pt_speed
        cap = capture(t_pt, dist, year, p)
        np.fill_diagonal(cap, 1.0)
        M[:nt, :nt] *= cap               # industries: walking/driving not allowed -> capture 1
    return nodes, M


def demand_summary(game: Game, params: PassengerParams | None = None, year: int | None = None) -> dict:
    p = params or PassengerParams()
    nodes, M = demand_matrix(game, p, year)
    _, C = demand_matrix(game, p, year, captured=True, nodes=nodes)
    nt = len(game.towns)
    towns = town_infos(game, p)
    intra = float(np.trace(M[:nt, :nt]))
    inter = float(M[:nt, :nt].sum() - intra)
    ind = float(M[:nt, nt:].sum())
    pairs = []
    for i in range(nt):
        for j in range(i + 1, nt):
            pairs.append((M[i, j] + M[j, i], i, j))
    pairs.sort(reverse=True)
    return {
        "residents": sum(t.residents for t in towns),
        "trips_per_resident_year": p.trips_per_resident_year * _year_scale(game, p),
        "legs_total": float(M.sum()), "legs_intra": intra, "legs_intercity": inter, "legs_industry": ind,
        "captured_intra": float(np.trace(C[:nt, :nt])),
        "captured_intercity": float(C[:nt, :nt].sum() - np.trace(C[:nt, :nt])),
        "captured_industry": float(C[:nt, nt:].sum()),
        "town_radius_m": (min(t.radius for t in towns), max(t.radius for t in towns)),
        "top_pairs": [(nodes[i].name, nodes[j].name, round(float(v), 1),
                       round(math.dist((nodes[i].x, nodes[i].y), (nodes[j].x, nodes[j].y)))) for v, i, j in pairs[:8]],
    }


# --------------------------------------------------------------------------- vehicles

@dataclass
class PaxConsist:
    mode: str                       # road | tram | rail
    units: list[tuple[Vehicle, int]]
    capacity: int
    speed: float                    # m/s top speed of the set
    price: float
    running: float
    life: float
    price_factor: float
    board_s: float                  # boarding/alighting time at a stop (s)

    @property
    def label(self) -> str:
        return " + ".join(f"{n}× {v.name}" if n > 1 else v.name for v, n in self.units)

    @property
    def n_units(self) -> int:
        return sum(n for _, n in self.units)


def _consist(mode: str, units: list[tuple[Vehicle, int]]) -> PaxConsist:
    cap = sum(v.carries("passengers") * n for v, n in units)
    pf = (sum(v.price_factor * v.carries("passengers") * n for v, n in units) / cap) if cap else 0.5
    board = max((v.carries("passengers") / max(v.load_speed, 0.1) for v, _ in units if v.carries("passengers")),
                default=0.0)
    return PaxConsist(mode, units, cap, min(v.top_speed for v, _ in units),
                      sum(v.price * n for v, n in units), sum(v.running_cost * n for v, n in units),
                      min(v.lifespan_years for v, _ in units) or 30.0, pf, board)


def _real(v: Vehicle) -> bool:
    return v.price > 1000 and v.year_from > 0 and v.top_speed > 0


def _engine_types(game: Game) -> dict[int, set[int]]:
    cache = getattr(game, "_pax_engine_types", None)
    if cache is None:
        cache = {}
        for raw in game.raw.get("vehicles", []):
            md = raw.get("metadata")
            if not isinstance(md, dict):
                continue
            eng = (md.get("landVehicle") or {}).get("engines")
            eng = eng if isinstance(eng, list) else (list(eng.values()) if isinstance(eng, dict) else [])
            cache[raw["id"]] = {e.get("type") for e in eng if isinstance(e, dict) and (e.get("power") or 0) > 0}
        game._pax_engine_types = cache
    return cache


def _tram_electric_available(game: Game, year: int) -> bool:
    ws = [w for w in game.ways if "tram_electrified" in w.key]
    return any(w.available(year) for w in ws) if ws else year >= 1910


def road_consists(vehicles: list[Vehicle], year: int) -> list[PaxConsist]:
    out = [_consist("road", [(v, 1)]) for v in available(vehicles, year, "road", "bus")
           if _real(v) and v.carries("passengers") > 0]
    return _pareto(out)


def tram_consists(game: Game, vehicles: list[Vehicle], year: int) -> list[PaxConsist]:
    et = _engine_types(game)
    elec = _tram_electric_available(game, year)
    avail = [v for v in available(vehicles, year, "tram") if _real(v)]
    motors = [v for v in avail if v.power_kw > 0 and (elec or 2 not in et.get(v.id, set()))]
    trailers = [v for v in avail if v.power_kw <= 0 and v.carries("passengers") > 0]
    out = []
    for m in motors:
        if m.carries("passengers") > 0:
            out.append(_consist("tram", [(m, 1)]))
        for t in trailers:
            for k in (1, 2):
                out.append(_consist("tram", [(m, 1), (t, k)]))
    return _pareto([c for c in out if c.capacity > 0])


def rail_consists(vehicles: list[Vehicle], year: int, p: PassengerParams, max_grade: float = 0.01,
                  game=None) -> list[PaxConsist]:
    avail = [v for v in available(vehicles, year, "rail") if _real(v)]
    if game is not None:            # electric locos / EMUs need catenary, which new lines don't build
        from .upgrades import needs_catenary
        avail = [v for v in avail if not needs_catenary(game, v)]
    locos = [v for v in avail if v.role == "loco"]
    wagons = [v for v in avail if v.role == "wagon" and v.carries("passengers") > 0]
    railcars = [v for v in avail if v.role == "railcar" and v.carries("passengers") > 0]
    out = []
    for loco in locos:
        max_mass_t = loco.tractive_effort_kn * 1000 / (9.81 * (0.002 + max_grade)) / 1000
        for w in wagons:
            per = w.weight_empty_t + w.weight_max_payload_t
            n_max = int((max_mass_t - loco.weight_empty_t) // per) if per > 0 else p.max_wagons
            for n in range(1, min(p.max_wagons, n_max) + 1):
                out.append(_consist("rail", [(loco, 1), (w, n)]))
    for rc in railcars:
        for n in (1, 2, 3):
            out.append(_consist("rail", [(rc, n)]))
    return _pareto(out)


def _pareto(cs: list[PaxConsist]) -> list[PaxConsist]:
    """Drop consists dominated in (capacity, speed) by a cheaper one (price and running cost)."""
    keep = []
    for c in cs:
        dominated = any(o is not c and o.capacity >= c.capacity and o.speed >= c.speed and o.price <= c.price
                        and o.running <= c.running and (o.capacity, o.speed, -o.price, -o.running)
                        != (c.capacity, c.speed, -c.price, -c.running) for o in cs)
        if not dominated:
            keep.append(c)
    # identical duplicates: keep one
    seen, out = set(), []
    for c in keep:
        k = (c.capacity, round(c.speed, 2), round(c.price), round(c.running))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def town_speed(v_top: float) -> float:
    """Average speed in town traffic (game cost formula for road/tram, stops and accelerations included)."""
    return min(((v_top * 3.6) ** 0.78 + 4) / 3.6, v_top)


# --------------------------------------------------------------------------- facilities

def _catalogue(game: Game) -> facilities.Catalogue:
    cat = getattr(game, "_catalogue", None)
    if cat is None:
        cat = game._catalogue = facilities.Catalogue(game)
    return cat


def small_stop(cat: facilities.Catalogue, year: int) -> tuple[float, float]:
    for name in ("small_old.con", "small_mid.con", "small_new.con"):
        c = cat.construction(year, "small_stops/" + name)
        if c:
            return c
    return 45_000.0, 7_500.0


def road_pax_station(cat: facilities.Catalogue, year: int, terminals: int = 1) -> tuple[float, float]:
    e, em = cat.price(year, "road_entrance", "modular_street_station/entrance.module")
    x, xm = cat.price(year, "road_entrance", "modular_street_station/exit.module")
    pp, pm = cat.price(year, "road_platform_special", "modular_street_station/passenger_platform.module")
    return e + x + terminals * 2 * pp, em + xm + terminals * 2 * pm


def rail_pax_station(cat: facilities.Catalogue, year: int, length_m: float, terminals: int = 1) -> tuple[float, float, str]:
    segs = max(2, math.ceil(length_m / facilities.SEGMENT_M))
    size = 1 if terminals < 3 else (2 if terminals < 6 else 3)
    b, bm = cat.price(year, "rail_building", f"modular_station/main_building_{size}_era")
    pl, plm = cat.price(year, "rail_platform_special", "modular_station/platform_passenger_era")
    t, tm = cat.price(year, "rail_track", "/infrastructure/track/simple/simple.street_template")
    cost = b + terminals * segs * (pl + t)
    return cost, bm + terminals * segs * (plm + tm), f"Пассажирский вокзал: {terminals}×{segs * 40} м"


def _depot(cat: facilities.Catalogue, game: Game, mode: str, year: int, n: int) -> tuple[float, float, str]:
    # trams: game script price 360k (menu shows 120k) x1.5 VeryHard -> one depot per 12 trams
    d = facilities.depot(cat, mode, year, n)
    return d.cost, d.maintenance, d.name


def _town_street(game: Game, year: int) -> tuple[float, float]:
    ws = [w for w in game.ways if w.key.startswith("town_") and "medium" in w.key and w.available(year)
          and not any(s in w.key for s in ("tram", "bus", "barriers", "trees", "one_way"))]
    if ws:
        w = min(ws, key=lambda w: w.cost_per_m)
        return w.cost_per_m, w.maintenance_per_m
    return 180.0, 30.0


# --------------------------------------------------------------------------- helpers

def _ticket(game: Game, a, b, pf: float, p: PassengerParams) -> float:
    """Mean ticket per passenger over both directions between two points (x, y, z)."""
    pax = game.cargo_by_key["passengers"]
    return 0.5 * (game.income_per_unit(pax, a, b, pf) + game.income_per_unit(pax, b, a, pf)) * p.rank_multiplier


def _ticket_per_m(game: Game, pf: float, p: PassengerParams) -> float:
    pax = game.cargo_by_key["passengers"]
    return pax.factor * game.pass_difficulty_income * (0.5 + pf) * p.rank_multiplier


def _objective(profit: float, capex: float, p: PassengerParams) -> float:
    return profit - capex / p.amort_years


def sqrt_rule_vehicles(cycle_s: float, demand_per_year: float, veh_cost_per_year: float,
                       wait_value_per_pax_s: float, year_s: float) -> int:
    """Mohring square-root rule: h* = sqrt(2 T c / (Q v_w)); returns ceil(T / h*)."""
    if demand_per_year <= 0 or wait_value_per_pax_s <= 0:
        return 1
    c = veh_cost_per_year / year_s
    q = demand_per_year / year_s
    h = math.sqrt(2 * cycle_s * c / (q * wait_value_per_pax_s))
    return max(1, math.ceil(cycle_s / max(h, 1e-6)))


def _fmt(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _vehicle_lines(c: PaxConsist, n: int) -> list[VehicleLine]:
    return [VehicleLine(v.id, v.name, v.file, k * n, v.price) for v, k in c.units]


def _decimate(poly) -> list[list[float]]:
    if not poly:
        return []
    step = max(1, len(poly) // 60)
    pts = [[round(x, 1), round(y, 1)] for x, y in poly[::step]]
    last = [round(poly[-1][0], 1), round(poly[-1][1], 1)]
    if pts[-1] != last:
        pts.append(last)
    return pts


def _route_geometry(pl, mode: str, i: int, j: int, a, b) -> list[list[float]]:
    try:
        r = pl.mat[mode].route(i, j)
        if r is not None and r.polyline:
            return _decimate(r.polyline)
    except Exception:
        pass
    return [[round(a[0], 1), round(a[1], 1)], [round(b[0], 1), round(b[1], 1)]]


def _circle(t: TownInfo, r: float, n: int = 12) -> list[list[float]]:
    pts = [[round(t.x + r * math.cos(2 * math.pi * k / n), 1), round(t.y + r * math.sin(2 * math.pi * k / n), 1)]
           for k in range(n)]
    return pts + [pts[0]]


# --------------------------------------------------------------------------- town streets (export v6 network)
#
# Buses and trams run on the EXISTING streets and roads of the map (tf3planner.network, export v6):
# stops are points on street edges (curve samples, so they lie on the street), town loops connect
# the stops by the fastest street paths (no new road at all), intercity / worker lines run between
# the central stops of the towns along existing roads or along a routing.py route whose genuinely new
# pieces are paid. New street links inside towns (loop shortcuts) need the demolition of houses; they
# are only planned when it is priced (routing.py demolition_cost, else export v7 buildings, see
# price_new_road) and only kept when the faster loop earns more than it costs (_intra_options).

STREET_SAMPLE_M = 8.0           # curve sampling of the network (stops are samples)
STOP_END_MARGIN_M = 12.0        # a stop keeps this distance from street nodes (junctions)
STOP_MIN_EDGE_M = 30.0          # streets shorter than this get no stop
STOP_MIN_GAP_M = 120.0          # two stops of one line are at least this far apart
INDUSTRY_STOP_M = 160.0         # worker stop: on a road within this distance of the industry
LOOP_SHORTCUT_RATIO = 1.6       # a loop leg this much longer than straight may get a new street link ...
LOOP_SHORTCUT_MIN_M = 150.0     # ... when that saves at least this many metres
DEPOT_ROAD_OFFSET_M = 30.0      # depot footprint centre beside the road it connects to
DEPOT_RADIUS_M = 400.0          # road/tram depot maintenance radius (dossier.MAINT)
UNPRICED_BUILT_MAX_M = 100.0    # without routing.py demolition prices: new road through houses allowed up to this
_TO_RU = ("на восток", "на северо-восток", "на север", "на северо-запад", "на запад", "на юго-запад", "на юг",
          "на юго-восток")
_AT_RU = ("к востоку", "к северо-востоку", "к северу", "к северо-западу", "к западу", "к юго-западу", "к югу",
          "к юго-востоку")


_BY_RU = {"городская улица": "городской улице", "узкая городская улица": "узкой городской улице",
          "загородная дорога": "загородной дороге", "подъездная дорога": "подъездной дороге",
          "въезд предприятия": "въезду предприятия", "дорога": "дороге"}


def _dir8(dx: float, dy: float) -> int:
    return int(round(math.atan2(dy, dx) / (math.pi / 4))) % 8


def street_class_ru(way_key: str) -> str:
    k = way_key or ""
    if "entrance" in k:
        return "въезд предприятия"
    if k.startswith("ind_"):
        return "подъездная дорога"
    if k.startswith("town_"):
        return "узкая городская улица" if "xsmall" in k else "городская улица"
    if k.startswith("country_"):
        return "загородная дорога"
    return "дорога"


NEW_WAY_WIDTH_M = 8.0           # road bed width of a new street (routing DEFAULT_BED_WIDTH["road"])


def _new_polylines(r) -> list:
    return [g["polyline"] for g in (getattr(r, "segments", None) or []) if g.get("type") == "new"
            and len(g.get("polyline") or []) >= 2]


def price_new_road(game: Game, r) -> tuple[float, float, bool]:
    """(total cost of the new pieces incl. demolition, demolition part, priced?) of a routing.Route.

    * routing.py prices demolition itself (``Route.demolition_cost``; that money is already part of
      ``build_cost``, routing module doc item 10);
    * export v7 buildings (tf3planner.buildings): every town building whose footprint touches the
      corridor of a NEW segment is bulldozed (bulldoze_cost from the in-game probes).
    The larger of the two counts (whatever routing.py did not include is added on top), so a routing
    version that does not compute demolition yet still pays for the houses. Neither available ->
    (build_cost, 0, False): the caller must not plan new streets through houses."""
    build = float(getattr(r, "build_cost", 0.0) or 0.0)
    rt = float(getattr(r, "demolition_cost", 0.0) or 0.0)
    rt_priced = hasattr(r, "demolition_cost") and (rt > 0 or float(getattr(r, "demolition_m", 0.0) or 0.0) > 0)
    try:
        from . import buildings as _bd
        tb = _bd.for_game(game)
    except Exception:                           # pragma: no cover
        tb = None
    if tb is None:
        return build, rt, rt_priced
    idx: set[int] = set()
    for P in _new_polylines(r):
        idx.update(int(k) for k in tb.in_corridor(P, NEW_WAY_WIDTH_M))
    bd = tb.demolition_cost(sorted(idx)) if idx else 0.0
    demo = max(rt, bd)
    return build + (demo - rt), demo, True


def _segment_demolition(game: Game, seg: dict) -> float | None:
    """Demolition money of one NEW segment: routing's value, else export v7 buildings, else None."""
    if seg.get("demolition_cost") is not None:
        return float(seg["demolition_cost"] or 0.0)
    try:
        from . import buildings as _bd
        tb = _bd.for_game(game)
    except Exception:                           # pragma: no cover
        tb = None
    if tb is None:
        return None
    idx = tb.in_corridor(seg["polyline"], NEW_WAY_WIDTH_M)
    return tb.demolition_cost(idx) if len(idx) else 0.0


@dataclass
class StreetLine:
    """A line on streets/roads: stops (travel order), map geometry, typed segments and costs."""
    stops: list[dict]
    geometry: list[list[float]]
    segments: list[dict]
    totals: dict
    length_m: float                     # loop: one full cycle; line: one way
    by_speed: dict                      # way speed (m/s) -> metres driven at it
    unique_m: float                     # distinct street length (tram lanes are laid once)
    route_text: str = ""
    coverage: float = 0.0               # share of the town's street frontage within a stop catchment
    new_cost: float = 0.0               # new road pieces incl. demolition
    new_maint: float = 0.0
    new_m: float = 0.0
    demolition: float = 0.0
    variant: str = "streets"            # streets | streets+new | route
    unpriced_built_m: float = 0.0       # new road through built-up area whose demolition routing.py does not price yet

    def run_time(self, v: float) -> float:
        """Driving time at vehicle speed v, limited by every street's speed."""
        return sum(L / max(min(v, s), 0.5) for s, L in self.by_speed.items())

    @property
    def streets_only(self) -> bool:
        return self.new_m <= 0.5

    def pax_json(self, depot: dict | None = None) -> dict:
        out = {"stops": self.stops, "route_text": self.route_text, "variant": self.variant,
               "length_m": round(self.length_m), "unique_m": round(self.unique_m), "coverage": round(self.coverage, 3),
               "new_m": round(self.new_m), "new_cost": round(self.new_cost), "demolition": round(self.demolition),
               "streets_only": self.streets_only}
        if self.unpriced_built_m > 0:
            out["unpriced_built_m"] = round(self.unpriced_built_m)
        if depot:
            out["depot"] = depot
        return out


class StreetPlanner:
    """Stops, loops and lines on the existing road network of one game (cached per game)."""

    def __init__(self, game: Game, net, towns: list[TownInfo]):
        from . import network as _nw
        self.game, self.net, self.towns = game, net, towns
        self.S = net._samples("road", STREET_SAMPLE_M)
        S = self.S
        self.G, self.E = net.graph("road", "time")
        n_e = net.n_edges
        self.Le = np.zeros(n_e)
        ok = S["end"] > S["start"]
        self.Le[ok] = S["s"][S["end"][ok] - 1]
        self.keys = [net.template_key(k) for k in range(n_e)]
        self.etime = net.length / np.maximum(net.speed, 0.1)
        road = net.mask("road")
        self.road = road
        # town streets belong to the nearest town (distance relative to the town radius)
        mid = 0.5 * (net.xyz[net.edges[:, 0], :2] + net.xyz[net.edges[:, 1], :2])
        self.mid = mid
        self.edge_town = np.full(n_e, -1, np.int64)
        own = np.nonzero(road & (net.owner == _nw.TOWN))[0]
        if len(own) and towns:
            tc = np.array([[t.x, t.y] for t in towns])
            tr = np.array([max(t.radius, 50.0) for t in towns])
            d = np.hypot(mid[own, None, 0] - tc[None, :, 0], mid[own, None, 1] - tc[None, :, 1])
            k = np.argmin(d / tr[None, :], axis=1)
            dk = d[np.arange(len(own)), k]
            keep = dk <= np.maximum(3 * tr[k], tr[k] + 600.0)
            self.edge_town[own[keep]] = k[keep]
        self.comp = net.components("road")
        self._town: dict = {}
        self._loops: dict = {}
        self._lines: dict = {}
        self._depots: dict = {}
        self._siting = None

    # ------------------------------------------------------------------ helpers
    @property
    def siting(self):
        if self._siting is None:
            from . import siting
            self._siting = siting.for_game(self.game)
        return self._siting

    def _stop_ok_edges(self, edges: np.ndarray) -> np.ndarray:
        net = self.net
        e = edges[(net.struct[edges] == 0) & (net.length[edges] >= STOP_MIN_EDGE_M)]
        return np.array([k for k in e.tolist() if "entrance" not in self.keys[k]], np.int64)

    def _samples_of(self, edges, margin: float = 0.0) -> np.ndarray:
        S = self.S
        out = []
        for k in np.asarray(edges, np.int64).tolist():
            a, b = int(S["start"][k]), int(S["end"][k])
            if b <= a:
                continue
            g = np.arange(a, b)
            if margin > 0:
                s = S["s"][g]
                g = g[(s >= margin) & (s <= self.Le[k] - margin)]
            out.append(g)
        return np.concatenate(out) if out else np.zeros(0, np.int64)

    def stop_dict(self, g: int, t: TownInfo | None = None, no: int | None = None, **extra) -> dict:
        S, net = self.S, self.net
        e = int(S["e"][g])
        x, y = (float(v) for v in S["xy"][g])
        s = float(S["s"][g])
        key = self.keys[e]
        d = {"x": round(x, 1), "y": round(y, 1), "edge": e, "edge_id": int(net.edge_ids[e]), "way": key,
             "street": street_class_ru(key), "junction_m": round(min(s, self.Le[e] - s)), "sample": int(g)}
        if no is not None:
            d["no"] = no
        if t is not None:
            dc = math.dist((x, y), (t.x, t.y))
            d["town"] = t.name
            d["from_center_m"] = round(dc)
            d["where"] = "в центре" if dc < 60 else f"{dc:.0f} м {_AT_RU[_dir8(x - t.x, y - t.y)]} от центра"
        d["label"] = (f"{d['street']} (ребро {d['edge_id']})"
                      + (f", {d['where']}" if "where" in d else "") + f"; до перекрёстка {d['junction_m']} м")
        d.update(extra)
        return d

    # ------------------------------------------------------------------ towns
    def town_streets(self, t: TownInfo, catchment: float, max_stops: int):
        key = (t.index, round(catchment), max_stops)
        if key in self._town:
            return self._town[key]
        from scipy import sparse as _sp
        from scipy.spatial import cKDTree
        S, net = self.S, self.net
        own = np.nonzero(self.edge_town == t.index)[0]
        res = None
        own_g = self._samples_of(own)
        if len(own) >= 2 and len(own_g) >= 4:
            demand = S["xy"][own_g[::2]]
            tree_own = cKDTree(S["xy"][own_g])
            near = np.nonzero(self.road & (self.edge_town < 0)
                              & (np.hypot(self.mid[:, 0] - t.x, self.mid[:, 1] - t.y) <= t.radius + 200.0))[0]
            if len(near):
                near = near[tree_own.query(self.mid[near])[0] <= 60.0]
            cand_e = self._stop_ok_edges(np.unique(np.concatenate([own, near])))
            if len(cand_e):
                # stops on the connected street component that carries most of the town's streets
                lab = self.comp[net.edges[cand_e, 0]]
                w = {}
                for c, L in zip(lab.tolist(), net.length[cand_e].tolist()):
                    w[c] = w.get(c, 0.0) + L
                best_c = max(w, key=w.get)
                cand_e = cand_e[lab == best_c]
                cand = self._samples_of(cand_e, STOP_END_MARGIN_M)[::2]
            else:
                cand = np.zeros(0, np.int64)
            if len(cand):
                cxy = S["xy"][cand]
                lists = cKDTree(demand).query_ball_point(cxy, catchment)
                rows = np.repeat(np.arange(len(cand)), [len(l) for l in lists])
                cols = np.concatenate([np.asarray(l, np.int64) for l in lists]) if len(rows) else np.zeros(0, np.int64)
                C = _sp.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(cand), len(demand)))
                dc = np.hypot(cxy[:, 0] - t.x, cxy[:, 1] - t.y)
                covered = np.zeros(len(demand), bool)
                order, cov = [], []
                blocked = np.zeros(len(cand), bool)
                for _ in range(max_stops):
                    gain = C @ (~covered).astype(float)
                    gain = np.where(blocked, -1.0, gain) - 1e-4 * dc      # ties: nearer the centre
                    k = int(np.argmax(gain))
                    if gain[k] < max(1.0, 0.02 * len(demand)) and order:
                        break
                    order.append(int(cand[k]))
                    covered[C[k].indices] = True
                    cov.append(float(covered.mean()))
                    blocked |= np.hypot(cxy[:, 0] - cxy[k, 0], cxy[:, 1] - cxy[k, 1]) < STOP_MIN_GAP_M
                res = SimpleNamespace(order=order, cov=cov, own=own, n_demand=len(demand),
                                      street_m=float(net.length[own].sum()))
        self._town[key] = res
        return res

    def main_stop(self, t: TownInfo, catchment: float = 160.0) -> int | None:
        """Central stop of a town (the one covering most street frontage), a sample index."""
        ts = self.town_streets(t, catchment, 1)
        if ts is not None and ts.order:
            return ts.order[0]
        g = self.snap(t.x, t.y, max_dist=max(t.radius, 200.0))
        return g

    def snap(self, x: float, y: float, max_dist: float = math.inf) -> int | None:
        """Nearest stop-capable road sample (not on a bridge, away from junctions)."""
        S = self.S
        if S["tree"] is None:
            return None
        k = min(64, len(S["e"]))
        d, gs = S["tree"].query([x, y], k=k)
        for dd, g in zip(np.atleast_1d(d), np.atleast_1d(gs)):
            if not np.isfinite(dd) or dd > max_dist:
                break
            e = int(S["e"][g])
            s = float(S["s"][g])
            if (self.net.struct[e] == 0 and "entrance" not in self.keys[e]
                    and STOP_END_MARGIN_M <= s <= self.Le[e] - STOP_END_MARGIN_M):
                return int(g)
        return None

    # ------------------------------------------------------------------ paths (fastest, on the real curves)
    def _ends(self, g: int) -> dict:
        """Street nodes of the stop's edge -> time from the stop to them."""
        e = int(self.S["e"][g])
        f = float(self.S["s"][g]) / max(self.Le[e], 1e-6)
        u, v = (int(q) for q in self.net.edges[e])
        T = self.etime[e]
        return {u: f * T, v: (1 - f) * T} if u != v else {u: 0.0}

    def _dijkstra(self, gs: list[int]):
        from scipy.sparse import csgraph
        nodes = sorted({n for g in gs for n in self._ends(g)})
        dist, pred = csgraph.dijkstra(self.G, directed=False, indices=nodes, return_predecessors=True)
        return {n: r for r, n in enumerate(nodes)}, dist, pred

    def _path(self, ga: int, gb: int, D) -> tuple[float, list] | None:
        """(time, pieces [(edge, xy (k, 2), metres)]) of the fastest street path between two stops."""
        row, dist, pred = D
        S, net = self.S, self.net
        ea, eb = int(S["e"][ga]), int(S["e"][gb])
        oa, ob = self._ends(ga), self._ends(gb)
        best = None
        if ea == eb:
            fa = float(S["s"][ga]) / max(self.Le[ea], 1e-6)
            fb = float(S["s"][gb]) / max(self.Le[ea], 1e-6)
            best = (abs(fa - fb) * self.etime[ea], [(ea, net._piece(S, ea, ga, gb), abs(fa - fb) * net.length[ea])])
        combo = None
        for na, ta in oa.items():
            r = row[na]
            for nb, tb in ob.items():
                tot = ta + float(dist[r, nb]) + tb
                if np.isfinite(tot) and (combo is None or tot < combo[0]):
                    combo = (tot, r, na, nb)
        if combo is not None and (best is None or combo[0] < best[0] - 1e-9):
            tot, r, na, nb = combo
            chain = [nb]
            while chain[-1] != na:
                chain.append(int(pred[r, chain[-1]]))
            chain.reverse()
            u0 = int(net.edges[ea, 0])
            g_end = int(S["start"][ea]) if na == u0 else int(S["end"][ea]) - 1
            fa = float(S["s"][ga]) / max(self.Le[ea], 1e-6)
            pieces = [(ea, net._piece(S, ea, ga, g_end), (fa if na == u0 else 1 - fa) * net.length[ea])]
            if len(chain) > 1:
                eids = (np.asarray(self.E[chain[:-1], chain[1:]]).ravel().astype(np.int64) - 1).tolist()
                for p, e in zip(chain[:-1], eids):
                    g0, g1 = int(S["start"][e]), int(S["end"][e]) - 1
                    xy = net._piece(S, e, g0, g1) if int(net.edges[e, 0]) == p else net._piece(S, e, g1, g0)
                    pieces.append((e, xy, float(net.length[e])))
            ub = int(net.edges[eb, 0])
            g_start = int(S["start"][eb]) if nb == ub else int(S["end"][eb]) - 1
            fb = float(S["s"][gb]) / max(self.Le[eb], 1e-6)
            pieces.append((eb, net._piece(S, eb, g_start, gb), (fb if nb == ub else 1 - fb) * net.length[eb]))
            best = (tot, pieces)
        if best is None:
            return None
        return best[0], [p for p in best[1] if len(p[1]) >= 2 and p[2] > 0.05]

    # ------------------------------------------------------------------ assembling lines
    def _assemble(self, legs: list, stops: list[dict], closed: bool) -> StreetLine:
        """legs: per leg either ('streets', pieces) or ('route', routing.Route, new_cost)."""
        from . import routing as _rt
        net = self.net
        geom: list[list[float]] = []
        segs: list[dict] = []
        by_speed: dict[float, float] = {}
        seen: set[int] = set()
        unique = total = new_cost = new_maint = new_m = demo = 0.0
        texts = []
        n = len(stops)

        def add_geom(P):
            pts = [[float(x), float(y)] for x, y in P]
            if geom and pts and math.dist(geom[-1], pts[0]) < 0.5:
                pts = pts[1:]
            geom.extend(pts)

        for k, leg in enumerate(legs):
            a = stops[k]
            b = stops[(k + 1) % n] if closed else stops[k + 1]
            if leg[0] == "route":
                r, cost, dm = leg[1], leg[2], leg[3]
                for g in _rt.route_segments_json(r, leg=k):
                    g["repeat_m"] = 0
                    if g["type"] == "new":
                        rt = float(g.get("demolition_cost") or 0.0)        # routing.py: already in build_cost
                        sd = _segment_demolition(self.game, dict(g, demolition_cost=None))
                        if sd is not None and sd > rt:
                            g["build_cost"] = round(g["build_cost"] + sd - rt)
                            g["demolition_cost"] = round(sd)
                        else:
                            g["demolition_cost"] = round(rt)
                    segs.append(g)
                add_geom(r.polyline)
                L = float(r.length_m)
                v = L / max(float(r.travel_time_s), 1e-6)
                by_speed[round(v, 2)] = by_speed.get(round(v, 2), 0.0) + L
                total += L
                unique += L
                new_cost += cost
                new_maint += float(r.maintenance_per_year)
                new_m += float(r.new_m)
                demo += dm
                texts.append(f"ост.{a.get('no', k + 1)} → {L:.0f} м по трассе routing.py (новые участки "
                             f"{r.new_m:.0f} м) → ост.{b.get('no', k + 2)}")
                continue
            pieces = leg[1]
            runs: list[list] = []          # [class, dir, metres, xy0, xy1]
            cur = None
            for e, xy, L in pieces:
                rep = L if e in seen else 0.0
                key = self.keys[e]
                sp = float(net.speed[e])
                by_speed[round(sp, 2)] = by_speed.get(round(sp, 2), 0.0) + L
                total += L
                unique += L - rep
                add_geom(xy)
                tag = (key, rep > 0)
                if cur is not None and cur[0] == tag:
                    cur[1].extend(xy[1:].tolist())
                    cur[2] += L
                    cur[3] += rep
                else:
                    cur = [tag, xy.tolist(), L, rep]
                    segs.append(cur)       # replaced by dicts below
                cls = street_class_ru(key)
                d = _dir8(xy[-1][0] - xy[0][0], xy[-1][1] - xy[0][1])
                if runs and runs[-1][0] == cls and (runs[-1][1] - d) % 8 in (0, 1, 7):
                    runs[-1][2] += L
                else:
                    runs.append([cls, d, L])
            for e, _xy, _L in pieces:
                seen.add(e)
            for i, s in enumerate(segs):
                if isinstance(s, list):
                    (key, _rep), P, L, rep = s
                    P = _rt.route_geometry(SimpleNamespace(polyline=P))
                    segs[i] = {"type": "existing", "polyline": P, "length_m": round(L), "way": key, "build_cost": 0,
                               "leg": k, "repeat_m": round(rep)}
            parts = [f"{L:.0f} м {_TO_RU[d]} по {_BY_RU.get(c, c)}" for c, d, L in runs if L >= 15]
            texts.append(f"ост.{a.get('no', k + 1)} → " + (", ".join(parts) or "рядом") + f" → ост.{b.get('no', k + 2)}")
        geom = _rt.route_geometry(SimpleNamespace(polyline=geom)) if len(geom) >= 2 else geom
        return StreetLine(stops=stops, geometry=geom, segments=segs, totals=_rt.segment_totals(segs),
                          length_m=float(total), by_speed={float(k): float(v) for k, v in by_speed.items()},
                          unique_m=float(unique), route_text="; ".join(texts), new_cost=float(new_cost),
                          new_maint=float(new_maint), new_m=float(new_m), demolition=float(demo),
                          variant="streets+new" if new_m > 0.5 else "streets")

    # ------------------------------------------------------------------ town loops
    def loop(self, t: TownInfo, n: int, catchment: float, max_stops: int, router=None) -> list[StreetLine]:
        """Loop variants through the first n greedy stops of the town (all on streets): the pure-street
        loop, plus a variant with new street links where routing.py prices the demolition."""
        key = (t.index, n, round(catchment), max_stops, router is not None)
        if key in self._loops:
            return self._loops[key]
        out: list[StreetLine] = []
        ts = self.town_streets(t, catchment, max_stops)
        if ts is not None and 2 <= n <= len(ts.order):
            gs = ts.order[:n]
            D = self._dijkstra(gs)
            T = np.full((n, n), np.inf)
            P: dict = {}
            for i in range(n):
                for j in range(n):
                    if i != j:
                        p = self._path(gs[i], gs[j], D)
                        if p is not None:
                            T[i, j], P[i, j] = p
            if np.isfinite(T[~np.eye(n, dtype=bool)]).all():
                if n == 2:
                    order = [0, 1]
                else:
                    from .tours import solve_tour
                    order = list(solve_tour(T).order)
                    k0 = order.index(0)
                    order = order[k0:] + order[:k0]
                stops = [self.stop_dict(gs[i], t, no=k + 1) for k, i in enumerate(order)]
                legs = [("streets", P[order[k], order[(k + 1) % n]]) for k in range(n)]
                base = self._assemble(legs, stops, closed=True)
                base.coverage = float(ts.cov[n - 1])
                out.append(base)
                if router is not None:
                    alt = list(legs)
                    changed = False
                    for k in range(n):
                        a, b = stops[k], stops[(k + 1) % n]
                        straight = math.dist((a["x"], a["y"]), (b["x"], b["y"]))
                        Ls = sum(p[2] for p in legs[k][1])
                        if Ls < LOOP_SHORTCUT_RATIO * straight or Ls - straight < LOOP_SHORTCUT_MIN_M:
                            continue
                        r = router((a["x"], a["y"]), (b["x"], b["y"]))
                        if r is None or r.new_m <= 0 or r.length_m >= Ls - LOOP_SHORTCUT_MIN_M / 2:
                            continue
                        cost, demo, priced = price_new_road(self.game, r)
                        if not priced:              # demolition unknown (no routing price, no export v7)
                            continue
                        alt[k] = ("route", r, cost, demo)
                        changed = True
                    if changed:
                        v = self._assemble(alt, stops, closed=True)
                        v.coverage = base.coverage
                        out.append(v)
        self._loops[key] = out
        return out

    # ------------------------------------------------------------------ lines between towns / industries
    def street_path_line(self, ga: int, gb: int, stops: list[dict]) -> StreetLine | None:
        key = ("streets", ga, gb)
        if key not in self._lines:
            p = self._path(ga, gb, self._dijkstra([ga]))
            self._lines[key] = None if p is None else self._assemble([("streets", p[1])], stops, closed=False)
        return self._lines[key]

    def route_line(self, a_xy, b_xy, stops: list[dict], router) -> StreetLine | None:
        """A routing.py route between two stops; None when it builds nothing new (the street path covers
        that) or builds through houses while demolition is not priced."""
        key = ("route", tuple(np.round(a_xy, 1)), tuple(np.round(b_xy, 1)))
        if key in self._lines:
            return self._lines[key]
        out = None
        r = router(a_xy, b_xy) if router is not None else None
        if r is not None and r.new_m > 0.5:
            cost, demo, priced = price_new_road(self.game, r)
            unpriced = 0.0 if priced else self.built_up_new_m(r)
            if unpriced <= UNPRICED_BUILT_MAX_M:
                out = self._assemble([("route", r, cost, demo)], stops, closed=False)
                out.variant = "route"
                out.unpriced_built_m = unpriced
        self._lines[key] = out
        return out

    def built_up_new_m(self, r) -> float:
        """Metres of the NEW segments of a route that run through built-up area (houses to demolish)."""
        S = self.siting
        tot = 0.0
        for g in getattr(r, "segments", None) or []:
            if g["type"] != "new":
                continue
            P = np.asarray(g["polyline"], float)
            for p0, p1 in zip(P[:-1], P[1:]):
                L = float(math.dist(p0, p1))
                k = max(1, int(L // 10.0))
                for q in range(k):
                    x, y = p0 + (p1 - p0) * (q + 0.5) / k
                    if S.built_depth(float(x), float(y)) > 0:
                        tot += L / k
        return tot

    # ------------------------------------------------------------------ depots
    def depot_site(self, anchor_xy, radius: float = DEPOT_RADIUS_M) -> dict | None:
        """Depot outside the built-up area, beside an existing road (its access), within the
        maintenance radius of the anchor stop, on flat dry ground."""
        key = (round(anchor_xy[0]), round(anchor_xy[1]), round(radius))
        if key in self._depots:
            return self._depots[key]
        S, net, sit = self.S, self.net, self.siting
        best = None
        if S["tree"] is not None:
            idx = np.asarray(S["tree"].query_ball_point(list(anchor_xy), radius - DEPOT_ROAD_OFFSET_M), np.int64)
            idx = idx[np.isin(S["e"][idx], np.nonzero(net.struct == 0)[0])][::2]
            ax, ay = anchor_xy
            for g in idx.tolist():
                e = int(S["e"][g])
                g0 = max(int(S["start"][e]), g - 1)
                g1 = min(int(S["end"][e]) - 1, g + 1)
                if g1 <= g0:
                    continue
                tx, ty = S["xy"][g1] - S["xy"][g0]
                L = math.hypot(tx, ty)
                if L < 1e-6:
                    continue
                nx, ny = -ty / L, tx / L
                rx, ry = (float(v) for v in S["xy"][g])
                for sgn in (1, -1):
                    x = float(round(rx + sgn * DEPOT_ROAD_OFFSET_M * nx))      # published (rounded) point is checked
                    y = float(round(ry + sgn * DEPOT_ROAD_OFFSET_M * ny))
                    da = math.dist((x, y), (ax, ay))
                    if da > radius * 0.97:
                        continue
                    if sit.built_depth(x, y) > 0 or not sit.dry_flat(x, y, 20.0):
                        continue
                    if S["tree"].query([x, y])[0] < DEPOT_ROAD_OFFSET_M - 6:   # would stand on another road
                        continue
                    gr = sit.grade(x, y)
                    if gr > 0.08:
                        continue
                    sc = 3000 * max(0.0, gr - 0.02) + 0.5 * da + (0.0 if net.owner[e] != 1 else 40.0)
                    if best is None or sc < best[0]:
                        best = (sc, x, y, e, rx, ry, da, gr)
        out = None
        if best is not None:
            _, x, y, e, rx, ry, da, gr = best
            out = {"x": round(x), "y": round(y), "road_x": round(rx), "road_y": round(ry), "road_edge": e,
                   "road": street_class_ru(self.keys[e]), "dist_anchor_m": round(da), "grade": round(gr, 3),
                   "reason": (f"депо вне застройки, вплотную к существующей дороге ({street_class_ru(self.keys[e])}, "
                              f"въезд в ({round(rx)}, {round(ry)})), {da:.0f} м от остановки (радиус обслуживания "
                              f"{radius:.0f} м), уклон ~{gr * 100:.0f} %; дома не сносим")}
        self._depots[key] = out
        return out

    def depot_for(self, stops: list[dict], center=None) -> dict | None:
        """Depot site near any stop of a line (outer stops first: towns are built-up around the centre)."""
        if not stops:
            return None
        cx, cy = center if center is not None else (stops[0]["x"], stops[0]["y"])
        for st in sorted(stops, key=lambda s: -math.dist((s["x"], s["y"]), (cx, cy))):
            d = self.depot_site((st["x"], st["y"]))
            if d is not None:
                return dict(d, anchor_stop=st.get("no"), anchor_label=st.get("label"))
        return None


def street_planner(game: Game, towns: list[TownInfo] | None = None) -> StreetPlanner | None:
    """StreetPlanner of a game (None without an export v6 street network)."""
    from . import network as _nw
    try:
        net = _nw.for_game(game)
    except Exception:                          # pragma: no cover
        net = None
    if net is None or not net.mask("road").any():
        return None
    sp = getattr(game, "_pax_streets", None)
    if sp is None or sp.net is not net:
        sp = StreetPlanner(game, net, towns if towns is not None else town_infos(game))
        game._pax_streets = sp
    return sp


def _router(pl, mode: str = "road"):
    """route_xy of the routing grid used by the planner's matrices (None if unavailable)."""
    m = getattr(pl, "mat", {}).get(mode) if hasattr(pl, "mat") else None
    if m is None or getattr(m, "_game", None) is None:
        return None
    from .routing import get_planner
    try:
        rp = get_planner(m._game, m.kind, m.year, m.params)
    except Exception:                           # pragma: no cover
        return None

    def route(a, b):
        try:
            return rp.route_xy((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
        except Exception:
            return None
    return route


def _has_feeder(built_stations: set, idx: int) -> bool:
    return (idx, "town_loop") in built_stations


def _has_mode_station(built_stations: set, idx: int, mode: str) -> bool:
    fam = {"road": ("road", "road_pax"), "tram": ("tram_pax",), "rail": ("rail", "rail_pax")}[mode]
    return any((idx, m) in built_stations for m in fam)


# --------------------------------------------------------------------------- intra-town lines

@dataclass
class _Ctx:
    game: Game
    pl: object
    year: int
    p: PassengerParams
    cat: facilities.Catalogue
    towns: list[TownInfo]
    M: np.ndarray
    year_s: float
    built_keys: set
    built_stations: set
    boost_value: Callable[[int, float], float]
    streets: StreetPlanner | None = None     # existing street network (export v6); None -> schematic model
    router: Callable | None = None           # routing.py route_xy on the planner's road grid


def _loop_layouts(ctx: _Ctx, t: TownInfo) -> dict[int, list[StreetLine]]:
    """n_stops -> loop variants on the town's streets ({} without a street network / streets)."""
    p = ctx.p
    if ctx.streets is None:
        return {}
    out = {}
    for n in range(p.min_intra_stops, p.max_intra_stops + 1):
        ls = ctx.streets.loop(t, n, p.stop_catchment_m, p.max_intra_stops, ctx.router if p.town_new_links else None)
        if ls:
            out[n] = ls
    return out


def _intra_options(ctx: _Ctx, t: TownInfo, mode: str, consists: list[PaxConsist]) -> ProjectCandidate | None:
    p, year = ctx.p, ctx.year
    pot = float(ctx.M[t.index, t.index])
    if pot <= 0 or not consists:
        return None
    d, w = intra_distance_pdf(t.radius)
    acc = 2 * p.access_fraction * p.stop_catchment_m * p.walk_detour / p.walk_speed
    stop_c, stop_m = small_stop(ctx.cat, year)
    street_c, street_m = _town_street(ctx.game, year)
    if (t.index, "town_loop") in ctx.built_stations:
        return None                     # the town already has its loop
    layouts = _loop_layouts(ctx, t)
    if layouts:
        options = [(n, lay) for n, ls in sorted(layouts.items()) for lay in ls]
    elif ctx.streets is not None:
        return None                     # street network known but no two stops on the town's streets
    else:                               # no street network: schematic ring (coverage of random stops)
        n0 = int(round(t.radius ** 2 / p.stop_catchment_m ** 2))
        stop_range = range(max(p.min_intra_stops, n0 - 1),
                           min(p.max_intra_stops, max(n0 + 2, p.min_intra_stops)) + 1)
        options = [(n, None) for n in stop_range]
    have_stop = (t.index, "road_pax") in ctx.built_stations or (t.index, "tram_pax") in ctx.built_stations
    best = None
    for c in consists:
        v = town_speed(c.speed)
        tpm = _ticket_per_m(ctx.game, c.price_factor, p)
        electric = mode == "tram" and any(2 in _engine_types(ctx.game).get(u.id, set()) for u, _ in c.units)
        for n_stops, lay in options:
            if lay is not None:
                loop = lay.length_m
                spacing = loop / n_stops
                drive = lay.run_time(v)
                cov = lay.coverage
                lanes_m = lay.unique_m
            else:
                spacing = t.radius * math.sqrt(math.pi / n_stops)
                loop = n_stops * spacing * 1.2
                drive = loop / v
                cov = coverage(n_stops, p.stop_catchment_m, t.radius)
                lanes_m = loop
            cycle = drive + n_stops * (STOP_DELAY_S + c.board_s * 0.5)
            v_eff = loop / max(drive, 1e-6)
            ride = d * p.ride_detour_town / v_eff + (d * p.ride_detour_town / spacing) * STOP_DELAY_S

            def evaluate(n_veh: int):
                h = cycle / n_veh
                s = capture(acc + h / 2 + ride, d, year, p)
                ws = w * s
                share = float(ws.sum())
                demand = pot * cov * cov * share
                d_mean = float((ws * d).sum() / share) if share > 0 else 0.0
                ride_len = max(d_mean * p.ride_detour_town, spacing)
                turnover = min(n_stops, loop / ride_len)
                cap_year = n_veh * c.capacity * (ctx.year_s / cycle) * turnover * p.load_factor_town
                return demand, min(demand, cap_year), cap_year, d_mean, share, h

            # Mohring start: marginal ticket loss per second of extra wait, from the capture curve
            dem1, _, _, dm1, _, _ = evaluate(1)
            dem2, *_ = evaluate(2)
            dh = cycle / 2
            vw = max(dem2 - dem1, 0.0) / max(dem1, 1e-9) / dh * tpm * dm1 if dem1 > 0 else 0.0
            n_sqrt = sqrt_rule_vehicles(cycle, dem1, c.running + c.price / p.amort_years, vw, ctx.year_s)
            for n_veh in sorted({1, 2, 3, n_sqrt - 1, n_sqrt, n_sqrt + 1, n_sqrt + 2}):
                if n_veh < 1 or n_veh > p.max_road_vehicles:
                    continue
                demand, carried, cap_year, d_mean, share, h = evaluate(n_veh)
                if carried < p.min_volume:
                    continue
                income = carried * tpm * d_mean
                n_paid = n_stops - (1 if have_stop else 0)
                infra = n_paid * stop_c
                maint = n_paid * stop_m
                depot_name = ""
                if not _has_mode_station(ctx.built_stations, t.index, mode):
                    dc, dm, depot_name = _depot(ctx.cat, ctx.game, mode, year, n_veh)
                    infra += dc
                    maint += dm
                new_roads = 0.0
                if lay is not None and lay.new_cost > 0:          # new street links (incl. demolition)
                    new_roads = lay.new_cost
                    infra += lay.new_cost
                    maint += lay.new_maint
                track = 0.0
                if mode == "tram":
                    share_t = p.tram_electric_share if electric else p.tram_track_share
                    track = lanes_m * street_c * share_t
                    infra += track
                    maint += lanes_m * street_m * share_t
                running = n_veh * c.running
                vcap = n_veh * c.price
                profit = income - running - maint
                obj = _objective(profit, vcap + infra, p)
                if best is None or obj > best[0]:
                    best = (obj, dict(c=c, n_veh=n_veh, n_stops=n_stops, loop=loop, cycle=cycle, cov=cov,
                                      demand=demand, carried=carried, cap_year=cap_year, d_mean=d_mean,
                                      share=share, h=h, income=income, running=running, maint=maint,
                                      vcap=vcap, infra=infra, depot=depot_name, track=track, n_sqrt=n_sqrt,
                                      stop_c=stop_c, n_paid=n_paid, lay=lay, new_roads=new_roads))
    if best is None:
        return None
    b = best[1]
    c = b["c"]
    lay: StreetLine | None = b["lay"]
    kind_ru = "Трамвай" if mode == "tram" else "Автобус"
    skey = "tram_pax" if mode == "tram" else "road_pax"
    details = (f"Кольцевая линия по городу {t.name}: {b['n_stops']} остановок (малые, {_fmt(b['stop_c'])} каждая), "
               f"кольцо ~{b['loop'] / 1000:.1f} км, круг {b['cycle']:.0f} с, интервал {b['h']:.0f} с. "
               f"Техника: {b['n_veh']}× {c.label}. Охват города {b['cov']:.0%}, доля поездок на линии "
               f"{b['share']:.0%}, средняя поездка {b['d_mean']:.0f} м по прямой. "
               f"Пассажиров {_fmt(b['carried'])}/год (спрос {_fmt(b['demand'])}, провозная способность "
               f"{_fmt(b['cap_year'])}).")
    depot_site = None
    pax = None
    if lay is not None:
        shape = "кольцо" if b["n_stops"] > 2 else "челнок туда-обратно по одной улице"
        stops_ru = "; ".join(f"{s['no']}) ({s['x']:.0f}, {s['y']:.0f}) {s['label']}" for s in lay.stops)
        details += (f" Маршрут по существующим улицам ({shape}, {lay.unique_m:.0f} м разных улиц): "
                    f"остановки {stops_ru}. По улицам: {lay.route_text}.")
        if lay.streets_only:
            details += " Новых дорог не строим, дома не сносим."
        else:
            details += (f" Новые участки улиц: {lay.new_m:.0f} м за {_fmt(lay.new_cost)} (снос домов "
                        f"{_fmt(lay.demolition)}).")
        if b["depot"]:
            depot_site = ctx.streets.depot_for(lay.stops, (t.x, t.y))
        pax = lay.pax_json(depot_site)
    if b["depot"]:
        details += f" {b['depot']}."
        if depot_site:
            details += f" Место депо ({depot_site['x']}, {depot_site['y']}): {depot_site['reason']}."
    if b["track"]:
        details += f" Трамвайные пути {_fmt(b['track'])}."
    meta = {"line": "intra", "town": t.name, "stops": b["n_stops"], "headway_s": round(b["h"]),
            "cycle_s": round(b["cycle"]), "coverage": round(b["cov"], 3), "capture": round(b["share"], 3),
            "avg_trip_m": round(b["d_mean"]), "demand_potential": round(float(ctx.M[t.index, t.index])),
            "demand": round(b["demand"], 1), "capacity": round(b["cap_year"], 1),
            "sqrt_rule_vehicles": b["n_sqrt"], "ticket_income": b["income"],
            "town_xp_per_year": round(5 * b["carried"]), "loop_m": round(b["loop"]),
            "new_road_cost": round(b["new_roads"])}
    if lay is not None:
        meta.update(segments=lay.segments, segment_totals=lay.totals, pax=pax)
    return ProjectCandidate(
        kind="passenger", mode=mode, title=f"{kind_ru} по городу: {t.name}", details=details,
        nodes=[t.index], cargo="passengers", volume=b["carried"], income=b["income"], running=b["running"],
        maintenance=b["maint"], vehicle_capex=b["vcap"], infra_capex=b["infra"],
        vehicles=_vehicle_lines(c, b["n_veh"]), vehicle_life_years=c.life,
        geometry=lay.geometry if lay is not None else _circle(t, 0.6 * t.radius),
        station_keys=[(t.index, skey), (t.index, "town_loop")], meta=meta,
    )


def intra_town_candidates(ctx: _Ctx, towns=None) -> list[ProjectCandidate]:
    """towns: only these town indices (None = all; towns are independent)."""
    out = []
    buses = road_consists(ctx.pl.vehicles if hasattr(ctx.pl, "vehicles") else [], ctx.year)
    trams = tram_consists(ctx.game, ctx.pl.vehicles if hasattr(ctx.pl, "vehicles") else [], ctx.year)
    only = set(towns) if towns is not None else None
    for t in ctx.towns:
        if only is not None and t.index not in only:
            continue
        for mode, cs in (("road", buses), ("tram", trams)):
            c = _intra_options(ctx, t, mode, cs)
            if c is not None:
                out.append(c)
    return out


# --------------------------------------------------------------------------- intercity lines

@dataclass
class _Way:
    """One way to run a line between two ends: length, driving time, paid construction, map data."""
    route_m: float
    build: float
    maint: float
    geometry: list
    segments: list | None
    totals: dict | None
    line: StreetLine | None
    label: str
    base_time_s: float = 0.0        # matrix travel time (lower bound) for matrix ways

    def run_time(self, v: float) -> float:
        if self.line is not None:
            return self.line.run_time(v)
        return max(self.route_m / max(v, 0.1), self.base_time_s)


def _matrix_way(ctx: _Ctx, mode: str, i: int, j: int, free: bool) -> _Way | None:
    m = ctx.pl.mat[mode]
    cache = m.__dict__.setdefault("_pax_ways", {})
    if (i, j, free) not in cache:
        cache[(i, j, free)] = _matrix_way_new(ctx, m, mode, i, j, free)
    return cache[(i, j, free)]


def _matrix_way_new(ctx: _Ctx, m, mode: str, i: int, j: int, free: bool) -> _Way | None:
    if not np.isfinite(m.length[i, j]) or not np.isfinite(m.build[i, j]):
        return None
    geom, segs, tot = None, None, None
    try:
        from .routing import line_json
        r = m.route(i, j)
        if r is not None:
            lj = line_json([r])
            geom, segs, tot = lj["geometry"], lj["segments"], lj["segment_totals"]
    except Exception:
        pass
    if not geom:
        a, b = ctx.pl.nodes[i], ctx.pl.nodes[j]
        geom = _route_geometry(ctx.pl, mode, i, j, (a.x, a.y), (b.x, b.y))
    build = 0.0 if free else float(m.build[i, j])
    maint = 0.0 if free else float(m.maint[i, j])
    t = float(m.time[i, j]) if np.isfinite(m.time[i, j]) else 0.0
    return _Way(float(m.length[i, j]), build, maint, geom, segs, tot, None,
                "трасса routing.py" + (" (уже построена)" if free and build == 0 else ""), t)


def _road_ways(ctx: _Ctx, a: TownInfo, j: int, built: bool) -> tuple[list[_Way], list[dict]]:
    """Ways for a bus between town a's central stop and node j (town: its central stop; industry: a
    road next to it): along existing roads only (no construction) and, when it builds something new
    that is worth comparing, the routing.py route (new pieces paid). Returns (ways, end stops)."""
    S = ctx.streets
    if S is None:
        return [], []
    p = ctx.p
    node = ctx.pl.nodes[j]
    ga = S.main_stop(a, p.stop_catchment_m)
    if ga is None:
        return [], []
    sa = S.stop_dict(ga, a, no=1)
    if j < len(ctx.towns):
        b = ctx.towns[j]
        gb = S.main_stop(b, p.stop_catchment_m)
        sb = S.stop_dict(gb, b, no=2) if gb is not None else None
        b_xy = (sb["x"], sb["y"]) if sb else (b.x, b.y)
    else:
        gb = S.snap(node.x, node.y, max_dist=INDUSTRY_STOP_M)
        sb = None
        if gb is not None:
            sb = S.stop_dict(gb, None, no=2)
            sb["where"] = f"{math.dist((sb['x'], sb['y']), (node.x, node.y)):.0f} м от предприятия «{node.name}»"
            sb["label"] = f"{sb['street']} (ребро {sb['edge_id']}), {sb['where']}"
        b_xy = (sb["x"], sb["y"]) if sb else (node.x, node.y)
    ways = []
    if sb is not None:
        ln = S.street_path_line(ga, gb, [sa, sb])
        if ln is not None:
            ways.append(_Way(ln.length_m, 0.0, 0.0, ln.geometry, ln.segments, ln.totals, ln,
                             "только по существующим дорогам"))
    end_b = sb or {"no": 2, "x": round(b_xy[0], 1), "y": round(b_xy[1], 1), "street": "новая подъездная дорога",
                   "label": f"новая подъездная дорога у «{node.name}» (строится)", "edge": None}
    ln = S.route_line((sa["x"], sa["y"]), b_xy, [sa, end_b], ctx.router)
    if ln is not None:
        build = 0.0 if built else ln.new_cost
        ways.append(_Way(ln.length_m, build, 0.0 if built else ln.new_maint, ln.geometry, ln.segments, ln.totals, ln,
                         "по существующим дорогам + новые участки (routing.py)"))
    return ways, [sa, end_b]


def extra_town_stops(sp: StreetPlanner | None, geom: list, t: TownInfo, from_start: bool, k: int) -> list[dict]:
    """k - 1 more stops of an intercity bus inside town t, along its route, snapped to the street."""
    out = []
    if k <= 1 or not geom or len(geom) < 2 or sp is None:
        return out
    P = np.asarray(geom if from_start else geom[::-1], float)
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
    step = min(350.0, cum[-1] / (2 * k + 1))
    for q in range(1, k):
        s = q * step
        x, y = float(np.interp(s, cum, P[:, 0])), float(np.interp(s, cum, P[:, 1]))
        g = sp.snap(x, y, max_dist=40.0)
        if g is not None:
            out.append(sp.stop_dict(g, t, no=q + 1))
    return out


def _intercity(ctx: _Ctx, a: TownInfo, b: TownInfo, mode: str, consists: list[PaxConsist]) -> ProjectCandidate | None:
    p, year = ctx.p, ctx.year
    i, j = a.index, b.index
    key = (min(i, j), max(i, j), mode)
    built = key in ctx.built_keys
    pot = float(ctx.M[i, j] + ctx.M[j, i])
    if pot <= 0 or not consists:
        return None
    ways, end_stops = (_road_ways(ctx, a, j, built) if mode == "road" else ([], []))
    if not ways:
        if mode == "road" and ctx.streets is not None and not built:
            return None             # no road without demolishing houses (unpriced): no bus line
        w0 = _matrix_way(ctx, mode, i, j, built or (mode == "road" and p.town_roads_exist))
        if w0 is None:
            return None
        ways = [w0]
    pa, pb = (a.x, a.y, a.z), (b.x, b.y, b.z)
    straight = math.dist(pa[:2], pb[:2])
    way_speed = max((w.speed for w in ctx.game.ways_available(mode, year)), default=30.0)
    catch = p.stop_catchment_m if mode == "road" else p.rail_catchment_m
    acc = 2 * p.access_fraction * catch * p.walk_detour / p.walk_speed
    feed = {i: _has_feeder(ctx.built_stations, i), j: _has_feeder(ctx.built_stations, j)}
    stop_c, stop_m = small_stop(ctx.cat, year)
    best = None
    k_range = (1, 2, 3) if mode == "road" else (1,)
    for c in consists:
        v = 0.85 * min(c.speed, way_speed)
        ticket = _ticket(ctx.game, pa, pb, c.price_factor, p)
        dwell = 2 * STOP_DELAY_S + c.board_s
        for way in ways:
            t_run = way.run_time(v)
            for k in k_range:
                # extra stops per town: in-town detour between them at town speed
                extra = 0.0
                covs = []
                for t in (a, b):
                    spacing = t.radius * math.sqrt(math.pi / k)
                    extra += (k - 1) * spacing * 1.2 / town_speed(c.speed)
                    own = coverage(k, catch, t.radius)
                    if feed[t.index]:
                        own = max(own, p.feeder_coverage * p.transfer_factor)
                    covs.append(own)
                one_way = t_run + extra + (2 * k - 1) * dwell
                cycle = 2 * one_way
                per_veh = 2 * c.capacity * ctx.year_s / cycle * p.load_factor_intercity
                transfer = p.transfer_wait_s if (feed[i] or feed[j]) else 0.0
                max_n = p.max_road_vehicles if mode == "road" else p.max_trains
                for n in range(1, max_n + 1):
                    h = cycle / n
                    s = float(capture(acc + h / 2 + one_way + transfer, straight, year, p))
                    demand = pot * covs[0] * covs[1] * s
                    cap_year = n * per_veh
                    carried = min(demand, cap_year)
                    if carried < p.min_volume:
                        if cap_year >= demand:
                            break
                        continue
                    income = carried * ticket
                    infra, maint = way.build, way.maint
                    st_name = ""
                    if mode == "road":
                        for t in (a, b):
                            if (t.index, "road_pax") not in ctx.built_stations:
                                infra += k * stop_c
                                maint += k * stop_m
                        st_name = f"{k} малые остановки в каждом городе" if k > 1 else "малая остановка в каждом городе"
                    else:
                        length = c.n_units * VEHICLE_LENGTH_M
                        occ = n * (dwell + 2 * STOP_DELAY_S) / cycle
                        terms = max(1, math.ceil(occ / MAX_OCCUPANCY))
                        sc, sm, st_name = rail_pax_station(ctx.cat, year, length, terms)
                        for t in (a, b):
                            if (t.index, "rail_pax") not in ctx.built_stations:
                                infra += sc
                                maint += sm
                    depot_name = ""
                    if not (_has_mode_station(ctx.built_stations, i, mode) or _has_mode_station(ctx.built_stations, j, mode)):
                        dc, dm, depot_name = _depot(ctx.cat, ctx.game, mode, year, n)
                        infra += dc
                        maint += dm
                    running = n * c.running
                    vcap = n * c.price
                    profit = income - running - maint
                    obj = _objective(profit, vcap + infra, p)
                    if best is None or obj > best[0]:
                        best = (obj, dict(c=c, n=n, k=k, cycle=cycle, h=h, s=s, covs=covs, demand=demand,
                                          carried=carried, cap_year=cap_year, income=income, running=running,
                                          maint=maint, vcap=vcap, infra=infra, ticket=ticket, st_name=st_name,
                                          depot=depot_name, route_build=way.build, way=way))
                    if cap_year >= demand:
                        break           # more vehicles only add cost (capture barely depends on headway here)
    if best is None:
        return None
    bb = best[1]
    c = bb["c"]
    way: _Way = bb["way"]
    route_m = way.route_m
    near = sorted(ctx.towns, key=lambda t: math.dist((a.x, a.y), (t.x, t.y)))[1:5]
    tier = 1 if b in near else 2
    if mode == "road":
        title = f"Автобус: {a.name} ↔ {b.name}"
        skey = "road_pax"
    else:
        title = f"Пассажирский поезд: {a.name} ↔ {b.name}"
        skey = "rail_pax"
    feeders = [t.name for t in (a, b) if feed[t.index]]
    details = (f"{straight / 1000:.1f} км по прямой, маршрут {route_m / 1000:.1f} км"
               f"{' (дорога уже есть)' if mode == 'road' and bb['route_build'] == 0 else ''}. "
               f"Состав: {bb['n']}× {c.label}, круг {bb['cycle']:.0f} с, интервал {bb['h']:.0f} с. "
               f"Билет {_fmt(bb['ticket'])} за пассажира. Станции: {bb['st_name']}. "
               f"Охват городов {bb['covs'][0]:.0%} / {bb['covs'][1]:.0%}"
               f"{' (с подвозом: ' + ', '.join(feeders) + ')' if feeders else ''}. "
               f"Пассажиров {_fmt(bb['carried'])}/год (спрос {_fmt(bb['demand'])}, провозная способность "
               f"{_fmt(bb['cap_year'])}).")
    pax = None
    tot = way.totals or {}
    if way.line is not None:
        k = bb["k"]
        sa, sb = end_stops
        stops = ([dict(sa, no=1)] + extra_town_stops(ctx.streets, way.geometry, a, True, k)
                 + extra_town_stops(ctx.streets, way.geometry, b, False, k)[::-1] + [dict(sb)])
        for q, st in enumerate(stops):
            st["no"] = q + 1
        depot_site = ctx.streets.depot_for([sa], (a.x, a.y)) if bb["depot"] else None
        pax = dict(way.line.pax_json(depot_site), stops=stops)
        details += (f" Остановки на улицах: {a.name} — ({sa['x']:.0f}, {sa['y']:.0f}) {sa['label']}; "
                    f"{b.name} — ({sb['x']:.0f}, {sb['y']:.0f}) {sb['label']}. "
                    f"Путь: {way.label}, {tot.get('existing_m', 0) / 1000:.1f} км по существующим дорогам")
        if tot.get("new_m"):
            details += (f", новые участки {tot['new_m'] / 1000:.2f} км за {_fmt(way.line.new_cost)}"
                        f"{' (уже построены)' if bb['route_build'] == 0 else ''}.")
            if way.line.demolition > 0:
                details += f" В том числе снос домов {_fmt(way.line.demolition)}."
            if way.line.unpriced_built_m > 0:
                details += (f" Из них ~{way.line.unpriced_built_m:.0f} м через застройку — снос домов в цене "
                            f"не учтён (нет данных о зданиях).")
        else:
            details += ". Новых дорог не строим."
        if depot_site:
            details += f" Место депо ({depot_site['x']}, {depot_site['y']}): {depot_site['reason']}."
    elif tot:
        details += (f" Трасса: {tot.get('existing_m', 0) / 1000:.1f} км по существующим путям/дорогам, "
                    f"{tot.get('new_m', 0) / 1000:.1f} км строить.")
    if bb["depot"]:
        details += f" {bb['depot']}."
    if not feeders:
        details += " Городские автобусные кольца в обоих городах резко увеличат охват."
    meta = {"line": "intercity", "straight_m": round(straight), "route_m": round(route_m),
            "headway_s": round(bb["h"]), "cycle_s": round(bb["cycle"]), "capture": round(bb["s"], 3),
            "coverage": [round(x, 3) for x in bb["covs"]], "stops_per_town": bb["k"],
            "demand_potential": round(pot, 1), "demand": round(bb["demand"], 1),
            "capacity": round(bb["cap_year"], 1), "ticket": round(bb["ticket"]), "feeders": feeders,
            "ticket_income": bb["income"], "town_xp_per_year": round(5 * 2 ** tier * bb["carried"]),
            "route_build": round(bb["route_build"]), "way": way.label}
    if way.segments:
        meta.update(segments=way.segments, segment_totals=way.totals)
    if pax:
        meta["pax"] = pax
    return ProjectCandidate(
        kind="passenger", mode=mode, title=title, details=details, nodes=[i, j], cargo="passengers",
        volume=bb["carried"], income=bb["income"], running=bb["running"], maintenance=bb["maint"],
        vehicle_capex=bb["vcap"], infra_capex=bb["infra"], vehicles=_vehicle_lines(c, bb["n"]),
        vehicle_life_years=c.life, geometry=way.geometry,
        pair_keys=[key] if bb["route_build"] > 0 else [], station_keys=[(i, skey), (j, skey)], meta=meta,
    )


def intercity_pairs(ctx: _Ctx) -> list[tuple[int, int]]:
    """Town pairs intercity_candidates() evaluates, in its order."""
    pairs = set()
    for a in ctx.towns:
        near = sorted((math.dist((a.x, a.y), (b.x, b.y)), b.index) for b in ctx.towns if b is not a)
        for dist, j in near[:ctx.p.intercity_neighbours]:
            if dist <= ctx.p.intercity_max_m:
                pairs.add((min(a.index, j), max(a.index, j)))
    return sorted(pairs)


def intercity_candidates(ctx: _Ctx, pairs=None) -> list[ProjectCandidate]:
    """pairs: only these town pairs (None = all; pairs are independent)."""
    vehicles = getattr(ctx.pl, "vehicles", [])
    buses = road_consists(vehicles, ctx.year)
    trains = rail_consists(vehicles, ctx.year, ctx.p, game=getattr(getattr(ctx, 'pl', None), 'game', None))
    only = set(pairs) if pairs is not None else None
    out = []
    for i, j in intercity_pairs(ctx):
        if only is not None and (i, j) not in only:
            continue
        a, b = ctx.towns[i], ctx.towns[j]
        for mode, cs in (("road", buses), ("rail", trains)):
            if mode not in ctx.pl.mat:
                continue
            c = _intercity(ctx, a, b, mode, cs)
            if c is not None:
                out.append(c)
    return out


# --------------------------------------------------------------------------- bundles (intercity + feeders)

def _merge(parts: list[ProjectCandidate], **kw) -> ProjectCandidate:
    vehicles, pair_keys, station_keys, legs, segments = [], [], [], [], []
    for li, c in enumerate(parts):
        vehicles += c.vehicles
        pair_keys += [k for k in c.pair_keys if k not in pair_keys]
        station_keys += [k for k in c.station_keys if k not in station_keys]
        leg = {"title": c.title, "mode": c.mode, "kind": c.kind, "nodes": c.nodes, "volume": round(c.volume, 1),
               "income": round(c.income), "running": round(c.running), "maintenance": round(c.maintenance),
               "vehicle_capex": round(c.vehicle_capex), "infra_capex": round(c.infra_capex),
               "vehicles": [vars(v) for v in c.vehicles], "geometry": c.geometry, "details": c.details}
        m = c.meta or {}
        if m.get("segments"):
            leg["segments"] = m["segments"]
            leg["segment_totals"] = m.get("segment_totals")
            segments += [dict(g, leg=li) for g in m["segments"]]
        if m.get("pax"):
            leg["pax"] = m["pax"]
        legs.append(leg)
    if segments:
        from .routing import segment_totals
        meta = dict(kw.pop("meta", None) or {})
        meta.update(segments=segments, segment_totals=segment_totals(segments))
        kw["meta"] = meta
    vcap = sum(c.vehicle_capex for c in parts)
    life = (sum(c.vehicle_capex * c.vehicle_life_years for c in parts) / vcap) if vcap else 30.0
    return ProjectCandidate(
        vehicle_capex=vcap, infra_capex=sum(c.infra_capex for c in parts),
        running=sum(c.running for c in parts), maintenance=sum(c.maintenance for c in parts),
        vehicles=vehicles, vehicle_life_years=life, pair_keys=pair_keys, station_keys=station_keys, legs=legs, **kw)


def bundle_candidates(ctx: _Ctx, intra: list[ProjectCandidate], inter: list[ProjectCandidate]) -> list[ProjectCandidate]:
    """Intercity line + the town loops it needs at ends that do not have one yet."""
    p = ctx.p
    best_loop: dict[int, ProjectCandidate] = {}
    for c in intra:
        t = c.nodes[0]
        if t not in best_loop or _objective(c.profit, c.capex, p) > _objective(best_loop[t].profit, best_loop[t].capex, p):
            best_loop[t] = c
    out = []
    consists: dict[str, list] = {}
    for base in inter:
        i, j = base.nodes
        missing = [t for t in (i, j) if not _has_feeder(ctx.built_stations, t) and t in best_loop]
        if not missing:
            continue
        stations = set(ctx.built_stations)
        for t in missing:
            stations |= set(best_loop[t].station_keys)
        ctx2 = replace(ctx, built_stations=stations)
        mode = base.mode
        if mode not in consists:
            consists[mode] = (road_consists(ctx.pl.vehicles, ctx.year) if mode == "road" else
                              rail_consists(ctx.pl.vehicles, ctx.year, p, game=getattr(ctx.pl, "game", None)))
        cs = consists[mode]
        line = _intercity(ctx2, ctx.towns[i], ctx.towns[j], mode, cs)
        if line is None:
            continue
        loops = [best_loop[t] for t in missing]
        # transfer passengers also ride the loop (own ticket, ~0.6 R straight) within its spare capacity
        extra_income, extra_volume = 0.0, 0.0
        own_cov = base.meta["coverage"]
        new_cov = line.meta["coverage"]
        for k, t in enumerate((i, j)):
            loop = best_loop.get(t)
            if loop is None or t not in missing or new_cov[k] <= 0:
                continue
            via = line.volume * max(0.0, 1 - own_cov[k] / new_cov[k])
            spare = max(0.0, loop.meta["capacity"] - loop.volume)
            via = min(via, spare)
            pf = 0.5
            extra_income += via * _ticket_per_m(ctx.game, pf, p) * 0.6 * ctx.towns[t].radius
            extra_volume += via
        parts = loops + [line]
        names = " ↔ ".join(ctx.towns[t].name for t in (i, j))
        what = "автобус" if mode == "road" else "поезд"
        loops_ru = ", ".join(ctx.towns[t].name for t in missing)
        out.append(_merge(
            parts, kind="passenger", mode="multi",
            title=f"Пассажирская сеть: {names} ({what} + городские кольца)",
            details=(f"Междугородняя линия ({what}) вместе с городскими кольцами в: {loops_ru}. "
                     f"Кольца подвозят пассажиров к станциям и поднимают охват городов "
                     f"{own_cov[0]:.0%}/{own_cov[1]:.0%} → {new_cov[0]:.0%}/{new_cov[1]:.0%}. "
                     f"Пересаживающихся на кольцах: {_fmt(extra_volume)}/год (+{_fmt(extra_income)}/год). "
                     + " | ".join(c.title for c in parts)),
            nodes=[i, j], cargo="passengers", volume=sum(c.volume for c in parts) + extra_volume,
            income=sum(c.income for c in parts) + extra_income, geometry=line.geometry,
            meta={"line": "bundle", "intercity_mode": mode, "loops": missing, "transfer_volume": round(extra_volume, 1),
                  "transfer_income": extra_income, "intercity_volume": round(line.volume, 1),
                  "ticket_income": sum(c.meta.get("ticket_income", c.income) for c in parts) + extra_income,
                  "town_xp_per_year": sum(c.meta.get("town_xp_per_year", 0) for c in parts)}))
    return out


# --------------------------------------------------------------------------- worker lines

def _worker_line(ctx: _Ctx, t: TownInfo, k_idx: int, buses: list[PaxConsist]) -> ProjectCandidate | None:
    p, year = ctx.p, ctx.year
    node = ctx.pl.nodes[k_idx]
    i = t.index
    key = (min(i, k_idx), max(i, k_idx), "road")
    legs = float(ctx.M[i, k_idx])           # commuting legs/year (both directions)
    workers = legs / 2                      # arrivals at the industry per year
    if legs < 1:
        return None
    ways, end_stops = _road_ways(ctx, t, k_idx, key in ctx.built_keys)
    if not ways:
        w0 = _matrix_way(ctx, "road", i, k_idx, key in ctx.built_keys)
        if w0 is None:
            return None
        ways = [w0]
    pa, pb = (t.x, t.y, t.z), (node.x, node.y, node.z)
    straight = math.dist(pa[:2], pb[:2])
    way_speed = max((w.speed for w in ctx.game.ways_available("road", year)), default=16.7)
    stop_c, stop_m = small_stop(ctx.cat, year)
    gain_full = ctx.boost_value(k_idx, p.worker_boost_factor) or 0.0
    best = None
    for c, way in ((c, w) for c in buses for w in ways):
        v = 0.85 * min(c.speed, way_speed)
        ticket = _ticket(ctx.game, pa, pb, c.price_factor, p)
        cycle = 2 * (way.run_time(v) + 2 * STOP_DELAY_S + c.board_s)
        per_veh = 2 * c.capacity * ctx.year_s / cycle * p.load_factor_intercity
        route_build, route_maint = way.build, way.maint
        for n in range(1, 5):
            cap_year = n * per_veh
            carried = min(legs, cap_year)
            arrivals = carried / 2
            # destinations are recomputed every cycle (probability 1.0), so arrivals are drawn from the
            # whole town population rather than a fixed pool of 20 workers
            pool = max(t.residents, 1.0)
            distinct = pool * (1 - math.exp(-p.worker_window_years * arrivals / pool))
            boosted = distinct >= p.min_workers
            ticket_income = carried * ticket
            boost = gain_full if boosted else 0.0
            infra, maint = route_build, route_maint
            if (i, "road_pax") not in ctx.built_stations:
                infra += stop_c
                maint += stop_m
            if (k_idx, "road_pax") not in ctx.built_stations:
                infra += stop_c
                maint += stop_m
            depot_name = ""
            if not any(_has_mode_station(ctx.built_stations, x, "road") for x in (i, k_idx)):
                dc, dm, depot_name = _depot(ctx.cat, ctx.game, "road", year, n)
                infra += dc
                maint += dm
            running = n * c.running
            vcap = n * c.price
            profit = ticket_income + boost - running - maint
            obj = _objective(profit, vcap + infra, p) + (1e-3 if boosted else 0.0)
            if best is None or obj > best[0]:
                best = (obj, dict(c=c, n=n, cycle=cycle, carried=carried, cap_year=cap_year, distinct=distinct,
                                  boosted=boosted, ticket_income=ticket_income, boost=boost, running=running,
                                  maint=maint, vcap=vcap, infra=infra, ticket=ticket, depot=depot_name,
                                  route_build=route_build, way=way))
            if cap_year >= legs:
                break
    if best is None:
        return None
    bb = best[1]
    c = bb["c"]
    way = bb["way"]
    route_m = way.route_m
    details = (f"Возить рабочих из {t.name} на «{node.name}»: {straight / 1000:.1f} км по прямой, "
               f"маршрут {route_m / 1000:.1f} км{' (дорогу нужно построить)' if bb['route_build'] > 0 else ''}. "
               f"{bb['n']}× {c.label}, круг {bb['cycle']:.0f} с. Ожидается {workers:.1f} поездок на работу в год, "
               f"разных рабочих за 2 года ~{bb['distinct']:.0f} (нужно ≥{p.min_workers}). "
               f"Пассажиров {_fmt(bb['carried'])}/год, билеты {_fmt(bb['ticket_income'])}/год. "
               f"Две малые остановки.")
    pax = None
    if way.line is not None:
        sa, sb = end_stops
        tot = way.totals or {}
        details += (f" Остановки: {t.name} — ({sa['x']:.0f}, {sa['y']:.0f}) {sa['label']}; у предприятия — "
                    f"({sb['x']:.0f}, {sb['y']:.0f}) {sb['label']}. Путь: {way.label}"
                    + (f", новые участки {tot['new_m']:.0f} м." if tot.get("new_m") else ", новых дорог не строим."))
        depot_site = ctx.streets.depot_for([sa, sb], (t.x, t.y)) if bb["depot"] else None
        if depot_site:
            details += f" Место депо ({depot_site['x']}, {depot_site['y']}): {depot_site['reason']}."
        pax = way.line.pax_json(depot_site)
    if bb["boosted"]:
        details += (f" Бонус производства ×{p.worker_boost_factor:.1f}"
                    f"{' (+' + _fmt(bb['boost']) + '/год прибыли)' if bb['boost'] else ''}.")
    else:
        details += " Рабочих меньше порога — бонуса производства не будет."
    if bb["depot"]:
        details += f" {bb['depot']}."
    return ProjectCandidate(
        kind="workers", mode="road", title=f"Рабочие: {t.name} → {node.name}", details=details,
        nodes=[i, k_idx], cargo="passengers", volume=bb["carried"],
        income=bb["ticket_income"] + bb["boost"], running=bb["running"], maintenance=bb["maint"],
        vehicle_capex=bb["vcap"], infra_capex=bb["infra"], vehicles=_vehicle_lines(c, bb["n"]),
        vehicle_life_years=c.life, geometry=way.geometry,
        pair_keys=[key] if bb["route_build"] > 0 else [], station_keys=[(i, "road_pax"), (k_idx, "road_pax")],
        boosts={(k_idx, "workers"): p.worker_boost_factor} if bb["boosted"] else {},
        meta={"line": "workers", "straight_m": round(straight), "route_m": round(route_m),
              "workers": round(workers, 1), "distinct_workers_2y": round(bb["distinct"], 1),
              "boosted": bb["boosted"], "boost_value": bb["boost"], "ticket_income": bb["ticket_income"],
              "capacity": round(bb["cap_year"], 1), "ticket": round(bb["ticket"]),
              "town_xp_per_year": round(5 * bb["carried"] / 2), "route_build": round(bb["route_build"]),
              **({"segments": way.segments, "segment_totals": way.totals} if way.segments else {}),
              **({"pax": pax} if pax else {})},
    )


def worker_candidates(ctx: _Ctx) -> list[ProjectCandidate]:
    buses = road_consists(getattr(ctx.pl, "vehicles", []), ctx.year)
    if "road" not in ctx.pl.mat or not buses:
        return []
    nt = len(ctx.towns)
    out = []
    for k_idx in range(nt, len(ctx.pl.nodes)):
        node = ctx.pl.nodes[k_idx]
        if getattr(node, "water_only", False):
            continue
        near = sorted((math.dist((t.x, t.y), (node.x, node.y)), t.index) for t in ctx.towns)
        near = [(d, i) for d, i in near if d <= ctx.p.worker_range_m][:ctx.p.worker_towns_per_industry]
        for _d, i in near:
            c = _worker_line(ctx, ctx.towns[i], k_idx, buses)
            if c is not None:
                out.append(c)
    return out


# --------------------------------------------------------------------------- entry point

def generate(game: Game, vehicles: list[Vehicle], planner_like, year: int, built_keys: set | None = None,
             built_stations: set | None = None, boost_value: Callable[[int, float], float] | None = None,
             params: PassengerParams | None = None) -> list[ProjectCandidate]:
    """All passenger ('passenger') and worker ('workers') candidates for `year`.

    planner_like needs .nodes (towns first), .mat {'road'|'rail': NetworkMatrix}; .town/.ind are optional.
    built_keys: infrastructure (min, max, mode) already built -> route cost 0.
    built_stations: (node, mode) stations already built; passenger keys use modes 'road_pax', 'tram_pax',
    'rail_pax' and 'town_loop' (an intra-town loop exists); cargo keys 'road'/'rail' at a node make the
    depot free (same rule as the cargo planner).
    boost_value(industry_node_index, factor) -> yearly profit gain of the worker boost (default 0).
    """
    ctx = make_context(game, vehicles, planner_like, year, built_keys, built_stations, boost_value, params)
    intra = intra_town_candidates(ctx)
    inter = intercity_candidates(ctx)
    out = intra + inter + bundle_candidates(ctx, intra, inter) + worker_candidates(ctx)
    return finish(ctx, out)


def finish(ctx: _Ctx, out: list[ProjectCandidate]) -> list[ProjectCandidate]:
    """generate()'s final filter (also for outputs assembled from split parts)."""
    return [c for c in out if c.volume >= ctx.p.min_volume and c.income > 0]


def make_context(game: Game, vehicles: list[Vehicle], planner_like, year: int, built_keys: set | None = None,
                 built_stations: set | None = None, boost_value: Callable[[int, float], float] | None = None,
                 params: PassengerParams | None = None) -> _Ctx:
    """The generate() context: demand matrix, town infos, street planner, routing (see generate)."""
    p = params or PassengerParams()
    pl = planner_like
    if getattr(pl, "vehicles", None) is not vehicles:
        pl = SimpleNamespace(nodes=planner_like.nodes, mat=planner_like.mat, vehicles=vehicles,
                             town=getattr(planner_like, "town", None), ind=getattr(planner_like, "ind", None))
    nodes, M = demand_matrix(game, p, year, nodes=pl.nodes)
    towns = town_infos(game, p)
    return _Ctx(game, pl, year, p, _catalogue(game), towns, M, game.year_ms / 1000.0,
                set(built_keys or ()), set(built_stations or ()), boost_value or (lambda _k, _f: 0.0),
                street_planner(game, towns) if p.use_streets else None, _router(pl, "road"))


def light_planner(game: Game | None = None):
    """Minimal planner-like object (nodes, routing matrices, vehicles) without building a cargo plan."""
    from .routing import RoutingParams, load_game, network_matrix, network_nodes
    from .vehicles import load_vehicles
    game = game or load_game()
    params = RoutingParams()
    return SimpleNamespace(
        game=game, vehicles=load_vehicles(game), nodes=network_nodes(game),
        mat={k: network_matrix(game, k, game.year, params) for k in ("rail", "road")},
        town={t.id: t for t in game.towns}, ind={i.id: i for i in game.industries})


def main():
    import time
    t0 = time.time()
    pl = light_planner()
    game = pl.game
    t1 = time.time()
    cands = generate(game, pl.vehicles, pl, game.year, set(), set())
    t2 = time.time()
    print(f"{len(cands)} candidates for {game.year} (setup {t1 - t0:.1f}s, generate {t2 - t1:.1f}s): "
          + ", ".join(f"{k} {sum(1 for c in cands if (c.kind, c.mode) == k)}"
                      for k in sorted({(c.kind, c.mode) for c in cands})))
    print(f"{'payback':>7} {'capex':>9} {'income':>9} {'profit':>9} {'pax/y':>6} {'veh':>4} nlm  title")
    for c in sorted(cands, key=lambda c: c.payback_years)[:15]:
        nveh = sum(v.count for v in c.vehicles)
        print(f"{c.payback_years:7.2f} {c.capex / 1e3:8.0f}k {c.income / 1e3:8.0f}k {c.profit / 1e3:8.0f}k "
              f"{c.volume:6.0f} {nveh:4d} {'yes' if c.not_loss_making else 'no ':3}  {c.title}")
    print("\nWith feeders: intercity lines re-evaluated assuming every town has a bus loop:")
    feeders = {(t, m) for t in range(len(game.towns)) for m in ("road_pax", "town_loop")}
    ic = [c for c in generate(game, pl.vehicles, pl, game.year, set(), feeders) if c.meta.get("line") == "intercity"]
    for c in sorted(ic, key=lambda c: c.payback_years)[:5]:
        print(f"{c.payback_years:7.2f} {c.capex / 1e3:8.0f}k {c.income / 1e3:8.0f}k {c.profit / 1e3:8.0f}k "
              f"{c.volume:6.0f} {sum(v.count for v in c.vehicles):4d} {'yes' if c.not_loss_making else 'no ':3}  {c.title}")
    s = demand_summary(game)
    print("\nDemand summary (legs/year):")
    for k, v in s.items():
        if k != "top_pairs":
            print(f"  {k}: {v if not isinstance(v, float) else round(v, 1)}")
    print("  top intercity pairs (both directions, legs/y, straight m):")
    for row in s["top_pairs"]:
        print("   ", row)


if __name__ == "__main__":
    main()
