"""Era technology upgrades: fleet replacement for EVERY line kind and infrastructure upgrades
(track type, catenary, road type, passing loops / double track), see docs/upgrades.md.

Public API
----------
fleet_replacement(planner, sim, year)        -> [ProjectCandidate kind "upgrade", meta["upgrade"] == "fleet"]
infrastructure_upgrades(planner, sim, year)  -> [ProjectCandidate kind "upgrade", meta["upgrade"] in
                                                 {"track", "catenary", "road", "capacity"}]
apply_upgrade(planner, sim, candidate)       -> state-delta dict (already applied to sim, cash paid,
                                                 plan step written)
yearly_upgrades(planner, sim)                -> applied candidates (generate + rank + cash check + apply);
                                                 the one call planner.run() needs every January
segment_cycle_s(planner, sim, segment, units, overrides=None) -> round-trip seconds (dynamics)
lines(planner, sim)                          -> [Segment]: every running fleet of the plan

Model
-----
* A line is split into fleets ("segments"): a direct cargo line = 1 fleet; a tour = 1 fleet driving
  several legs; a passenger bundle / multimodal chain = 1 fleet per leg. Each leg knows its infra key
  (min_node, max_node, mode) - the same keys the planner keeps in sim.built_pairs.
* Trip times come from dynamics.simulate_run on the routing-matrix route (profile, curves) with the
  speed limit of the leg's CURRENT way (sim.infra state: simple track 100 km/h, old country road
  60 km/h until upgraded). A 140 km/h diesel on simple track runs at 100 km/h - this is why the old
  planner._renew bought loss-making locomotives (evaluate_link ignores the track speed).
  Electric traction (engine type 2 without a steam/diesel engine) is only offered when every leg of
  the fleet is electrified.
* Defender / challenger (docs/real_logistics.md 6.3): the defender's marginal cost of one more year
  MC = (S_now - S_next) + r * S_now + running(age + 0.5) against the challenger's equivalent annual
  cost over the remaining horizon EAC = (P - S_T / (1+r)^T) * CRF(r, T) + PV(running with aging) *
  CRF. Resale S = RESALE_FACTOR * value * (1 - age / lifespan) (planner convention). Running cost
  grows with age past AGING_START of the lifespan by AGING_RUN_PER_EFFECT x
  vehicleMaintenanceRunningCostsEffect (2 on VeryHard) - ASSUMPTION, the engine-side decay is
  unknown (mechanics.md 5, Q7). The challenger fleet is sized to the same throughput (fewer, faster
  or bigger vehicles allowed); the defender may also be "resized" (surplus consists sold) after a
  faster track. Replacement only when the saving > max(MIN_SAVING_ABS, MIN_SAVING_SHARE x MC).
* Infrastructure (docs/real_logistics.md 6.4): per corridor (the pair keys of one fleet), every way
  better than the current one and available in the year is priced: benefit B = sum over all fleets
  touching the corridor of [best net annual cost now - best net annual cost after] minus the upkeep
  change. Cost C = length x new way price (x (1 + railroadCatenary) when electrified; catenary only:
  length x price x railroadCatenary). Built in the FIRST year with B / C >= LAMBDA and NPV > 0
  (first-year-return rule) - benefits grow with traffic and technology, so thin lines wait.
* Capacity: trains/year per rail pair vs. the current layout (loops recorded at first sight from
  capacity.loops_needed); when traffic outgrows it, capacity.rail_line_plan prices loops / a second
  track; benefit = profit of the volume that could not run otherwise.
"""
from __future__ import annotations

import copy
import time as _time

import math
from dataclasses import dataclass, field

from . import capacity as cap_mod
from . import dynamics as dy
from .candidates import ProjectCandidate, VehicleLine
from .vehicles import Vehicle, available

# --------------------------------------------------------------------------- parameters

LAMBDA = 0.15                 # first-year-return hurdle for infrastructure (B_first_year / C)
DISCOUNT = 0.05               # = planner.DISCOUNT
RESALE_FACTOR = 0.8           # = planner.RESALE_FACTOR (sale vs linear depreciated value, ASSUMPTION)
AGING_START = 0.5             # share of lifespan after which maintenance no longer restores fully (ASSUMPTION)
AGING_RUN_PER_EFFECT = 0.15   # running cost +15 % x effect at end of life (VeryHard effect 2 -> +30 %) (ASSUMPTION)
AGING_PERF_PER_EFFECT = 0.05  # top speed -5 % x effect at end of life (VeryHard -> -10 %) (ASSUMPTION)
MIN_SAVING_SHARE = 0.05       # replace only if the yearly saving > 5 % of the defender's yearly cost...
MIN_SAVING_ABS = 20_000.0     # ...and > 20 000 per year
MIN_FLEET_AGE_YEARS = 3.0     # no replacement of a fleet younger than this (anti-churn)
MIN_MODEL_YEARS_LEFT = 10     # never buy a model leaving the market within this many years (no repurchase for growth)
MIN_EVAL_YEARS = 5            # EAC horizon floor (end of the plan)
MAX_EVAL_YEARS = 40
FREQ_KEEP = 0.75              # passenger fleets keep >= 75 % of the departures
TOP_K = 3                     # challengers re-timed with dynamics after the analytic prescreen
DYN_DS = 20.0                 # dynamics integration step (m) - coarse, cached
MAX_WAGONS = 30
PLATFORM_SEG_M = 40.0
PLATFORM_SEG_COST = 60_000.0  # track module 30 000 + platform module 30 000 per 40 m (x infra scale)
CATENARY_UPKEEP_SHARE = 0.3   # catenary upkeep as share of the track upkeep (ASSUMPTION, cost share is 0.3)
REBUILD_SHARE = 1.0           # track/road upgrade costs the full price of the new way (ASSUMPTION, Q8)
INFRA_LIFE_YEARS = 30
TOWN_STOP_SPACING_M = 600.0   # town bus/tram loops without a known cycle: length = stops x spacing
TOWN_SPEED_OLD, TOWN_SPEED_NEW = 40 / 3.6, 50 / 3.6     # town roads before / after 1940 (towns rebuild them)
STOP_DELAY_S = 4.0

STEAM, ELECTRIC, DIESEL = 1, 2, 3                      # landVehicle.engines[].type
TRACTION_RU = {"steam": "паровая", "diesel": "дизельная", "electric": "электрическая",
               "horse": "гужевая", "motor": "моторная", "ship": "судно", "none": "—"}
WAY_RU = {"simple": "простой путь (100 км/ч)", "standard": "стандартный путь (160 км/ч)",
          "high_speed": "скоростной путь (350 км/ч)"}
SKIP_WAYS = ("schienenzeppelin",)


# --------------------------------------------------------------------------- data

@dataclass
class Leg:
    a: object                  # node index (int) or hub key
    b: object
    length_m: float
    pair: tuple | None         # infra key (min, max, mode) or None (water, town loop)
    loaded: bool = True


@dataclass
class Segment:
    """One fleet of one line."""
    line_id: str
    idx: int                   # fleet index inside the line (pc.legs index for multi-fleet lines, else 0)
    kind: str
    mode: str                  # road | rail | tram | water
    cargos: list[str]
    units: list[tuple[Vehicle, int]]    # one consist
    n: int                     # consists
    need: float                # units/year on the busiest direction / leg
    legs: list[Leg]
    round_trip: bool           # out-and-back over each leg (False: the legs form the whole cycle)
    stops: int
    cycle_s: float | None      # cycle known from the generator (fallback only)
    income: float              # income attributable to this fleet
    bought_month: int
    value: float               # purchase value of the fleet
    ref: object                # planner.Project | ProjectCandidate
    leg_ref: int | None = None
    passenger: bool = False
    town_loop: bool = False
    vehicle_format: str = "line"   # how pc.legs[k]["vehicles"] is stored: "line" (vars) | "step" (model/name)
    ipu: float = 0.0           # income per unit carried (lost volume of an aged fleet is priced with it)

    @property
    def life(self) -> float:
        return min((v.lifespan_years for v, _ in self.units), default=30.0) or 30.0

    @property
    def capacity(self) -> int:
        return consist_capacity(self.units, self.cargos)

    @property
    def running(self) -> float:
        return self.n * sum(v.running_cost * k for v, k in self.units)

    @property
    def key(self) -> tuple:
        return (self.line_id, self.idx)


@dataclass
class Option:
    """A fleet choice for a segment under a given infrastructure."""
    units: list[tuple[Vehicle, int]]
    n: int
    cycle_s: float
    price: float               # whole fleet purchase (0 for the kept defender)
    running: float             # base running per year (fleet)
    extra_capex: float = 0.0   # platform extension
    income: float = 0.0
    annual: float = 0.0        # equivalent annual cost (MC for the defender, EAC for challengers)
    keep: bool = False         # defender kept (possibly resized)
    sold: int = 0              # defender consists sold when resized

    @property
    def net(self) -> float:
        return self.annual - self.income

    @property
    def label(self) -> str:
        return label(self.units)


@dataclass
class InfraState:
    way: str
    electrified: bool = False
    tracks: int = 1
    loops: int | None = None   # None = not yet recorded


# --------------------------------------------------------------------------- helpers

def label(units) -> str:
    return " + ".join(f"{k}× {v.name}" if k > 1 else v.name for v, k in units)


def consist_capacity(units, cargos) -> int:
    if not cargos:
        return 0
    return min(sum(v.carries(c) * k for v, k in units) for c in cargos)


def crf(r: float, years: float) -> float:
    years = max(years, 1e-9)
    return r / (1 - (1 + r) ** -years) if r > 0 else 1 / years


def aging_ramp(age: float, life: float) -> float:
    """0 until AGING_START of the lifespan, 1 at the end of it, growing linearly beyond."""
    x = age / max(life, 1.0)
    return min(1.0, max(0.0, (x - AGING_START) / (1 - AGING_START))) + max(0.0, x - 1.0)


def aging_mult(age: float, life: float, effect: float) -> float:
    """Running-cost multiplier of a vehicle of `age` years (ASSUMPTION, see module doc)."""
    return 1.0 + AGING_RUN_PER_EFFECT * effect * aging_ramp(age, life)


def aging_speed(age: float, life: float, effect: float) -> float:
    """Top-speed factor of an aged vehicle (maintenance performance penalty, ASSUMPTION)."""
    return max(0.5, 1.0 - AGING_PERF_PER_EFFECT * effect * aging_ramp(age, life))


def resale(value: float, age: float, life: float) -> float:
    return RESALE_FACTOR * value * max(0.0, 1.0 - age / max(life, 1.0))


_EAC: dict = {}


def eac_new(price: float, running: float, life: float, years: int, effect: float, r: float = DISCOUNT) -> float:
    """Equivalent annual cost of buying a fleet now and holding it `years` (sold at the end).
    Memoized (pure; the same fleets are priced again and again)."""
    k = (price, running, life, years, effect, r, AGING_RUN_PER_EFFECT, AGING_START, RESALE_FACTOR)
    v = _EAC.get(k)
    if v is None:
        if len(_EAC) > 500_000:
            _EAC.clear()
        v = _EAC[k] = _eac_new(price, running, life, years, effect, r)
    return v


def _eac_new(price: float, running: float, life: float, years: int, effect: float, r: float = DISCOUNT) -> float:
    years = max(1, int(years))
    pv_run = sum(running * aging_mult(t + 0.5, life, effect) / (1 + r) ** (t + 1) for t in range(years))
    s_t = resale(price, years, life)
    return (price - s_t / (1 + r) ** years) * crf(r, years) + pv_run * crf(r, years)


def mc_keep(value: float, age: float, life: float, running: float, effect: float, r: float = DISCOUNT) -> float:
    """Marginal cost of keeping a fleet one more year (lost resale + interest on it + aged running)."""
    s0, s1 = resale(value, age, life), resale(value, age + 1, life)
    return (s0 - s1) + r * s0 + running * aging_mult(age + 0.5, life, effect)


def should_replace(mc_defender: float, eac_challenger: float, income_gain: float = 0.0) -> bool:
    """Defender/challenger rule with the anti-churn threshold."""
    saving = mc_defender - (eac_challenger - income_gain)
    return saving > max(MIN_SAVING_ABS, MIN_SAVING_SHARE * mc_defender)


def engine_types(game) -> dict[int, set[int]]:
    cache = getattr(game, "_upg_engine_types", None)
    if cache is None:
        cache = {}
        for raw in game.raw.get("vehicles", []):
            md = raw.get("metadata")
            if not isinstance(md, dict):
                continue
            eng = (md.get("landVehicle") or {}).get("engines")
            eng = eng if isinstance(eng, list) else (list(eng.values()) if isinstance(eng, dict) else [])
            cache[raw["id"]] = {e.get("type") for e in eng if isinstance(e, dict) and (e.get("power") or 0) > 0}
        game._upg_engine_types = cache
    return cache


def needs_catenary(game, v: Vehicle) -> bool:
    if v.carrier != "rail":
        return False
    t = engine_types(game).get(v.id, set())
    return ELECTRIC in t and not (t & {STEAM, DIESEL})


def traction(game, units) -> str:
    powered = [v for v, _ in units if v.power_kw > 0 or v.carrier == "water"]
    if not powered:
        return "none"
    v = powered[0]
    if v.carrier == "water":
        return "ship"
    t = engine_types(game).get(v.id, set())
    if v.carrier == "rail":
        return "electric" if ELECTRIC in t and not (t & {STEAM, DIESEL}) else \
            "diesel" if DIESEL in t or ELECTRIC in t else "steam" if STEAM in t else "steam"
    if v.carrier == "road" and v.top_speed * 3.6 <= 20.5 and v.power_kw < 10:
        return "horse"
    return "electric" if t == {ELECTRIC} else "motor"


def _real(v: Vehicle) -> bool:
    nm = (v.name or "").lower()
    return (v.price > 1000 and v.year_from > 0 and v.top_speed > 0 and not nm.startswith(("fake", "lead -"))
            and "invisible" not in nm and "/fake" not in v.file and "_fake" not in v.file)


def _role_group(v: Vehicle) -> str:
    return "bus" if v.role == "bus" else "truck" if v.role == "truck" else v.carrier


# --------------------------------------------------------------------------- context

class _Ctx:
    def __init__(self, pl, sim, year: int):
        self.pl, self.sim, self.game, self.year = pl, sim, pl.game, year
        self.vid = {v.id: v for v in pl.vehicles}
        self.vfile = {}
        for v in pl.vehicles:
            self.vfile.setdefault(v.file.rsplit("/", 1)[-1], v)
        self.year_s = self.game.year_ms / 1000.0
        self.horizon = getattr(pl, "horizon", 2030) or 2030
        adv = (self.game.raw.get("baseConfig") or {}).get("advancedOptions") or {}
        self.effect = float(adv.get("vehicleMaintenanceRunningCostsEffect", 2.0) or 0.0)
        self.perf_effect = float(adv.get("vehicleMaintenancePerformanceEffect", 2.0) or 0.0)
        costs = (self.game.raw.get("baseConfig") or {}).get("costs") or {}
        self.cat_share = float(costs.get("railroadCatenary", 0.3) or 0.3)
        self.infra_scale = getattr(self.game, "infra_cost_scale", 1.5) or 1.0
        self.ways = {w.key: w for w in self.game.ways}
        self.cache = sim.__dict__.setdefault("_upg_cache", {})
        self.routes = sim.__dict__.setdefault("_upg_routes", {})
        self.cache_year: dict = {}
        self.infra = infra_state(pl, sim)

    # ---- years over which a new fleet is evaluated
    def eval_years(self, life: float) -> int:
        return int(max(MIN_EVAL_YEARS, min(MAX_EVAL_YEARS, life, self.horizon - self.year)))

    def vehicle(self, item) -> Vehicle | None:
        if isinstance(item, VehicleLine):
            return self.vid.get(item.vehicle_id)
        if isinstance(item, dict):
            if item.get("vehicle_id") in self.vid:
                return self.vid[item["vehicle_id"]]
            return self.vfile.get(str(item.get("model") or item.get("file") or "").rsplit("/", 1)[-1])
        return None

    # ---- ways
    def state(self, pair, mode: str) -> InfraState:
        if pair is None:
            return InfraState(_default_way(self.pl, mode))
        st = self.infra.get(pair)
        if st is None:
            st = self.infra[pair] = InfraState(_default_way(self.pl, mode))
        return st

    def way(self, key: str | None):
        return self.ways.get(key) if key else None

    def route(self, mode: str, a, b):
        """Routing-matrix Route a -> b (None for hubs, water, missing matrices)."""
        if not (isinstance(a, int) and isinstance(b, int)) or a == b:
            return None
        mat = (getattr(self.pl, "mat", None) or {}).get(mode)
        if mat is None:
            return None
        k = (mode, a, b)
        if k not in self.routes:
            try:
                self.routes[k] = mat.route(a, b)
            except Exception:
                self.routes[k] = None
        return self.routes[k]


def _default_way(pl, mode: str) -> str:
    mat = (getattr(pl, "mat", None) or {}).get(mode)
    if mat is not None and getattr(mat, "way_key", None):
        return mat.way_key
    from .routing import default_way
    w = default_way(pl.game, "rail" if mode == "rail" else "road", pl.game.year)
    return w.key if w else ("simple" if mode == "rail" else "country_old_small")


def infra_state(pl, sim) -> dict:
    """sim.infra: pair key -> InfraState (way template, catenary, tracks, loops). Missing pairs are on
    the way the routing matrices were built with (1900: simple track / old country road)."""
    return sim.__dict__.setdefault("infra", {})


# --------------------------------------------------------------------------- line extraction

def _units_from_list(ctx: _Ctx, items, n_hint=None) -> tuple[list[tuple[Vehicle, int]], int]:
    counts: dict[int, list] = {}
    for it in items or []:
        v = ctx.vehicle(it)
        cnt = it.count if isinstance(it, VehicleLine) else int((it or {}).get("count", 0) or 0)
        if v is None or cnt <= 0:
            continue
        e = counts.setdefault(v.id, [v, 0])
        e[1] += cnt
    if not counts:
        return [], 0
    powered = [c for v, c in counts.values() if v.power_kw > 0 or v.carrier == "water"]
    n = int(n_hint) if n_hint else (min(powered) if powered else min(c for _, c in counts.values()))
    n = max(1, n)
    return [(v, max(1, round(c / n))) for v, c in counts.values()], n


def _pair(mode: str, a, b) -> tuple | None:
    if mode not in ("rail", "road") or a is None or b is None or a == b:
        return None
    if isinstance(a, int) and isinstance(b, int):
        return (min(a, b), max(a, b), mode)
    s = sorted([a, b], key=str)
    return (s[0], s[1], mode)


def _length(ctx: _Ctx, mode: str, a, b, fallback: float | None = None) -> float:
    if fallback:
        return float(fallback)
    mat = (getattr(ctx.pl, "mat", None) or {}).get(mode)
    if mat is not None and isinstance(a, int) and isinstance(b, int):
        L = float(mat.length[a, b])
        if math.isfinite(L) and L > 0:
            return L
    if mode == "water" and getattr(ctx.pl, "water", None) is not None and isinstance(a, int) and isinstance(b, int):
        L = float(ctx.pl.water.length[a, b])
        if math.isfinite(L):
            return L
    if isinstance(a, int) and isinstance(b, int):
        na, nb = ctx.pl.nodes[a], ctx.pl.nodes[b]
        return 1.3 * math.dist((na.x, na.y), (nb.x, nb.y))
    return 5000.0


def _project_segments(ctx: _Ctx) -> list[Segment]:
    out = []
    for pr in ctx.sim.projects:
        c = pr.cand
        units = [(ctx.vid[i], n) for i, n in pr.consist_ids if i in ctx.vid]
        if not units or pr.n_consists <= 0:
            continue
        L = float(getattr(c.ev, "route_m", 0) or _length(ctx, c.mode, c.p, c.d))
        out.append(Segment(pr.id, 0, "cargo", c.mode, [c.cargo], units, pr.n_consists, pr.volume,
                           [Leg(c.p, c.d, L, _pair(c.mode, c.p, c.d))], True, 2,
                           getattr(c.ev, "trip_time_s", None), pr.income, pr.bought_month, pr.vehicle_value, pr,
                           ipu=pr.income / pr.volume if pr.volume > 0 else 0.0))
    return out


def _generic_segments(ctx: _Ctx, pc: ProjectCandidate) -> list[Segment]:
    m = pc.meta or {}
    lid = m.get("id")
    if not lid or pc.kind == "upgrade":
        return []
    bought = m.get("fleet_bought") or {}
    start = int(m.get("start_month", 0) or 0)
    pax = pc.cargo == "passengers" or pc.kind in ("passenger", "workers")
    out: list[Segment] = []
    if m.get("generator") == "tours" and pc.legs:
        units, n = _units_from_list(ctx, pc.vehicles, m.get("n_consists"))
        if not units:
            return []
        legs = [Leg(l["from"], l["to"], float(l.get("length_m") or 0), _pair(pc.mode, l["from"], l["to"]),
                    float(l.get("load", 1) or 0) > 0) for l in pc.legs]
        cargos = list(m.get("cargos") or [pc.cargo])
        need = float(m.get("peak_load") or pc.volume)
        out.append(Segment(lid, 0, pc.kind, pc.mode, cargos, units, n, need, legs, False, len(legs),
                           m.get("trip_s"), pc.income, int(bought.get(0, start)), pc.vehicle_capex, pc,
                           ipu=pc.income / pc.volume if pc.volume > 0 else 0.0))
        return out
    if pc.legs and all(isinstance(l, dict) and l.get("vehicles") for l in pc.legs):
        for k, l in enumerate(pc.legs):
            fmt = "step" if any("model" in (x or {}) for x in l["vehicles"]) else "line"
            units, n = _units_from_list(ctx, l["vehicles"], l.get("n_consists"))
            mode = l.get("mode")
            if not units or mode not in ("road", "rail", "tram", "water"):
                continue
            nodes = l.get("nodes")
            if nodes is not None:
                a, b = (nodes[0], nodes[1]) if len(nodes) >= 2 else (nodes[0], None)
            else:
                a, b = (l.get("from") or {}).get("key"), (l.get("to") or {}).get("key")
            vol = float(l.get("volume") or 0)
            value = float(l.get("vehicle_capex") or sum(v.price * c * n for v, c in units))
            seg_pax = pax or l.get("kind") in ("passenger", "workers")
            if b is None:          # town loop inside a passenger bundle
                out.append(_town_loop(ctx, lid, k, pc, units, n, vol, None, l.get("stops"),
                                      float(l.get("income") or 0), int(bought.get(k, start)), value, fmt))
                continue
            L = _length(ctx, mode, a, b, l.get("length_m"))
            out.append(Segment(lid, k, pc.kind, mode, [pc.cargo or "passengers"], units, n,
                               vol / 2 if seg_pax else vol, [Leg(a, b, L, _pair(mode, a, b))], True, 2,
                               l.get("trip_time_s"), float(l.get("income") or 0), int(bought.get(k, start)),
                               value, pc, leg_ref=k, passenger=seg_pax, vehicle_format=fmt,
                               ipu=float(l.get("income") or 0) / vol if vol > 0 else 0.0))
        return out
    if pc.mode not in ("road", "rail", "tram", "water"):
        return []
    units, n = _units_from_list(ctx, pc.vehicles, m.get("n_consists"))
    if not units or len({v.carrier for v, _ in units}) > 1:
        return []
    if m.get("line") == "intra" or len(pc.nodes) == 1:
        return [_town_loop(ctx, lid, 0, pc, units, n, pc.volume, m.get("cycle_s"), m.get("stops"),
                           pc.income, int(bought.get(0, start)), pc.vehicle_capex, None)]
    if len(pc.nodes) < 2:
        return []
    a, b = pc.nodes[0], pc.nodes[1]
    L = _length(ctx, pc.mode, a, b, m.get("route_m"))
    cargos = [pc.cargo] if pc.cargo else (["passengers"] if pax else [])
    if not cargos:
        return []
    return [Segment(lid, 0, pc.kind, pc.mode, cargos, units, n, pc.volume / 2 if pax else pc.volume,
                    [Leg(a, b, L, _pair(pc.mode, a, b))], True, 2, m.get("cycle_s") or m.get("trip_s"),
                    pc.income, int(bought.get(0, start)), pc.vehicle_capex, pc, passenger=pax,
                    ipu=pc.income / pc.volume if pc.volume > 0 else 0.0)]


def _town_loop(ctx, lid, k, pc, units, n, volume, cycle_s, stops, income, bought, value, fmt) -> Segment:
    stops = int(stops or 8)
    mode = units[0][0].carrier if units[0][0].carrier in ("road", "tram") else "road"
    v_lim = TOWN_SPEED_NEW if ctx.year >= 1940 else TOWN_SPEED_OLD
    L = stops * TOWN_STOP_SPACING_M
    if cycle_s:
        v = min(min(u.top_speed for u, _ in units), v_lim)
        fixed = stops * (STOP_DELAY_S + 10.0)
        L = max(500.0, (float(cycle_s) - fixed) * v * 0.85)
    return Segment(lid, k, pc.kind, mode, ["passengers"], units, n, volume / 2, [Leg(None, None, L, None)],
                   False, stops, cycle_s, income, bought, value, pc, leg_ref=k if fmt else None,
                   passenger=True, town_loop=True, vehicle_format=fmt or "line",
                   ipu=income / volume if volume > 0 else 0.0)


def lines(pl, sim, year: int | None = None) -> list[Segment]:
    """Every running fleet of the plan (direct projects + generic lines of all kinds)."""
    ctx = _Ctx(pl, sim, year or sim.year)
    return _all_segments(ctx)


def _all_segments(ctx: _Ctx) -> list[Segment]:
    out = _project_segments(ctx)
    for pc in ctx.sim.generic:
        try:
            out += _generic_segments(ctx, pc)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out


# --------------------------------------------------------------------------- trip times

def _spec_key(units) -> tuple:
    return tuple((v.id, int(k)) for v, k in units)


def _quick_leg_s(spec: dy.ConsistSpec, payload: float, L: float, v_lim: float, stops: int = 1) -> float:
    v = max(0.5, min(spec.top_speed, v_lim if v_lim > 0 else spec.top_speed))
    m = max(spec.mass_empty_t + payload, 0.1)
    a = max(0.02, (spec.traction(0.5 * v) - spec.resistance(0.5 * v, 0.0, m)) / m)
    return L / v + stops * (v / (2 * a) + v / (2 * spec.brake))


def _leg_runs(ctx: _Ctx, seg: Segment, units, leg: Leg, way, quick: bool) -> tuple[float, float]:
    """(loaded run, return run) seconds over one leg; return run = 0 for loop legs."""
    mode = "tram" if seg.mode == "tram" else seg.mode
    cargo = seg.cargos[0] if seg.cargos else None
    pay = dy.payload_t(units, cargo, 1.0) if (cargo and leg.loaded) else 0.0
    back_pay = pay if seg.passenger else 0.0
    v_lim = way.speed if way is not None else 0.0
    if seg.town_loop:
        v_lim = TOWN_SPEED_NEW if ctx.year >= 1940 else TOWN_SPEED_OLD
    route = None if (quick or seg.town_loop or mode == "water") else ctx.route(seg.mode, leg.a, leg.b)
    key = (mode, leg.a, leg.b, round(leg.length_m), getattr(way, "key", None) if not seg.town_loop else v_lim,
           _spec_key(units), round(pay, 1), seg.round_trip, round(back_pay, 1), quick or route is None,
           seg.stops if seg.town_loop else 1)
    hit = ctx.cache.get(key)
    if hit is None and _COLLECT is not None:
        hit = _DRY.get(key)
    if hit is not None:
        return hit
    spec = dy.consist_spec(units, mode)
    if quick or route is None:
        stops = seg.stops if seg.town_loop else 1
        if mode == "water":
            fwd = leg.length_m / max(spec.top_speed, 0.5) + 60.0
        else:
            fwd = _quick_leg_s(spec, pay, leg.length_m, v_lim, stops)
        back = 0.0
        if seg.round_trip:
            back = fwd if (seg.passenger or mode == "water") else _quick_leg_s(spec, back_pay, leg.length_m, v_lim, 1)
        res = (fwd, back)
    else:
        coeffs = dy.speed_coeffs(ctx.game, way.key) if way is not None else None
        zones = dy.route_speed_limits(route, way, None, coeffs) if way is not None else []
        ds = DYN_DS if route.length_m < 60_000 else route.length_m / 3000.0
        job = (units, pay, route.profile if len(route.profile) >= 2 else route.length_m, zones,
               dict(mode=mode, round_trip=seg.round_trip, empty_payload_t=back_pay, ds=ds,
                    max_grade=getattr(way, "max_slope", None), spec=spec, sample_m=1e9),
               leg.length_m / route.length_m if route.length_m > 0 else 1.0)   # matrix vs line length
        # exact inputs of this simulation (the cache key rounds payload / length, so the first
        # caller's exact values win - prefetched results are matched on the exact inputs instead,
        # which keeps the cache history identical to a run without prefetching)
        xkey = (key, tuple((v.id, k) for v, k in units), pay, back_pay, leg.length_m)
        if _COLLECT is not None:
            # dry pass (yearly_upgrades with a prefetcher): remember the simulation, answer with
            # the analytic estimate, do not cache it - the real pass gets the exact value
            if xkey not in _COLLECT:
                _COLLECT[xkey] = job
            fwd = _quick_leg_s(spec, pay, leg.length_m, v_lim, 1)
            back = (fwd if seg.passenger else _quick_leg_s(spec, back_pay, leg.length_m, v_lim, 1))                 if seg.round_trip else 0.0
            return fwd, back
        res = _PREFETCHED.pop(xkey, None)
        if res is None:
            t_sim = _time.perf_counter()
            res = run_leg_job(job)
            _SIM_SERIAL[0] += _time.perf_counter() - t_sim
            _SIM_SERIAL[1] += 1
    if _COLLECT is not None:
        _DRY[key] = res             # the dry pass never writes the real cache (first caller wins there)
    else:
        ctx.cache[key] = res
    return res


_COLLECT: dict | None = None        # exact inputs -> simulation job while yearly_upgrades runs its dry pass
_PREFETCHED: dict = {}              # exact inputs -> result computed by the workers (this January)
_DRY: dict = {}                     # analytic results of the dry pass (kept apart from the real cache)
_SIM_SERIAL = [0.0, 0]              # seconds / count of simulations run in this process


def run_leg_job(job) -> tuple[float, float]:
    """One dynamics simulation of _leg_runs (pure; picklable job for worker processes)."""
    units, pay, profile, zones, kw, scale = job
    r = dy.simulate_run(units, pay, profile, zones, **kw)
    return r.time_s * scale, (r.empty.time_s * scale) if r.empty is not None else 0.0


def _cycle(ctx: _Ctx, seg: Segment, units, overrides: dict | None = None, quick: bool = False,
           speed_factor: float = 1.0) -> float:
    total = 0.0
    capu = consist_capacity(units, seg.cargos)
    cargo = seg.cargos[0] if seg.cargos else None
    lf = cap_mod.cargo_load_factor(ctx.game, cargo) if cargo else 1.0
    n_units = sum(k for v, k in units if v.capacity) or 1
    load_speed = min((v.load_speed for v, _ in units if v.capacity), default=1.0)
    load_t = cap_mod.loading_time_s(capu, load_speed, lf, n_units=n_units, calibration=1.0) if capu else 0.0
    for leg in seg.legs:
        way = None
        if leg.pair is not None:
            st = (overrides or {}).get(leg.pair) or ctx.state(leg.pair, seg.mode)
            way = ctx.way(st.way)
        elif seg.mode in ("rail", "road") and not seg.town_loop:
            way = ctx.way(_default_way(ctx.pl, seg.mode))
        fwd, back = _leg_runs(ctx, seg, units, leg, way, quick)
        total += (fwd + back) / max(speed_factor, 0.1)
        if seg.town_loop:
            total += seg.stops * (STOP_DELAY_S + (load_t / max(seg.stops, 1)))
        elif seg.round_trip:
            total += 2 * load_t + 2 * STOP_DELAY_S
        else:
            total += (2 * load_t if leg.loaded else 0.0) + STOP_DELAY_S
    return total


def segment_cycle_s(pl, sim, seg: Segment, units=None, overrides: dict | None = None, quick: bool = False) -> float:
    """Round-trip (cycle) seconds of one consist of `seg` (default: its current consist) with the ways in
    sim.infra, or `overrides` {pair: InfraState}. quick=True: analytic (no dynamics)."""
    return _cycle(_Ctx(pl, sim, sim.year), seg, units or seg.units, overrides, quick)


# --------------------------------------------------------------------------- options

def _electric_ok(ctx: _Ctx, seg: Segment, overrides: dict | None) -> bool:
    if seg.mode != "rail":
        return True
    for leg in seg.legs:
        st = (overrides or {}).get(leg.pair) if leg.pair is not None else None
        st = st or (ctx.state(leg.pair, seg.mode) if leg.pair is not None else None)
        if st is None or not st.electrified:
            return False
    return True


def _rail_templates(ctx: _Ctx, seg: Segment, elec_ok: bool) -> list[tuple]:
    """Consist templates for a rail fleet: (loco, wagon) for cargo, fixed consists for passengers."""
    avail = [v for v in available(ctx.pl.vehicles, ctx.year, "rail") if _real(v)]
    ok = lambda v: elec_ok or not needs_catenary(ctx.game, v)
    if seg.passenger:
        k = ("pax_rail", elec_ok)
        if k not in ctx.cache_year:
            from .passengers import PassengerParams, rail_consists
            ctx.cache_year[k] = [("fixed", list(c.units))
                                 for c in rail_consists(ctx.pl.vehicles, ctx.year, PassengerParams())
                                 if all(ok(v) and _real(v) for v, _ in c.units)]
        return ctx.cache_year[k]
    locos = [v for v in avail if v.role == "loco" and ok(v)]
    wagons = [v for v in avail if v.role == "wagon" and all(v.carries(c) for c in seg.cargos)]
    return [("lw", loco, w) for loco in locos for w in wagons]


def _max_wagons(loco: Vehicle, wagon: Vehicle, grade: float) -> int:
    resist = 0.002 + grade
    max_mass_t = (loco.tractive_effort_kn * 1000) / (9.81 * resist) / 1000 if loco.tractive_effort_kn else 0
    per = wagon.weight_empty_t + wagon.weight_max_payload_t
    n = int((max_mass_t - loco.weight_empty_t) // per) if per > 0 else MAX_WAGONS
    return max(1, min(MAX_WAGONS, n))


def _grade(ctx: _Ctx, seg: Segment) -> float:
    """Ruling grade for the tractive limit: the planner's convention (economics.evaluate_link 1 %).
    Steeper sections are then caught by dynamics (crawl -> long cycle -> more trains)."""
    return 0.01


def _platform_cost(ctx: _Ctx, seg: Segment, units) -> float:
    if seg.mode != "rail":
        return 0.0
    old = sum(k for _, k in seg.units) * dy.UNIT_LENGTH_M["rail"]
    new = sum(k for _, k in units) * dy.UNIT_LENGTH_M["rail"]
    if new <= old:
        return 0.0
    stations = len({x for leg in seg.legs for x in (leg.a, leg.b) if x is not None}) or 2
    return stations * math.ceil((new - old) / PLATFORM_SEG_M) * PLATFORM_SEG_COST * ctx.infra_scale


def _size(ctx: _Ctx, seg: Segment, units, cycle: float, need: float, n_old_freq: float | None) -> int:
    capu = consist_capacity(units, seg.cargos)
    if capu <= 0 or cycle <= 0:
        return 0
    n = max(1, math.ceil(need * cycle / (capu * ctx.year_s) - 1e-9))
    if n_old_freq is not None:
        n = max(n, math.ceil(FREQ_KEEP * n_old_freq * cycle - 1e-9))
    return n


def _model_ok(ctx: _Ctx, units) -> bool:
    return all(v.year_to == 0 or v.year_to - ctx.year >= MIN_MODEL_YEARS_LEFT for v, _ in units)


def _candidate_units(ctx: _Ctx, seg: Segment, elec_ok: bool, need: float, overrides) -> list[list]:
    """Challenger consists (one consist each), already sized in wagons; models about to leave the
    market are skipped (MIN_MODEL_YEARS_LEFT)."""
    return [u for u in _candidate_units_all(ctx, seg, elec_ok, need, overrides) if _model_ok(ctx, u)]


def _candidate_units_all(ctx: _Ctx, seg: Segment, elec_ok: bool, need: float, overrides) -> list[list]:
    if seg.mode == "rail":
        out = []
        grade = _grade(ctx, seg)
        for t in _rail_templates(ctx, seg, elec_ok):
            if t[0] == "fixed":
                out.append(t[1])
                continue
            _, loco, w = t
            n_max = _max_wagons(loco, w, grade)
            wcap = consist_capacity([(w, 1)], seg.cargos)
            if wcap <= 0:
                continue
            full = [(loco, 1), (w, n_max)]
            T = _cycle(ctx, seg, full, overrides, quick=True)
            per_wagon_year = wcap * ctx.year_s / T
            wagons = max(1, math.ceil(need / per_wagon_year))
            trains = math.ceil(wagons / n_max)
            out.append([(loco, 1), (w, max(1, math.ceil(wagons / trains)))])
        return out
    if seg.mode == "tram":
        from .passengers import tram_consists
        return [list(c.units) for c in tram_consists(ctx.game, ctx.pl.vehicles, ctx.year)]
    group = _role_group(seg.units[0][0])
    out = []
    for v in available(ctx.pl.vehicles, ctx.year, seg.mode if seg.mode != "water" else "water"):
        if not _real(v) or _role_group(v) != group or not all(v.carries(c) for c in seg.cargos):
            continue
        out.append([(v, 1)])
    return out


def _price_factor(units) -> float:
    cap = sum(v.carries("passengers") * k for v, k in units)
    return (sum(v.price_factor * v.carries("passengers") * k for v, k in units) / cap) if cap else 0.5


def _need(ctx: _Ctx, seg: Segment) -> tuple[float, float | None]:
    """(units/year the fleet must move, departures/s for passenger lines) - measured with the CURRENT
    infrastructure and fleet, so a model mismatch (dynamics vs. the generator's cycle) never makes the
    defender look over- or under-sized: the challenger only has to match what the defender delivers."""
    k = ("need", seg.key)
    hit = ctx.cache_year.get(k)
    if hit is None:
        T = _cycle(ctx, seg, seg.units)
        capu = consist_capacity(seg.units, seg.cargos)
        thr = seg.n * capu * ctx.year_s / T if T > 0 and capu else 0.0
        need = min(seg.need, thr) if seg.need > 0 else 0.9 * thr
        freq = (seg.n / T) if (seg.passenger and T > 0) else None
        hit = ctx.cache_year[k] = (need, freq)
    return hit


def _leg_states(ctx: _Ctx, seg: Segment, overrides: dict | None) -> tuple:
    """Effective (way, catenary) of every leg of seg under sim.infra + overrides."""
    out = []
    for leg in seg.legs:
        if leg.pair is None:
            out.append(None)
            continue
        st = (overrides or {}).get(leg.pair) or ctx.state(leg.pair, seg.mode)
        out.append((st.way, st.electrified))
    return tuple(out)


def evaluate_segment(ctx: _Ctx, seg: Segment, overrides: dict | None = None) -> tuple[Option, list[Option]]:
    """(defender kept/resized, challengers sorted by net annual cost) under the infra state
    (sim.infra plus `overrides`). Memoized per context on the segment and the effective state of
    its own legs (corridor upgrades re-evaluate the same segment under many overrides that differ
    only on other pairs); copies are returned."""
    memo = ctx.__dict__.setdefault("_eval_memo", {})
    k = (id(seg), seg.key, _leg_states(ctx, seg, overrides))
    hit = memo.get(k)
    if hit is None:
        hit = memo[k] = (seg, _evaluate_segment(ctx, seg, overrides))
    d, out = hit[1]
    return copy.copy(d), [copy.copy(o) for o in out]


def _evaluate_segment(ctx: _Ctx, seg: Segment, overrides: dict | None = None) -> tuple[Option, list[Option]]:
    age = max(0.0, (ctx.sim.month - seg.bought_month) / 12)
    life = seg.life
    need, freq = _need(ctx, seg)
    T_old = _cycle(ctx, seg, seg.units, overrides)
    # defender: keep, resized to the need (surplus consists sold); an aged fleet is slower
    # (maintenance performance penalty) - the volume it can no longer move is lost income
    sf = aging_speed(age + 0.5, life, ctx.perf_effect)
    T_def = _cycle(ctx, seg, seg.units, overrides, speed_factor=sf) if sf < 0.999 else T_old
    n_def = min(seg.n, max(1, _size(ctx, seg, seg.units, T_def, need, freq) or seg.n))
    capu = consist_capacity(seg.units, seg.cargos)
    thr = n_def * capu * ctx.year_s / T_def if T_def > 0 else 0.0
    lost = max(0.0, need - thr) * seg.ipu * (2 if seg.passenger else 1)
    per_value = seg.value / max(seg.n, 1)
    run_one = sum(v.running_cost * k for v, k in seg.units)
    d = Option(seg.units, n_def, T_def, 0.0, n_def * run_one, keep=True, sold=seg.n - n_def,
               income=(seg.income if seg.passenger else 0.0) - lost)
    d.annual = mc_keep(per_value * n_def, age, life, n_def * run_one, ctx.effect)
    # challengers
    years = ctx.eval_years(life)
    elec_ok = _electric_ok(ctx, seg, overrides)
    pf_old = _price_factor(seg.units) if seg.passenger else 0.5
    pre = []
    for units in _candidate_units(ctx, seg, elec_ok, need, overrides):
        if _spec_key(units) == _spec_key(seg.units):
            continue
        T = _cycle(ctx, seg, units, overrides, quick=True)
        n = _size(ctx, seg, units, T, need, freq)
        if n <= 0:
            continue
        price = n * sum(v.price * k for v, k in units)
        run = n * sum(v.running_cost * k for v, k in units)
        lifec = min(v.lifespan_years for v, _ in units) or 30.0
        pre.append((eac_new(price, run, lifec, ctx.eval_years(lifec), ctx.effect), units))
    pre.sort(key=lambda x: x[0])
    out = []
    for _, units in pre[:TOP_K]:
        T = _cycle(ctx, seg, units, overrides)
        n = _size(ctx, seg, units, T, need, freq)
        if n <= 0:
            continue
        price = n * sum(v.price * k for v, k in units)
        run = n * sum(v.running_cost * k for v, k in units)
        lifec = min(v.lifespan_years for v, _ in units) or 30.0
        plat = _platform_cost(ctx, seg, units)
        inc = 0.0
        if seg.passenger:
            inc = seg.income * (0.5 + _price_factor(units)) / (0.5 + pf_old)
        o = Option(units, n, T, price, run, plat, inc)
        o.annual = eac_new(price, run, lifec, ctx.eval_years(lifec), ctx.effect) + plat * crf(DISCOUNT, years)
        out.append(o)
    out.sort(key=lambda o: o.net)
    return d, out


def best_option(ctx: _Ctx, seg: Segment, overrides: dict | None = None) -> tuple[Option, Option]:
    """(defender, chosen option) - the challenger only when the replacement rule says so."""
    d, ch = evaluate_segment(ctx, seg, overrides)
    if ch and should_replace(d.annual, ch[0].annual, ch[0].income - d.income):
        return d, ch[0]
    return d, d


# --------------------------------------------------------------------------- 1. fleets

def _vehicle_steps(units, n) -> list[dict]:
    return [{"model": v.file.rsplit("/", 1)[-1], "name": v.name, "count": k * n, "price": round(v.price)}
            for v, k in units]


def _fleet_candidate(ctx: _Ctx, seg: Segment, d: Option, o: Option, reason: str = "") -> ProjectCandidate | None:
    age = max(0.0, (ctx.sim.month - seg.bought_month) / 12)
    if o.keep:
        if o.sold <= 0:
            return None
        sold_value = resale(seg.value / max(seg.n, 1) * o.sold, age, seg.life)
        d_run = -o.sold * sum(v.running_cost * k for v, k in seg.units)
        purchase, res, net = 0.0, sold_value, -sold_value
        title = f"Линия {seg.line_id}: продать лишние {o.sold}× {seg_label(seg)}"
        details = (f"После ускорения ({reason or 'круг короче'}) для {seg.need:,.0f} ед./год хватает {o.n} из {seg.n} "
                   f"составов. Продажа ≈ {sold_value:,.0f}, экономия на содержании {-d_run:,.0f}/год.")
        d_inc = 0.0
    else:
        res = resale(seg.value, age, seg.life)
        purchase = o.price + o.extra_capex
        net = purchase - res
        d_run = o.running - seg.running
        d_inc = (o.income - seg.income) if seg.passenger else 0.0
        t_from, t_to = traction(ctx.game, seg.units), traction(ctx.game, o.units)
        change = f"{TRACTION_RU.get(t_from, t_from)} → {TRACTION_RU.get(t_to, t_to)} тяга; " if t_from != t_to else ""
        title = f"Обновить технику на линии {seg.line_id}: {seg.n}× {seg_label(seg)} → {o.n}× {o.label}"
        aged = (f"старый парк изношен (скорость −{1 - aging_speed(age, seg.life, ctx.perf_effect):.0%}, "
                f"содержание +{aging_mult(age, seg.life, ctx.effect) - 1:.0%}); ") if aging_ramp(age, seg.life) > 0 else ""
        details = (f"{change}{aged}круг {d.cycle_s / 60:.1f} → {o.cycle_s / 60:.1f} мин, составов {seg.n} → {o.n}. "
                   f"Покупка {purchase:,.0f}"
                   f"{f' (вкл. удлинение платформ {o.extra_capex:,.0f})' if o.extra_capex else ''}, "
                   f"продажа старой ≈ {res:,.0f} (возраст {age:.0f} лет из {seg.life:.0f}). "
                   f"Годовая стоимость: оставить {d.annual:,.0f} (износ, проценты, содержание с надбавкой за "
                   f"старение) против {o.annual:,.0f} у новой (EAC). Содержание {d_run:+,.0f}/год"
                   f"{f', доход {d_inc:+,.0f}/год' if d_inc else ''}.")
    saving = d.net - o.net
    if not o.keep and not should_replace(d.annual, o.annual, o.income - d.income):
        return None
    mode_kind = "road" if seg.mode == "tram" else seg.mode
    return ProjectCandidate(
        kind="upgrade", mode=mode_kind, title=title, details=details.replace(",", " "),
        nodes=[x for leg in seg.legs for x in (leg.a, leg.b) if isinstance(x, int)][:2],
        cargo=seg.cargos[0] if seg.cargos else None, volume=seg.need, income=d_inc, running=d_run,
        maintenance=0.0, vehicle_capex=max(0.0, net), infra_capex=0.0,
        vehicles=[VehicleLine(v.id, v.name, v.file, k * o.n, v.price) for v, k in o.units] if not o.keep else [],
        vehicle_life_years=min(v.lifespan_years for v, _ in o.units) or 30.0,
        meta={"upgrade": "fleet", "line": seg.line_id, "segment": seg.idx, "line_kind": seg.kind,
              "from": seg_label(seg), "to": o.label, "n_from": seg.n, "n_to": o.n,
              "traction_from": traction(ctx.game, seg.units), "traction_to": traction(ctx.game, o.units),
              "cycle_from_s": round(d.cycle_s, 1), "cycle_to_s": round(o.cycle_s, 1),
              "mc_keep": round(d.annual), "eac_new": round(o.annual), "eac_saving": round(saving),
              "purchase": round(purchase), "resale": round(res), "net_cost": round(net),
              "platform_cost": round(o.extra_capex), "age_years": round(age, 1), "year": ctx.year,
              "_seg": seg, "_opt": o})


def seg_label(seg: Segment) -> str:
    return label(seg.units)


def fleet_replacement(pl, sim, year: int) -> list[ProjectCandidate]:
    """Defender/challenger test for every fleet of every line kind (direct cargo, tours, passenger
    lines and bundles, worker lines, multimodal legs). Returns kind "upgrade" candidates:
    vehicle_capex = purchase (+ platform extension) - resale, income/running = yearly deltas (cash
    books), meta["eac_saving"] = economic gain incl. capital and aging."""
    ctx = _Ctx(pl, sim, year)
    out = []
    for seg in _all_segments(ctx):
        age = (sim.month - seg.bought_month) / 12
        if age < MIN_FLEET_AGE_YEARS or seg.capacity <= 0:
            continue
        try:
            d, o = best_option(ctx, seg)
        except (ValueError, ZeroDivisionError):
            continue
        c = _fleet_candidate(ctx, seg, d, o) if not o.keep else None
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: -c.meta["eac_saving"])
    return out


# --------------------------------------------------------------------------- 2. infrastructure

def _way_targets(ctx: _Ctx, mode: str, cur: InfraState) -> list[InfraState]:
    w0 = ctx.way(cur.way)
    v0 = w0.speed if w0 else 0.0
    out = []
    if mode == "rail":
        elec_years = ctx.year >= getattr(ctx.game, "catenary_year", 1921)
        for w in ctx.game.ways_available("rail", ctx.year):
            if w.electrified or any(s in w.key for s in SKIP_WAYS):
                continue
            for el in ((False, True) if elec_years else (False,)):
                if w.speed < v0 or (cur.electrified and not el):
                    continue
                if w.speed > v0 + 0.1 or (el and not cur.electrified):
                    out.append(InfraState(w.key, el, cur.tracks, cur.loops))
    else:
        for w in ctx.game.ways_available("road", ctx.year):
            if not w.key.startswith(("country", "highway")) or any(s in w.key for s in ("barriers", "bus", "trees")):
                continue
            if w.speed > v0 + 0.1:
                out.append(InfraState(w.key, False, cur.tracks, cur.loops))
    # same speed and catenary -> only the cheapest template (a dearer one can never win)
    best: dict = {}
    for t in out:
        w = ctx.way(t.way)
        k = (round(w.speed, 1), t.electrified)
        if k not in best or w.cost_per_m < ctx.way(best[k].way).cost_per_m:
            best[k] = t
    return list(best.values())


def _way_cost(ctx: _Ctx, L: float, cur: InfraState, new: InfraState) -> tuple[float, float]:
    """(construction, yearly upkeep change) of turning `cur` into `new` over L metres."""
    w0, w1 = ctx.way(cur.way), ctx.way(new.way)
    if w1 is None:
        return math.inf, 0.0
    tracks = max(1, new.tracks)
    if new.way != cur.way:
        cost = L * w1.cost_per_m * (1 + (ctx.cat_share if new.electrified else 0.0)) * REBUILD_SHARE * tracks
    else:
        cost = L * w1.cost_per_m * ctx.cat_share * tracks if new.electrified and not cur.electrified else 0.0
    m0 = (w0.maintenance_per_m if w0 else 0.0) * (1 + CATENARY_UPKEEP_SHARE * cur.electrified)
    m1 = w1.maintenance_per_m * (1 + CATENARY_UPKEEP_SHARE * new.electrified)
    return cost, L * (m1 - m0) * tracks


def _way_text(ctx: _Ctx, st: InfraState) -> str:
    w = ctx.way(st.way)
    base = WAY_RU.get(st.way) or (f"{w.name} ({w.speed * 3.6:.0f} км/ч)" if w else st.way)
    return base + (" с контактной сетью" if st.electrified else "")


def _owner_ref(segs: list[Segment]):
    return segs[0].ref if segs else None


def _pair_lengths(segs: list[Segment]) -> dict:
    out = {}
    for s in segs:
        for leg in s.legs:
            if leg.pair is not None:
                out[leg.pair] = max(out.get(leg.pair, 0.0), leg.length_m)
    return out


def _seg_profit(seg: Segment) -> float:
    ref = seg.ref
    if seg.leg_ref is not None or not hasattr(ref, "income"):
        return max(0.0, seg.income - seg.running)
    return max(0.0, ref.income - ref.running - ref.maintenance)


def infrastructure_upgrades(pl, sim, year: int) -> list[ProjectCandidate]:
    """Track type / catenary / road type upgrades per corridor (the pair keys of one fleet) and
    loops / double track where rail traffic outgrew the layout; first-year-return timing rule."""
    ctx = _Ctx(pl, sim, year)
    segs = [s for s in _all_segments(ctx) if s.mode in ("rail", "road") and not s.town_loop and s.capacity > 0]
    by_pair: dict = {}
    for s in segs:
        for leg in s.legs:
            if leg.pair is not None:
                by_pair.setdefault(leg.pair, [])
                if s not in by_pair[leg.pair]:
                    by_pair[leg.pair].append(s)
    lengths = _pair_lengths(segs)
    out: list[ProjectCandidate] = []
    base_cache: dict = {}
    # diagnostics: best first-year return per kind this year (sim.upgrade_stats[year])
    stats = sim.__dict__.setdefault("upgrade_stats", {}).setdefault(year, {})

    def best_net(seg: Segment, overrides) -> tuple[float, Option, Option]:
        d, o = best_option(ctx, seg, overrides)
        return o.net, d, o

    corridors = {}
    for s in segs:
        corr = tuple(sorted({leg.pair for leg in s.legs if leg.pair is not None}, key=str))
        if corr:
            corridors.setdefault(corr, s.mode)
    for corr, mode in corridors.items():
        cur = {p: ctx.state(p, mode) for p in corr}
        targets = {}
        for p in corr:
            for t in _way_targets(ctx, mode, cur[p]):
                targets[(t.way, t.electrified)] = t
        if not targets:
            continue
        affected = []
        for p in corr:
            for s in by_pair.get(p, []):
                if s not in affected:
                    affected.append(s)
        try:
            for s in affected:
                if s.key not in base_cache:
                    base_cache[s.key] = best_net(s, None)
        except (ValueError, ZeroDivisionError):
            continue
        best = None
        for (wk, el), t in targets.items():
            overrides, cost, d_maint, changed = {}, 0.0, 0.0, []
            for p in corr:
                c0 = cur[p]
                w0, w1 = ctx.way(c0.way), ctx.way(wk)
                new = InfraState(wk if (w1 and w0 and w1.speed >= w0.speed) else c0.way,
                                 el or c0.electrified, c0.tracks, c0.loops)
                if new.way == c0.way and new.electrified == c0.electrified:
                    continue
                cst, dm = _way_cost(ctx, lengths.get(p, 0.0), c0, new)
                overrides[p], cost, d_maint = new, cost + cst, d_maint + dm
                changed.append(p)
            if not overrides or cost <= 0:
                continue
            gain, plan = 0.0, []
            try:
                for s in affected:
                    net0, d0, _o0 = base_cache[s.key]
                    net1, d1, o1 = best_net(s, overrides)
                    gain += net0 - net1
                    plan.append((s, d1, o1, d0))
            except (ValueError, ZeroDivisionError):
                continue
            benefit = gain - d_maint
            years = max(1, min(INFRA_LIFE_YEARS, ctx.horizon - year))
            npv = benefit * (1 - (1 + DISCOUNT) ** -years) / DISCOUNT - cost
            kind = "road" if mode == "road" else ("catenary" if all(o.way == cur[p].way for p, o in overrides.items())
                                                  else "track")
            row = stats.setdefault(kind, {"best_ratio": -math.inf, "corridors": 0})
            row["corridors"] += 1
            if benefit / cost > row["best_ratio"]:
                row.update(best_ratio=round(benefit / cost, 4), way=wk, electrified=el, cost=round(cost),
                           benefit=round(benefit), lines=sorted({s.line_id for s in affected})[:5])
            if benefit / cost >= LAMBDA and npv > 0 and (best is None or npv > best[0]):
                best = (npv, t, overrides, cost, d_maint, gain, benefit, plan, changed)
        if best is None:
            continue
        npv, t, overrides, cost, d_maint, gain, benefit, plan, changed = best
        out.append(_infra_candidate(ctx, mode, corr, cur, t, overrides, cost, d_maint, gain, benefit, npv,
                                    plan, changed, lengths, affected))
    out += _capacity_candidates(ctx, by_pair, lengths)
    out.sort(key=lambda c: -c.meta["npv"])
    return out


def _infra_candidate(ctx, mode, corr, cur, t, overrides, cost, d_maint, gain, benefit, npv, plan, changed,
                     lengths, affected) -> ProjectCandidate:
    L = sum(lengths.get(p, 0.0) for p in changed)
    c0 = cur[changed[0]]
    if mode == "road":
        what = "road"
    elif t.way != c0.way:
        what = "track"
    else:
        what = "catenary"
    fleet = []
    vcap = 0.0
    for s, d1, o1, d0 in plan:
        if o1.keep:
            extra = o1.sold - d0.sold          # consists freed by the faster way only
            if extra <= 0:
                continue
            run_one = sum(v.running_cost * k for v, k in s.units)
            o1 = Option(s.units, s.n - extra, o1.cycle_s, 0.0, (s.n - extra) * run_one, keep=True, sold=extra)
        fc = _fleet_candidate(ctx, s, d1, o1, reason=_way_text(ctx, t))
        if fc is not None:
            fleet.append(fc)
            vcap += fc.vehicle_capex
    names = []
    for p in changed:
        for x in p[:2]:
            nm = ctx.pl.nodes[x].name if isinstance(x, int) and x < len(ctx.pl.nodes) else str(x)
            if nm not in names:
                names.append(nm)
    lines_txt = ", ".join(sorted({s.line_id for s in affected}))
    if what == "road":
        title = f"Перестроить дорогу {' — '.join(names[:4])}: {_way_text(ctx, c0)} → {_way_text(ctx, t)}"
        how = "Инструмент «Улучшить дорогу»: выбрать новый тип и провести по всей трассе линии."
    elif what == "catenary":
        title = f"Электрифицировать путь {' — '.join(names[:4])}"
        how = ("Инструмент «Электрификация» по всему пути, включая пути станций и депо: электровоз не "
               "проедет ни одного неэлектрифицированного участка.")
    else:
        title = f"Улучшить путь {' — '.join(names[:4])}: {_way_text(ctx, c0)} → {_way_text(ctx, t)}"
        how = "Инструмент «Улучшить путь»: новый тип пути по всей трассе (и стрелки разъездов)."
    fleet_txt = "; ".join(f"{fc.meta['line']}: {fc.meta['n_from']}× {fc.meta['from']} → "
                          f"{fc.meta['n_to']}× {fc.meta['to']}" for fc in fleet) or "техника прежняя, круг короче"
    details = (f"{L / 1000:.1f} км, линии {lines_txt}. {how} Стройка {cost:,.0f}, содержание пути "
               f"{d_maint:+,.0f}/год. Выигрыш линий (годовая стоимость парка с учётом капитала) "
               f"{gain:,.0f}/год → чистая выгода {benefit:,.0f}/год, отдача первого года "
               f"{benefit / cost:.0%} (порог {LAMBDA:.0%}), NPV {npv:,.0f}. Техника: {fleet_txt}.").replace(",", " ")
    geometry = _corr_geometry(ctx, mode, changed)
    return ProjectCandidate(
        kind="upgrade", mode=mode, title=title, details=details,
        nodes=[x for p in changed for x in p[:2] if isinstance(x, int)], cargo=None, volume=0.0,
        income=sum(fc.income for fc in fleet), running=sum(fc.running for fc in fleet), maintenance=d_maint,
        vehicle_capex=vcap, infra_capex=cost, geometry=geometry,
        pair_keys=list(changed),
        meta={"upgrade": what, "pairs": list(changed), "length_m": round(L), "from": _way_text(ctx, c0),
              "to": _way_text(ctx, t), "to_way": t.way, "electrified": t.electrified,
              "way_year_from": getattr(ctx.way(t.way), "year_from", 0), "construction": round(cost),
              "maintenance_delta": round(d_maint), "fleet_gain": round(gain), "benefit": round(benefit),
              "first_year_return": round(benefit / cost, 3), "npv": round(npv), "year": ctx.year,
              "lines": sorted({s.line_id for s in affected}), "_overrides": overrides, "_fleet": fleet,
              "_owner": _owner_ref(affected)})


def _corr_geometry(ctx: _Ctx, mode: str, pairs) -> list[list[float]] | None:
    pts = []
    for p in pairs:
        r = ctx.route(mode, p[0], p[1])
        if r is None or not r.polyline:
            continue
        poly = list(r.polyline[:: max(1, len(r.polyline) // 40)]) + [r.polyline[-1]]
        pts += [[round(x, 1), round(y, 1)] for x, y in poly]
    return pts or None


def _capacity_candidates(ctx: _Ctx, by_pair: dict, lengths: dict) -> list[ProjectCandidate]:
    out = []
    for pair, segs in by_pair.items():
        if pair[2] != "rail":
            continue
        st = ctx.state(pair, "rail")
        way = ctx.way(st.way)
        if way is None:
            continue
        L = lengths.get(pair, 0.0)
        if L <= 0:
            continue
        trains, n_tr, v, tlen = 0.0, 0, way.speed, 0.0
        for s in segs:
            T = _cycle(ctx, s, s.units)
            if T <= 0:
                continue
            trains += s.n * ctx.year_s / T
            n_tr += s.n
            v = min(v, min(u.top_speed for u, _ in s.units))
            tlen = max(tlen, sum(k for _, k in s.units) * dy.UNIT_LENGTH_M["rail"])
        if trains <= 0:
            continue
        if st.loops is None:              # first sight: the planner's capacity layer built what was needed
            k = cap_mod.loops_needed(trains, L, v, tlen, trains_running=n_tr)
            st.loops, st.tracks = (k, 1) if k is not None else (0, 2)
            continue
        cap = (cap_mod.double_track_capacity(v, tlen) if st.tracks >= 2
               else cap_mod.single_track_capacity(L, v, st.loops, tlen))
        if trains <= cap:
            continue
        plan = cap_mod.rail_line_plan(trains, L, v, tlen, way.cost_per_m, way.maintenance_per_m, n_trains=n_tr)
        if plan.kind == "infeasible" or (plan.kind != "double" and plan.loops <= st.loops):
            continue
        have = st.loops * cap_mod.loop_cost(way.cost_per_m, tlen)[0] if plan.kind != "double" else 0.0
        cost = max(0.0, plan.capex - have)
        if cost <= 0:
            continue
        lost = sum(_seg_profit(s) for s in segs) * (1 - cap / trains)
        d_maint = plan.maintenance - (st.loops * cap_mod.loop_cost(way.cost_per_m, tlen, way.maintenance_per_m)[1]
                                      if plan.kind != "double" else 0.0)
        benefit = lost - d_maint
        years = max(1, min(INFRA_LIFE_YEARS, ctx.horizon - ctx.year))
        npv = benefit * (1 - (1 + DISCOUNT) ** -years) / DISCOUNT - cost
        if benefit / cost < LAMBDA or npv <= 0:
            continue
        names = [ctx.pl.nodes[x].name if isinstance(x, int) else str(x) for x in pair[:2]]
        what = "второй путь" if plan.kind == "double" else f"разъезды: {st.loops} → {plan.loops}"
        out.append(ProjectCandidate(
            kind="upgrade", mode="rail", title=f"Пропускная способность {names[0]} — {names[1]}: {what}",
            details=(f"{trains:.0f} поездов/год в каждую сторону при пропускной способности {cap:.0f}. "
                     f"Стройка {cost:,.0f}, без неё теряется ≈ {lost:,.0f}/год прибыли. NPV {npv:,.0f}.")
            .replace(",", " "),
            nodes=[x for x in pair[:2] if isinstance(x, int)], cargo=None, volume=0.0, income=lost,
            running=0.0, maintenance=d_maint, vehicle_capex=0.0, infra_capex=cost,
            geometry=_corr_geometry(ctx, "rail", [pair]), pair_keys=[pair],
            meta={"upgrade": "capacity", "pairs": [pair], "length_m": round(L), "layout": plan.kind,
                  "loops": plan.loops, "trains_per_year": round(trains, 1), "capacity": round(cap, 1),
                  "construction": round(cost), "benefit": round(benefit), "first_year_return": round(benefit / cost, 3),
                  "npv": round(npv), "year": ctx.year, "lines": sorted({s.line_id for s in segs}),
                  "_owner": _owner_ref(segs)}))
    return out


# --------------------------------------------------------------------------- 3. apply

def _set_fleet(pl, sim, seg: Segment, o: Option) -> dict:
    """Write a new fleet into the line object (Project or generic ProjectCandidate)."""
    ref = seg.ref
    old_run, new_run = seg.running, o.running
    d_inc = (o.income - seg.income) if (seg.passenger and not o.keep) else 0.0
    new_value = (seg.value / max(seg.n, 1) * o.n) if o.keep else (o.price + o.extra_capex)
    if hasattr(ref, "consist_ids"):                       # planner.Project (direct cargo line)
        ref.consist_ids = sorted((v.id, k) for v, k in o.units)
        ref.n_consists = o.n
        ref.running += new_run - old_run
        ref.vehicle_value = new_value
        if not o.keep:
            ref.bought_month = sim.month
            ref.lifespan_years = min(v.lifespan_years for v, _ in o.units)
        if getattr(ref.cand, "ev", None) is not None:
            ref.cand.ev.trip_time_s = o.cycle_s
    else:                                                 # generic ProjectCandidate
        pc = ref
        lines_new = [VehicleLine(v.id, v.name, v.file, k * o.n, v.price) for v, k in o.units]
        old = {}
        for v, k in seg.units:
            old[v.id] = old.get(v.id, 0) + k * seg.n
        keep = []
        for vl in pc.vehicles:
            take = min(vl.count, old.get(vl.vehicle_id, 0))
            old[vl.vehicle_id] = old.get(vl.vehicle_id, 0) - take
            if vl.count - take > 0:
                keep.append(VehicleLine(vl.vehicle_id, vl.name, vl.file, vl.count - take, vl.price))
        pc.vehicles = keep + lines_new
        pc.running += new_run - old_run
        pc.income += d_inc
        pc.vehicle_capex = max(0.0, pc.vehicle_capex - seg.value + new_value)
        m = pc.meta
        if not o.keep:
            m.setdefault("fleet_bought", {})[seg.idx] = sim.month
            pc.vehicle_life_years = min(pc.vehicle_life_years or 1e9, min(v.lifespan_years for v, _ in o.units))
        if seg.leg_ref is not None and seg.leg_ref < len(pc.legs) and isinstance(pc.legs[seg.leg_ref], dict):
            leg = pc.legs[seg.leg_ref]
            leg["vehicles"] = (_vehicle_steps(o.units, o.n) if seg.vehicle_format == "step"
                               else [vars(vl) for vl in lines_new])
            if "n_consists" in leg:
                leg["n_consists"] = o.n
            if "consist" in leg:
                leg["consist"] = o.label
            for k_, val in (("running", new_run), ("vehicle_capex", new_value)):
                if k_ in leg:
                    leg[k_] = round(val)
            if "income" in leg and d_inc:
                leg["income"] = round(leg["income"] + d_inc)
            if "trip_time_s" in leg:
                leg["trip_time_s"] = round(o.cycle_s, 1)
        else:
            if "n_consists" in m:
                m["n_consists"] = o.n
            if "consist" in m:
                m["consist"] = o.label
            for k_ in ("trip_s", "cycle_s"):
                if k_ in m:
                    m[k_] = round(o.cycle_s)
    return {"line": seg.line_id, "segment": seg.idx, "consist": o.label, "n": o.n,
            "running_delta": round(new_run - old_run), "income_delta": round(d_inc),
            "vehicle_value": round(new_value)}


def _step_kind(mode: str) -> str:
    return "road" if mode in ("tram", "road") else mode


def apply_upgrade(pl, sim, cand: ProjectCandidate) -> dict:
    """Apply an upgrade candidate to the planner state, pay for it and write the plan step(s).

    fleet:    line consist/count/running/vehicle value/bought month (Project or generic line, incl. one
              leg of a bundle/chain); passenger income x ticket price factor.
    track / catenary / road: sim.infra[pair] -> new way/catenary; upkeep delta booked on the owner
              line's maintenance (so planner cash books include it); the fleet re-plan computed with the
              new ways is applied line by line.
    capacity: sim.infra[pair].loops / tracks; upkeep delta on the owner line.
    Returns the state delta {"cash": -spent, "lines": [...], "infra": {pair: {...}}, "steps": [ids]}.
    """
    m = cand.meta
    kind = m.get("upgrade")
    delta = {"upgrade": kind, "cash": 0.0, "lines": [], "infra": {}, "steps": []}
    if kind == "fleet":
        seg, o = m["_seg"], m["_opt"]
        delta["lines"].append(_set_fleet(pl, sim, seg, o))
        spend = m["net_cost"]
        sim.cash -= spend
        delta["cash"] = -spend
        sim.add_step(action="replace_vehicles", kind=_step_kind(cand.mode), cargo=cand.cargo, line=seg.line_id,
                     title=cand.title, details=cand.details, vehicles=_vehicle_steps(o.units, o.n) if not o.keep else [],
                     cost=round(max(spend, 0)), income_per_year=round(cand.income),
                     running_cost_per_year=round(cand.running),
                     roi_years=(round(spend / (cand.income - cand.running), 1)
                                if cand.income - cand.running > 0 and spend > 0 else None))
        delta["steps"].append(sim.steps[-1]["id"])
        return delta
    infra = infra_state(pl, sim)
    owner = m.get("_owner")
    if kind in ("track", "catenary", "road"):
        for p, new in m["_overrides"].items():
            old = infra.get(p) or InfraState(_default_way(pl, p[2]))
            delta["infra"][p] = {"from": (old.way, old.electrified), "to": (new.way, new.electrified)}
            infra[p] = InfraState(new.way, new.electrified, old.tracks, old.loops)
    elif kind == "capacity":
        p = m["pairs"][0]
        st = infra.get(p) or InfraState(_default_way(pl, "rail"))
        delta["infra"][p] = {"from": (st.tracks, st.loops), "to": (2 if m["layout"] == "double" else 1, m["loops"])}
        if m["layout"] == "double":
            st.tracks = 2
        else:
            st.loops = m["loops"]
        infra[p] = st
    if owner is not None:
        owner.maintenance = max(0.0, owner.maintenance + cand.maintenance)
    spend = cand.infra_capex
    sim.cash -= spend
    owner_id = getattr(owner, "id", None) or (getattr(owner, "meta", {}) or {}).get("id")
    sim.add_step(action="upgrade_track", kind=_step_kind(cand.mode), cargo=None, line=owner_id,
                 title=cand.title, details=cand.details, geometry=cand.geometry,
                 length_m=m.get("length_m"), cost=round(spend), income_per_year=round(m.get("benefit", 0)),
                 running_cost_per_year=round(cand.maintenance),
                 roi_years=round(spend / m["benefit"], 1) if m.get("benefit", 0) > 0 else None)
    delta["steps"].append(sim.steps[-1]["id"])
    for fc in m.get("_fleet") or []:
        d = apply_upgrade(pl, sim, fc)
        delta["lines"] += d["lines"]
        delta["steps"] += d["steps"]
        spend += -d["cash"]
    delta["cash"] = -spend
    return delta


# --------------------------------------------------------------------------- 4. yearly driver

def score(c: ProjectCandidate) -> float:
    """Ranking: NPV per unit of money spent (fleet: EAC saving annuity)."""
    m = c.meta
    if m.get("upgrade") == "fleet":
        years = 15
        gain = m["eac_saving"] * (1 - (1 + DISCOUNT) ** -years) / DISCOUNT
        return gain / max(c.capex, 1.0)
    return m.get("npv", 0.0) / max(c.infra_capex, 1.0)


def _prefetch_leg_runs(pl, sim, year: int, infrastructure: bool, run_jobs) -> None:
    """Dry pass of the evaluation that only collects the dynamics simulations it misses, runs them
    in the worker processes (run_jobs: list of jobs -> list of results) and stores them in the
    cache. The real pass then finds the exact values (any it still misses it computes itself),
    so the outcome is the same as without prefetching. Side effects of the dry pass on the
    simulation (diagnostics, default way states) are undone."""
    global _COLLECT
    import time as _time
    t0 = _time.perf_counter()
    stats = sim.__dict__.get("upgrade_stats")
    saved_stats = copy.deepcopy(stats) if stats is not None else None
    had_infra = "infra" in sim.__dict__
    saved_infra = dict(sim.__dict__["infra"]) if had_infra else None
    _COLLECT = {}
    _DRY.clear()
    try:
        if infrastructure:
            infrastructure_upgrades(pl, sim, year)
        fleet_replacement(pl, sim, year)
        jobs = _COLLECT
    finally:
        _COLLECT = None
        _DRY.clear()
        if saved_stats is None:
            sim.__dict__.pop("upgrade_stats", None)
        else:
            sim.upgrade_stats = saved_stats
        if had_infra:
            sim.__dict__["infra"].clear()
            sim.__dict__["infra"].update(saved_infra)
        else:
            sim.__dict__.pop("infra", None)
    acc = getattr(getattr(pl, "_pool", None), "_acc", None)
    if acc is not None:
        acc("upgrade_dry_pass", _time.perf_counter() - t0)
    if not jobs:
        return
    keys = list(jobs)
    try:
        results = run_jobs([jobs[k] for k in keys])
    except Exception as e:          # workers unavailable: the real pass simulates serially
        print(f"upgrades: parallel dynamics failed ({e})", flush=True)
        return
    _PREFETCHED.clear()
    _PREFETCHED.update(zip(keys, results))


def yearly_upgrades(pl, sim, ensure_cash=None, infrastructure: bool = True) -> list[ProjectCandidate]:
    """Generate, rank and apply upgrades for the current January. Infrastructure first (its fleet
    re-plan supersedes the plain replacements of the same lines). `ensure_cash(sim, amount)`:
    the planner's _ensure_cash (loans allowed) - default: pay only from cash."""
    year = sim.year
    ensure = ensure_cash or (lambda s, need: s.cash >= need)
    pool = getattr(pl, "_pool", None)
    prefetch = getattr(pool, "leg_runs", None)
    # the dry pass costs about one evaluation without simulations: only worth it in years with
    # many new simulations (new models / ways). Decided from last year's measurements.
    st = pool.__dict__.setdefault("upg_state", {}) if pool is not None else {}
    do_pre = prefetch is not None and st.get("want", True)
    pool_cpu0 = getattr(pool, "last_sims_cpu", 0.0)
    if do_pre:
        pool.last_sims_cpu = 0.0
        _prefetch_leg_runs(pl, sim, year, infrastructure, prefetch)
    t_real, s0 = _time.perf_counter(), _SIM_SERIAL[0]
    try:
        cands = (infrastructure_upgrades(pl, sim, year) if infrastructure else []) + fleet_replacement(pl, sim, year)
    finally:
        _PREFETCHED.clear()
    if prefetch is not None:
        sims_serial = _SIM_SERIAL[0] - s0
        eval_s = max(0.0, _time.perf_counter() - t_real - sims_serial)
        sims_all = sims_serial + (getattr(pool, "last_sims_cpu", 0.0) if do_pre else 0.0)
        n = max(1, getattr(pool, "n", 1))
        st["want"] = sims_all * (1 - 1 / n) > 1.2 * eval_s
        if not do_pre:
            pool.last_sims_cpu = pool_cpu0
    infra_c = sorted([c for c in cands if c.meta["upgrade"] != "fleet"], key=score, reverse=True)
    fleet_c = sorted([c for c in cands if c.meta["upgrade"] == "fleet"], key=score, reverse=True)
    applied, pairs_done, lines_done = [], set(), set()
    for c in infra_c + fleet_c:
        m = c.meta
        if m["upgrade"] == "fleet":
            key = (m["line"], m["segment"])
            if key in lines_done:
                continue
        else:
            if pairs_done & set(m["pairs"]) or any((fc.meta["line"], fc.meta["segment"]) in lines_done
                                                   for fc in m.get("_fleet") or []):
                continue
        if c.capex > 0 and not ensure(sim, c.capex):
            continue
        apply_upgrade(pl, sim, c)
        applied.append(c)
        if m["upgrade"] == "fleet":
            lines_done.add((m["line"], m["segment"]))
        else:
            pairs_done |= set(m["pairs"])
            lines_done |= {(fc.meta["line"], fc.meta["segment"]) for fc in m.get("_fleet") or []}
    return applied
