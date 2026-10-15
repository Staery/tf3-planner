"""Town growth and company rank dynamics (TF3 game scripts, see docs/towns.md and mechanics.md §8-9, §12.2).

Every game year the planner calls TownModel.yearly_update(sim, year):

1. XP of each town from what the plan delivered there during the past year
   [game_mechanics/towns/town_growth.script.tl, town_growth_config.res.lua]
     cargo unit unloaded into a town building   10 * 2^tier * xpFactor   (industries give no town XP)
     resident arriving by line at a town building 5 * 2^tier * xpFactor   (tier 0 same town, 1 neighbour, 2 other;
                                                                         trips to/from industries: none)
2. xpFactor from ratings [town_util.tl getGrowthLevelMult / getXpFactor, town_cargo_util.tl authority2xpFactor]
     authorityLevel = level(min(six ratings)), delivery rating from the on-time share (VeryHard: >51 %)
     suppliesLevel  = rounded-up average of level(cargoRating) and level(passengerRating)
         cargoRating = max over districts of supplied/demand, passengerRating = clamp(2 * lineUsage)
     growthLevel = authorityLevel/5 * (suppliesLevel+1)/6 (0 when nothing at all is delivered)
     xpFactor    = 0 / .2 / .4 / .6 / .8 / 1 at growthLevel < .061 / .147 / .267 / .435 / .671 / else
3. level from XP: xp(L) = 1000 * (1.5^(L-1) - 1) / 0.5; initial level by residential capacity (>200: 4,
   >150: 3, >100: 2) [town_cargo_util.tl ensureState / getLevelAndFraction]
4. target size: initial capacity sum * F(l), l = level + 1 - initialLevel + fraction,
   F(l) = 0.00930472 l^3 - 0.08061382 l^2 + 0.63330472 l + 0.438   [scripts/util/town_growth_function.tl]
   growth split over residential / commercial / industrial by usage (line usage, supply ratios), min 5 %
   per district, dampened above 40 % town / 32 % map share   [town_growth.script.tl update()]
5. new cargo needs at levels 5 (commercial, tier 0-1), 7 (industrial, 0-1), 9 (commercial, 0-2),
   11 (industrial, 0-2), only after the town received cargo once; the game picks the cargo of that
   land use demanded by the FEWEST towns on the map (weight x0.01 outside the tier range)
   [town_growth.script.tl addCargo, economy/placementparamsutil.lua getDefaultTownLevels2CargoInfos]
6. demand per needed cargo = 0.3 * district capacity * factor(tier) (1 / 0.5 / 0.25)
7. world population = Σ residential capacity = company XP (never decreasing) -> potential rank from the
   export's xpForRank table (company_progression_util.tl) -> ticket multiplier (x0.5 at rank 15, VeryHard)

The module never edits the planner's Sim; it returns deltas the planner applies (see docs/towns.md).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .model import LAND_USES, Game

# --------------------------------------------------------------------------- game constants

XP_BASE = 1000                 # expBaseTownLevel
XP_SCALING = 1.5               # townExpScaling
XP_PER_CARGO = 10              # expPerCargoItemToTown
XP_PER_PERSON = 5              # expPerPersonArriveAtTown
DEMAND_PER_CAPACITY = 0.3      # 1.2 x ceil(C/4) per building and year (townbuildingutil.lua)
NUM_RATING_LEVELS = 6
XP_FACTORS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)       # VeryPoor .. Excellent
LINE_USAGE_RATING_MULT = 2.0
MAX_TOWN_DISTRICT = 0.5
MAX_TOWN_DISTRICT_DAMPEN_FROM = 0.4
MAX_MAP_DISTRICT = 0.4
MAX_MAP_DISTRICT_DAMPEN_FROM = 0.32
MAX_RANK = 15
NORMALIZED_BASE_POPULATION = 725

# cargo categories (placementparamsutil.getCargoCategories): key -> (tier, land use)
CARGO_CATEGORY = {
    "fish": (0, "commercial"), "meat": (0, "commercial"), "vegetables": (0, "commercial"),
    "beverages": (1, "commercial"), "books": (1, "commercial"), "clothes": (1, "commercial"),
    "tinned_food": (1, "commercial"),
    "bricks": (0, "industrial"), "cement": (0, "industrial"), "fuel": (0, "industrial"),
    "planks": (0, "industrial"), "furniture": (1, "industrial"), "machines": (1, "industrial"),
    "tools": (1, "industrial"), "vehicles": (2, "industrial"),
    "chemicals": (1, None),     # has a tier but no town land use
}
TIER_FACTOR = {0: 1.0, 1: 0.5, 2: 0.25}
# town level -> (land use, min tier, max tier) of the extra need it unlocks; level 0 = the start needs
LEVEL_NEEDS = {0: [("commercial", 0, 1), ("industrial", 0, 1)], 5: [("commercial", 0, 1)],
               7: [("industrial", 0, 1)], 9: [("commercial", 0, 2)], 11: [("industrial", 0, 2)]}
# difficulty (vehicle/infrastructure cost scale) -> town delivery sensitivity (difficulty_util.tl)
SENSITIVITY_BY_COST_SCALE = {0.5: 0.75, 1.0: 1.0, 1.25: 1.5, 1.5: 2.0}


def tier_of(cargo: str) -> int:
    return CARGO_CATEGORY.get(cargo, (0, None))[0]


def land_use_of(cargo: str) -> str | None:
    return CARGO_CATEGORY.get(cargo, (0, None))[1]


# --------------------------------------------------------------------------- formulas

def xp_for_level(level: int) -> float:
    """Total XP at which `level` starts: 0, 1000, 2500, 4750, 8125, 13187.5, ..."""
    if level <= 1:
        return 0.0
    return XP_BASE * (XP_SCALING ** (level - 1) - 1) / (XP_SCALING - 1)


def level_and_fraction(xp: float) -> tuple[int, float]:
    """town_cargo_util.getLevelAndFraction (incl. its exp + 1 quirk)."""
    if xp < XP_BASE:
        return 1, max(0.0, xp) / XP_BASE
    level = int(math.floor(math.log(1 - (xp + 1) * (1 - XP_SCALING) / XP_BASE) / math.log(XP_SCALING))) + 1
    lo, hi = xp_for_level(level), xp_for_level(level + 1)
    return level, (xp - lo) / (hi - lo)


def growth_factor(l: float) -> float:
    """F(l), F(1) = 1, F(18) = 40 (town_growth_function.getGrowthFactor)."""
    return 0.00930472 * l ** 3 - 0.08061382 * l ** 2 + 0.63330472 * l + 0.438


def world_growth_factor(rank: int) -> float:
    """W(r), W(1) = 1, W(15) = 12.5 (town_growth_function.worldGrowthFactor)."""
    return 0.001 * rank ** 3 + 0.027398 * rank ** 2 + 0.14221 * rank + 0.829392


def initial_level(residential_capacity: float) -> int:
    if residential_capacity > 200:
        return 4
    if residential_capacity > 150:
        return 3
    if residential_capacity > 100:
        return 2
    return 1


def rating_level(x: float) -> int:
    return max(0, min(NUM_RATING_LEVELS - 1, int(math.floor(x * NUM_RATING_LEVELS))))


def delivery_rating(on_time_share: float, sensitivity: float = 2.0) -> float:
    """Town cargo-delivery rating from the on-time share (town_util.getRatingCargoDelivery)."""
    s = sensitivity
    lo = 1 - math.exp(-0.35667 * s)
    hi = -1.2 * min(s, 1) ** 2 + 2.2 * min(s, 1)
    return max(0.0, min(1.0, (on_time_share - lo) / (hi - lo)))


def _combine_rounded_average(a: int, b: int) -> int:
    s = a + b
    return int(math.floor(s / 2 + 0.5)) if s % 2 == 0 else int(math.floor((s + 1) / 2 + 0.5))


_W = [1.4 ** i for i in range(NUM_RATING_LEVELS)]
GROWTH_THRESHOLDS = tuple(sum(_W[:i + 1]) / sum(_W) for i in range(NUM_RATING_LEVELS))   # .061 ... 1.0


def growth_level(authority_score: float, cargo_rating: float | None, passenger_rating: float | None) -> int:
    """Rating level 0..5 of town growth (getGrowthLevelMult)."""
    authority = rating_level(authority_score) / (NUM_RATING_LEVELS - 1)
    if cargo_rating is None and passenger_rating is None:
        supplies, none = NUM_RATING_LEVELS - 1, False
    elif cargo_rating is None:
        supplies, none = rating_level(passenger_rating), passenger_rating <= 0
    elif passenger_rating is None:
        supplies, none = rating_level(cargo_rating), cargo_rating <= 0
    else:
        supplies = _combine_rounded_average(rating_level(cargo_rating), rating_level(passenger_rating))
        none = cargo_rating <= 0 and passenger_rating <= 0
    g = authority * (0.0 if none else (supplies + 1) / NUM_RATING_LEVELS)
    for i, t in enumerate(GROWTH_THRESHOLDS):
        if g < t:
            return i
    return NUM_RATING_LEVELS - 1


def xp_factor(authority_score: float, cargo_rating: float | None, passenger_rating: float | None,
              xp_bonus: float = 0.0) -> float:
    return XP_FACTORS[growth_level(authority_score, cargo_rating, passenger_rating)] * (1 + xp_bonus)


def _map_clamp(x, x0, x1, y0, y1):
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + (y1 - y0) * t


def _rebalance(weights: list[float], lo: list[float], hi: list[float]) -> list[float]:
    """town_growth.script.tl rebalance2: clamp to [lo, hi], spread the missing/extra share evenly over
    the districts that still have room."""
    w = list(weights)
    pin_dn = [False] * 3
    pin_up = [False] * 3
    for i in range(3):
        if w[i] < lo[i]:
            w[i], pin_dn[i] = lo[i], True
        if w[i] > hi[i]:
            w[i], pin_up[i] = hi[i], True
    diff = 1.0 - sum(w)
    if abs(diff) < 1e-12:
        return w
    neg = diff < 0
    rem = abs(diff)
    for _ in range(10):
        room = {}
        for i in range(3):
            if not neg and not pin_up[i]:
                room[i] = hi[i] - w[i]
            elif neg and not pin_dn[i]:
                room[i] = w[i] - lo[i]
        if not room:
            break
        per = rem / len(room)
        done = True
        for i, cap in room.items():
            if per < cap:
                w[i] += -per if neg else per
                rem -= per
            else:
                w[i] += -cap if neg else cap
                rem -= cap
                (pin_dn if neg else pin_up)[i] = True
                done = False
        if done or rem <= 1e-12:
            break
    return w


def split_growth(extra: float, current: list[float], usage: list[float], map_sum: list[float],
                 min_fraction: float = 0.05) -> list[float]:
    """Added capacity per district (res, com, ind) for `extra` new person capacity."""
    total = sum(usage)
    if total <= 0 or extra <= 0:
        return [0.0, 0.0, 0.0]
    w = [u / total for u in usage]
    added = [wi * extra for wi in w]
    new_world = [map_sum[i] + added[i] for i in range(3)]
    new_town = [current[i] + added[i] for i in range(3)]
    sw, st = sum(new_world), sum(new_town)
    hi = [1.0, 1.0, 1.0]
    for i in range(3):
        if sw > 0 and new_world[i] / sw >= MAX_MAP_DISTRICT_DAMPEN_FROM:
            hi[i] = _map_clamp(new_world[i] / sw, MAX_MAP_DISTRICT_DAMPEN_FROM, MAX_MAP_DISTRICT, 1.0, MAX_MAP_DISTRICT)
        if st > 0 and new_town[i] / st >= MAX_TOWN_DISTRICT_DAMPEN_FROM:
            hi[i] = min(hi[i], _map_clamp(new_town[i] / st, MAX_TOWN_DISTRICT_DAMPEN_FROM, MAX_TOWN_DISTRICT,
                                          1.0, MAX_TOWN_DISTRICT))
    w = _rebalance(w, [min_fraction] * 3, hi)
    return [wi * extra for wi in w]


# --------------------------------------------------------------------------- company rank

def rank_thresholds(export: dict | None, base_population: float | None = None) -> list[float]:
    """World population needed for ranks 1..15: export company.xpForRank, else the game formula."""
    comp = (export or {}).get("company") or {}
    tab = comp.get("xpForRank")
    if isinstance(tab, dict):
        tab = [tab[k] for k in sorted(tab, key=lambda k: int(k))]
    if isinstance(tab, list) and len(tab) >= 2:
        return [float(x) for x in tab]
    base = base_population or comp.get("basePopulation") or comp.get("worldPopulation") or 10000
    out = []
    for r in range(1, MAX_RANK + 1):
        w = (MAX_RANK - r) / (MAX_RANK - 1)
        interp = math.floor(w * NORMALIZED_BASE_POPULATION + (1 - w) * base)
        out.append(float(max(r, math.floor(interp * world_growth_factor(r)) - interp + base)))
    return out


def rank_from_population(export: dict | None, world_pop: float) -> int:
    """Potential company rank (drives the ticket multiplier, applied or not) for a world population."""
    r = 1
    for k, need in enumerate(rank_thresholds(export), start=1):
        if world_pop >= need:
            r = k
    return r


def ticket_multiplier(rank: int, export: dict | None = None, m15: float = 0.5) -> float:
    """1 + (rank-1)(m15-1)/14; the export's ticketMultiplierForLevel table wins when present."""
    tab = ((export or {}).get("company") or {}).get("ticketMultiplierForLevel")
    if isinstance(tab, dict):
        tab = [tab[k] for k in sorted(tab, key=lambda k: int(k))]
    r = max(1, min(MAX_RANK, int(rank)))
    if isinstance(tab, list) and tab:
        return float(tab[min(r, len(tab)) - 1])
    return 1 + (r - 1) * (m15 - 1) / (MAX_RANK - 1)


# --------------------------------------------------------------------------- state

@dataclass
class TownParams:
    """Assumptions to calibrate against in-game exports (docs/towns.md §4)."""
    on_time_share: float = 0.95          # share of town deliveries on time (planner checks deadlines for towns)
    other_ratings: float | None = None   # min of urban care/traffic/happiness/noise/pollution; None = export
    sensitivity: float | None = None     # delivery sensitivity; None = from difficulty (VeryHard 2.0)
    xp_bonus: float = 0.0                # constructionBoni.xpIncrease (landmarks)
    trips_per_resident: float | None = None   # legs/resident/game year; None = passengers.py default x year scale
    min_district_fraction: float | None = None  # None = export townGrowthMinDistrictFraction (0.05)
    neighbour_k: int = 0                 # 0 = Gabriel graph of town centres; k > 0 = k nearest towns
    count_from_income_month: bool = True  # lines count only after their first loaded arrival


@dataclass
class TownState:
    id: int
    name: str
    node: int                           # planner node index
    x: float
    y: float
    initial_caps: list[float]           # initialLandUseCapacities (res, com, ind)
    size: list[float]                   # sizeFactors: capacity = initial * size
    actual0: list[float]                # exported built capacity at t0 (lags the target a little)
    initial_level: int
    xp: float
    needs: dict[str, list[str]]         # land use -> cargo keys (in order of appearance)
    ratings: dict[str, float] = field(default_factory=dict)
    cargo_delivered: bool = False
    xp_factor: float = 0.0
    last: dict = field(default_factory=dict)       # last yearly breakdown
    size0: list[float] = field(default_factory=list)   # sizeFactors at t0

    @property
    def caps(self) -> list[float]:
        return [self.initial_caps[i] * self.size[i] for i in range(3)]

    @property
    def level(self) -> int:
        return level_and_fraction(self.xp)[0]

    @property
    def residents(self) -> float:
        """Population (residential capacity), anchored to the exported built capacity."""
        return self.actual0[0] + self.caps[0] - self.initial_caps[0] * self.size0[0]

    def demand(self, cargo: str, per_capacity: float = DEMAND_PER_CAPACITY) -> float:
        for lu, cs in self.needs.items():
            if cargo in cs:
                return per_capacity * self.caps[LAND_USES.index(lu)] * TIER_FACTOR[tier_of(cargo)]
        return 0.0


def town_needs(game: Game, raw_town: dict) -> dict[str, list[str]]:
    """Needs per land use from `town.cargoNeeds` ({}, [[cargoId, factor]...], [...]) — the
    correct mapping (Town.needs in model.py is shifted by one land use when residential is empty)."""
    out = {lu: [] for lu in LAND_USES}
    cn = (raw_town.get("town") or {}).get("cargoNeeds")
    if isinstance(cn, list) and len(cn) == 3:
        for i, entry in enumerate(cn):
            for pair in (entry if isinstance(entry, list) else []):
                c = game.cargos.get(int(pair[0])) if isinstance(pair, list) and pair else None
                if c:
                    out[LAND_USES[i]].append(c.key)
        return out
    for pairs in raw_town.get("landUse2CargoNeeds") or []:     # fallback: key names, land use from category
        for pair in (pairs if isinstance(pairs, list) else []):
            lu = land_use_of(pair[0])
            if lu:
                out[lu].append(pair[0])
    return out


# --------------------------------------------------------------------------- model

class TownModel:
    def __init__(self, game: Game, nodes: list | None = None, params: TownParams | None = None):
        self.game = game
        self.p = params or TownParams()
        raw = game.raw
        stats = {t.get("id"): t for t in ((raw.get("towns_stats") or {}).get("towns") or [])}
        node_of = {}
        if nodes is not None:
            node_of = {n.id: n.index for n in nodes if getattr(n, "kind", "") == "town"}
        self.towns: list[TownState] = []
        raw_by_id = {t["id"]: t for t in raw.get("towns", [])}
        for k, t in enumerate(game.towns):
            rt = raw_by_id.get(t.id, {})
            tt = rt.get("town") or {}
            init = [float(x) for x in (tt.get("initialLandUseCapacities")
                                       or [t.capacities.get(lu, 0) for lu in LAND_USES])]
            size = [float(x) for x in (tt.get("sizeFactors") or [1, 1, 1])]
            cu = rt.get("capacityUsage")
            if isinstance(cu, list) and len(cu) >= 3 and all(isinstance(c, dict) for c in cu[:3]):
                actual = [float(c.get("capacity", 0) or 0) for c in cu[:3]]
            else:
                actual = [init[i] * size[i] for i in range(3)]
            st = stats.get(t.id) or {}
            growth = st.get("growth") or {}
            lvl0 = int(growth.get("initialLevel") or initial_level(init[0]))
            xp = float(growth.get("experience") if growth.get("experience") is not None else xp_for_level(lvl0))
            ratings = {r["key"]: float(r["value"]) for r in st.get("ratings") or [] if "key" in r}
            ts = TownState(t.id, t.name, node_of.get(t.id, k), t.x, t.y, init, size, actual, lvl0, xp,
                           town_needs(game, rt), ratings)
            ts.cargo_delivered = bool(((st.get("state") or {}).get("eventFactors") or {}).get("numItemsDelivered"))
            ts.size0 = list(size)
            self.towns.append(ts)
        self.by_node = {t.node: t for t in self.towns}
        self.by_id = {t.id: t for t in self.towns}
        # cargos a land use may ask for in this economy (export landUse2CargoTypes), else the category table
        self.lu_cargos = {lu: [c for c, (tier, l) in CARGO_CATEGORY.items() if l == lu] for lu in ("commercial", "industrial")}
        for rt in raw.get("towns", []):
            l2c = rt.get("landUse2CargoTypes")
            if isinstance(l2c, list) and len(l2c) == 3:
                for i, lu in ((1, "commercial"), (2, "industrial")):
                    ids = l2c[i] if isinstance(l2c[i], list) else []
                    keys = [game.cargos[c].key for c in ids if c in game.cargos]
                    if keys:
                        self.lu_cargos[lu] = keys
                break
        self.produced = {c for i in game.industries for c in i.outputs}
        cost_scale = getattr(game, "vehicle_cost_scale", 1.5) or 1.5
        self.sensitivity = self.p.sensitivity or SENSITIVITY_BY_COST_SCALE.get(round(cost_scale, 2), 2.0)
        td = getattr(game, "town_dev", None) or {}
        self.min_district = self.p.min_district_fraction or float(td.get("townGrowthMinDistrictFraction", 0.05))
        self.neighbours = self._neighbours()
        comp = raw.get("company") or {}
        self.company_xp = float(comp.get("experience") or comp.get("worldPopulation") or self.world_population())
        self.rank0 = int(comp.get("potentialLevel") or comp.get("level")
                         or rank_from_population(raw, self.company_xp))
        self.start_year = game.year
        self.rank_history: dict[int, int] = {game.year: max(self.rank0, rank_from_population(raw, self.company_xp))}
        self.history: list[dict] = []
        self.totals = self.initial_demand()        # last demand totals handed to the planner

    # ---------------- helpers
    def _neighbours(self) -> set[tuple[int, int]]:
        """Neighbour town pairs (isNeighbourTown is engine-side): Gabriel graph of town centres, or the k
        nearest towns when params.neighbour_k > 0."""
        ts = self.towns
        out = set()
        for a in ts:
            d = sorted(((math.hypot(a.x - b.x, a.y - b.y), b.node) for b in ts if b is not a))
            if self.p.neighbour_k > 0:
                for _, bn in d[:self.p.neighbour_k]:
                    out.add((min(a.node, bn), max(a.node, bn)))
        if self.p.neighbour_k > 0:
            return out
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                mx, my = (a.x + b.x) / 2, (a.y + b.y) / 2
                r2 = ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) / 4
                if not any((c.x - mx) ** 2 + (c.y - my) ** 2 < r2 for c in ts if c is not a and c is not b):
                    out.add((min(a.node, b.node), max(a.node, b.node)))
        return out

    def pax_tier(self, a: int, b: int) -> int:
        if a == b:
            return 0
        return 1 if (min(a, b), max(a, b)) in self.neighbours else 2

    def _trips_per_resident(self) -> float:
        if self.p.trips_per_resident is not None:
            return self.p.trips_per_resident
        try:
            from .passengers import PassengerParams, _year_scale
            pp = PassengerParams()
            return pp.trips_per_resident_year * _year_scale(self.game, pp)
        except Exception:
            return 2.0

    def world_population(self) -> float:
        return sum(t.residents for t in self.towns)

    def initial_demand(self) -> dict[tuple[int, str], float]:
        """Total yearly demand per (town node, cargo) with the correct land use and tier factor."""
        out = {}
        for t in self.towns:
            for lu, cs in t.needs.items():
                for c in cs:
                    out[(t.node, c)] = out.get((t.node, c), 0.0) + t.demand(c)
        return out

    def rank_of(self, year: int) -> int:
        """Potential rank in `year` (as of its January update); later years keep the last known rank."""
        ys = [y for y in self.rank_history if y <= year]
        return self.rank_history[max(ys)] if ys else self.rank0

    def ticket_multiplier(self, year: int) -> float:
        return ticket_multiplier(self.rank_of(year), self.game.raw)

    def capacities(self) -> dict[int, tuple[float, float, float]]:
        """Built capacities per town id (exported t0 capacity + modelled growth) — the passenger model's input."""
        out = {}
        for t in self.towns:
            c = t.caps
            out[t.id] = tuple(t.actual0[i] + c[i] - t.initial_caps[i] * t.size0[i] for i in range(3))
        return out

    def apply_to_game(self, game: Game | None = None, town_objects: bool = False):
        """Write the grown capacities into game.raw['towns'][*].capacityUsage so passengers.town_infos /
        demand_matrix use them without code changes. town_objects=True also overwrites Town.capacities
        (read by flow.py, tours.py, multimodal.py) and Town.needs (correct land-use mapping)."""
        game = game or self.game
        caps = self.capacities()
        for rt in game.raw.get("towns", []):
            c = caps.get(rt.get("id"))
            if c is None:
                continue
            cu = rt.get("capacityUsage")
            if not (isinstance(cu, list) and len(cu) >= 3 and all(isinstance(x, dict) for x in cu[:3])):
                cu = [{"used": 0, "capacity": 0} for _ in range(3)]
                rt["capacityUsage"] = cu
            for i in range(3):
                cu[i]["capacity"] = round(c[i], 1)
        if town_objects:
            for t in game.towns:
                ts = self.by_id.get(t.id)
                if ts:
                    t.capacities = dict(zip(LAND_USES, (round(x) for x in ts.caps)))
                    t.needs = {lu: list(cs) for lu, cs in ts.needs.items()}

    # ---------------- flows from the planner's Sim
    def collect_flows(self, sim, months: int = 12) -> tuple[dict, dict, dict]:
        """(cargo_in {(town node, cargo): units}, arrivals {(town node, tier): persons},
        line_legs {town node: legs by line made by its residents}) over the last `months` months."""
        now = sim.month
        n_nodes = len(getattr(sim, "nodes", []) or []) or 10 ** 9

        def frac(start: int) -> float:
            return max(0.0, min(1.0, (now - start) / months))

        cargo_in: dict = {}
        arrivals: dict = {}
        legs: dict = {}
        for p in getattr(sim, "projects", []):
            c = p.cand
            if c.d in self.by_node:
                start = p.income_month if (self.p.count_from_income_month and p.income_month >= 0) else p.start_month
                f = frac(start)
                if f > 0:
                    cargo_in[(c.d, c.cargo)] = cargo_in.get((c.d, c.cargo), 0.0) + p.volume * f
        for g in getattr(sim, "generic", []):
            meta = g.meta or {}
            start = meta.get("income_month", meta.get("start_month", 0)) if self.p.count_from_income_month \
                else meta.get("start_month", 0)
            f = frac(start)
            if f <= 0:
                continue
            if g.kind == "workers":
                continue                        # trips to industries give no town XP
            if g.kind == "passenger":
                parts = [l for l in (g.legs or []) if isinstance(l, dict) and "nodes" in l and "volume" in l]
                parts = [(l["nodes"], float(l["volume"])) for l in parts] or [(g.nodes, float(g.volume))]
                for nodes, vol in parts:
                    self._add_pax(arrivals, legs, nodes, vol * f)
                continue
            for key, v in (g.uses_demand or {}).items():
                if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int) and key[0] < n_nodes \
                        and key[0] in self.by_node:
                    cargo_in[key] = cargo_in.get(key, 0.0) + float(v) * f
        return cargo_in, arrivals, legs

    def _add_pax(self, arrivals: dict, legs: dict, nodes: list, vol: float):
        towns = [n for n in nodes if n in self.by_node]
        if not towns or vol <= 0:
            return
        if len(towns) == 1 and len(nodes) == 1:
            a = towns[0]
            arrivals[(a, 0)] = arrivals.get((a, 0), 0.0) + vol
            legs[a] = legs.get(a, 0.0) + vol
            return
        if len(towns) < 2:
            return                               # town <-> industry (workers)
        a, b = towns[0], towns[-1]
        tier = self.pax_tier(a, b)
        for x in (a, b):                         # half of the legs end in each town, made by both towns' residents
            arrivals[(x, tier)] = arrivals.get((x, tier), 0.0) + vol / 2
            legs[x] = legs.get(x, 0.0) + vol / 2

    # ---------------- the yearly step
    def yearly_update(self, sim, year: int) -> dict:
        """Call once per January (before candidates are generated) with the deliveries of the past year.
        Returns the deltas for the planner (see docs/towns.md 'Интеграция')."""
        cargo_in, arrivals, legs = self.collect_flows(sim)
        return self.step(year, cargo_in, arrivals, legs)

    def step(self, year: int, cargo_in: dict, arrivals: dict, line_legs: dict | None = None) -> dict:
        line_legs = line_legs or {}
        trips = self._trips_per_resident()
        other_default = self.p.other_ratings
        map_sum = [sum(t.caps[i] for t in self.towns) for i in range(3)]
        need_count = self._need_counts()
        out_levels, notes, xp_gained = {}, [], {}
        for t in self.towns:
            caps = t.caps
            # supplies: per district supplied / demand (deliveries beyond demand are refused)
            dem_lu = [0.0, 0.0, 0.0]
            sup_lu = [0.0, 0.0, 0.0]
            xp_raw = 0.0
            for lu, cs in t.needs.items():
                i = LAND_USES.index(lu)
                for c in cs:
                    d = t.demand(c)
                    got = min(cargo_in.get((t.node, c), 0.0), d) if d > 0 else 0.0
                    dem_lu[i] += d
                    sup_lu[i] += got
                    xp_raw += got * XP_PER_CARGO * 2 ** tier_of(c)
            delivered = sum(sup_lu)
            ratios = [sup_lu[i] / dem_lu[i] if dem_lu[i] > 0 else None for i in range(3)]
            cargo_rating = max((r for r in ratios if r is not None), default=None)
            res = max(t.residents, 1.0)
            line_usage = min(1.0, line_legs.get(t.node, 0.0) / (res * trips)) if trips > 0 else 0.0
            pax_rating = max(0.0, min(1.0, LINE_USAGE_RATING_MULT * line_usage)) if res > 0 else None
            for tier in (0, 1, 2):
                xp_raw += arrivals.get((t.node, tier), 0.0) * XP_PER_PERSON * 2 ** tier
            # authority: min of the six ratings; delivery from the on-time share once cargo flows
            others = [v for k, v in t.ratings.items() if k != "cargo_delivery"]
            other = other_default if other_default is not None else (min(others) if others else 1.0)
            deliv = delivery_rating(self.p.on_time_share, self.sensitivity) if delivered > 0 else 1.0
            authority = min(other, deliv)
            xf = xp_factor(authority, cargo_rating, pax_rating, self.p.xp_bonus)
            t.xp_factor = xf
            gain = xp_raw * xf
            old_level = t.level
            t.xp += gain
            xp_gained[t.id] = gain
            if delivered > 0:
                t.cargo_delivered = True
            new_level, frac = level_and_fraction(t.xp)
            added_needs = []
            if new_level > old_level and t.cargo_delivered:
                added_needs = self._add_needs(t, new_level, need_count)
                for lu, c in added_needs:
                    notes.append({"title": f"{t.name}: уровень {new_level} — новый спрос «{self._cargo_name(c)}»",
                                  "details": f"Район {'торговый' if lu == 'commercial' else 'промышленный'}; "
                                             f"спрос ≈ {t.demand(c):.0f} ед./год (0,3 × ёмкость × "
                                             f"{TIER_FACTOR[tier_of(c)]:g}); производителей на карте "
                                             f"{'есть' if c in self.produced else 'нет'}.",
                                  "town": t.id, "cargo": c})
            if new_level > old_level:
                notes.append({"title": f"{t.name} достиг уровня {new_level}",
                              "details": f"Опыт {t.xp:,.0f}; размер ×{growth_factor(new_level + 1 - t.initial_level + frac):.2f} "
                                         f"от начального.".replace(",", " "), "town": t.id})
            # growth towards initialSum * F(l)
            l = new_level + 1 - t.initial_level + frac
            target = sum(t.initial_caps) * growth_factor(l)
            cur = sum(caps)
            usage = [min(1.0, LINE_USAGE_RATING_MULT * line_usage),
                     max(0.0, min(1.0, ratios[1])) if ratios[1] is not None else 0.0,
                     max(0.0, min(1.0, ratios[2])) if ratios[2] is not None else 0.0]
            grown = [0.0, 0.0, 0.0]
            if math.floor(target) > math.floor(cur) and sum(usage) > 0:
                grown = split_growth(target - cur, caps, usage, map_sum, self.min_district)
                for i in range(3):
                    if t.initial_caps[i] > 0:
                        t.size[i] = (caps[i] + grown[i]) / t.initial_caps[i]
            t.last = {"year": year, "xp_gain": gain, "xp_raw": xp_raw, "xp_factor": xf, "cargo_rating": cargo_rating,
                      "passenger_rating": pax_rating, "line_usage": line_usage, "authority": authority,
                      "delivered": delivered, "demand": sum(dem_lu), "grown": grown}
            out_levels[t.id] = (new_level, round(t.xp))
        # demand deltas vs the totals handed out last time
        totals = self.initial_demand()
        delta = {k: v - self.totals.get(k, 0.0) for k, v in totals.items() if abs(v - self.totals.get(k, 0.0)) > 1e-6}
        self.totals = totals
        pop = self.world_population()
        self.company_xp = max(self.company_xp, pop)
        rank = max(self.rank_of(year), rank_from_population(self.game.raw, self.company_xp))
        self.rank_history[year] = rank
        row = {"year": year, "world_population": round(pop), "rank": rank,
               "ticket_mult": round(ticket_multiplier(rank, self.game.raw), 4),
               "levels": {t.name: t.level for t in self.towns}}
        self.history.append(row)
        return {"year": year, "demand_delta": delta, "demand_total": totals,
                "population": {t.node: t.residents for t in self.towns},
                "capacities": self.capacities(), "world_population": pop, "rank": rank,
                "ticket_mult": row["ticket_mult"], "levels": out_levels, "xp_gained": xp_gained,
                "new_needs": [(n["town"], n["cargo"]) for n in notes if "cargo" in n], "notes": notes}

    # ---------------- needs
    def _need_counts(self) -> dict[str, dict[str, int]]:
        cnt = {"commercial": {}, "industrial": {}}
        for t in self.towns:
            for lu in cnt:
                for c in t.needs.get(lu, []):
                    cnt[lu][c] = cnt[lu].get(c, 0) + 1
        return cnt

    def _add_needs(self, t: TownState, level: int, counts: dict) -> list[tuple[str, str]]:
        """Fill the needs required up to `level` (town_growth.script.tl addCargo): among the cargos of the
        land use the town does not ask for yet, the biggest weight (1 in the tier range, 0.01 outside)
        x (max count + 1 - towns already asking for it). Ties (arbitrary in the game) -> produced on the
        map first, then lower tier, then name."""
        added = []
        req = {"commercial": 0, "industrial": 0}
        for lv in sorted(LEVEL_NEEDS):
            if lv > level:
                break
            for lu, lo, hi in LEVEL_NEEDS[lv]:
                req[lu] += 1
                while len(t.needs[lu]) < req[lu]:
                    cnt = counts[lu]
                    mx = max(cnt.values(), default=0)
                    cands = []
                    for c in self.lu_cargos.get(lu, []):
                        if c in t.needs[lu] or land_use_of(c) not in (lu, None):
                            continue
                        if c == "fish" and "fish" not in self.produced:
                            continue
                        tier = tier_of(c)
                        w = (1.0 if lo <= tier <= hi else 0.01) * (mx + 1 - cnt.get(c, 0))
                        cands.append((-w, c not in self.produced, tier, c))
                    if not cands:
                        break
                    c = min(cands)[3]
                    t.needs[lu].append(c)
                    cnt[c] = cnt.get(c, 0) + 1
                    added.append((lu, c))
        return added

    def _cargo_name(self, key: str) -> str:
        c = self.game.cargo_by_key.get(key)
        return c.name if c and c.name else key


# --------------------------------------------------------------------------- plan.json flows (projection)

_NUM = r"(\d[\d\s  ]*)"


def _num(s: str) -> float:
    return float(re.sub(r"[\s  ]", "", s))


def plan_flows(plan: dict, model: TownModel, year: int) -> tuple[dict, dict, dict]:
    """(cargo_in, arrivals, line_legs) of `year` reconstructed from a written plan.json (steps): direct
    lines, tours, warehouse chains, prospects and passenger lines with a destination town; extra
    vehicles (buy_vehicles '+N ед./год') add to their line. A line counts from 6 months after its build
    month (first loaded arrival, on average). Used for offline projections and tests."""
    game = model.game
    town_by_id = {t.id: t for t in model.towns}
    town_by_name = {t.name: t for t in model.towns}
    name2key = {c.name.lower(): c.key for c in game.cargos.values() if c.name}
    cargo_in: dict = {}
    arrivals: dict = {}
    legs: dict = {}
    line_dest: dict = {}

    def frac(s) -> float:
        start = s["year"] * 12 + (s.get("month") or 1) - 1 + 6
        return max(0.0, min(1.0, (year * 12 + 12 - start) / 12))

    def add_cargo(node, cargo, v):
        cargo_in[(node, cargo)] = cargo_in.get((node, cargo), 0.0) + v

    for s in plan.get("steps", []):
        if s.get("year", 9999) > year:
            continue
        f = frac(s)
        act, kind, det = s.get("action"), s.get("project_kind"), s.get("details") or ""
        if act == "buy_vehicles" and s.get("line") in line_dest and f > 0:
            m = re.search(r"\+" + _NUM + r"\s*ед\./год", det)
            node, cargo = line_dest[s["line"]]
            if m and cargo == s.get("cargo"):
                add_cargo(node, cargo, _num(m.group(1)) * f)
            continue
        if act != "build_line":
            continue
        to = s.get("to") or {}
        dest = town_by_id.get(to.get("id")) if to.get("type") == "town" else None
        if kind == "passenger":
            parts = [l for l in (s.get("legs") or []) if isinstance(l, dict) and "nodes" in l and "volume" in l]
            if parts:
                for l in parts:
                    model._add_pax(arrivals, legs, l["nodes"], float(l["volume"]) * f)
            else:
                m = re.search(r"Пассажиров " + _NUM + r"/год", det)
                a = town_by_id.get((s.get("from") or {}).get("id"))
                if m and a and dest:
                    model._add_pax(arrivals, legs, [a.node, dest.node], _num(m.group(1)) * f)
            continue
        if kind in ("workers", "booster"):
            continue
        if kind == "cargo":                     # tours: "Потоки: A → B: cargo N; ..."
            m = re.search(r"Потоки: (.*?)\. Каждая", det)
            for seg in (m.group(1).split("; ") if m else []):
                if " → " not in seg:
                    continue
                rest = seg.split(" → ", 1)[1]
                if ": " not in rest:
                    continue
                dname, cv = rest.rsplit(": ", 1)
                tt = town_by_name.get(dname.strip())
                mm = re.match(r"(.+?) " + _NUM + r"$", cv.strip())
                if tt and mm:
                    key = name2key.get(mm.group(1).strip().lower())
                    if key:
                        add_cargo(tt.node, key, _num(mm.group(2)) * f)
            continue
        if dest is None or not s.get("cargo"):
            continue
        if kind == "industry_build":
            m = re.search(r"Первая линия:.*?" + _NUM + r"\s*ед\./год", det)
        else:
            m = re.match(r"\s*" + _NUM + r"\s*ед\./год", det)
        if m:
            add_cargo(dest.node, s["cargo"], _num(m.group(1)) * f)
            if s.get("line"):
                line_dest[s["line"]] = (dest.node, s["cargo"])
    return cargo_in, arrivals, legs


def supply_caps(game: Game) -> dict[str, float]:
    """Upper bound of yearly output per cargo on the map: primaries at their max level, processing
    plants capped by their capacity and by the max supply of their inputs (no worker/booster bonus)."""
    from .economics import conversion, max_production
    caps: dict[str, float] = {}
    for ind in game.industries:
        if ind.inputs:
            continue
        for c in ind.outputs:
            caps[c] = caps.get(c, 0.0) + max_production(game, ind, c) * max(ind.max_level, ind.level, 1) \
                / max(ind.level, 1)
    plant: dict[str, float] = {}
    conv: dict[str, dict[str, float]] = {}
    for ind in game.industries:
        if not ind.inputs:
            continue
        for c in ind.outputs:
            plant[c] = plant.get(c, 0.0) + max_production(game, ind, c)
        for c_out, ins in conversion(ind).items():
            conv.setdefault(c_out, ins)
    for c, cap in plant.items():
        ins = conv.get(c) or {}
        by_input = [caps.get(ci, 0.0) * k for ci, k in ins.items()]
        caps[c] = min([cap] + by_input) if by_input else cap
    return caps


def project(plan: dict, game: Game, until: int = 2030, keep_up: bool = False,
            params: TownParams | None = None, nodes: list | None = None) -> tuple[TownModel, list[dict]]:
    """Run the town model over a written plan. keep_up=True: deliveries of every (town, cargo) the plan
    serves grow with the town's demand (the planner adds vehicles as towns grow), capped per cargo by
    the map's production (supply_caps), and passenger legs grow with the residents; False: the plan's
    flows as built."""
    model = TownModel(game, nodes=nodes, params=params)
    base_dem = dict(model.totals)
    base_res = {t.node: t.residents for t in model.towns}
    caps = supply_caps(game) if keep_up else {}
    rows = []
    for y in range(game.year + 1, until + 1):
        cargo_in, arrivals, legs = plan_flows(plan, model, y - 1)
        if keep_up:
            cargo_in = {k: v * model.totals.get(k, 0.0) / base_dem[k] if base_dem.get(k) else v
                        for k, v in cargo_in.items()}
            for c in {k[1] for k in cargo_in}:
                tot = sum(v for k, v in cargo_in.items() if k[1] == c)
                if c in caps and tot > caps[c] > 0:
                    cargo_in = {k: (v * caps[c] / tot if k[1] == c else v) for k, v in cargo_in.items()}
            scale = {n: model.by_node[n].residents / max(base_res[n], 1.0) for n in base_res}
            arrivals = {(n, tr): v * scale.get(n, 1.0) for (n, tr), v in arrivals.items()}
            legs = {n: v * scale.get(n, 1.0) for n, v in legs.items()}
        out = model.step(y, cargo_in, arrivals, legs)
        rows.append({"year": y, "world_population": round(out["world_population"]), "rank": out["rank"],
                     "ticket_mult": out["ticket_mult"],
                     "xp": round(sum(out["xp_gained"].values())),
                     "town_demand": round(sum(out["demand_total"].values())),
                     "delivered": round(sum(v for k, v in cargo_in.items())),
                     "levels": {t.name: t.level for t in model.towns},
                     "new_needs": out["new_needs"]})
    return model, rows


def main():
    import json
    import sys
    from .model import DATA
    game = Game.load()
    plan = json.loads((DATA / "plan.json").read_text(encoding="utf-8"))
    until = int(sys.argv[1]) if len(sys.argv) > 1 else 2030
    for keep in (False, True):
        model, rows = project(plan, game, until, keep_up=keep)
        print(f"\n=== {'keep_up' if keep else 'plan as built'} ===")
        print("year  pop    rank mult  XP/yr  demand deliv  levels(min/avg/max)")
        for r in rows:
            if r["year"] % 5 == 0 or r["year"] in (game.year + 1, until):
                lv = list(r["levels"].values())
                print(f"{r['year']} {r['world_population']:6d} {r['rank']:4d} {r['ticket_mult']:.3f} {r['xp']:6d} "
                      f"{r['town_demand']:6d} {r['delivered']:5d}  {min(lv)}/{sum(lv) / len(lv):.1f}/{max(lv)}")
        top = sorted(model.towns, key=lambda t: -t.xp)[:8]
        for t in top:
            print(f"  {t.name:15s} L{t.level} xp {t.xp:8.0f} caps {[round(c) for c in t.caps]} needs {t.needs}")


if __name__ == "__main__":
    main()
