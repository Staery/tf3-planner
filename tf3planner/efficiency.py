"""Industry efficiency program: boosters, workers, prospecting and production chains.

Goal (user): plan delivering everything industries need so that they become more productive
over time. Three levers exist in TF3 (docs/mechanics.md §8, §10, §12):

1. Booster cargo (fertilizer -> farms, tools -> clay pits / fishing grounds, machines -> forests /
   oil platforms ...). While the booster stock is supplied the main output is multiplied by
   (1 + boostFactor) (boostFactor random 0.5..1.5 per industry, exported). Booster consumption is
   NOT scaled by productivity (economics.booster_info). Boosters are ordinary cargo, so the
   delivery leg pays 3.8 $/unit/m (chord) like any other cargo.
2. Workers: >= 10 distinct residents arriving by public transport within 2 years ->
   productivity x(1.5..2.5) (industry_productivity_util.tl). Worker lines are built by
   passengers.worker_candidates; this module only feeds them the boost value.
3. Growth: every half year an industry shipping >= 75 % of its output gains one field (level);
   output is proportional to the level (industries.script.tl). Boosts raise the output, so the
   outbound lines must grow with them to keep the 75 %.

What the player can build (verified in the game code, build 40408)
-----------------------------------------------------------------
* The industry builder (menu "Industry") is shown only in the map editor or in sandbox mode:
  ``hasIndustryBuilder = params.isMapEditor or params.isSandboxMode`` (gui/main/game.tl:448).
  Industry .con files carry no company metadata / permit. This save has sandboxMode = false,
  so in career mode the player can NOT place a chemical plant, tool factory or machine factory.
* "Prospecting" (perks menu) is the career-mode way to get new industries: pick a town and a raw
  resource; prospectors search for 6 months (attempt every month, chance 0.2 -> 0.6,
  company_util.tl), then the engine spawns an industry of a matching type in that town's
  Voronoi cell (api.engine.util.proposal.makeIndustrySpawnProposal(type, town)). Free of charge
  (no money is debited in company.script.tl spawnIndustry). A failed prospection gives the
  permit back. Each resource has its own one-time permit unlocked at a company rank
  (game_mechanics/company/explorations/*.res.lua, rankAndPermits {{rank, 1}}):
  fish 2, clay 3, crude oil 4, logs 5, coal 6, iron ore 6, stone 8, wool 8, sand 9, rubber 12.
  Grain and vegetables share the permit key permit_key_farm.res = "Industry" {{4, 1}}:
  ONE farm prospection in total from rank 4. Permits consumed by prospection never come back
  (no cooldown on these keys; only "Marketing" has a 5-year cooldown).
  Ranks count only when applied in the company window (progressionState.level).
* Processing industries appear only through the engine's own spawning (industryDevelopment.
  spawnIndustries = true). The temperate economy lists ``companyRankRequiredIndustries``
  (rank 3: steel mill, iron ore mine, coal mine; 5: refinery, oil platform; 7: forest, saw mill;
  10: chemical plant; 13: furniture factory, glass works, sand excavator) and
  ``cargoRequiredIndustries`` (e.g. tools -> tool_factory). How the engine uses them is C++ and
  UNVERIFIED; the most plausible reading: from rank 10 a chemical plant is likely to spawn,
  after which tool / machine factories can follow.

Therefore ``industry_build_options`` reports the career rules and the prospection options, and
still solves the chain-siting problem (chemical plant + tool factory) as a weighted Weber problem
on the real road cost grid: it tells where such factories pay best (sandbox / editor, or to judge
an engine-spawned factory). ``efficiency_candidates`` emits "industry_build" candidates for
prospecting in career mode and for factory chains only when sandbox mode is on.

Conventions for ProjectCandidate (candidates.py)
-----------------------------------------------
* Booster links: kind "booster", income = transport income + boost value; the boost value uses
  the per-unit profit of the industry's existing outbound lines (same rule as
  Planner.boost_value), boosts {(industry_node, booster_cargo): 1 + boostFactor * supplied
  fraction} so the planner adds the extra output to the supply (planner._build_generic).
* Worker lines: kind "workers" from passengers.worker_candidates with our boost value.
* Prospecting: kind "industry_build", meta["action"] = "prospect"; bundled with the first
  outbound line of the new industry (the site is only known to be in the town's Voronoi cell,
  so it is estimated and the supply key is the town node, meta["approximate_site"] = True).
  station_keys contain (town_node, "prospect:<permit>") so a used permit is not offered again.
* Factory chains (sandbox only): kind "industry_build", meta["action"] = "build_chain".

All money per game year; texts for the plan are Russian.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np
from scipy import ndimage, sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree

from .candidates import ProjectCandidate, VehicleLine
from .economics import (INDUSTRY_PRODUCTIVITY, Endpoint, LinkEval, booster_info, evaluate_link,
                        max_production)
from .model import DATA, Game, Industry

# --------------------------------------------------------------------------- game facts

MAX_RANK = 15
NORMALIZED_BASE_POPULATION = 725           # company_progression_util.tl calcXpNeededForRank
PROSPECTION_MONTHS = 6                     # company_util.prospectionTimeMs = 6 months
PROSPECTION_CHANCE = (0.2, 0.6)            # attempt chance mapped over the 6 months
WORKERS_MIN = 10                           # 0.5 x industrialCapacity 20
WORKER_MULT = 2.0                          # expected 1.5 + U[0,1) (passengers.PassengerParams)
NEW_INDUSTRY_SIZE = 1.0                    # consumptionFactor of a freshly built processing industry (ASSUMPTION)
DETOUR = 1.3                               # route / straight when no route is known
INFRA_YEARS = 20.0                         # annualisation of generalized route cost in the Weber objective
SITE_TOWN_CLEARANCE = 1000.0               # m from a town centre (industries are not placed inside towns)
SITE_INDUSTRY_CLEARANCE = 600.0            # m from existing industries
SITE_MAX_SLOPE = 0.08                      # local terrain grade a large industry lot tolerates (ASSUMPTION)
SITE_BORDER = 1000.0                       # m from the map edge
PROSPECT_TOWN_CLEARANCE = 1500.0           # raw industries prefer distance from towns (placement TOWN = FAR..VERY_FAR)


@dataclass(frozen=True)
class Exploration:
    cargo: str
    rank: int
    permit: str                 # permit key: the exploration itself or a shared permit key
    types: tuple[str, ...]      # industry types the engine may spawn (temperate economy tags)


# game_mechanics/company/explorations/*.res.lua x economy/placementparamsutil.lua (temperate)
EXPLORATIONS: tuple[Exploration, ...] = (
    Exploration("fish", 2, "exploration_fish", ("fishing_grounds",)),
    Exploration("clay", 3, "exploration_clay", ("clay_pit",)),
    Exploration("crude_oil", 4, "exploration_crude_oil", ("oil_platform", "oil_well")),
    Exploration("grain", 4, "permit_key_farm", ("farm",)),
    Exploration("vegetables", 4, "permit_key_farm", ("farm",)),
    Exploration("logs", 5, "exploration_logs", ("forest",)),
    Exploration("coal", 6, "exploration_coal", ("coal_mine",)),
    Exploration("iron_ore", 6, "exploration_iron_ore", ("iron_ore_mine",)),
    Exploration("stone", 8, "exploration_stone", ("quarry",)),
    Exploration("wool", 8, "exploration_wool", ("cotton_farm",)),
    Exploration("sand", 9, "exploration_sand", ("sand_pit", "sand_excavator")),
)

# company permit keys (game_mechanics/company/permitKeys/*.res.lua)
PERMITS = {
    "hq": {"name": "Штаб-квартира", "rank_and_permits": ((1, 1),), "cooldown_years": None},
    "marketing": {"name": "Маркетинг", "rank_and_permits": ((4, 1),), "cooldown_years": 5},
    "permit_key_farm": {"name": "Предприятие (разведка фермы: зерно или овощи)",
                        "rank_and_permits": ((4, 1),), "cooldown_years": None},
}

# economy/temperate.eco.lua companyRankRequiredIndustries (engine-side use, UNVERIFIED)
ENGINE_RANK_INDUSTRIES = {
    2: ("quarry",),
    3: ("steel_mill", "iron_ore_mine", "coal_mine"),
    5: ("oil_refinery", "oil_platform"),
    7: ("forest", "saw_mill"),
    10: ("chemical_plant",),
    13: ("furniture_factory", "glass_works", "sand_excavator"),
}

TYPE_RU = {
    "farm": "ферма", "clay_pit": "глиняный карьер", "fishing_grounds": "рыболовный участок",
    "oil_platform": "нефтяная платформа", "oil_well": "нефтяная скважина", "forest": "лесозаготовка",
    "coal_mine": "угольная шахта", "iron_ore_mine": "железорудная шахта", "quarry": "каменоломня",
    "cotton_farm": "хлопковая ферма", "sand_pit": "песчаный карьер", "sand_excavator": "земснаряд",
    "chemical_plant": "химзавод", "tool_factory": "завод инструментов",
    "machine_factory": "машиностроительный завод", "steel_mill": "сталелитейный завод",
    "oil_refinery": "НПЗ", "saw_mill": "лесопилка", "livestock_farm": "животноводческая ферма",
    "bricks_works": "кирпичный завод", "furniture_factory": "мебельная фабрика",
    "glass_works": "стекольный завод",
}


# --------------------------------------------------------------------------- rank

def world_growth_factor(rank: int) -> float:
    """scripts/util/town_growth_function.tl worldGrowthFactor."""
    return 0.001 * rank ** 3 + 0.027398 * rank ** 2 + 0.14221 * rank + 0.829392


def rank_population(base_population: int, rank: int) -> int:
    """World population (= company XP) needed for `rank` (company_progression_util.tl)."""
    if rank < 1:
        return 0
    w = (MAX_RANK - rank) / (MAX_RANK - 1)
    interp = math.floor(w * NORMALIZED_BASE_POPULATION + (1 - w) * base_population)
    return max(rank, math.floor(interp * world_growth_factor(rank)) - interp + base_population)


def rank_for_population(base_population: int, population: float) -> int:
    r = 0
    for k in range(1, MAX_RANK + 1):
        if population >= rank_population(base_population, k):
            r = k
    return r


def world_population(game: Game) -> int:
    """Σ residential capacity of all towns (company_util.computeWorldPopulation)."""
    return int(sum(t.capacities.get("residential", 0) for t in game.towns))


def prospection_odds() -> tuple[float, float]:
    """(success probability, expected months until success | success) of one prospection,
    assuming the engine finds room for the industry. Attempts after months 1..5 with chance
    0.2 + 0.4 * k/6, final attempt after month 6 with 0.6 (company.script.tl)."""
    lo, hi = PROSPECTION_CHANCE
    fail, p_succ, e_month = 1.0, 0.0, 0.0
    for k in range(1, PROSPECTION_MONTHS + 1):
        p = lo + (hi - lo) * min(1.0, k / PROSPECTION_MONTHS)
        p_succ += fail * p
        e_month += fail * p * k
        fail *= 1 - p
    return p_succ, e_month / p_succ


# --------------------------------------------------------------------------- recipes / boosters

def load_recipes() -> dict[str, dict]:
    """All 32 industry recipes keyed by type stem (e.g. 'tool_factory')."""
    raw = json.loads((DATA / "industry_recipes.json").read_text(encoding="utf-8"))
    return {k.rsplit("/", 1)[-1].removesuffix(".con"): v for k, v in raw.items()}


def recipe_rate(recipe: dict, cargo: str, size: float = NEW_INDUSTRY_SIZE) -> tuple[float, dict[str, float]]:
    """(max output/year of `cargo`, inputs per unit of that output) for a processing recipe."""
    for r in recipe.get("rules", []):
        if r.get("booster") or cargo not in r.get("output", {}):
            continue
        n_out = r["output"][cargo]
        per_year = n_out * r["capacity"] * size * INDUSTRY_PRODUCTIVITY
        need = {c: n / n_out for c, n in (r["input"][0] if r["input"] else {}).items() if n}
        return per_year, need
    return 0.0, {}


@dataclass
class BoosterNeed:
    industry_id: int
    name: str
    type: str
    cargo: str
    need: float                         # booster units/year for a full boost
    boost_factor: float                 # output x (1 + boost_factor) while supplied
    base_output: dict[str, float]       # main outputs/year (unboosted, current level)
    node: int | None = None

    @property
    def extra_output(self) -> float:
        return sum(self.base_output.values()) * self.boost_factor

    @property
    def extra_per_booster_unit(self) -> float:
        return self.extra_output / self.need if self.need else 0.0


def booster_needs(game: Game, nodes=None) -> list[BoosterNeed]:
    node_of = {n.id: n.index for n in nodes if n.kind == "industry"} if nodes else {}
    out = []
    for ind in game.industries:
        for c, (need, bf) in booster_info(game, ind).items():
            base = {o: max_production(game, ind, o) for o in ind.outputs}
            out.append(BoosterNeed(ind.id, ind.name, ind.type, c, need, bf, base, node_of.get(ind.id)))
    return out


def _producers(game: Game, cargo: str) -> list[Industry]:
    return [i for i in game.industries if cargo in i.outputs]


def supply_chain(game: Game, cargo: str, recipes: dict | None = None, _seen: frozenset = frozenset()) -> dict:
    """Recursive chain for `cargo`: producers on the map, producer types and their inputs.
    `available` = some producer exists on the map and (for processing producers) its inputs are
    available too."""
    recipes = recipes or load_recipes()
    on_map = _producers(game, cargo)
    types = sorted(t for t, r in recipes.items()
                   if any(cargo in rule.get("output", {}) and not rule.get("booster") for rule in r["rules"]))
    node = {"cargo": cargo, "producers_on_map": [(i.id, i.name, i.type) for i in on_map],
            "producer_types": types, "inputs": {}, "available": False}
    if cargo in _seen:
        return node
    primary = [i for i in on_map if not i.inputs]
    if primary:
        node["available"] = True
        return node
    # processing producers (on the map or not): what do they need?
    cand_types = sorted({i.type for i in on_map}) or types
    for t in cand_types[:1]:
        _, need = recipe_rate(recipes.get(t, {"rules": []}), cargo)
        for c_in in need:
            node["inputs"][c_in] = supply_chain(game, c_in, recipes, _seen | {cargo})
    node["available"] = bool(on_map) and all(v["available"] for v in node["inputs"].values())
    node["producer_type"] = cand_types[0] if cand_types else None
    return node


def chain_text(game: Game, chain: dict) -> str:
    """Russian one-liner: 'инструменты ← завод инструментов [нет] ← (доски ← лесопилка [2 на карте] ...)'."""
    name = game.cargo_by_key[chain["cargo"]].name.lower() if chain["cargo"] in game.cargo_by_key else chain["cargo"]
    n = len(chain["producers_on_map"])
    t = chain.get("producer_type") or (chain["producer_types"][0] if chain["producer_types"] else None)
    who = TYPE_RU.get(t, t or "?")
    s = f"{name} ← {who} [{'есть ' + str(n) if n else 'нет на карте'}]"
    if chain["inputs"]:
        s += " ← (" + " + ".join(chain_text(game, v) for v in chain["inputs"].values()) + ")"
    return s


def booster_sources(game: Game, recipes: dict | None = None) -> dict[str, dict]:
    """For every booster cargo needed on the map: needs, sources now (with volumes), missing chains.

    Volumes of processing sources are their max output; `available_per_year` additionally caps
    them by what the primary producers on the map can feed (one level deep, e.g. fertilizer =
    0.25 x crude oil delivered to refineries)."""
    recipes = recipes or load_recipes()
    needs = booster_needs(game)
    out: dict[str, dict] = {}
    for c in sorted({n.cargo for n in needs}):
        nn = sorted([n for n in needs if n.cargo == c], key=lambda n: -n.extra_output)
        sources, cap_total, feed_total = [], 0.0, 0.0
        for ind in _producers(game, c):
            mx = max_production(game, ind, c)
            req = {}
            if ind.inputs:
                _, req = recipe_rate(recipes.get(ind.type, ind.recipe or {"rules": []}), c)
            sources.append({"industry_id": ind.id, "name": ind.name, "type": ind.type,
                            "max_per_year": round(mx, 1), "requires_per_unit": req})
            cap_total += mx
        if sources:
            # what the map's primary producers can feed into these sources
            feed = math.inf
            req = sources[0]["requires_per_unit"]
            for c_in, per in req.items():
                primary = sum(max_production(game, i, c_in) for i in _producers(game, c_in) if not i.inputs)
                feed = min(feed, primary / per if per else math.inf)
            feed_total = min(cap_total, feed) if req else cap_total
        chain = supply_chain(game, c, recipes)
        total_need = sum(n.need for n in nn)
        out[c] = {
            "cargo": c, "name": game.cargo_by_key[c].name if c in game.cargo_by_key else c,
            "needs": nn, "consumers": len(nn), "total_need": round(total_need, 1),
            "total_extra_output": round(sum(n.extra_output for n in nn), 1),
            "sources": sources, "max_per_year": round(cap_total, 1),
            "available_per_year": round(feed_total, 1),
            "coverage": round(min(1.0, feed_total / total_need), 3) if total_need else 0.0,
            "chain": chain, "chain_text": chain_text(game, chain),
            "missing": not chain["available"],
            "missing_types": _missing_types(chain),
        }
    return out


def _missing_types(chain: dict) -> list[str]:
    out = []
    if not chain["producers_on_map"]:
        t = chain.get("producer_type") or (chain["producer_types"][0] if chain["producer_types"] else None)
        if t:
            out.append(t)
    for v in chain["inputs"].values():
        out += [t for t in _missing_types(v) if t not in out]
    return out


# --------------------------------------------------------------------------- planner helpers

def _sim_or_initial(planner, sim_state):
    if sim_state is not None:
        return sim_state
    if hasattr(planner, "initial_state"):
        return planner.initial_state()
    return SimpleNamespace(supply={}, demand={}, projects=[], generic=[], built_pairs=set(), stations=set())


def _node_ind(planner, idx: int) -> Industry | None:
    n = planner.nodes[idx]
    if n.kind != "industry":
        return None
    ind = getattr(planner, "ind", None)
    return ind[n.id] if ind else next(i for i in planner.game.industries if i.id == n.id)


def _vehicle_lines(ev: LinkEval) -> list[VehicleLine]:
    return [VehicleLine(v.id, v.name, v.file, n * ev.n_consists, v.price) for v, n in ev.consist.units]


def _life(ev: LinkEval) -> float:
    return min((v.lifespan_years for v, _ in ev.consist.units), default=30.0) if ev.consist else 30.0


def _decimate(poly) -> list[list[float]]:
    if not poly:
        return []
    step = max(1, len(poly) // 60)
    pts = [[round(x, 1), round(y, 1)] for x, y in poly[::step]]
    last = [round(poly[-1][0], 1), round(poly[-1][1], 1)]
    if pts[-1] != last:
        pts.append(last)
    return pts


def _endpoint(n) -> Endpoint:
    return Endpoint(n.kind, n.id, n.name, n.x, n.y, n.z)


def _fmt(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def outbound_lines(sim, node: int) -> list[tuple[str, float, float, float, float]]:
    """(cargo, volume, income, running, vehicle capex) of lines leaving `node` in the planner state."""
    out = []
    for pr in getattr(sim, "projects", []) or []:
        c = pr.cand
        if c.p == node and pr.volume > 0:
            out.append((c.cargo, pr.volume, pr.income, pr.running, getattr(pr, "vehicle_value", 0.0)))
    for pc in getattr(sim, "generic", []) or []:
        if pc.kind in ("cargo", "booster", "industry_build") and pc.nodes and pc.nodes[0] == node and pc.volume > 0:
            out.append((pc.cargo, pc.volume, pc.income - pc.meta.get("boost_value", 0.0), pc.running, pc.vehicle_capex))
    return out


def boost_profit(planner, sim, node: int, mult: float, outputs: dict[str, float] | None = None) -> tuple[float, float]:
    """(yearly profit gain, extra vehicle capex) when the industry's output is multiplied by `mult`:
    extra units x per-unit profit of its existing outbound lines (0 if nothing ships yet).
    Same rule as Planner.boost_value, plus the vehicles the extra volume needs."""
    if mult <= 1:
        return 0.0, 0.0
    gain, capex = 0.0, 0.0
    for cargo, vol, inc, run, vcap in outbound_lines(sim, node):
        if outputs is not None and cargo not in outputs:
            continue
        per_unit = (inc - run) / vol
        extra = vol * (mult - 1)
        gain += max(0.0, per_unit) * extra
        capex += vcap / vol * extra
    return gain, capex


_MARGIN_CACHE: dict = {}


def outbound_margin(planner, sim, node: int, cargo: str, volume: float, year: int) -> dict:
    """Per-unit profit of shipping `cargo` out of `node`: from the existing line if there is one,
    else the best of the 4 nearest consumers with free demand (evaluate_link, route from the
    planner matrices; water for water-only industries). Keys: profit, capex, income, consumer, mode,
    source ('line' | 'estimate' | 'none')."""
    lines = [l for l in outbound_lines(sim, node) if l[0] == cargo]
    if lines:
        vol = sum(l[1] for l in lines)
        return {"profit": sum(l[2] - l[3] for l in lines) / vol, "capex": sum(l[4] for l in lines) / vol,
                "income": sum(l[2] for l in lines) / vol, "consumer": None, "mode": None, "source": "line"}
    key = (id(planner), node, cargo, round(volume), year)
    if key in _MARGIN_CACHE:
        return _MARGIN_CACHE[key]
    best = _best_link(planner, sim, node, cargo, volume, year)
    if best is None:
        res = {"profit": 0.0, "capex": 0.0, "income": 0.0, "consumer": None, "mode": None, "source": "none"}
    else:
        ev, j, mode = best
        res = {"profit": ev.profit / ev.volume, "capex": ev.capex / ev.volume, "income": ev.income / ev.volume,
               "consumer": j, "mode": mode, "source": "estimate", "ev": ev}
    _MARGIN_CACHE[key] = res
    return res


def _consumers(planner, sim, cargo: str) -> list[int]:
    dem = getattr(sim, "demand", {}) or {}
    out = []
    for n in planner.nodes:
        if dem:
            if dem.get((n.index, cargo), 0) >= 1:
                out.append(n.index)
            continue
        if n.kind == "industry":
            ind = _node_ind(planner, n.index)
            if ind and cargo in ind.inputs:
                out.append(n.index)
        else:
            t = planner.town[n.id] if getattr(planner, "town", None) else None
            if t and any(cargo in cs for cs in t.needs.values()):
                out.append(n.index)
    return out


def _link(planner, i: int, j: int, cargo: str, volume: float, year: int, mode: str,
          built_keys: set = frozenset()) -> LinkEval | None:
    a, b = planner.nodes[i], planner.nodes[j]
    g = planner.game
    if mode == "water":
        w = getattr(planner, "water", None)
        if w is None or not np.isfinite(w.length[i, j]):
            return None
        if hasattr(planner, "water_ok") and not planner.water_ok(i, j):
            return None
        route, build, maint, grade = float(w.length[i, j]), 0.0, 0.0, 0.0
    else:
        if a.water_only or b.water_only or mode not in planner.mat:
            return None
        m = planner.mat[mode]
        if not np.isfinite(m.build[i, j]):
            return None
        route, build, maint, grade = float(m.length[i, j]), float(m.build[i, j]), float(m.maint[i, j]), 0.01
        if (min(i, j), max(i, j), mode) in built_keys:
            build = maint = 0.0
    return evaluate_link(g, planner.vehicles, g.cargo_by_key[cargo], _endpoint(a), _endpoint(b), mode, volume,
                         year, route_m=route, route_build_cost=build, route_maintenance=maint, max_grade=grade)


def _best_link(planner, sim, i: int, cargo: str, volume: float, year: int, built_keys: set = frozenset(),
               k: int = 4):
    src = planner.nodes[i]
    cons = [j for j in _consumers(planner, sim, cargo) if j != i]
    cons.sort(key=lambda j: math.dist((src.x, src.y), (planner.nodes[j].x, planner.nodes[j].y)))
    best = None
    modes = ("water",) if src.water_only else ("road", "rail", "water")
    for j in cons[:k]:
        for mode in modes:
            ev = _link(planner, i, j, cargo, volume, year, mode, built_keys)
            if ev is None or ev.volume <= 0:
                continue
            score = ev.profit - ev.capex / 10
            if best is None or score > best[0]:
                best = (score, ev, j, mode)
    return None if best is None else best[1:]


# --------------------------------------------------------------------------- site grid (Weber)

class SiteGrid:
    """Road cost grid of tf3planner.routing for siting factories: length / generalized-cost fields
    from terminals (scipy Dijkstra over the 64 m grid) and a validity mask for industry lots."""

    def __init__(self, game: Game, year: int | None = None):
        from .routing import get_planner
        self.game = game
        self.rp = rp = get_planner(game, "road", year)
        g = rp.graph.tocsr()
        rows = np.repeat(np.arange(rp.N), np.diff(g.indptr))
        uy, ux = np.divmod(rows, rp.nx)
        vy, vx = np.divmod(g.indices, rp.nx)
        lens = rp.cell * np.hypot(vy - uy, vx - ux)
        self.g_cost = g
        self.g_len = sparse.csr_matrix((lens, g.indices.copy(), g.indptr.copy()), shape=g.shape)
        xs, ys = rp.cell_xy(np.arange(rp.N))
        self.xs, self.ys = np.asarray(xs, float), np.asarray(ys, float)
        self.valid = self._valid_mask()

    def _valid_mask(self) -> np.ndarray:
        rp, g = self.rp, self.game
        z = rp.z
        gy, gx = np.gradient(z, rp.cell)
        slope = ndimage.maximum_filter(np.hypot(gx, gy), size=5)
        ok = (~rp.water) & (slope <= SITE_MAX_SLOPE)
        ok &= ~ndimage.binary_dilation(rp.water, iterations=2)       # keep lots off the shore line
        ok = ok.ravel()
        x0, x1 = self.xs.min() + SITE_BORDER, self.xs.max() - SITE_BORDER
        y0, y1 = self.ys.min() + SITE_BORDER, self.ys.max() - SITE_BORDER
        ok &= (self.xs >= x0) & (self.xs <= x1) & (self.ys >= y0) & (self.ys <= y1)
        pts = np.c_[self.xs, self.ys]
        if g.towns:
            d, _ = cKDTree([(t.x, t.y) for t in g.towns]).query(pts)
            ok &= d >= SITE_TOWN_CLEARANCE
        if g.industries:
            d, _ = cKDTree([(i.x, i.y) for i in g.industries]).query(pts)
            ok &= d >= SITE_INDUSTRY_CLEARANCE
        return ok

    def cell(self, x: float, y: float) -> int:
        return self.rp.snap(x, y)

    def xy(self, cell: int) -> tuple[float, float]:
        return float(self.xs[cell]), float(self.ys[cell])

    def fields(self, cells: list[int]) -> tuple[np.ndarray, np.ndarray]:
        """(length, generalized cost) from each terminal cell to every grid cell, shape (K, N).
        The graph is (nearly) symmetric, so terminal -> site equals site -> terminal."""
        idx = np.asarray(cells, dtype=np.int64)
        L = csgraph.dijkstra(self.g_len, directed=True, indices=idx)
        G = csgraph.dijkstra(self.g_cost, directed=True, indices=idx)
        return np.atleast_2d(L), np.atleast_2d(G)

    def route(self, a: int, b: int):
        """routing.Route along the cheapest buildable road between two cells."""
        path, _ = self.rp.dijkstra_path(a, b)
        return None if path is None else self.rp.make_route(path)


def weiszfeld(points: np.ndarray, weights: np.ndarray, x0: np.ndarray | None = None,
              iters: int = 500, tol: float = 0.1) -> np.ndarray:
    """Weighted Euclidean Weber point (Weiszfeld iteration, with the Vardi-Zhang guard for
    iterates that land on a terminal)."""
    P = np.asarray(points, float)
    w = np.asarray(weights, float)
    x = np.average(P, axis=0, weights=w) if x0 is None else np.asarray(x0, float)
    for _ in range(iters):
        d = np.hypot(*(P - x).T)
        on = d < 1e-6
        if on.any():
            k = int(np.argmax(on))
            # optimality test at terminal k: |R| <= w_k
            dd = np.where(on, 1.0, d)
            R = ((w / dd)[:, None] * (P - x))[~on].sum(axis=0)
            if np.hypot(*R) <= w[k]:
                return x
            d = np.where(on, 1e-6, d)
        inv = w / d
        nx = (inv[:, None] * P).sum(axis=0) / inv.sum()
        if np.hypot(*(nx - x)) < tol:
            return nx
        x = nx
    return x


# --------------------------------------------------------------------------- unit transport costs

def unit_cost_per_m(game: Game, vehicles, cargo: str, year: int, mode: str = "road",
                    volume: float = 150.0, ref_m: float = 5000.0) -> float:
    """Vehicle cost per unit per route metre: (running + capex / lifespan) / (volume x route)."""
    ev = evaluate_link(game, vehicles, game.cargo_by_key[cargo], Endpoint("industry", 0, "a", 0, 0, 0),
                       Endpoint("industry", 1, "b", ref_m / DETOUR, 0, 0), mode, volume, year,
                       route_m=ref_m, route_build_cost=0.0, route_maintenance=0.0)
    if ev is None or ev.volume <= 0:
        return 1.5
    return (ev.running + ev.vehicle_capex / _life(ev)) / (ev.volume * ev.route_m)


# --------------------------------------------------------------------------- chain siting

@dataclass
class Flow:
    cargo: str
    volume: float
    src: tuple                   # ("node", index) | ("site", facility name)
    dst: tuple


@dataclass
class ChainPlan:
    feasible: bool
    facilities: dict = field(default_factory=dict)       # name -> {x, y, cell, weiszfeld_xy, cost, ...}
    flows: list = field(default_factory=list)            # Flow
    targets: list = field(default_factory=list)          # boosted industries
    legs: list = field(default_factory=list)             # evaluated legs (dict)
    value: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    alternatives: dict = field(default_factory=dict)


def _target_value(planner, sim, need: BoosterNeed, year: int) -> tuple[float, dict]:
    """Yearly profit of boosting `need`'s industry fully: extra output x per-unit profit of its
    outbound line (existing or the best estimate)."""
    total, info = 0.0, {}
    for c, base in need.base_output.items():
        if base <= 0:
            continue
        m = outbound_margin(planner, sim, need.node, c, base * (1 + need.boost_factor), year)
        extra = base * need.boost_factor
        total += extra * max(0.0, m["profit"])
        info[c] = {"extra": extra, "per_unit_profit": m["profit"], "per_unit_capex": m["capex"],
                   "source": m["source"], "consumer": m["consumer"], "mode": m["mode"]}
    return total, info


def chain_sites(planner, year: int | None = None, sim_state=None, grid: SiteGrid | None = None,
                booster: str = "tools", recipes: dict | None = None, n_factories: int = 1,
                max_iter: int = 6) -> ChainPlan:
    """Optimal sites for a chemical plant + tool factory feeding the `booster` = 'tools' targets
    (clay pits, fishing grounds ...). Weighted Weber problem solved two ways:

    * Weiszfeld on straight distances (weights = volume x unit cost x detour) - reference point;
    * exact discrete optimum over all valid land cells of the 64 m road grid with real route
      lengths and generalized build costs (Dijkstra fields), alternating between the two
      facilities and re-assigning inputs to the nearest sources (Cooper's location-allocation).

    Objective per facility: Σ_legs volume x unit_cost(cargo) x route_length + Σ_legs
    generalized_route_cost / INFRA_YEARS. Then every leg is evaluated with economics.evaluate_link
    and the boost value of the targets is added (extra output x outbound per-unit profit)."""
    g = planner.game
    year = g.year if year is None else year
    recipes = recipes or load_recipes()
    sim = _sim_or_initial(planner, sim_state)
    plan = ChainPlan(False)
    if booster == "machines":
        return _machine_chain(planner, plan, recipes)
    tools_max, tool_need = recipe_rate(recipes["tool_factory"], "tools")
    plastic_max, chem_need = recipe_rate(recipes["chemical_plant"], "plastic")
    tools_max *= n_factories
    # targets: industries needing tools, best value per tool first
    needs = [n for n in booster_needs(g, planner.nodes) if n.cargo == booster and n.node is not None]
    scored = []
    for n in needs:
        val, info = _target_value(planner, sim, n, year)
        scored.append((val / n.need if n.need else 0.0, val, n, info))
    scored.sort(key=lambda s: -s[0])
    targets, tools_out = [], 0.0
    for dens, val, n, info in scored:
        if tools_out + n.need > tools_max + 1e-6 or val <= 0:
            continue
        targets.append({"need": n, "value": val, "info": info})
        tools_out += n.need
    if not targets:
        plan.notes.append("нет целей с положительной ценностью буста")
        return plan
    planks = tools_out * tool_need.get("planks", 0.8)
    plastic = tools_out * tool_need.get("plastic", 0.4)
    chemicals = plastic * chem_need.get("chemicals", 1.2)
    dyes = plastic * 2 / 5
    saws = [n for n in planner.nodes if n.kind == "industry" and "planks" in (_node_ind(planner, n.index).outputs)]
    refs = [n for n in planner.nodes if n.kind == "industry" and "chemicals" in (_node_ind(planner, n.index).outputs)]
    if not saws or not refs:
        plan.notes.append("нет лесопилок или НПЗ на карте")
        return plan
    cap = {n.index: max_production(g, _node_ind(planner, n.index), "planks") for n in saws}
    cap.update({n.index: max_production(g, _node_ind(planner, n.index), "chemicals") for n in refs})
    # unit costs ($/unit/route m) per cargo
    u = {c: unit_cost_per_m(g, planner.vehicles, c, year) for c in ("planks", "plastic", "chemicals", "tools")}
    grid = grid or SiteGrid(g, year)
    tgt_nodes = [t["need"].node for t in targets]
    term = [n.index for n in saws] + [n.index for n in refs] + tgt_nodes
    cells = [grid.cell(planner.nodes[i].x, planner.nodes[i].y) for i in term]
    L, G = grid.fields(cells)
    row = {i: k for k, i in enumerate(term)}
    valid = grid.valid & np.all(np.isfinite(L), axis=0)
    if not valid.any():
        plan.notes.append("нет допустимых площадок")
        return plan

    def allocate(site_cell: int, sources: list, total: float) -> dict[int, float]:
        """Greedy: nearest sources (by route length from the site) first, capped by capacity."""
        order = sorted((n.index for n in sources), key=lambda i: L[row[i], site_cell])
        out, left = {}, total
        for i in order:
            take = min(left, cap[i])
            if take > 0:
                out[i] = take
                left -= take
        if left > 1e-6 and order:
            out[order[0]] = out.get(order[0], 0) + left       # over capacity: note it, keep nearest
        return out

    def objective(legs: list[tuple[int, float, float]], extra: np.ndarray | None = None,
                  away_from: int | None = None) -> np.ndarray:
        """legs: (terminal node, volume, unit cost); away_from: the other facility's cell (two
        industries cannot share a lot, keep SITE_INDUSTRY_CLEARANCE between them)."""
        f = np.zeros(L.shape[1])
        for i, vol, uc in legs:
            k = row[i]
            f += vol * uc * L[k] + G[k] / INFRA_YEARS
        if extra is not None:
            f += extra
        ok = valid
        if away_from is not None:
            ox, oy = grid.xy(away_from)
            ok = ok & (np.hypot(grid.xs - ox, grid.ys - oy) >= SITE_INDUSTRY_CLEARANCE)
        return np.where(ok, f, np.inf)

    # start: tool factory with chemistry co-located near the refineries' weighted centre
    tool_legs = [(i, t["need"].need, u["tools"]) for i, t in zip(tgt_nodes, targets)]
    tool_cell = int(np.argmin(objective(tool_legs + [(n.index, planks / len(saws), u["planks"]) for n in saws]
                                        + [(n.index, chemicals / len(refs), u["chemicals"]) for n in refs])))
    chem_cell = tool_cell
    hist = []
    for _ in range(max_iter):
        p_alloc = allocate(tool_cell, saws, planks)
        c_alloc = allocate(chem_cell, refs, chemicals)
        Lt, Gt = grid.fields([tool_cell])
        chem_obj = objective([(i, v, u["chemicals"]) for i, v in c_alloc.items()],
                             plastic * u["plastic"] * Lt[0] + Gt[0] / INFRA_YEARS, away_from=tool_cell)
        new_chem = int(np.argmin(chem_obj))
        Lc, Gc = grid.fields([new_chem])
        tool_obj = objective(tool_legs + [(i, v, u["planks"]) for i, v in p_alloc.items()],
                             plastic * u["plastic"] * Lc[0] + Gc[0] / INFRA_YEARS, away_from=new_chem)
        new_tool = int(np.argmin(tool_obj))
        hist.append((new_chem, new_tool, float(chem_obj[new_chem]), float(tool_obj[new_tool])))
        if new_chem == chem_cell and new_tool == tool_cell:
            break
        chem_cell, tool_cell = new_chem, new_tool
    p_alloc = allocate(tool_cell, saws, planks)
    c_alloc = allocate(chem_cell, refs, chemicals)

    # Weiszfeld references (Euclidean, other facility fixed at its grid optimum)
    def wz(legs_xy: list[tuple[float, float, float]]) -> tuple[float, float]:
        P = np.array([(x, y) for x, y, _ in legs_xy])
        W = np.array([w for _, _, w in legs_xy])
        x = weiszfeld(P, W * DETOUR)
        return float(x[0]), float(x[1])

    tx, ty = grid.xy(tool_cell)
    cx, cy = grid.xy(chem_cell)
    nxy = lambda i: (planner.nodes[i].x, planner.nodes[i].y)
    w_tool = wz([(*nxy(i), t["need"].need * u["tools"]) for i, t in zip(tgt_nodes, targets)]
                + [(*nxy(i), v * u["planks"]) for i, v in p_alloc.items()] + [(cx, cy, plastic * u["plastic"])])
    w_chem = wz([(*nxy(i), v * u["chemicals"]) for i, v in c_alloc.items()] + [(tx, ty, plastic * u["plastic"])])

    def site_info(name, cell, wxy, obj):
        x, y = grid.xy(cell)
        near = min(g.towns, key=lambda t: math.dist((t.x, t.y), (x, y)))
        wcell = grid.cell(*wxy)
        obj_val = float(obj[cell])
        w_ratio = float(obj[wcell] / obj_val) if np.isfinite(obj[wcell]) and obj_val > 0 else None
        return {"type": name, "name": TYPE_RU.get(name, name), "x": round(x, 1), "y": round(y, 1), "cell": cell,
                "near_town": near.name, "near_town_km": round(math.dist((near.x, near.y), (x, y)) / 1000, 1),
                "weiszfeld_x": round(wxy[0], 1), "weiszfeld_y": round(wxy[1], 1),
                "weiszfeld_valid": bool(grid.valid[wcell]),
                "weiszfeld_gap_km": round(math.dist(wxy, (x, y)) / 1000, 2),
                "weiszfeld_objective_ratio": None if w_ratio is None else round(w_ratio, 3),
                "objective_per_year": round(obj_val)}

    plan.facilities["chemical_plant"] = site_info("chemical_plant", chem_cell, w_chem, chem_obj)
    plan.facilities["tool_factory"] = site_info("tool_factory", tool_cell, w_tool, tool_obj)
    plan.alternatives["tool_factory"] = _alternatives(grid, tool_obj, tool_cell)
    plan.alternatives["chemical_plant"] = _alternatives(grid, chem_obj, chem_cell)
    plan.flows = ([Flow("chemicals", v, ("node", i), ("site", "chemical_plant")) for i, v in c_alloc.items()]
                  + [Flow("plastic", plastic, ("site", "chemical_plant"), ("site", "tool_factory"))]
                  + [Flow("planks", v, ("node", i), ("site", "tool_factory")) for i, v in p_alloc.items()]
                  + [Flow("tools", t["need"].need, ("site", "tool_factory"), ("node", i))
                     for i, t in zip(tgt_nodes, targets)])
    plan.targets = targets
    over = [i for i, v in list(p_alloc.items()) + list(c_alloc.items()) if v > cap[i] + 1e-6]
    if over:
        plan.notes.append("часть входов превышает мощность ближайших источников")
    plan.notes.append(f"красители {dyes:.0f} ед./год: на карте их никто не потребляет (тупик)")
    _evaluate_chain(planner, sim, plan, grid, year)
    plan.feasible = True
    return plan


def _alternatives(grid: SiteGrid, obj: np.ndarray, best: int, n: int = 3, sep_m: float = 2500.0) -> list[dict]:
    order = np.argsort(obj)
    out = []
    bx, by = grid.xy(best)
    chosen = [(bx, by)]
    for c in order[:20000]:
        if not np.isfinite(obj[c]):
            break
        x, y = grid.xy(int(c))
        if all(math.dist((x, y), p) >= sep_m for p in chosen):
            chosen.append((x, y))
            out.append({"x": round(x, 1), "y": round(y, 1),
                        "objective_vs_best": round(float(obj[c] / obj[best]) if obj[best] > 0 else 1.0, 3)})
            if len(out) >= n:
                break
    return out


def _evaluate_chain(planner, sim, plan: ChainPlan, grid: SiteGrid, year: int):
    g = planner.game
    site_xy = {k: (v["x"], v["y"]) for k, v in plan.facilities.items()}
    site_cell = {k: v["cell"] for k, v in plan.facilities.items()}
    legs = []
    for f in plan.flows:
        if f.volume <= 0:
            continue

        def ep(end):
            if end[0] == "node":
                n = planner.nodes[end[1]]
                return _endpoint(n), grid.cell(n.x, n.y), n.water_only, n.name
            x, y = site_xy[end[1]]
            z = g.terrain.height(x, y)
            return Endpoint("industry", -1, TYPE_RU.get(end[1], end[1]), x, y, z), site_cell[end[1]], False, \
                TYPE_RU.get(end[1], end[1])

        a, ca, wa, na = ep(f.src)
        b, cb, wb, nb = ep(f.dst)
        cargo = g.cargo_by_key[f.cargo]
        if ca == cb:
            legs.append({"cargo": f.cargo, "from": na, "to": nb, "volume": f.volume, "mode": "local",
                         "route_m": 0, "income": 0.0, "running": 0.0, "maintenance": 0.0, "capex": 0.0,
                         "profit": 0.0, "note": "на одной площадке"})
            continue
        r = grid.route(ca, cb)
        route_m = r.length_m if r else math.dist((a.x, a.y), (b.x, b.y)) * DETOUR
        build = r.build_cost if r else None
        maint = r.maintenance_per_year if r else None
        mode = "water" if (wa or wb) else "road"
        if mode == "water":
            build = maint = 0.0
        ev = evaluate_link(g, planner.vehicles, cargo, a, b, mode, f.volume, year, route_m=route_m,
                           route_build_cost=build, route_maintenance=maint)
        if ev is None:
            continue
        legs.append({"cargo": f.cargo, "from": na, "to": nb, "volume": round(ev.volume, 1), "mode": mode,
                     "route_m": round(route_m), "straight_m": round(ev.straight_m), "income": ev.income,
                     "running": ev.running, "maintenance": ev.maintenance, "capex": ev.capex,
                     "vehicle_capex": ev.vehicle_capex, "profit": ev.profit,
                     "vehicles": f"{ev.n_consists}× {ev.consist.label}",
                     "geometry": _decimate(r.polyline) if r and mode == "road" else
                     [[round(a.x, 1), round(a.y, 1)], [round(b.x, 1), round(b.y, 1)]]})
    plan.legs = legs
    boost = sum(t["value"] for t in plan.targets)
    boost_capex = sum(i["extra"] * max(0.0, i["per_unit_capex"]) for t in plan.targets for i in t["info"].values())
    extra_units = sum(t["need"].extra_output for t in plan.targets)
    inc = sum(l["income"] for l in legs)
    cost = sum(l["running"] + l["maintenance"] for l in legs)
    capex = sum(l["capex"] for l in legs) + boost_capex
    profit = inc - cost + boost
    plan.value = {"legs_income": inc, "legs_costs": cost, "legs_profit": inc - cost, "boost_profit": boost,
                  "extra_units": extra_units, "capex": capex, "boost_vehicle_capex": boost_capex,
                  "profit": profit, "payback_years": capex / profit if profit > 0 else math.inf}


def _machine_chain(planner, plan: ChainPlan, recipes: dict) -> ChainPlan:
    g = planner.game
    steel = _producers(g, "steel")
    plan.feasible = False
    mx, need = recipe_rate(recipes["machine_factory"], "machines")
    plan.value = {"machines_per_factory": mx, "inputs_per_machine": need}
    if not steel:
        chain = supply_chain(g, "machines", recipes)
        plan.notes.append("станков не будет: " + chain_text(g, chain))
        plan.notes.append("нужны сталь (сталелитейный завод ← железная руда + уголь: разведка с ранга 6, "
                          "сам завод только спавном движка) и пластик (химзавод)")
    return plan


# --------------------------------------------------------------------------- prospecting

class _Voronoi:
    """Town Voronoi cells on a coarse grid with land / water eligibility for new industries."""

    def __init__(self, grid: SiteGrid, step: int = 4):
        rp = grid.rp
        g = grid.game
        iy, ix = np.mgrid[0:rp.ny:step, 0:rp.nx:step]
        cells = (iy * rp.nx + ix).ravel()
        self.cells = cells
        self.xy = np.c_[grid.xs[cells], grid.ys[cells]]
        self.water = rp.water.ravel()[cells]
        self.town_d, self.town_i = cKDTree([(t.x, t.y) for t in g.towns]).query(self.xy)
        self.ind_d, _ = cKDTree([(i.x, i.y) for i in g.industries]).query(self.xy)
        z = rp.z
        gy, gx = np.gradient(z, rp.cell)
        self.slope = ndimage.maximum_filter(np.hypot(gx, gy), size=3).ravel()[cells]
        self.area_cell = (rp.cell * step) ** 2

    def site(self, town_k: int, on_water: bool | None) -> tuple[tuple[float, float] | None, float]:
        """Expected site (eligible cell nearest to the centroid of the eligible area) and area km²."""
        m = (self.town_i == town_k) & (self.ind_d >= SITE_INDUSTRY_CLEARANCE)
        if on_water is True:
            m &= self.water
        elif on_water is False:
            m &= (~self.water) & (self.slope <= SITE_MAX_SLOPE) & (self.town_d >= PROSPECT_TOWN_CLEARANCE)
        else:
            land = (~self.water) & (self.slope <= SITE_MAX_SLOPE) & (self.town_d >= PROSPECT_TOWN_CLEARANCE)
            m &= self.water | land
        if not m.any():
            return None, 0.0
        P = self.xy[m]
        c = P.mean(axis=0)
        k = int(np.argmin(np.hypot(*(P - c).T)))
        return (float(P[k, 0]), float(P[k, 1])), float(m.sum() * self.area_cell / 1e6)


def _type_stats(game: Game, t: str) -> dict | None:
    """Per-level output and the smallest level of an industry type on the map."""
    inds = [i for i in game.industries if i.type == t]
    if not inds:
        return None
    per_level = {}
    for c in inds[0].outputs:
        vals = [max_production(game, i, c) / max(i.level, 1) for i in inds]
        per_level[c] = float(np.mean(vals))
    return {"per_level": per_level, "start_level": min(max(i.level, 1) for i in inds),
            "max_level": float(np.mean([i.max_level for i in inds])), "on_water": inds[0].on_water,
            "boost": booster_info(game, inds[0])}


def prospect_options(planner, year: int, rank: int, sim_state=None, used_permits=(), grid: SiteGrid | None = None,
                     top: int = 3) -> list[dict]:
    """For every exploration: availability at `rank`, and the best towns to prospect near, with
    the expected new output and the profit of its first line (estimated site in the town's
    Voronoi cell, evaluate_link to the best consumer with free demand)."""
    g = planner.game
    sim = _sim_or_initial(planner, sim_state)
    grid = grid or SiteGrid(g, year)
    vor = _Voronoi(grid)
    used = set(used_permits)
    nt = len(g.towns)
    out = []
    for ex in EXPLORATIONS:
        stats = [s for s in (_type_stats(g, t) for t in ex.types) if s]
        entry = {"cargo": ex.cargo, "cargo_name": g.cargo_by_key[ex.cargo].name if ex.cargo in g.cargo_by_key else ex.cargo,
                 "rank": ex.rank, "permit": ex.permit, "types": list(ex.types),
                 "types_ru": [TYPE_RU.get(t, t) for t in ex.types],
                 "unlocked": rank >= ex.rank, "permit_used": ex.permit in used, "options": [], "note": ""}
        if not stats:
            entry["note"] = "такого предприятия нет на карте — выпуск не оценить"
            out.append(entry)
            continue
        st = stats[0]
        on_water = None if len(stats) > 1 and stats[0]["on_water"] != stats[-1]["on_water"] else st["on_water"]
        outputs = {c: v * st["start_level"] for c, v in st["per_level"].items()}
        consumers_by_cargo = {c: _consumers(planner, sim, c) for c in outputs}
        if not any(consumers_by_cargo.values()):
            entry["note"] = "на карте нет потребителя этого груза — разведка ничего не даст (только задел)"
            out.append(entry)
            continue
        opts = []
        for k, town in enumerate(g.towns):
            xy, area = vor.site(k, on_water)
            if xy is None:
                continue
            z = g.terrain.height(*xy)
            src = Endpoint("industry", -1, f"{TYPE_RU.get(ex.types[0], ex.types[0])} у {town.name}", xy[0], xy[1], z)
            lines, profit, capex, income = [], 0.0, 0.0, 0.0
            for c, vol in outputs.items():
                best = None
                cons = sorted(consumers_by_cargo[c],
                              key=lambda j: math.dist(xy, (planner.nodes[j].x, planner.nodes[j].y)))[:4]
                for j in cons:
                    d = planner.nodes[j]
                    mode = "water" if (st["on_water"] or d.water_only) else "road"
                    ev = evaluate_link(g, planner.vehicles, g.cargo_by_key[c], src, _endpoint(d), mode, vol, year,
                                       route_m=math.dist(xy, (d.x, d.y)) * DETOUR)
                    if ev is None or ev.volume <= 0:
                        continue
                    sc = ev.profit - ev.capex / 10
                    if best is None or sc > best[0]:
                        best = (sc, ev, j, mode)
                if best:
                    _, ev, j, mode = best
                    lines.append({"cargo": c, "volume": ev.volume, "consumer": j, "consumer_name": planner.nodes[j].name,
                                  "mode": mode, "income": ev.income, "profit": ev.profit, "capex": ev.capex, "ev": ev})
                    profit += ev.profit
                    capex += ev.capex
                    income += ev.income
            if not lines:
                continue
            opts.append({"town": town.name, "town_node": k, "site": (round(xy[0], 1), round(xy[1], 1)),
                         "area_km2": round(area, 1), "outputs": outputs, "lines": lines, "income": income,
                         "profit": profit, "capex": capex,
                         "payback_years": capex / profit if profit > 0 else math.inf})
        opts.sort(key=lambda o: -(o["profit"] - o["capex"] / 10))
        entry["options"] = opts[:top]
        entry["start_level"] = st["start_level"]
        entry["max_level_mean"] = st["max_level"]
        entry["growth_note"] = (f"старт ~уровень {st['start_level']}, при вывозе ≥75 % растёт на 1 уровень в полгода "
                                f"(в среднем до {st['max_level']:.0f})")
        out.append(entry)
    _ = nt
    return out


# --------------------------------------------------------------------------- build options

def industry_build_options(game: Game, year: int | None = None, rank: int = 1, planner=None, sim_state=None,
                           sandbox: bool | None = None, used_permits=(), with_sites: bool = True) -> dict:
    """What the player can do to get new industries, from the game code, plus the optimal sites
    for a chemical plant + tool factory (weighted Weber problem) and the value of the full chain.

    planner: a tf3planner.planner.Planner (or anything with game/vehicles/nodes/mat[/town/ind]);
    created lightly when omitted."""
    year = game.year if year is None else year
    if planner is None:
        from .passengers import light_planner
        planner = light_planner(game)
    if sandbox is None:
        sandbox = bool((game.raw.get("baseConfig") or {}).get("sandboxMode"))
    base_pop = world_population(game)
    p_succ, e_month = prospection_odds()
    recipes = load_recipes()
    res = {
        "sandbox": sandbox,
        "industry_builder": sandbox,
        "industry_builder_rule": ("Строитель предприятий (меню «Предприятия») есть только в редакторе карт и в режиме "
                                  "песочницы: hasIndustryBuilder = isMapEditor or isSandboxMode (gui/main/game.tl). "
                                  "В карьере химзавод/инструментальный/машиностроительный завод поставить нельзя."),
        "permits": [{"key": k, **v, "unlocked": rank >= v["rank_and_permits"][0][0],
                     "count": sum(n for r, n in v["rank_and_permits"] if r <= rank)} for k, v in PERMITS.items()],
        "prospection_rules": {
            "how": "Перки → Разведка: выбрать ресурс и город; через ≤6 мес. движок ставит предприятие в зоне "
                   "(ячейке Вороного) этого города. Денег не стоит; при неудаче разрешение возвращается.",
            "success_probability": round(p_succ, 3), "expected_months": round(e_month, 1),
            "permits_one_time": True,
        },
        "rank_population": {r: rank_population(base_pop, r) for r in range(1, MAX_RANK + 1)},
        "world_population": base_pop,
        "current_rank_by_population": rank_for_population(base_pop, base_pop),
        "engine_spawn_by_rank": {r: [TYPE_RU.get(t, t) for t in ts] for r, ts in ENGINE_RANK_INDUSTRIES.items()},
        "factory_recipes": {t: {"output_per_year": round(recipe_rate(recipes[t], c)[0], 1),
                                "inputs_per_unit": recipe_rate(recipes[t], c)[1]}
                            for t, c in (("chemical_plant", "plastic"), ("tool_factory", "tools"),
                                         ("machine_factory", "machines"))},
        "booster_sources": booster_sources(game, recipes),
    }
    if with_sites:
        grid = SiteGrid(game, year)
        res["prospects"] = prospect_options(planner, year, rank, sim_state, used_permits, grid)
        res["tools_chain"] = chain_sites(planner, year, sim_state, grid, "tools", recipes)
        res["machines_chain"] = chain_sites(planner, year, sim_state, grid, "machines", recipes)
    return res


# --------------------------------------------------------------------------- candidates

def _booster_candidates(planner, year: int, sim, built_keys: set, built_stations: set) -> list[ProjectCandidate]:
    g = planner.game
    out = []
    needs = [n for n in booster_needs(g, planner.nodes) if n.node is not None]
    supply = getattr(sim, "supply", {}) or {}
    demand = getattr(sim, "demand", {}) or {}
    boosted = getattr(sim, "boosted", set()) or set()
    for nd in needs:
        d = nd.node
        left = demand.get((d, nd.cargo), nd.need) if demand else nd.need
        if left < 1 or (d, nd.cargo) in boosted:
            continue
        srcs = [(k[0], v) for k, v in supply.items() if k[1] == nd.cargo and v >= 1]
        dn = planner.nodes[d]
        srcs.sort(key=lambda s: math.dist((planner.nodes[s[0]].x, planner.nodes[s[0]].y), (dn.x, dn.y)))
        for p, avail in srcs[:3]:
            vol = min(left, avail)
            best = None
            for mode in ("road", "rail", "water"):
                ev = _link(planner, p, d, nd.cargo, vol, year, mode, built_keys)
                if ev is None or ev.volume <= 0:
                    continue
                free = sum(ev.station_each for n in (p, d) if (n, mode) in built_stations)
                free_m = sum(ev.station_maint_each for n in (p, d) if (n, mode) in built_stations)
                if (p, mode) in built_stations or (d, mode) in built_stations:
                    free += ev.depot_cost
                ev.infra_capex = max(0.0, ev.infra_capex - free)
                ev.maintenance = max(0.0, ev.maintenance - free_m)
                if best is None or ev.profit - ev.capex / 10 > best[1].profit - best[1].capex / 10:
                    best = (mode, ev)
            if best is None:
                continue
            mode, ev = best
            frac = min(1.0, ev.volume / nd.need) if nd.need else 0.0
            mult = 1 + nd.boost_factor * frac
            gain, extra_vcap = boost_profit(planner, sim, d, mult, nd.base_output)
            extra_units = sum(nd.base_output.values()) * nd.boost_factor * frac
            pn = planner.nodes[p]
            geom = None
            if mode in planner.mat:
                try:
                    r = planner.mat[mode].route(p, d)
                    geom = _decimate(r.polyline) if r else None
                except Exception:
                    geom = None
            if geom is None:
                geom = [[round(pn.x, 1), round(pn.y, 1)], [round(dn.x, 1), round(dn.y, 1)]]
            cname = g.cargo_by_key[nd.cargo].name
            mode_ru = {"road": "дорога", "rail": "ж/д", "water": "судно"}[mode]
            details = (f"Бустер {cname.lower()} {ev.volume:.0f} из {nd.need:.0f} ед./год → +{nd.boost_factor * frac:.0%} "
                       f"выпуска ({TYPE_RU.get(nd.type, nd.type)}: +{extra_units:.0f} ед./год). {mode_ru}, "
                       f"{ev.straight_m / 1000:.1f} км по прямой, маршрут {ev.route_m / 1000:.1f} км, "
                       f"{ev.n_consists}× {ev.consist.label}. Доход за перевозку {_fmt(ev.income)}/год")
            if gain > 0:
                details += (f", прибыль от буста на существующих линиях +{_fmt(gain)}/год "
                            f"(доп. техника ~{_fmt(extra_vcap)})")
            else:
                details += "; продукцию сверх нынешней ещё нечем вывозить — буст окупится после линии вывоза"
            details += "."
            out.append(ProjectCandidate(
                kind="booster", mode=mode, title=f"Бустер: {cname.lower()} {pn.name} → {dn.name}", details=details,
                nodes=[p, d], cargo=nd.cargo, volume=ev.volume, income=ev.income + gain, running=ev.running,
                maintenance=ev.maintenance, vehicle_capex=ev.vehicle_capex + extra_vcap, infra_capex=ev.infra_capex,
                vehicles=_vehicle_lines(ev), vehicle_life_years=_life(ev), geometry=geom,
                pair_keys=[(min(p, d), max(p, d), mode)] if ev.route_build > 0 else [],
                station_keys=[(p, mode), (d, mode)], uses_supply={(p, nd.cargo): ev.volume},
                uses_demand={(d, nd.cargo): ev.volume}, boosts={(d, nd.cargo): mult},
                meta={"line": "booster", "boost_factor": nd.boost_factor, "fraction": round(frac, 3),
                      "extra_units": round(extra_units, 1), "boost_value": gain, "transport_income": ev.income,
                      "boost_vehicle_capex": extra_vcap, "trip_s": ev.trip_time_s,
                      "straight_m": round(ev.straight_m), "route_m": round(ev.route_m)}))
    return out


def _worker_candidates(planner, year: int, sim, built_keys: set, built_stations: set) -> list[ProjectCandidate]:
    from . import passengers as pax
    p = pax.PassengerParams()

    def value(node: int, factor: float) -> float:
        ind = _node_ind(planner, node)
        if ind is None:
            return 0.0
        return boost_profit(planner, sim, node, factor, {c: 1 for c in ind.outputs})[0]

    nodes, M = pax.demand_matrix(planner.game, p, year, nodes=planner.nodes)
    ctx = pax._Ctx(planner.game, planner, year, p, pax._catalogue(planner.game), pax.town_infos(planner.game, p), M,
                   planner.game.year_ms / 1000.0, set(built_keys), set(built_stations), value)
    out = []
    boosted = getattr(sim, "boosted", set()) or set()
    for c in pax.worker_candidates(ctx):
        k = c.nodes[-1]
        if (k, "workers") in boosted:
            continue
        ind = _node_ind(planner, k)
        if ind is not None:
            extra = sum(max_production(planner.game, ind, o) for o in ind.outputs) * (p.worker_boost_factor - 1)
            c.meta["extra_units"] = round(extra, 1)
        c.meta["source"] = "efficiency"
        out.append(c)
    return out


def _prospect_candidates(planner, year: int, sim, rank: int, built_stations: set, used_permits,
                         grid: SiteGrid | None) -> list[ProjectCandidate]:
    used = set(used_permits or ())
    used |= {m.split(":", 1)[1] for (_n, m) in built_stations if isinstance(m, str) and m.startswith("prospect:")}
    exs = [e for e in EXPLORATIONS if rank >= e.rank and e.permit not in used]
    if not exs:
        return []
    p_succ, e_month = prospection_odds()
    out = []
    # one candidate per permit: the best town of the best exploration using it (the planner does
    # not know permits, so offering two would let it spend one permit twice)
    best_per_permit: dict[str, tuple] = {}
    for entry in _cached_prospects(planner, year, rank, sim, used, grid):
        ex = next((e for e in exs if e.cargo == entry["cargo"]), None)
        if ex is None or not entry["options"]:
            continue
        o = entry["options"][0]
        sc = o["profit"] - o["capex"] / 10
        if ex.permit not in best_per_permit or sc > best_per_permit[ex.permit][0]:
            best_per_permit[ex.permit] = (sc, ex, entry, o)
    for _sc, ex, entry, o in best_per_permit.values():
        main = next((l for l in o["lines"] if l["cargo"] == ex.cargo), o["lines"][0])
        ev = main["ev"]
        t_idx = o["town_node"]
        j = main["consumer"]
        adds = {(t_idx, c): v - (main["volume"] if c == main["cargo"] else 0.0) for c, v in o["outputs"].items()}
        adds = {k: v for k, v in adds.items() if v >= 1}
        cname = entry["cargo_name"]
        details = (f"Разведка ресурса «{cname}» у города {o['town']} (ранг {ex.rank}, разрешение одноразовое; "
                   f"успех ~{p_succ:.0%}, ~{e_month:.1f} мес.). Ожидается {', '.join(TYPE_RU.get(t, t) for t in ex.types)} "
                   f"~{sum(o['outputs'].values()):.0f} ед./год ({entry.get('growth_note', '')}). "
                   f"Первая линия: {main['cargo']} → {main['consumer_name']} ({main['mode']}), "
                   f"{main['volume']:.0f} ед./год, доход {_fmt(main['income'])}/год. "
                   f"Место — оценка (центр свободной зоны города), уточнить после появления предприятия.")
        out.append(ProjectCandidate(
            kind="industry_build", mode=main["mode"], title=f"Разведка: {cname.lower()} у {o['town']}",
            details=details, nodes=[t_idx, j], cargo=main["cargo"], volume=ev.volume,
            income=ev.income * p_succ, running=ev.running, maintenance=ev.maintenance,
            vehicle_capex=ev.vehicle_capex, infra_capex=ev.infra_capex, vehicles=_vehicle_lines(ev),
            vehicle_life_years=_life(ev),
            geometry=[[o["site"][0], o["site"][1]], [round(planner.nodes[j].x, 1), round(planner.nodes[j].y, 1)]],
            station_keys=[(t_idx, f"prospect:{ex.permit}")], uses_demand={(j, main["cargo"]): ev.volume},
            adds_supply=adds,
            meta={"action": "prospect", "permit": ex.permit, "rank": ex.rank, "approximate_site": True,
                  "site": o["site"], "types": list(ex.types), "success_probability": round(p_succ, 3),
                  "expected_months": round(e_month, 1), "outputs": o["outputs"], "area_km2": o["area_km2"]}))
    return out


_PROSPECT_CACHE: dict = {}


def _cached_prospects(planner, year: int, rank: int, sim, used: set, grid) -> list[dict]:
    """prospect_options is a few seconds; the planner calls generators often, so cache per
    (planner, year, rank, used permits, free demand of the consumer side rounded)."""
    dem = getattr(sim, "demand", {}) or {}
    sig = hash(tuple(sorted((k, round(v, -1)) for k, v in dem.items() if v >= 1)))
    key = (id(planner), year, rank, frozenset(used), sig)
    if key not in _PROSPECT_CACHE:
        if len(_PROSPECT_CACHE) > 32:
            _PROSPECT_CACHE.pop(next(iter(_PROSPECT_CACHE)))
        _PROSPECT_CACHE[key] = prospect_options(planner, year, rank, sim, used, grid)
    return _PROSPECT_CACHE[key]


def _chain_candidate(planner, year: int, sim, grid: SiteGrid | None) -> list[ProjectCandidate]:
    plan = chain_sites(planner, year, sim, grid, "tools")
    if not plan.feasible:
        return []
    v = plan.value
    ch, tf = plan.facilities["chemical_plant"], plan.facilities["tool_factory"]
    geom = []
    for l in plan.legs:
        geom += l.get("geometry") or []
    boosts = {(t["need"].node, "tools"): 1 + t["need"].boost_factor for t in plan.targets}
    uses = {}
    for f in plan.flows:
        if f.src[0] == "node":
            uses[(f.src[1], f.cargo)] = uses.get((f.src[1], f.cargo), 0) + f.volume
    details = (f"Песочница: {ch['name']} у ({ch['x']:.0f}, {ch['y']:.0f}) ~{ch['near_town_km']} км от {ch['near_town']}, "
               f"{tf['name']} у ({tf['x']:.0f}, {tf['y']:.0f}). Инструменты → {len(plan.targets)} предприятий, "
               f"+{v['extra_units']:.0f} ед./год их продукции. Плечи цепочки: доход {_fmt(v['legs_income'])}/год, "
               f"прибыль {_fmt(v['legs_profit'])}; буст +{_fmt(v['boost_profit'])}/год.")
    return [ProjectCandidate(
        kind="industry_build", mode="multi", title="Цепочка: химзавод + завод инструментов", details=details,
        nodes=[t["need"].node for t in plan.targets], cargo="tools", volume=sum(t["need"].need for t in plan.targets),
        income=v["legs_income"] + v["boost_profit"], running=sum(l["running"] for l in plan.legs),
        maintenance=sum(l["maintenance"] for l in plan.legs), vehicle_capex=sum(l.get("vehicle_capex", 0) for l in plan.legs)
        + v["boost_vehicle_capex"], infra_capex=v["capex"] - sum(l.get("vehicle_capex", 0) for l in plan.legs)
        - v["boost_vehicle_capex"], geometry=geom or None,
        legs=[{k: val for k, val in l.items() if k != "geometry"} for l in plan.legs],
        uses_supply=uses, boosts=boosts,
        meta={"action": "build_chain", "facilities": plan.facilities, "boost_value": v["boost_profit"],
              "boost_vehicle_capex": v["boost_vehicle_capex"],
              "sandbox_only": True, "alternatives": plan.alternatives})]


def efficiency_candidates(planner, year: int | None = None, sim_state=None, built_keys=None, rank: int = 1, *,
                          built_stations=None, sandbox: bool | None = None, include_workers: bool = True,
                          include_prospects: bool = True, used_permits=(), grid: SiteGrid | None = None
                          ) -> list[ProjectCandidate]:
    """Efficiency projects for `year`:

    * "booster" links: booster cargo (fertilizer ...) from producers with free supply in the
      planner state to the industries that need it (income = transport + boost value);
    * "workers": bus lines from nearby towns (passengers.worker_candidates, value = boosted extra
      output x per-unit profit of the industry's outbound lines). Pass include_workers=False when
      the passenger generator is registered too (it already emits them);
    * "industry_build": prospecting (career, from the exploration's rank, one-time permits) bundled
      with the first outbound line; factory chains (chemical plant + tool factory at the Weber
      sites) only in sandbox mode.

    sim_state: planner.Sim (supply/demand/projects/generic/boosted/stations); None -> initial state.
    built_keys: infrastructure keys (min, max, mode) already built (route part free).
    """
    year = planner.game.year if year is None else year
    sim = _sim_or_initial(planner, sim_state)
    built_keys = set(built_keys or getattr(sim, "built_pairs", set()) or set())
    built_stations = set(built_stations or getattr(sim, "stations", set()) or set())
    if sandbox is None:
        sandbox = bool((planner.game.raw.get("baseConfig") or {}).get("sandboxMode"))
    out = _booster_candidates(planner, year, sim, built_keys, built_stations)
    if include_workers:
        try:
            out += _worker_candidates(planner, year, sim, built_keys, built_stations)
        except Exception as e:          # passengers.py is owned by another module; never break the plan
            print(f"efficiency: worker lines skipped ({e})")
    if include_prospects or sandbox:
        grid = grid or SiteGrid(planner.game, year)
    if include_prospects:
        out += _prospect_candidates(planner, year, sim, rank, built_stations, used_permits, grid)
    if sandbox and rank >= 1:
        out += _chain_candidate(planner, year, sim, grid)
    return [c for c in out if c.volume > 0]


def make_generator(rank=1, include_workers: bool = False, include_prospects: bool = True):
    """Planner generator: rank is an int or a callable year -> rank (the planner does not simulate
    world population). Register with planner.generators.append(make_generator(...))."""
    cache = {}

    def gen_efficiency(planner, sim, year):
        r = rank(year) if callable(rank) else rank
        if "grid" not in cache:
            cache["grid"] = SiteGrid(planner.game, planner.game.year)
        return efficiency_candidates(planner, year, sim, set(sim.built_pairs), r, built_stations=set(sim.stations),
                                     include_workers=include_workers, include_prospects=include_prospects,
                                     grid=cache["grid"])
    return gen_efficiency


# --------------------------------------------------------------------------- roadmap

def fertilizer_program(planner, year: int, sim_state=None) -> dict:
    """Which farms get the limited fertilizer first: extra output x outbound per-unit profit per
    fertilizer unit; fertilizer available = 0.25 x crude oil the platforms produce (capped by the
    refineries)."""
    g = planner.game
    sim = _sim_or_initial(planner, sim_state)
    src = booster_sources(g)["fertilizer"] if "fertilizer" in booster_sources(g) else None
    if not src:
        return {}
    rows = []
    for n in booster_needs(g, planner.nodes):
        if n.cargo != "fertilizer":
            continue
        val, info = _target_value(planner, sim, n, year)
        # nearest refinery and the fertilizer leg
        refs = [x for x in planner.nodes if x.kind == "industry" and "fertilizer" in (_node_ind(planner, x.index).outputs)]
        dn = planner.nodes[n.node]
        leg = None
        for r in sorted(refs, key=lambda r: math.dist((r.x, r.y), (dn.x, dn.y)))[:2]:
            for mode in ("road", "rail"):
                ev = _link(planner, r.index, n.node, "fertilizer", n.need, year, mode)
                if ev and (leg is None or ev.profit - ev.capex / 10 > leg[1].profit - leg[1].capex / 10):
                    leg = (r, ev, mode)
        rows.append({"farm": n.name, "node": n.node, "boost_factor": n.boost_factor, "need": n.need,
                     "extra_units": n.extra_output, "boost_profit": val, "info": info,
                     "per_fertilizer": val / n.need if n.need else 0.0,
                     "leg": None if leg is None else {"from": leg[0].name, "mode": leg[2], "income": leg[1].income,
                                                      "profit": leg[1].profit, "capex": leg[1].capex,
                                                      "route_m": leg[1].route_m}})
    rows.sort(key=lambda r: -(r["boost_profit"] + (r["leg"]["profit"] if r["leg"] else 0)))
    avail = src["available_per_year"]
    left, chosen = avail, []
    for r in rows:
        if left < 1:
            break
        take = min(left, r["need"])
        chosen.append({**r, "supplied": take, "fraction": take / r["need"]})
        left -= take
    return {"available": avail, "max_refineries": src["max_per_year"], "total_need": src["total_need"],
            "rows": rows, "chosen": chosen}


def worker_program(planner, year: int, sim_state=None, range_m: float = 4000.0) -> list[dict]:
    """Industries with a town within `range_m`: extra output and value of the worker boost."""
    g = planner.game
    sim = _sim_or_initial(planner, sim_state)
    out = []
    for n in planner.nodes:
        if n.kind != "industry" or n.water_only:
            continue
        ind = _node_ind(planner, n.index)
        if not ind.outputs or ind.inputs:
            continue        # processing industries produce only what is delivered
        town = min(g.towns, key=lambda t: math.dist((t.x, t.y), (n.x, n.y)))
        d = math.dist((town.x, town.y), (n.x, n.y))
        if d > range_m:
            continue
        val, extra = 0.0, 0.0
        for c in ind.outputs:
            base = max_production(g, ind, c)
            m = outbound_margin(planner, sim, n.index, c, base * WORKER_MULT, year)
            extra += base * (WORKER_MULT - 1)
            val += base * (WORKER_MULT - 1) * max(0.0, m["profit"])
        out.append({"industry": n.name, "node": n.index, "type": ind.type, "town": town.name,
                    "distance_km": round(d / 1000, 1), "extra_units": extra, "boost_profit": val})
    out.sort(key=lambda r: -r["boost_profit"])
    return out


def roadmap(planner=None, year: int | None = None, sim_state=None) -> dict:
    """Staged efficiency program with numbers (used for docs/efficiency.md)."""
    if planner is None:
        from .planner import Planner
        planner = Planner()
    g = planner.game
    year = g.year if year is None else year
    opts = industry_build_options(g, year, rank=MAX_RANK, planner=planner, sim_state=sim_state)
    return {"options": opts, "fertilizer": fertilizer_program(planner, year, sim_state),
            "workers": worker_program(planner, year, sim_state)}


def main():
    import time
    t0 = time.time()
    from .planner import Planner
    pl = Planner()
    rm = roadmap(pl)
    o = rm["options"]
    print(f"[{time.time() - t0:.0f} s] население мира {o['world_population']}, песочница {o['sandbox']}")
    print("Пороги рангов (население):", {r: p for r, p in o["rank_population"].items() if r <= 10})
    for c, s in o["booster_sources"].items():
        print(f"\n== {s['name']} ({c}): потребителей {s['consumers']}, нужно {s['total_need']:.0f}/год, "
              f"источники max {s['max_per_year']:.0f}, доступно {s['available_per_year']:.0f} "
              f"(покрытие {s['coverage']:.0%}); +{s['total_extra_output']:.0f} ед. продукции при полном бусте")
        print("   цепочка:", s["chain_text"])
    f = rm["fertilizer"]
    print(f"\nУдобрения: доступно {f['available']:.0f}/год из нужных {f['total_need']:.0f}")
    for r in f["rows"][:8]:
        leg = r["leg"] or {}
        print(f"  {r['farm'][:45]:45} bf {r['boost_factor']:.2f} +{r['extra_units']:.0f} ед. буст {r['boost_profit'] / 1e6:5.2f} M"
              f"  плечо {leg.get('from', '-')[:25]:25} {leg.get('mode', '')} доход {leg.get('income', 0) / 1e6:5.2f} M"
              f" прибыль {leg.get('profit', 0) / 1e6:5.2f} M capex {leg.get('capex', 0) / 1e6:5.2f} M")
    print("\nРабочие (≤4 км от города):")
    for r in rm["workers"][:10]:
        print(f"  {r['industry'][:45]:45} {r['town'][:15]:15} {r['distance_km']:4.1f} км +{r['extra_units']:.0f} ед. "
              f"{r['boost_profit'] / 1e6:5.2f} M/год")
    print("\nРазведка:")
    for e in o["prospects"]:
        best = e["options"][0] if e["options"] else None
        s = (f"{best['town']} ~{sum(best['outputs'].values()):.0f} ед./год, прибыль линии {best['profit'] / 1e6:.2f} M, "
             f"capex {best['capex'] / 1e6:.2f} M") if best else e["note"]
        print(f"  ранг {e['rank']:2} {e['cargo_name']:12} {e['permit']:22} {s}")
    tc = o["tools_chain"]
    print("\nЦепочка инструментов:", tc.feasible, tc.notes)
    for k, v in tc.facilities.items():
        print(f"  {v['name']}: ({v['x']:.0f}, {v['y']:.0f}) у {v['near_town']} {v['near_town_km']} км; "
              f"Вейсфельд ({v['weiszfeld_x']:.0f}, {v['weiszfeld_y']:.0f}) разрыв {v['weiszfeld_gap_km']} км")
    for t in tc.targets:
        print(f"  цель {t['need'].name[:45]:45} +{t['need'].extra_output:.0f} ед. буст {t['value'] / 1e6:.2f} M")
    for l in tc.legs:
        print(f"  плечо {l['cargo']:10} {l['from'][:28]:28} → {l['to'][:28]:28} {l['volume']:6.0f} {l['mode']:5} "
              f"{l['route_m'] / 1000:5.1f} км доход {l['income'] / 1e6:5.2f} M прибыль {l['profit'] / 1e6:5.2f} M "
              f"capex {l['capex'] / 1e6:5.2f} M")
    v = tc.value
    if v:
        print(f"  ИТОГО прибыль {v['profit'] / 1e6:.2f} M/год (плечи {v['legs_profit'] / 1e6:.2f} + буст "
              f"{v['boost_profit'] / 1e6:.2f}), capex {v['capex'] / 1e6:.2f} M, окупаемость {v['payback_years']:.1f} г.")
    print("\nСтанки:", o["machines_chain"].notes)
    print(f"\n[{time.time() - t0:.0f} s]")


if __name__ == "__main__":
    main()
