"""Capacity layer for every generic project kind (tours, warehouse chains, passengers, workers,
boosters, prospecting). `capacity.apply_capacity` only sees direct cargo links (LinkEval); the
generators in tours.py / multimodal.py / passengers.py / efficiency.py / planner.worker_candidates
size fleets and stations with their own simpler formulas. `check_candidate` re-checks such a
ProjectCandidate with the same rules as apply_capacity and returns an adjusted copy:

1. Line capacity per vehicle line ("service") of the project:
   - road / tram: `capacity.road_link_ok` (lane flow v/c with BPR slowdown, jam density, entrance
     gates, MAX_ROAD_VEHICLES per line); congestion lengthens the trip and cargo lines get more
     vehicles (same damped fixed point as apply_capacity); extra entrance_exit modules when one
     gate is not enough.
   - rail: every track section the line uses in both directions (direct links, shuttles,
     passenger trains) gets `capacity.rail_line_plan` (single track + passing loops / platoons /
     second track); sections used in one direction only (loop tours) are checked against the
     block-signal headway. Way speed below the vehicle's top speed slows the line (as apply_capacity).
2. Terminals from occupancy at every place the project serves: busy seconds per game year =
   handling (units x per-unit load time, capacity.loading_time_s) + calls x (stop delays +
   manoeuvre); terminals = ceil(occupancy / MAX_OCC). Road cargo stations <= MAX_ROAD_TERMINALS,
   harbors <= MAX_HARBOR_TERMINALS piers, small passenger stops have one bay each (more bays ->
   extra small stops next to it) and hold SMALL_STOP_WAITING passengers (overflow is lost:
   passenger volume and income are scaled down). New stations pay station(T) - station(T_priced);
   stations that already exist pay only terminals beyond the first (their own lines keep theirs).
3. Maintenance (mechanics §5): vehicles of the project that share a depot location form one
   pool: one depot (free when the generator could use an existing one) + maintenance buildings
   for the rest (`facilities.maintenance_counts`) - the dossier uses the same rule per leg, so
   plan and dossier report the same depot / maintenance-building counts. Generators that treated
   a shared depot as free used to drop the maintenance buildings too; they are added here.

Infeasible projects (road over its limits, more than 8 truck bays or 4 piers, rail beyond double
track) -> None. Otherwise a copy with extra capex / upkeep / vehicles folded in, notes in
meta["capacity_notes"] (Russian, also appended to `details`) and a JSON-ready summary in
meta["capacity_check"] (services, stations, maintenance groups, extras). The input is not mutated and a
checked candidate (meta["capacity_check"] present) is returned unchanged.

`plan_step_maintenance(game, step)` gives the maintenance groups of a plan.json step with the
same grouping and counting rule (used by the plan/dossier consistency test).
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field

from . import capacity as C
from . import facilities
from .candidates import ProjectCandidate, VehicleLine
from .economics import MAX_OCCUPANCY, STOP_DELAY_S, VEHICLE_LENGTH_M
from .model import Game

SPEED_EFF = 0.85                    # generators run at 0.85 x top speed (economics, tours, passengers)
SMALL_STOP_WAITING = 50             # passengers waiting at a small road stop (mechanics §4.1, §11.2)
MAX_RAIL_TRACKS = 8                 # station template 1-8 tracks
MAX_PIERS = C.MAX_HARBOR_TERMINALS
TOUR_KEYS = ("from", "to", "load")  # plan / candidate legs of tours.py
_NUM = r"(\d[\d   ]*(?:[.,]\d+)?)"
DEPOT_RU = {"road": "дорожное депо", "tram": "трамвайное депо", "rail": "ж/д депо", "water": "судовое депо"}
PAX_FAMILY = {"road": ("road", "road_pax"), "tram": ("tram_pax",), "rail": ("rail", "rail_pax")}


# --------------------------------------------------------------------------- helpers

def _num(pattern: str, text: str | None) -> float | None:
    m = re.search(pattern, text or "")
    if not m:
        return None
    s = re.sub(r"[\s  ]", "", m.group(1)).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _vehicle_index(game: Game, vehicles=None) -> dict:
    idx = getattr(game, "_cg_vehicle_index", None)
    if idx is None or vehicles is not None:
        if vehicles is None:
            from .vehicles import load_vehicles
            vehicles = load_vehicles(game)
        idx = {"id": {}, "file": {}, "name": {}}
        for v in vehicles:
            idx["id"][v.id] = v
            idx["file"].setdefault(v.file.rsplit("/", 1)[-1], v)
            idx["name"].setdefault(v.name, v)
        game._cg_vehicle_index = idx
    return idx


def _totals(idx: dict, vehicles) -> list[tuple]:
    """[(Vehicle, total count)] from VehicleLine objects or plan dicts {model|file, name, count}."""
    out = []
    for v in vehicles or []:
        if isinstance(v, VehicleLine):
            veh = idx["id"].get(v.vehicle_id) or idx["file"].get(v.file.rsplit("/", 1)[-1]) or idx["name"].get(v.name)
            cnt = v.count
        else:
            vid = v.get("vehicle_id")
            f = (v.get("model") or v.get("file") or "").rsplit("/", 1)[-1]
            veh = (idx["id"].get(vid) if vid is not None else None) or idx["file"].get(f) or idx["name"].get(v.get("name"))
            cnt = v.get("count", 1)
        if veh is not None and cnt:
            out.append((veh, int(cnt)))
    return out


def n_consists(mode: str, totals: list[tuple], hint=None) -> int:
    """Consists of a line: the generator's number when known, else locomotives / railcars for
    rail and vehicles otherwise (the dossier's rule)."""
    if hint:
        return max(1, int(hint))
    if mode == "rail":
        return max(1, sum(c for v, c in totals if v.role in ("loco", "railcar")) or 1)
    return max(1, sum(c for _, c in totals))


@dataclass
class _Consist:
    units: list                     # [(Vehicle, per consist)]

    @property
    def speed(self) -> float:
        return min((v.top_speed for v, _ in self.units if v.top_speed > 0), default=10.0)

    @property
    def price(self) -> float:
        return sum(v.price * k for v, k in self.units)

    @property
    def running(self) -> float:
        return sum(v.running_cost * k for v, k in self.units)

    @property
    def length_m(self) -> float:
        return sum(k for _, k in self.units) * VEHICLE_LENGTH_M

    @property
    def loading_units(self) -> int:
        return sum(k for v, k in self.units if v.capacity) or 1

    @property
    def load_speed(self) -> float:
        return max((v.load_speed for v, _ in self.units if v.capacity), default=1.0) or 1.0

    def cap(self, cargos) -> float:
        cargos = [c for c in cargos if c]
        if not cargos:
            return 0.0
        return min(sum(v.carries(c) * k for v, k in self.units) for c in cargos)

    def board_s(self) -> float:
        """passengers._consist: boarding/alighting time of the largest unit."""
        return max((v.carries("passengers") / max(v.load_speed, 0.1) for v, _ in self.units
                    if v.carries("passengers")), default=0.0)


def _consist(totals: list[tuple], n: int) -> _Consist:
    return _Consist([(v, max(1, round(c / max(n, 1)))) for v, c in totals])


def _terminals_in_name(name: str | None) -> int | None:
    """'Грузовая автостанция: 2 терминал(а)' -> 2; 'Ж/д грузовая станция: 2×120 м' -> 2;
    'Пассажирский вокзал: 1×80 м' -> 1; harbor -> 1."""
    if not name:
        return None
    m = re.search(r"(\d+)\s*терминал", name) or re.search(r":\s*(\d+)\s*[×x]\s*\d+\s*м", name)
    if m:
        return int(m.group(1))
    if "порт" in name.lower():
        return 1
    return None


# --------------------------------------------------------------------------- model

@dataclass
class Call:
    service: int                    # index into the services list
    times: float = 1.0              # calls per round trip
    fixed_s: float = 0.0            # bay time per call besides handling (stop delays, manoeuvre, boarding)


@dataclass
class Place:
    key: object                     # node index / hub key
    family: str                     # cargo | harbor | builtin | small_stop | rail_pax
    mode: str
    built: bool = False
    priced_terminals: int = 1
    handling_s: float = 0.0         # load + unload seconds per game year (volume based)
    calls: list[Call] = field(default_factory=list)
    classes: tuple = ("UNIVERSAL",)
    length_m: float = 0.0
    count: int = 1                  # identical stops at this place (k bus stops in a town)
    pax_per_year: float = 0.0       # boardings at each stop (small stops: waiting room check)
    label: str = ""


@dataclass
class Service:
    name: str
    mode: str
    n: int
    consist: _Consist
    trip_s: float
    running_s: float | None         # moving part of trip_s (None: unknown -> no fleet re-sizing)
    route_m: float | None           # one-way route (loops: half the cycle) for the jam-density check
    trips_needed: float | None      # round trips / yr for the volume (cargo); None: fleet is fixed (passengers)
    tracks: list = field(default_factory=list)      # rail: [(length_m, both_directions)]
    anchor: tuple = ()              # maintenance pool (place, mode)
    depot_free: bool = False        # generator used an existing depot
    leg_index: int | None = None    # multimodal / bundle leg this service comes from
    road_gates: bool = True         # cargo road stations: entrance gate check


@dataclass
class _Ctx:
    game: Game
    cat: facilities.Catalogue
    year: int
    built: set
    nodes: list | None
    idx: dict
    year_s: float

    def water_only(self, key) -> bool:
        if self.nodes is None or not isinstance(key, int) or not (0 <= key < len(self.nodes)):
            return False
        return bool(getattr(self.nodes[key], "water_only", False))

    def name(self, key) -> str:
        if self.nodes is not None and isinstance(key, int) and 0 <= key < len(self.nodes):
            return str(getattr(self.nodes[key], "name", key))
        return str(key)

    def is_built(self, key, mode) -> bool:
        return (key, mode) in self.built

    def per_unit_s(self, cons: _Consist, cargo: str, cap: float) -> float:
        if cap <= 0:
            return 0.0
        f = C.cargo_load_factor(self.game, cargo)
        return C.loading_time_s(cap, cons.load_speed, f, n_units=cons.loading_units) / cap


def _classes(game: Game, cargos) -> tuple:
    cl = sorted({c for k in cargos if k in game.cargo_by_key for c in game.cargo_by_key[k].classes})
    return tuple(cl or ["UNIVERSAL"])


def _place(places: dict, key, family: str, mode: str, **kw) -> Place:
    p = places.get((key, family, mode))
    if p is None:
        p = places[(key, family, mode)] = Place(key, family, mode, **kw)
    return p


# --------------------------------------------------------------------------- decomposition per kind

def _tour(ctx: _Ctx, pc: ProjectCandidate, services: list, places: dict):
    m, mode = pc.meta, pc.mode
    totals = _totals(ctx.idx, pc.vehicles)
    n = n_consists(mode, totals, m.get("n_consists"))
    cons = _consist(totals, n)
    cargos = list(m.get("cargos") or [pc.cargo])
    cap = cons.cap(cargos)
    if cap <= 0 or not totals:
        return
    trip = float(m.get("trip_s") or 0.0)
    cycle = float(m.get("cycle_m") or 0.0)
    visits = list(m.get("visits") or m.get("tour") or pc.nodes)
    stops = list(dict.fromkeys(m.get("tour") or pc.nodes))
    if trip <= 0:
        return
    per = {c: ctx.per_unit_s(cons, c, cap) for c in cargos}
    load_t = cap * max(per.values())
    # tours.evaluate_line: occupancy = n (load_t + STOP_DELAY_S) / trip, one size for every stop
    t0 = max(1, math.ceil(n * (load_t + STOP_DELAY_S) / trip / MAX_OCCUPANCY))
    si = len(services)
    fixed = STOP_DELAY_S + C.MANOEUVRE_S.get(mode, 10.0)
    for s in stops:
        fam = "builtin" if (mode == "water" and ctx.water_only(s)) else ("harbor" if mode == "water" else "cargo")
        p = _place(places, s, fam, mode, built=ctx.is_built(s, mode), priced_terminals=t0,
                   classes=_classes(ctx.game, cargos), length_m=cons.length_m, label=ctx.name(s))
        p.calls.append(Call(si, float(visits.count(s)), fixed))
        for f in m.get("flows") or []:
            if f.get("from") == s or f.get("to") == s:
                p.handling_s += float(f.get("volume", 0.0)) * per.get(f.get("cargo"), max(per.values()))
    tracks = []
    if mode == "rail":
        dirs: dict = {}
        legs = pc.legs or []
        for a in range(len(visits)):
            i, j = visits[a], visits[(a + 1) % len(visits)]
            if i == j:
                continue
            k = (min(i, j), max(i, j))
            L = float(legs[a].get("length_m", 0.0)) if a < len(legs) else 0.0
            d = dirs.setdefault(k, [L, set()])
            d[1].add((i, j))
        tracks = [(L, len(ds) > 1) for L, ds in dirs.values() if L > 0]
    running = cycle / max(cons.speed * SPEED_EFF, 0.1) if cycle > 0 else None
    peak = float(m.get("peak_load") or pc.volume or 0.0)
    services.append(Service(f"маршрут ({n}× {m.get('consist', '')})", mode, n, cons, trip, running,
                            cycle / 2 if cycle > 0 else None, peak / cap if peak > 0 else None, tracks,
                            ("tour", mode), any(ctx.is_built(s, mode) for s in stops)))


def _multimodal(ctx: _Ctx, pc: ProjectCandidate, services: list, places: dict):
    cargo = pc.cargo
    for li, l in enumerate(pc.legs or []):
        mode = l.get("mode", "road")
        totals = _totals(ctx.idx, l.get("vehicles"))
        n = n_consists(mode, totals, l.get("n_consists"))
        cons = _consist(totals, n)
        cap = cons.cap([cargo])
        trip = float(l.get("trip_time_s") or 0.0)
        if cap <= 0 or trip <= 0:
            continue
        vol = float(l.get("volume") or 0.0)
        L = float(l.get("length_m") or 0.0)
        per = ctx.per_unit_s(cons, cargo, cap)
        fixed = STOP_DELAY_S + C.MANOEUVRE_S.get(mode, 10.0)
        si = len(services)
        for end, sname in ((l.get("from") or {}, l.get("station_from")), (l.get("to") or {}, l.get("station_to"))):
            key = end.get("key")
            builtin = "встроенный" in (sname or "") or (mode == "water" and ctx.water_only(key))
            fam = "builtin" if builtin else ("harbor" if mode == "water" else "cargo")
            p = _place(places, key, fam, mode, built=ctx.is_built(key, mode),
                       priced_terminals=_terminals_in_name(sname) or 1,
                       classes=_classes(ctx.game, [cargo]), label=end.get("name", ""))
            p.length_m = max(p.length_m, cons.length_m)
            p.calls.append(Call(si, 1.0, fixed))
            p.handling_s += vol * per
        role = l.get("role", "line")
        anchor_end = (l.get("to") if role == "feeder" else l.get("from")) or {}
        anchor = (role, mode)
        services.append(Service(f"{role} ({mode}): {n}× {l.get('consist', '')}", mode, n, cons, trip,
                                2 * L / max(cons.speed * SPEED_EFF, 0.1) if L > 0 else None, L or None,
                                vol / cap if vol > 0 else None, [(L, True)] if (mode == "rail" and L > 0) else [],
                                anchor, ctx.is_built(anchor_end.get("key"), mode), li))


def _pax_part(ctx: _Ctx, kind: str, mode: str, nodes: list, vehicles, details: str, meta: dict,
              volume: float, services: list, places: dict, leg_index=None, planner_workers: bool = False,
              extra_built: set | None = None):
    totals = _totals(ctx.idx, vehicles)
    if not totals:
        return
    n = n_consists(mode, totals)
    cons = _consist(totals, n)
    board = cons.board_s()
    built = ctx.built | (extra_built or set())
    nodes = [int(x) for x in nodes or []]
    line = meta.get("line") or ("loop" if len(nodes) == 1 else ("workers" if kind == "workers" else "intercity"))
    cycle = meta.get("cycle_s") or _num(rf"круг {_NUM} с", details)
    si = len(services)
    man = C.MANOEUVRE_S.get(mode, 10.0)
    anchor = (f"pax{leg_index if leg_index is not None else ''}", mode)

    def has_mode_station(i):
        return any((i, f) in built for f in PAX_FAMILY.get(mode, (mode,)))

    if planner_workers:
        free = any((i, "road") in built for i in nodes)
    else:
        free = any(has_mode_station(i) for i in nodes)
    route_m = meta.get("route_m") or ((_num(rf"маршрут {_NUM} км", details) or 0) * 1000.0) or None
    tracks = []
    if line in ("intra", "loop"):
        stops = int(meta.get("stops") or _num(rf"{_NUM} остановок", details) or 1)
        loop_m = (_num(rf"кольцо ~{_NUM} км", details) or 0) * 1000.0
        route_m = loop_m / 2 if loop_m else None
        skey = "tram_pax" if mode == "tram" else "road_pax"
        t = nodes[0] if nodes else None
        p = _place(places, t, "small_stop", mode, built=(t, skey) in built or (t, "road_pax") in built
                   or (t, "tram_pax") in built, count=stops, label=f"кольца {ctx.name(t)}")
        p.calls.append(Call(si, 1.0, STOP_DELAY_S + 0.5 * board + man))
        p.pax_per_year += volume / max(stops, 1)
    elif mode == "rail":
        length = cons.length_m
        name = meta.get("station") or (re.search(r"Пассажирский вокзал: [^.]+", details or "") or [None])[0]
        for t in nodes[:2]:
            p = _place(places, t, "rail_pax", mode, built=(t, "rail_pax") in built,
                       priced_terminals=_terminals_in_name(name) or 1, length_m=length, label=ctx.name(t))
            p.calls.append(Call(si, 1.0, 2 * STOP_DELAY_S + board + man))
            p.pax_per_year += volume / 2
        if route_m:
            tracks = [(float(route_m), True)]
    else:
        k = int(meta.get("stops_per_town") or _num(rf"{_NUM} малые остановки в каждом городе", details) or 1)
        for t in nodes[:2]:
            here = 1 if (line == "workers" and ctx.nodes is not None and isinstance(t, int)
                         and t < len(ctx.nodes) and getattr(ctx.nodes[t], "kind", "") == "industry") else k
            p = _place(places, t, "small_stop", mode, built=(t, "road_pax") in built, count=here,
                       label=ctx.name(t))
            p.calls.append(Call(si, 2.0, 2 * STOP_DELAY_S + board + man))
            p.pax_per_year += volume / 2 / max(here, 1)
    if not cycle:
        cycle = None
    services.append(Service(f"{line}: {n}× {cons.units[0][0].name}", mode, n, cons,
                            float(cycle) if cycle else 0.0, None, route_m, None, tracks, anchor, free, leg_index,
                            road_gates=False))


def _passengers(ctx: _Ctx, pc: ProjectCandidate, services: list, places: dict, planner_workers=False):
    if pc.mode == "multi" or (pc.legs and all("title" in l for l in pc.legs)):
        extra: set = set()
        for li, l in enumerate(pc.legs or []):
            mode = l.get("mode") or "road"
            _pax_part(ctx, l.get("kind", "passenger"), mode, l.get("nodes") or [], l.get("vehicles"),
                      l.get("details") or "", {}, float(l.get("volume") or 0.0), services, places, li,
                      extra_built=set(extra))
            # passengers.bundle_candidates: the intercity part sees the loops' stops as built
            for t in l.get("nodes") or []:
                extra.add((int(t), "tram_pax" if mode == "tram" else ("rail_pax" if mode == "rail" else "road_pax")))
        return
    _pax_part(ctx, pc.kind, pc.mode, pc.nodes, pc.vehicles, pc.details, pc.meta, pc.volume, services, places,
              planner_workers=planner_workers)


def _direct(ctx: _Ctx, pc: ProjectCandidate, services: list, places: dict):
    """booster / prospecting first line: one evaluate_link link p -> d."""
    mode = pc.mode
    if mode not in ("road", "rail", "water") or len(pc.nodes) < 2:
        return
    totals = _totals(ctx.idx, pc.vehicles)
    n = n_consists(mode, totals)
    cons = _consist(totals, n)
    cap = cons.cap([pc.cargo])
    if cap <= 0 or pc.volume <= 0:
        return
    per = ctx.per_unit_s(cons, pc.cargo, cap)
    load_e = C.loading_time_s(cap, cons.load_speed, C.cargo_load_factor(ctx.game, pc.cargo), n_units=cons.loading_units)
    route = pc.meta.get("route_m")
    trip = pc.meta.get("trip_s")
    if not trip and route:
        trip = 2 * route / (cons.speed * SPEED_EFF) + 2 * load_e + 2 * STOP_DELAY_S
    if not trip:
        trip = n * cap * ctx.year_s / pc.volume           # fleet sized by ceil(): upper bound of the trip
    t0 = max(1, math.ceil(n * (load_e + STOP_DELAY_S) / trip / MAX_OCCUPANCY))
    p_, d_ = pc.nodes[0], pc.nodes[1]
    si = len(services)
    fixed = STOP_DELAY_S + C.MANOEUVRE_S.get(mode, 10.0)
    for key in (p_, d_):
        fam = "builtin" if (mode == "water" and ctx.water_only(key)) else ("harbor" if mode == "water" else "cargo")
        p = _place(places, key, fam, mode, built=ctx.is_built(key, mode), priced_terminals=t0,
                   classes=_classes(ctx.game, [pc.cargo]), length_m=cons.length_m, label=ctx.name(key))
        p.calls.append(Call(si, 1.0, fixed))
        p.handling_s += pc.volume * per
    free = pc.kind == "booster" and (ctx.is_built(p_, mode) or ctx.is_built(d_, mode))
    services.append(Service(f"{n}× {cons.units[0][0].name}", mode, n, cons, float(trip),
                            2 * route / (cons.speed * SPEED_EFF) if route else None, route,
                            pc.volume / cap, [(float(route), True)] if (mode == "rail" and route) else [],
                            (p_, mode), free))


def decompose(ctx: _Ctx, pc: ProjectCandidate) -> tuple[list[Service], dict]:
    services: list[Service] = []
    places: dict = {}
    m = pc.meta or {}
    if m.get("generator") == "tours" or (pc.legs and all(all(k in l for k in TOUR_KEYS) for l in pc.legs)
                                         and pc.kind == "cargo"):
        _tour(ctx, pc, services, places)
    elif pc.kind == "multimodal":
        _multimodal(ctx, pc, services, places)
    elif pc.kind in ("passenger", "workers"):
        _passengers(ctx, pc, services, places, planner_workers=(pc.kind == "workers" and "line" not in m))
    elif pc.kind in ("booster", "industry_build"):
        _direct(ctx, pc, services, places)
    return services, places


# --------------------------------------------------------------------------- evaluation

def _way(ctx: _Ctx, mode: str):
    try:
        return C._way_for(ctx.game, mode, ctx.year, None)
    except Exception:                                   # pragma: no cover - routing optional
        return None


def _station_cost(ctx: _Ctx, p: Place, terminals: int) -> tuple[float, float]:
    if p.family == "cargo":
        b = facilities.station(ctx.cat, "road" if p.mode == "tram" else p.mode, ctx.year, list(p.classes),
                               p.length_m or 80.0, max(1, terminals))
        return b.cost, b.maintenance
    if p.family == "harbor":
        pc_, pm = C._harbor_pier_cost(ctx.cat, ctx.year)
        return terminals * pc_, terminals * pm
    if p.family == "rail_pax":
        from .passengers import rail_pax_station
        c, m, _ = rail_pax_station(ctx.cat, ctx.year, p.length_m or 80.0, max(1, terminals))
        return c, m
    if p.family == "small_stop":
        from .passengers import small_stop
        c, m = small_stop(ctx.cat, ctx.year)
        return terminals * c, terminals * m
    return 0.0, 0.0


def _maint_cost(ctx: _Ctx, mode: str, n: int, free: bool) -> tuple[float, float, int, int]:
    """(capex, upkeep, depots, maintenance buildings) for n vehicles at one place."""
    if mode == "tram":                                  # tram depots only, pool 12 each; priced like passengers._depot
        from .passengers import _depot
        k = facilities.maintenance_counts("tram", n)[0]
        if free:
            return 0.0, 0.0, k, 0
        c, m, _ = _depot(ctx.cat, ctx.game, "tram", ctx.year, n)
        return c, m, k, 0
    d, k = facilities.maintenance_counts(mode, n)
    mc, mm = facilities.maint_building(ctx.cat, mode, ctx.year)
    dc, dm = (0.0, 0.0) if free else facilities.depot_price(ctx.cat, mode, ctx.year)
    return dc + k * mc, dm + k * mm, d, k


def _priced_maint(ctx: _Ctx, mode: str, n: int, free: bool) -> tuple[float, float]:
    """What the generators charged: facilities.depot(n) (passengers._depot for trams), nothing
    when they reused an existing depot."""
    if free:
        return 0.0, 0.0
    if mode == "tram":
        from .passengers import _depot
        c, m, _ = _depot(ctx.cat, ctx.game, "tram", ctx.year, n)
        return c, m
    b = facilities.depot(ctx.cat, mode, ctx.year, n)
    return b.cost, b.maintenance


def evaluate(ctx: _Ctx, services: list[Service], places: dict) -> dict:
    Y = ctx.year_s
    sens = float(((ctx.game.raw.get("baseConfig") or {}).get("advancedOptions") or {})
                 .get("trafficSpeedSensitivityScale", 1.0) or 1.0)
    notes: list[str] = []
    reasons: list[str] = []
    out_s = []
    extra_vcap = extra_run = extra_infra = extra_maint = 0.0
    final_n, final_trip = [], []
    for si, s in enumerate(services):
        n, trip = s.n, s.trip_s
        way = _way(ctx, s.mode) if s.mode in ("road", "rail") else None
        running = s.running_s
        fixed = max(0.0, trip - running) if (running is not None and trip > 0) else None
        run_eff = running
        if running is not None and way is not None and way.speed > 0 and s.consist.speed > way.speed:
            run_eff = running * s.consist.speed / way.speed
            notes.append(f"{s.name}: скорость ограничена путём {way.speed * 3.6:.0f} км/ч")
        rail_plans: list = []
        road = None
        sf = 1.0
        trip0 = trip
        # vehicles the generator really needed (<= its fleet): only the slowdown re-sizes the fleet
        need0 = min(float(s.n), s.trips_needed * trip0 / Y) if (s.trips_needed and trip0 > 0) else float(s.n)
        for _ in range(30):
            if run_eff is not None and fixed is not None:
                trip = run_eff / sf + fixed
                if s.trips_needed and trip0 > 0:
                    n = max(s.n, math.ceil(need0 * trip / trip0 - 1e-9))
            if trip <= 0:
                break
            new_sf = 1.0
            if s.mode in ("road", "tram"):
                road = C.road_link_ok(n, trip, s.route_m or 1e9, s.consist.speed, year_s=Y, sensitivity=sens)
                new_sf = road.speed_factor
            elif s.mode == "rail" and s.tracks:
                rail_plans = []
                v_line = min(s.consist.speed, way.speed) if (way and way.speed) else s.consist.speed
                cpm = way.cost_per_m if way else 0.0
                mpm = way.maintenance_per_m if way else 0.0
                tpy = n * Y / trip
                share = (run_eff / sf / trip) if (run_eff is not None and trip > 0) else 1.0
                for L, both in s.tracks:
                    if both:
                        rp = C.rail_line_plan(tpy, L, v_line, s.consist.length_m, cpm, mpm, n, share, L * cpm,
                                              year_s=Y)
                    else:
                        cap1 = C.double_track_capacity(v_line, s.consist.length_m, year_s=Y)
                        u = tpy / cap1 * C.MAX_OCC if cap1 else math.inf
                        rp = C.RailPlan("one-way" if tpy <= cap1 else "infeasible", 0, 0.0, 0.0, cap1, u,
                                        1.0 / (1.0 + C.RAIL_ALPHA_DOUBLE * u ** C.RAIL_BETA_DOUBLE),
                                        "" if tpy <= cap1 else f"{tpy:.0f} поездов/год в одну сторону > {cap1:.0f}")
                    rail_plans.append((L, rp))
                # running time is spread over the sections: length-weighted (harmonic) slowdown
                tot = sum(L for L, _ in rail_plans)
                slow = sum(L / max(rp.speed_factor, 1e-3) for L, rp in rail_plans)
                new_sf = tot / slow if slow > 0 else 1.0
            if abs(new_sf - sf) < 1e-4 or run_eff is None:
                sf = new_sf
                break
            sf = 0.5 * (sf + new_sf)
        info = {"name": s.name, "mode": s.mode, "n_before": s.n, "n": n, "trip_s": round(trip, 1),
                "speed_factor": round(sf, 4)}
        if road is not None:
            info["road"] = {"vc": round(road.vc, 4), "entrances": road.entrances, "ok": road.ok}
            if not road.ok:
                reasons.append(f"{s.name}: {road.reason}")
            if s.road_gates and road.entrances > 1:
                e, em = facilities.road_entrance(ctx.cat, ctx.year)
                k = sum(1 for p in places.values() if p.family == "cargo"
                        and any(c.service == si for c in p.calls))
                extra_infra += k * (road.entrances - 1) * e
                extra_maint += k * (road.entrances - 1) * em
                notes.append(f"{s.name}: {road.entrances} въезда на станциях")
        for L, rp in rail_plans:
            if rp.kind == "infeasible":
                reasons.append(f"{s.name}: {rp.reason}")
            extra_infra += rp.capex
            extra_maint += rp.maintenance
            if rp.kind == "single+loops":
                notes.append(f"{s.name}: участок {L / 1000:.1f} км — однопутка + {rp.loops} разъезд(ов)"
                             + (f", пакеты по {rp.platoon}" if rp.platoon > 1 else ""))
            elif rp.kind == "double":
                notes.append(f"{s.name}: участок {L / 1000:.1f} км — нужен второй путь")
        if rail_plans:
            info["track"] = [{"length_m": round(L), "kind": rp.kind, "loops": rp.loops, "capex": round(rp.capex)}
                             for L, rp in rail_plans]
        dn = n - s.n
        if dn > 0:
            extra_vcap += dn * s.consist.price
            extra_run += dn * s.consist.running
            notes.append(f"{s.name}: +{dn} состав(а) из-за замедления (×{sf:.2f})")
        final_n.append(n)
        final_trip.append(trip)
        out_s.append(info)

    # ---- terminals at every place
    out_p = []
    volume_scale = 1.0
    for p in places.values():
        if p.family == "builtin" or p.key is None:
            continue
        # calls reach every one of the `count` identical stops; handling is shared among them
        calls_y = 0.0                                  # calls per year at one stop
        busy = p.handling_s / max(p.count, 1)          # bay seconds per year at one stop
        for c in p.calls:
            tr = final_trip[c.service]
            if tr > 0:
                k = final_n[c.service] * Y / tr * c.times
                calls_y += k
                busy += k * c.fixed_s
        occ = busy / Y
        need = C.terminals_needed(occ, C.MAX_OCC)
        rec = {"place": str(p.key), "family": p.family, "mode": p.mode, "occupancy": round(occ, 4),
               "terminals": need, "priced": p.priced_terminals, "built": p.built}
        if p.family == "cargo" and p.mode in ("road", "tram") and need > C.MAX_ROAD_TERMINALS:
            reasons.append(f"станция {p.label or p.key}: нужно {need} терминалов > {C.MAX_ROAD_TERMINALS}")
        if p.family == "cargo" and p.mode == "rail" and need > MAX_RAIL_TRACKS:
            reasons.append(f"станция {p.label or p.key}: нужно {need} путей > {MAX_RAIL_TRACKS}")
        if p.family == "harbor" and need > MAX_PIERS:
            reasons.append(f"порт {p.label or p.key}: нужно {need} причалов > {MAX_PIERS}")
        base = 1 if p.built else p.priced_terminals
        if p.family == "small_stop":
            if need > 1:
                c1, m1 = _station_cost(ctx, p, (need - 1) * p.count)
                extra_infra += c1
                extra_maint += m1
                notes.append(f"остановки {p.label or p.key}: по {need} малых остановки рядом "
                             f"(загрузка {occ:.0%} одной)")
            # waiting room: boardings per headway at one stop
            if calls_y > 0 and p.pax_per_year > 0:
                per_call = p.pax_per_year / calls_y / max(need, 1)
                if per_call > SMALL_STOP_WAITING:
                    volume_scale = min(volume_scale, SMALL_STOP_WAITING / per_call)
                    rec["waiting"] = round(per_call, 1)
        elif need > base:
            if p.family == "harbor":
                c1, m1 = _station_cost(ctx, p, need - base)
            else:
                ca, ma = _station_cost(ctx, p, need)
                cb, mb = _station_cost(ctx, p, base)
                c1, m1 = ca - cb, ma - mb
            extra_infra += c1
            extra_maint += m1
            what = {"cargo": "терминал(ов)", "harbor": "причал(ов)", "rail_pax": "путь(и) вокзала"}.get(p.family, "")
            notes.append(f"{'станция' if p.family != 'harbor' else 'порт'} {p.label or p.key}: {need} {what} "
                         f"вместо {base} (загрузка {occ:.0%})")
        out_p.append(rec)

    # ---- maintenance pools
    groups: dict = {}
    for s, n in zip(services, final_n):
        g = groups.setdefault(s.anchor, {"mode": s.mode, "n0": 0, "n": 0, "free": s.depot_free})
        g["n0"] += s.n
        g["n"] += n
        g["free"] = g["free"] and s.depot_free
    out_m = []
    for anchor, g in groups.items():
        cost, upkeep, d, k = _maint_cost(ctx, g["mode"], g["n"], g["free"])
        pcost, pupk = _priced_maint(ctx, g["mode"], g["n0"], g["free"])
        extra_infra += cost - pcost
        extra_maint += upkeep - pupk
        if cost - pcost > 1:
            notes.append(f"ТО ({g['n']} ед.): {DEPOT_RU.get(g['mode'], 'депо')}"
                         + (" (уже есть)" if g["free"] else "") + (f" + {k} здание(я) техобслуживания" if k else ""))
        out_m.append({"group": "/".join(str(a) for a in anchor), "mode": g["mode"], "vehicles": g["n"],
                      "depots": d, "maint_buildings": k, "depot_free": g["free"],
                      "capex": round(cost), "extra_capex": round(cost - pcost)})
    return {"feasible": not reasons, "reason": "; ".join(reasons), "notes": notes, "services": out_s,
            "places": out_p, "maintenance": out_m, "final_n": final_n,
            "extra_vehicle_capex": extra_vcap, "extra_running": extra_run,
            "extra_infra_capex": extra_infra, "extra_maintenance": extra_maint, "volume_scale": volume_scale}


# --------------------------------------------------------------------------- public API

def _context(game: Game, catalogue, year: int, built_stations, nodes, vehicles) -> _Ctx:
    cat = catalogue or getattr(game, "_catalogue", None)
    if cat is None:
        cat = game._catalogue = facilities.Catalogue(game)
    return _Ctx(game, cat, year, set(built_stations or ()), nodes, _vehicle_index(game, vehicles),
                game.year_ms / 1000.0)


def _add_vehicles(lines: list, cons: _Consist, dn: int) -> list:
    out = [copy.copy(v) for v in lines]
    for veh, k in cons.units:
        for v in out:
            if isinstance(v, VehicleLine) and v.vehicle_id == veh.id:
                v.count += dn * k
                break
            if isinstance(v, dict) and (v.get("model") == veh.file.rsplit("/", 1)[-1] or v.get("name") == veh.name):
                v["count"] = v.get("count", 0) + dn * k
                break
        else:
            out.append(VehicleLine(veh.id, veh.name, veh.file, dn * k, veh.price))
    return out


def check_candidate(game: Game, catalogue: facilities.Catalogue | None, pc: ProjectCandidate, year: int, *,
                    built_stations=None, nodes=None, vehicles=None) -> ProjectCandidate | None:
    """Capacity check of a generic ProjectCandidate (see the module docstring).

    built_stations: the planner's sim.stations when the generators ran (stations / depots that
    already exist); nodes: planner.nodes (offshore industries have built-in piers); vehicles:
    planner.vehicles (loaded from the export when omitted). Returns an adjusted copy, the same
    object when it was already checked, or None when the project cannot run within the limits.
    """
    if pc is None:
        return None
    if "capacity_check" in (pc.meta or {}):
        return pc
    ctx = _context(game, catalogue, year, built_stations, nodes, vehicles)
    services, places = decompose(ctx, pc)
    new = copy.copy(pc)
    new.meta = dict(pc.meta or {})
    if not services:
        new.meta["capacity_check"] = {"checked": False}
        new.meta["capacity_notes"] = []
        return new
    res = evaluate(ctx, services, places)
    if not res["feasible"]:
        return None
    new.legs = [dict(l) for l in pc.legs] if pc.legs else pc.legs
    new.vehicles = list(pc.vehicles)
    details = pc.details or ""
    for s, n in zip(services, res["final_n"]):
        dn = n - s.n
        if dn <= 0:
            continue
        new.vehicles = _add_vehicles(new.vehicles, s.consist, dn)
        if s.leg_index is not None and new.legs:
            l = new.legs[s.leg_index]
            if "vehicles" in l:
                l["vehicles"] = [dict(v) for v in l["vehicles"]]
                l["vehicles"] = _add_vehicles(l["vehicles"], s.consist, dn)
            if "n_consists" in l:
                l["n_consists"] = n
            for k_, add in (("vehicle_capex", dn * s.consist.price), ("running", dn * s.consist.running)):
                if k_ in l and l[k_] is not None:
                    l[k_] = round(l[k_] + add)
        elif len(services) == 1:
            details = re.sub(rf"(Состав: ){s.n}([×x])", rf"\g<1>{n}\g<2>", details, count=1)
            if "n_consists" in new.meta:
                new.meta["n_consists"] = n
    new.vehicle_capex = pc.vehicle_capex + res["extra_vehicle_capex"]
    new.infra_capex = pc.infra_capex + res["extra_infra_capex"]
    new.running = pc.running + res["extra_running"]
    new.maintenance = pc.maintenance + res["extra_maintenance"]
    vs = res["volume_scale"]
    if vs < 0.999:
        new.volume = pc.volume * vs
        new.income = pc.income * vs
        res["notes"].append(f"малые остановки переполнены: пассажиров ×{vs:.2f}")
    notes = res["notes"]
    if notes:
        details = details.rstrip() + " Пропускная способность: " + "; ".join(notes) + "."
    new.details = details
    new.meta["capacity_notes"] = notes
    new.meta["capacity_check"] = {
        "services": res["services"], "places": res["places"], "maintenance": res["maintenance"],
        "extra_capex": round(res["extra_vehicle_capex"] + res["extra_infra_capex"]),
        "extra_maintenance": round(res["extra_running"] + res["extra_maintenance"]),
        "volume_scale": round(vs, 4)}
    return new


# --------------------------------------------------------------------------- plan steps (consistency)

def _step_project_kind(step: dict) -> str:
    pk = step.get("project_kind")
    legs = step.get("legs") or []
    if legs and all(all(k in l for k in TOUR_KEYS) for l in legs):
        return "tour"
    if pk in ("passenger", "workers", "multimodal", "booster", "industry_build"):
        return pk
    return "direct"


def plan_step_maintenance(game: Game, step: dict, vehicles=None) -> list[dict]:
    """Maintenance groups of a plan.json build_line step: [{mode, vehicles, depots,
    maint_buildings}], same grouping (one pool per depot place of the project; multimodal: per
    role and mode at the hub; passenger bundles: per part) and counting rule as check_candidate.
    Prefers the step's own "capacity_check" record when the planner wrote one."""
    cap = step.get("capacity_check") or {}
    if cap.get("maintenance"):
        return [{k: g[k] for k in ("mode", "vehicles", "depots", "maint_buildings")} for g in cap["maintenance"]]
    idx = _vehicle_index(game, vehicles)
    kind = _step_project_kind(step)
    mode = step.get("kind") or "road"
    groups: dict = {}

    def add(key, m, n):
        g = groups.setdefault(key, {"mode": m, "vehicles": 0})
        g["vehicles"] += n

    if kind == "multimodal":
        for l in step.get("legs") or []:
            m = l.get("mode", "road")
            add((l.get("role", "line"), m), m, n_consists(m, _totals(idx, l.get("vehicles")), l.get("n_consists")))
    elif kind in ("passenger", "workers") and step.get("legs") and all("title" in l for l in step["legs"]):
        for li, l in enumerate(step["legs"]):
            m = l.get("mode") or "road"
            add(("pax", li), m, n_consists(m, _totals(idx, l.get("vehicles"))))
    else:
        hint = _num(r"Состав:\s*(\d+)\s*[×x]", step.get("details")) if kind in ("tour", "direct") else None
        add(("line",), mode, n_consists(mode, _totals(idx, step.get("vehicles")), hint))
    out = []
    for g in groups.values():
        d, k = facilities.maintenance_counts(g["mode"], g["vehicles"])
        out.append({"mode": g["mode"], "vehicles": g["vehicles"], "depots": d, "maint_buildings": k})
    return out


def dossier_maintenance(dossier: dict) -> list[dict]:
    """[{mode, depots, maint_buildings}] per leg of a dossier (dossier.build_dossier output)."""
    out = []
    for leg in dossier.get("legs") or []:
        if leg.get("error"):
            continue
        d = sum(int(x.get("count", 1)) for x in leg.get("depots") or [] if "техобслуживания" not in x.get("what", ""))
        k = sum(int(x.get("count", 1)) for x in leg.get("depots") or [] if "техобслуживания" in x.get("what", ""))
        out.append({"mode": leg.get("mode"), "depots": d, "maint_buildings": k})
    return out
