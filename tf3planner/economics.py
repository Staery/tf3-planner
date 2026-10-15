"""Economics of single transport links: production volumes, vehicle choice, fleet size,
yearly profit and payback. Pure functions over Game/Vehicle; routing supplies real lengths.

Calibration notes (see memory/project notes; refine as in-game data arrives):
- Max production per year = runtime rule output * rule capacity * consumptionFactor
  * industryProductivity (0.5 on VeryHard); matches the game for all 82 cargo figures (v3 export).
- Boosts (in-game window): booster cargo (tools/machines/fertilizer) gives (1 + boostFactor),
  e.g. +111..118 %; delivering workers by public transport gives up to +131 %.
- Primary industries only keep producing while their output stock is emptied; a full stock
  destroys new production, so an unserved industry yields nothing.
- One game year = yearDurationMs (1461 s of simulation); vehicle speeds are in m/s of the same clock.
- Stations/depots: concrete modular buildings sized per line (facilities.py, export v4 catalogue).
- Late cargo is paid in full; lateness only hurts town delivery ratings, so the delivery limit
  is enforced for town destinations only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .model import Cargo, Game, Industry, Town
from .vehicles import Vehicle, available
from . import facilities

INDUSTRY_PRODUCTIVITY = 0.5          # VeryHard preset (level2IndustryProductivityFactor[1])
WORKER_BOOST = 1.31                  # +131 % seen in the industry window
STOP_DELAY_S = 4.0                   # arrival + departure delay per stop (2000 ms each)
DETOUR_DEFAULT = 1.3                 # route length / straight distance when no route is known
LOAD_FACTOR_RETURN = 0.0             # cargo lines usually return empty

VEHICLE_LENGTH_M = 15.0              # per rail unit (loco or wagon) for platform sizing (ASSUMPTION)
MAX_OCCUPANCY = 0.7                  # planned terminal occupancy (docs/real_logistics.md 10.1)


# --------------------------------------------------------------------------- production

def max_production(game: Game, ind: Industry, cargo_key: str, boosted: bool = False, workers: bool = False) -> float:
    """Max units/year of `cargo_key` the industry outputs (or consumes, for inputs).

    Mirrors gui/main/industry_util.tl getIndustryProductionData on the runtime stock list:
    output[c] (or input[stock]) * rule.capacity * consumptionFactor, times industryProductivity
    for non-booster rules. Runtime rule outputs already include the industry level.
    Verified against api.engine.util.stock.getCargoMaxProductionPerYear for all 48 industries.
    """
    raw = next((i for i in game.raw["industries"] if i["id"] == ind.id), {})
    sl = raw.get("stockList") or {}
    rules = sl.get("rules") if isinstance(sl.get("rules"), list) else []
    stocks = sl.get("stocks") if isinstance(sl.get("stocks"), list) else []
    cid = game.cargo_by_key[cargo_key].id if cargo_key in game.cargo_by_key else None
    stock_idx = [j for j, s in enumerate(stocks) if s.get("cargoType") == cid]
    best_out, best_in, boost = 0.0, 0.0, 1.0
    for r in rules:
        cap = (r.get("capacity") or 0) * (r.get("consumptionFactor") or 1.0)
        if r.get("booster"):
            if boosted:
                boost *= 1 + (r.get("boostFactor") or 0)
            continue
        out = r.get("output") or {}
        amount = out.get(str(cid), out.get(cid, 0)) if isinstance(out, dict) else 0
        if amount:
            best_out += amount * cap * INDUSTRY_PRODUCTIVITY
        inp = (r.get("input") or [[]])[0] or []
        for j in stock_idx:
            if j < len(inp) and inp[j]:
                best_in = max(best_in, inp[j] * cap * INDUSTRY_PRODUCTIVITY)
    if workers:
        boost *= 1 + WORKER_BOOST
    return (best_out if best_out else best_in) * boost


def booster_info(game: Game, ind: Industry) -> dict[str, tuple[float, float]]:
    """Booster cargos of an industry: cargo -> (units/year consumed while boosted, boostFactor).

    Booster rules are not scaled by industryProductivity (clay pit tools: 12 * 4 = 48/y in game).
    While supplied, the industry's main production is multiplied by (1 + boostFactor).
    """
    raw = next((i for i in game.raw["industries"] if i["id"] == ind.id), {})
    sl = raw.get("stockList") or {}
    rules = sl.get("rules") if isinstance(sl.get("rules"), list) else []
    stocks = sl.get("stocks") if isinstance(sl.get("stocks"), list) else []
    by_id = {c.id: c.key for c in game.cargos.values()}
    out: dict[str, tuple[float, float]] = {}
    for r in rules:
        if not r.get("booster"):
            continue
        inp = (r.get("input") or [[]])[0] or []
        cap = (r.get("capacity") or 0) * (r.get("consumptionFactor") or 1.0)
        for j, amount in enumerate(inp):
            if amount and j < len(stocks):
                key = by_id.get(stocks[j].get("cargoType"))
                if key:
                    out[key] = (amount * cap, r.get("boostFactor") or 0.0)
    return out


def conversion(ind: Industry) -> dict[str, dict[str, float]]:
    """For processing industries: output units produced per unit of each input (first alternative)."""
    out: dict[str, dict[str, float]] = {}
    for r in (ind.recipe or {}).get("rules", []):
        if r["booster"] or not r["input"]:
            continue
        alt = r["input"][0]
        need = {k: v for k, v in alt.items() if v}
        for c_out, n_out in r["output"].items():
            out[c_out] = {c_in: n_out / n_in for c_in, n_in in need.items()}
    return out


# --------------------------------------------------------------------------- links

@dataclass
class Endpoint:
    kind: str          # "industry" | "town"
    id: int
    name: str
    x: float
    y: float
    z: float


def endpoint(o: Industry | Town) -> Endpoint:
    return Endpoint("industry" if isinstance(o, Industry) else "town", o.id, o.name, o.x, o.y, o.z)


@dataclass
class Consist:
    """A vehicle set running on a line: one road vehicle, a ship, or a loco + n wagons."""
    mode: str
    units: list[tuple[Vehicle, int]]
    capacity: int
    speed: float
    price: float
    running_cost: float
    load_speed: float

    @property
    def label(self) -> str:
        return " + ".join(f"{n}x {v.name}" if n > 1 else v.name for v, n in self.units)


@dataclass
class LinkEval:
    cargo: str
    src: Endpoint
    dst: Endpoint
    mode: str
    year: int
    volume: float                  # units/year moved
    straight_m: float
    route_m: float
    income_per_unit: float
    consist: Consist | None
    n_consists: int
    trip_time_s: float
    income: float                  # per year
    vehicle_capex: float
    infra_capex: float
    running: float                 # vehicles per year
    maintenance: float             # infrastructure per year
    notes: list[str] = field(default_factory=list)
    station_each: float = 0.0      # cost of one end's station (for sharing)
    station_maint_each: float = 0.0
    station_name: str = ""
    depot_cost: float = 0.0
    depot_name: str = ""
    route_build: float = 0.0

    @property
    def capex(self) -> float:
        return self.vehicle_capex + self.infra_capex

    @property
    def profit(self) -> float:
        return self.income - self.running - self.maintenance

    @property
    def payback_years(self) -> float:
        return self.capex / self.profit if self.profit > 0 else math.inf

    @property
    def roi(self) -> float:
        return self.profit / self.capex if self.capex > 0 else math.inf


def _wagon_options(vs: list[Vehicle], cargo: str) -> list[Vehicle]:
    return [v for v in vs if v.role == "wagon" and v.carries(cargo)]


def best_consists(game: Game, vehicles: list[Vehicle], mode: str, cargo: str, year: int,
                  max_wagons: int = 30, max_grade: float = 0.0) -> list[Consist]:
    """Candidate vehicle sets able to carry `cargo` in `year` for transport `mode`.
    Memoized per game (the vehicle list and year fully determine the result; Consist objects are
    never mutated by callers, a fresh list is returned)."""
    cache = getattr(game, "_best_consists_cache", None)
    if cache is None:
        cache = {}
        try:
            game._best_consists_cache = cache
        except Exception:                 # pragma: no cover - games without attribute support
            return _best_consists(game, vehicles, mode, cargo, year, max_wagons, max_grade)
    key = (id(vehicles), len(vehicles), mode, cargo, year, max_wagons, max_grade)
    hit = cache.get(key)
    if hit is None or hit[0] is not vehicles:
        hit = cache[key] = (vehicles, _best_consists(game, vehicles, mode, cargo, year, max_wagons, max_grade))
    return list(hit[1])


def _best_consists(game: Game, vehicles: list[Vehicle], mode: str, cargo: str, year: int,
                   max_wagons: int = 30, max_grade: float = 0.0) -> list[Consist]:
    avail = available(vehicles, year)
    out: list[Consist] = []
    if mode == "road":
        for v in avail:
            if v.carrier == "road" and v.carries(cargo):
                out.append(Consist(mode, [(v, 1)], v.carries(cargo), v.top_speed, v.price, v.running_cost, v.load_speed))
    elif mode == "water":
        for v in avail:
            if v.carrier == "water" and v.carries(cargo):
                out.append(Consist(mode, [(v, 1)], v.carries(cargo), v.top_speed, v.price, v.running_cost, v.load_speed))
    elif mode == "rail":
        from .upgrades import needs_catenary          # new lines are built without catenary
        locos = [v for v in avail if v.carrier == "rail" and v.role == "loco" and v.price > 1000 and v.year_from > 0
                 and not needs_catenary(game, v)]
        wagons = _wagon_options(avail, cargo)
        for loco in locos:
            for w in wagons:
                # tractive limit: total mass the loco can pull on the ruling grade (plus rolling resistance)
                g = 9.81
                resist = 0.002 + max_grade
                max_mass_t = (loco.tractive_effort_kn * 1000) / (g * resist) / 1000 if loco.tractive_effort_kn else 0
                per_wagon_t = w.weight_empty_t + w.weight_max_payload_t
                n_by_force = int((max_mass_t - loco.weight_empty_t) // per_wagon_t) if per_wagon_t > 0 else max_wagons
                n = max(1, min(max_wagons, n_by_force))
                speed = min(loco.top_speed, w.top_speed)
                out.append(Consist(mode, [(loco, 1), (w, n)], n * w.carries(cargo), speed,
                                   loco.price + n * w.price, loco.running_cost + n * w.running_cost,
                                   w.load_speed))
    return out


def evaluate_link(game: Game, vehicles: list[Vehicle], cargo: Cargo, src: Endpoint, dst: Endpoint,
                  mode: str, volume: float, year: int | None = None,
                  route_m: float | None = None, route_build_cost: float | None = None,
                  route_maintenance: float | None = None, max_grade: float = 0.01) -> LinkEval | None:
    """Best-consist evaluation of moving `volume` units/year of `cargo` from src to dst."""
    year = game.year if year is None else year
    straight = math.dist((src.x, src.y), (dst.x, dst.y))
    route = route_m if route_m is not None else straight * DETOUR_DEFAULT
    ipu = game.income_per_unit(cargo, (src.x, src.y, src.z), (dst.x, dst.y, dst.z))
    if mode in ("rail", "road") and route_build_cost is None:
        way = game.cheapest_way(mode, year)
        route_build_cost = route * way.cost_per_m if way else 0.0
        route_maintenance = route * way.maintenance_per_m if way else 0.0
    route_build_cost = route_build_cost or 0.0
    route_maintenance = route_maintenance or 0.0
    cat = getattr(game, "_catalogue", None)
    if cat is None:
        cat = game._catalogue = facilities.Catalogue(game)
    year_s = game.year_ms / 1000.0
    best: LinkEval | None = None
    for c in best_consists(game, vehicles, mode, cargo.key, year, max_grade=max_grade):
        if c.capacity <= 0 or c.speed <= 0:
            continue
        if mode == "rail" and len(c.units) == 2:
            # size trains to the volume: as few trains as possible, wagons spread evenly
            (loco, _), (wagon, n_max) = c.units
            wcap = wagon.carries(cargo.key)
            from . import capacity as _cap
            load_w = _cap.loading_time_s(wcap * n_max, c.load_speed, _cap.cargo_load_factor(game, cargo.key),
                                         n_units=n_max)
            trip_w = 2 * route / (c.speed * 0.85) + 2 * load_w + 2 * STOP_DELAY_S
            per_wagon_year = wcap * year_s / trip_w
            wagons_needed = max(1, math.ceil(volume / per_wagon_year))
            n_trains = math.ceil(wagons_needed / n_max)
            n_w = math.ceil(wagons_needed / n_trains)
            c = Consist(mode, [(loco, 1), (wagon, n_w)], n_w * wcap, c.speed,
                        loco.price + n_w * wagon.price, loco.running_cost + n_w * wagon.running_cost, c.load_speed)
        from . import capacity as _cap      # lazy: capacity imports this module
        n_units = sum(k for v, k in c.units if v.capacity) or 1
        load_t = _cap.loading_time_s(c.capacity, c.load_speed, _cap.cargo_load_factor(game, cargo.key),
                                     n_units=n_units)
        trip = 2 * route / (c.speed * 0.85) + 2 * load_t + 2 * STOP_DELAY_S
        if (dst.kind == "town" and cargo.time_to_deliver_s
                and route / (c.speed * 0.85) + load_t > cargo.time_to_deliver_s):
            continue    # late deliveries ruin the town's delivery rating
        per_consist = c.capacity * year_s / trip
        n = max(1, math.ceil(volume / per_consist))
        moved = min(volume, n * per_consist)
        # station size: platform length from the consist, terminals from occupancy at each end
        units = sum(k for _, k in c.units)
        occupancy = n * (load_t + STOP_DELAY_S) / trip
        terminals = max(1, math.ceil(occupancy / MAX_OCCUPANCY))
        st = facilities.station(cat, mode, year, cargo.classes, units * VEHICLE_LENGTH_M, terminals)
        dp = facilities.depot(cat, mode, year, n)
        infra = route_build_cost + 2 * st.cost + dp.cost
        ev = LinkEval(cargo.key, src, dst, mode, year, moved, straight, route, ipu, c, n, trip,
                      income=moved * ipu, vehicle_capex=n * c.price, infra_capex=infra,
                      running=n * c.running_cost,
                      maintenance=route_maintenance + 2 * st.maintenance + dp.maintenance,
                      station_each=st.cost, station_maint_each=st.maintenance, station_name=st.name,
                      depot_cost=dp.cost, depot_name=dp.name, route_build=route_build_cost)
        if best is None or (ev.profit - ev.capex / 10) > (best.profit - best.capex / 10):
            best = ev
    return best
