"""Year-by-year build plan: month-by-month cash simulation with greedy, chain-aware project
selection, loans and vehicle renewal. Writes data/plan.json (see web/PLAN_SCHEMA.md) and
data/routes.json.

Model (v1, to be refined with in-game calibration):
- Candidates: producer -> consumer links per cargo and mode (rail, road from the routing
  matrices; water from the water network). Water-only industries (oil platforms, fishing
  grounds) can only ship by water.
- Volume: the producer's max production (economics.max_production, verified vs the game);
  processing industries produce only what is delivered to them (recipe conversion, capped by
  their max production). Consumers are capped: industries by max consumption, towns by
  TOWN_DEMAND_PER_CAPACITY * land-use capacity (an ASSUMPTION — town demand is not exported yet).
- Selection: each month, while money (cash + loans) allows, build the candidate with the best
  NPV per unit of capital (horizon NPV_YEARS, discount DISCOUNT) whose payback < MAX_PAYBACK.
  Infrastructure already built for the same node pair & mode, and stations already built at a
  node for a mode, are free for later candidates.
- Loans: current offers from the export, cheapest effective rate first; a taken slot gets an
  average new offer after LOAN_COOLDOWN_MONTHS (game: 4-8 months, random type). Interest is
  flat and repaying early costs only the remaining principal (verified in loan.script.tl), so
  loans are repaid when spare cash exceeds a buffer and nothing worth building is left.
- Vehicles: every January the best consist for each running line is re-evaluated; it is
  replaced when the yearly profit gain pays the swap back within REPLACE_PAYBACK years or the
  fleet exceeds its lifespan. Old vehicles are sold at linear depreciated value.
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .economics import (Endpoint, LinkEval, booster_info, conversion,
                        evaluate_link, max_production)
from .model import DATA, Game, Industry, Town
from .routing import RoutingParams, load_game, network_matrix, network_nodes, water_network
from .vehicles import Vehicle, load_vehicles
from .candidates import ProjectCandidate

NPV_YEARS = 15
DISCOUNT = 0.05
MAX_PAYBACK = 12.0
TOWN_DEMAND_PER_CAPACITY = 0.3        # units/year per land-use capacity point (mechanics.md: 0.3 x cap x tier)
LOAN_COOLDOWN_MONTHS = 6
MAX_LOANS = 4                         # loan_util.maximalObtainableLoans
CASH_BUFFER_MONTHS = 3                # keep this many months of fixed costs in cash
REPLACE_PAYBACK = 4.0
MIN_AGE_VOLUNTARY = 10.0              # years before a voluntary (non end-of-life) replacement
RESALE_FACTOR = 0.8                   # sale value vs linear depreciated value (ASSUMPTION)
PORT_RADIUS = 1500.0
HARBOR_CATCHMENT = 250.0              # land node must be this close to its port to load/unload directly
CAPACITY_CHECK = True                 # capacity.apply_capacity on every candidate
UPGRADES = True                       # era upgrades (upgrades.py) instead of the old _renew
UPGRADE_INFRA = True                  # track / catenary / road / loops upgrades
TOWN_GROWTH = True                    # towns.TownModel: XP, growth, new needs, rank
MILP_GUIDED = True                    # restrict phase-1 builds to the MILP target network (flow.design)
COVERAGE_PHASE = True                 # after max-profit projects: connect the rest if not loss-making
INFRA_LIFE_YEARS = 30                 # depreciation horizon for the "not loss-making" test
DEAD_END_OK = False                   # skip cargos nobody on the map consumes
AFFORD_TRIES = 25                     # candidates tried (by score) when the best one is not affordable
_MISSING = object()
RANK_INCOME = True                    # scale cash income by the company-rank ticket multiplier (x0.5 at rank 15)


# --------------------------------------------------------------------------- data

@dataclass
class Loan:
    kind: str
    amount: float
    months: int
    rate: float
    taken_month: int = -1
    paid_months: int = 0

    @property
    def monthly_principal(self) -> float:
        return self.amount / self.months

    @property
    def monthly_interest(self) -> float:
        return self.amount * self.rate / self.months

    @property
    def remaining_principal(self) -> float:
        return self.amount - self.paid_months * self.monthly_principal

    @property
    def eff_rate(self) -> float:
        return self.rate * 12 / self.months


@dataclass
class Candidate:
    p: int                     # producer node index
    d: int                     # consumer node index
    cargo: str
    mode: str
    ev: LinkEval
    geometry: list[tuple[float, float]] | None
    infra_route: float         # route build cost part of ev.infra_capex
    station_cost: float

    def pair_key(self) -> tuple[int, int, str]:
        return (min(self.p, self.d), max(self.p, self.d), self.mode)


@dataclass
class Project:
    id: str
    cand: Candidate
    start_month: int
    volume: float
    income: float
    running: float
    maintenance: float
    consist_ids: list[tuple[int, int]]
    n_consists: int
    vehicle_value: float
    bought_month: int
    lifespan_years: float
    income_month: int = -1     # first month with income (first loaded arrival); -1 = start_month


@dataclass
class Sim:
    game: Game
    vehicles: list[Vehicle]
    nodes: list
    month: int = 0                      # months since start
    cash: float = 0.0
    loans: list[Loan] = field(default_factory=list)
    offers: list[Loan] = field(default_factory=list)
    offer_return: list[int] = field(default_factory=list)     # months when a new offer appears
    projects: list[Project] = field(default_factory=list)
    generic: list = field(default_factory=list)          # built ProjectCandidates (passengers, chains, ...)
    supply: dict[tuple[int, str], float] = field(default_factory=dict)       # (node, cargo) -> units/y free
    demand: dict[tuple[int, str], float] = field(default_factory=dict)       # (node, cargo) -> units/y free
    delivered: dict[tuple[int, str], float] = field(default_factory=dict)    # processing inputs received
    built_pairs: set = field(default_factory=set)
    stations: set = field(default_factory=set)
    steps: list[dict] = field(default_factory=list)
    cashflow: list[dict] = field(default_factory=list)
    year_income: float = 0.0
    year_costs: float = 0.0

    line_counter: int = 0

    def new_line_id(self) -> str:
        """One counter for every kind of line (cargo, passenger, multimodal...): ids never collide."""
        self.line_counter += 1
        return f"L{self.line_counter}"

    @property
    def year(self) -> int:
        return self.game.year + self.month // 12

    def add_step(self, **kw):
        kw.setdefault("kind", None)
        kw.setdefault("cargo", None)
        kw.setdefault("cost", 0)
        kw.setdefault("income_per_year", 0)
        kw.setdefault("running_cost_per_year", 0)
        kw.setdefault("roi_years", None)
        same = [s for s in self.steps if s["year"] == self.year and s.get("month") == self.month % 12 + 1]
        self.steps.append({"id": f"s{len(self.steps) + 1}", "year": self.year, "month": self.month % 12 + 1,
                           "order": len(same) + 1, "money_after": round(self.cash), **kw})


# --------------------------------------------------------------------------- helpers

CLASS_RU = {"bulk": "сыпучих грузов", "liquid": "жидких грузов", "flatbed": "негабаритных грузов",
            "goods": "товаров"}


def _platform_text(ev, game) -> str:
    """Which terminal modules to build: specialized (x2 loading, for one cargo class) or universal."""
    from .facilities import _special
    cls = _special(game.cargo_by_key[ev.cargo].classes) if ev.cargo in game.cargo_by_key else None
    kind = getattr(ev, "platform", None)
    if kind == "specialized" and cls:
        return (f"Платформы: специализированные для {CLASS_RU.get(cls, cls)} "
                f"(погрузка ×2; чужой класс груза грузится ×0,25). ")
    if cls:
        return f"Платформы: универсальные (специализированная для {CLASS_RU.get(cls, cls)} здесь не окупается). "
    return "Платформы: универсальные. "


REPUTATION_WARN = 0.5      # a town losing more than this share of reputation may block demolition / stations


def _demolition_summary(cost=0.0, m=0.0, buildings=0.0, towns=None) -> tuple[dict | None, list[str]]:
    """step['demolition'] + Russian warnings. towns: {town name: reputation penalty (1.0 = 100 %)}."""
    towns = towns or {}
    if not cost and not buildings and not towns:
        return None, []
    demo = {"cost": round(float(cost or 0)), "m": round(float(m or 0)), "buildings": round(float(buildings or 0)),
            "towns": {k: round(float(v), 3) for k, v in towns.items()}}
    warns = [f"Снос в г. {t}: репутация −{v:.0%}. При «Плохой» репутации игра запрещает снос, "
             f"при «Очень плохой» — станции в городе; восстановление ~1/6 за 3 месяца."
             for t, v in towns.items() if v > REPUTATION_WARN]
    return demo, warns


def _endpoint_json(node) -> dict:
    return {"type": node.kind, "id": node.id, "name": node.name, "x": round(node.x, 1), "y": round(node.y, 1)}


def _npv(profit: float, capex: float, years: int = NPV_YEARS, r: float = DISCOUNT) -> float:
    annuity = (1 - (1 + r) ** -years) / r
    return profit * annuity - capex


def _loan_mult(year: int) -> float:
    # loan_util.tl adjustMultiplier: 2^(map(year, 1900..2020 -> log2 0.4 .. log2 2.4))
    t = min(max((year - 1900) / 120, 0), 1)
    return 2 ** (math.log2(0.4) + t * (math.log2(2.4) - math.log2(0.4)))


def _average_offer(year: int) -> Loan:
    # mean over the four generator functions (type label is random in createAvailableLoans)
    amount = np.mean([7.5, 20, 40, 50]) * _loan_mult(year) * 1e6
    years = np.mean([3, 5.5, 8.5, 17])
    rate = np.mean([0.04, 0.065, 0.09, 0.11])
    return Loan("Avg", round(amount, -6), int(years * 12), rate)


def _endpoint(node) -> Endpoint:
    return Endpoint(node.kind, node.id, node.name, node.x, node.y, node.z)


PROSPECT_DELAY_MONTHS = 6             # prospectors search up to 6 months before the industry spawns


def _delay_months(seconds: float, year_s: float) -> int:
    """Whole months of game time in `seconds` of simulation (1 year = year_s)."""
    if not seconds or not math.isfinite(seconds) or seconds <= 0:
        return 0
    return int(round(seconds / year_s * 12))


def _first_income_s(pc: ProjectCandidate) -> float:
    """Simulation seconds until a generic project's first paid unloading: vehicles start at the
    depot next to the loading stop, so the first income arrives after ~half a round trip
    (loading + loaded run). A 20 km horse-cart loop takes > 1 game year."""
    m = pc.meta or {}
    for k in ("trip_s", "cycle_s"):
        if m.get(k):
            return 0.5 * float(m[k])
    legs = [l.get("one_way_s") or 0.5 * (l.get("trip_time_s") or 0) for l in (pc.legs or []) if isinstance(l, dict)]
    return max(legs, default=0.0)


def ticket_multiplier(game: Game, rank: int) -> float:
    """Company rank income multiplier (company_growth.script.tl): exported table, else
    1 + (rank - 1) * (0.5 - 1) / 14 (VeryHard). Applies to ALL cargo and passenger income and is
    driven by the potential rank (world population), applied or not (mechanics.md 12.2)."""
    comp = game.raw.get("company") or {}
    table = comp.get("ticketMultiplierForLevel")
    if isinstance(table, dict):
        table = [table[k] for k in sorted(table, key=lambda k: int(k))]
    r = max(1, int(rank))
    if isinstance(table, list) and table:
        return float(table[min(r, len(table)) - 1])
    return 1 + (min(r, 15) - 1) * (0.5 - 1) / 14


# --------------------------------------------------------------------------- planner

class Planner:
    def __init__(self, game: Game | None = None, horizon_year: int | None = None):
        self.game = game or load_game()
        self.vehicles = load_vehicles(self.game)
        self.horizon = horizon_year or 2030
        self.nodes = network_nodes(self.game)
        self.by_key = {n.key: n for n in self.nodes}
        params = RoutingParams()
        self.mat = {k: network_matrix(self.game, k, self.game.year, params) for k in ("rail", "road")}
        self.water = water_network(self.game, self.game.year, port_radius=PORT_RADIUS)
        self.ind = {i.id: i for i in self.game.industries}
        self.town = {t.id: t for t in self.game.towns}
        self.boosters = {i.id: booster_info(self.game, i) for i in self.game.industries}
        # town growth + company rank from world population (towns.py)
        from .towns import TownModel
        self.towns = TownModel(self.game, nodes=self.nodes) if TOWN_GROWTH else None
        # extra candidate generators: callables (planner, sim, year) -> list[ProjectCandidate]
        self.generators: list = []
        self._eval_cache: dict = {}          # _candidate memo (one year)
        self._eval_cache_year = None

    # ---------------- supply / demand
    def _obj(self, node) -> Industry | Town:
        return self.ind[node.id] if node.kind == "industry" else self.town[node.id]

    def initial_state(self) -> Sim:
        sim = Sim(self.game, self.vehicles, self.nodes)
        sim.cash = self.game.money
        for o in self.game.loans:
            sim.offers.append(Loan(o.kind, o.amount, o.months, o.rate))
        consumers = {c for i in self.game.industries for c in i.inputs} | \
                    {c for t in self.game.towns for cs in t.needs.values() for c in cs}
        for n in self.nodes:
            if n.kind == "industry":
                ind = self.ind[n.id]
                if not ind.inputs:          # primary producer
                    for c in ind.outputs:
                        if DEAD_END_OK or c in consumers:
                            sim.supply[(n.index, c)] = max_production(self.game, ind, c)
                for c in ind.inputs:
                    sim.demand[(n.index, c)] = max_production(self.game, ind, c)
                for c, (need, _bf) in self.boosters[ind.id].items():
                    sim.demand[(n.index, c)] = need
            else:
                t = self.town[n.id]
                for lu, cs in t.needs.items():
                    for c in cs:
                        sim.demand[(n.index, c)] = sim.demand.get((n.index, c), 0) + \
                            TOWN_DEMAND_PER_CAPACITY * t.capacities.get(lu, 0)
        if getattr(self, "towns", None) is not None:
            sim.demand.update(self.towns.initial_demand())
        return sim

    # ---------------- candidates
    def candidates(self, sim: Sim) -> list[Candidate]:
        out = []
        for (pi, c), vol in sim.supply.items():
            if vol < 1:
                continue
            p = self.nodes[pi]
            cargo = self.game.cargo_by_key[c]
            for (di, dc), cap in sim.demand.items():
                if dc != c or cap < 1 or di == pi:
                    continue
                d = self.nodes[di]
                v = min(vol, cap)
                for mode in ("rail", "road", "water"):
                    cand = self._candidate(sim, p, d, cargo, mode, v)
                    if cand:
                        out.append(cand)
        return out

    def _candidate(self, sim: Sim, p, d, cargo, mode: str, vol: float) -> Candidate | None:
        year = sim.year
        geometry = None
        if mode == "water":
            L = self.water.length[p.index, d.index]
            if not np.isfinite(L) or not self.water_ok(p.index, d.index):
                return None
            route_m, route_build, route_maint, grade = float(L), 0.0, 0.0, 0.0
        else:
            if p.water_only or d.water_only:
                return None
            m = self.mat[mode]
            if not np.isfinite(m.build[p.index, d.index]):
                return None
            route_m = float(m.length[p.index, d.index])
            route_build = float(m.build[p.index, d.index])
            route_maint = float(m.maint[p.index, d.index])
            grade = 0.01
        key = (min(p.index, d.index), max(p.index, d.index), mode)
        if key in sim.built_pairs:
            route_build = 0.0
            route_maint = 0.0
        # evaluate_link + capacity layer are pure functions of these inputs: most candidates do not
        # change from one build to the next, so they are memoized for the year (exact float keys)
        ekey = (p.index, d.index, cargo.key, mode, vol, year, route_m, route_build, route_maint, grade)
        cache = self.__dict__.setdefault("_eval_cache", {})
        if getattr(self, "_eval_cache_year", None) != year:
            cache.clear()
            self._eval_cache_year = year
        base = cache.get(ekey, _MISSING)
        if base is _MISSING:
            base = cache[ekey] = self._evaluate(p, d, cargo, mode, vol, year, route_m, route_build, route_maint, grade)
        if base is None:
            return None
        ev = copy.copy(base)            # station sharing below adjusts the copy only
        # stations already present at an end are free
        st = ev.station_each
        # existing stations are free; offshore industries have built-in ship terminals
        def has_station(n):
            return (n.index, mode) in sim.stations or (mode == "water" and n.water_only)
        free = sum(st for n in (p, d) if has_station(n))
        free_m = sum(ev.station_maint_each for n in (p, d) if has_station(n))
        if (p.index, mode) in sim.stations or (d.index, mode) in sim.stations:
            # a shared station frees the depot itself, not the extra maintenance buildings
            from . import facilities as _fac
            _cat = getattr(self.game, "_catalogue", None) or _fac.Catalogue(self.game)
            free += min(ev.depot_cost, _fac.depot_price(_cat, mode, sim.year)[0])
        ev.infra_capex = max(0.0, ev.infra_capex - free)
        ev.maintenance = max(0.0, ev.maintenance - free_m)
        return Candidate(p.index, d.index, cargo.key, mode, ev, geometry, route_build, st)

    def _evaluate(self, p, d, cargo, mode, vol, year, route_m, route_build, route_maint, grade) -> LinkEval | None:
        """evaluate_link + capacity layer of one direct link (None: unprofitable / infeasible)."""
        ev = evaluate_link(self.game, self.vehicles, cargo, _endpoint(p), _endpoint(d), mode if mode != "water" else "water",
                           vol, year, route_m=route_m, route_build_cost=route_build,
                           route_maintenance=route_maint, max_grade=grade)
        if ev is None or ev.profit <= 0:
            return None
        # capacity layer: track loops/double track, terminals, road limits, maintenance coverage
        if CAPACITY_CHECK:
            from . import capacity
            adj = capacity.apply_capacity(ev, route_m, self.game)
            if not adj.get("feasible", True):
                return None
            ev = capacity.apply_to(ev, adj)
            ev.platform = adj.get("platform")          # "specialized" | "universal"
            ev.capacity_notes = adj.get("notes") or []
            if ev.profit <= 0:
                return None
        return ev

    def water_ok(self, i: int, j: int) -> bool:
        """Both ends can use a harbor directly: water industries, or land nodes within catchment."""
        body = self.water.body[i, j]
        for n in (i, j):
            if self.nodes[n].water_only:
                continue
            dists = [dist for b, _cell, dist in self.water.ports.get(n, []) if b == body]
            if not dists or min(dists) > HARBOR_CATCHMENT:
                return False
        return True

    @staticmethod
    def not_loss_making(c: Candidate) -> bool:
        ev = c.ev
        life = min((v.lifespan_years for v, _ in ev.consist.units), default=30) if ev.consist else 30
        dep = ev.vehicle_capex / max(life, 1) + ev.infra_capex / INFRA_LIFE_YEARS
        return ev.profit - dep >= 0

    @staticmethod
    def score(c: Candidate) -> float:
        npv = _npv(c.ev.profit, c.ev.capex)
        return npv / max(c.ev.capex, 1.0)

    # ---------------- money
    def _monthly_fixed(self, sim: Sim) -> float:
        run = (sum(p.running + p.maintenance for p in sim.projects)
               + sum(g.running + g.maintenance for g in sim.generic)) / 12
        loans = sum(l.monthly_principal + l.monthly_interest for l in sim.loans)
        return run + loans

    def _cand_flows(self, cand) -> tuple[float, float, int]:
        """(income/yr, costs/yr, months until the first income) of a candidate not built yet."""
        year_s = self.game.year_ms / 1000.0
        if isinstance(cand, ProjectCandidate):
            delay = _delay_months(_first_income_s(cand), year_s)
            if cand.meta.get("action") == "prospect":
                delay += PROSPECT_DELAY_MONTHS
            return cand.income, cand.running + cand.maintenance, delay
        ev = cand.ev
        return ev.income, ev.running + ev.maintenance, _delay_months(0.5 * ev.trip_time_s, year_s)

    def _projected_min_cash(self, sim: Sim, spend: float = 0.0, cand=None) -> float:
        """Lowest cash over the next months after paying `spend` now (and building `cand`):
        running costs and loan payments start at once, income only after each line's first
        loaded arrival (half a round trip - more than a game year for long early lines)."""
        year = sim.year
        tmult = ticket_multiplier(self.game, self._rank_of(year)) if RANK_INCOME else 1.0
        rows = [((p.income_month if p.income_month >= 0 else p.start_month), p.start_month, p.income,
                 p.running + p.maintenance) for p in sim.projects]
        rows += [(g.meta.get("income_month", g.meta.get("start_month", 0)), g.meta.get("start_month", 0),
                  g.income, g.running + g.maintenance) for g in sim.generic]
        horizon = CASH_BUFFER_MONTHS
        if cand is not None:
            inc, cost, delay = self._cand_flows(cand)
            rows.append((sim.month + 1 + delay, sim.month + 1, inc, cost))
        last = max((r[0] for r in rows), default=sim.month)
        horizon = max(horizon, min(36, last - sim.month + 1))
        cash, low = sim.cash - spend, sim.cash - spend
        for t in range(1, horizon + 1):
            m = sim.month + t
            inc = sum(r[2] for r in rows if r[0] <= m) / 12 * tmult
            cost = sum(r[3] for r in rows if r[1] <= m) / 12
            cost += sum(l.monthly_principal + l.monthly_interest for l in sim.loans if l.paid_months + t <= l.months)
            cash += inc - cost
            low = min(low, cash)
        return low

    def _rank_of(self, year: int) -> int:
        if not hasattr(self, "_rank_fn"):
            self._rank_fn = rank_schedule(self)
        return self._rank_fn(year)

    def _cash_ok(self, sim: Sim, need: float, cand=None, buffer: float | None = None) -> bool:
        buffer = CASH_BUFFER_MONTHS * self._monthly_fixed(sim) if buffer is None else buffer
        return sim.cash >= need + buffer and self._projected_min_cash(sim, need, cand) >= 0

    def _ensure_cash(self, sim: Sim, need: float, cand=None) -> bool:
        while not self._cash_ok(sim, need, cand) and sim.offers and len(sim.loans) < MAX_LOANS:
            sim.offers.sort(key=lambda l: l.eff_rate)
            o = sim.offers.pop(0)
            o.taken_month = sim.month
            sim.loans.append(o)
            sim.cash += o.amount
            sim.offer_return.append(sim.month + LOAN_COOLDOWN_MONTHS)
            sim.add_step(action="take_loan", title=f"Взять кредит {o.amount / 1e6:.0f} млн",
                         details=f"{o.months} мес., {o.rate:.0%} (реально {o.eff_rate:.2%}/год). "
                                 f"Платёж {(o.monthly_principal + o.monthly_interest):,.0f}/мес.".replace(",", " "),
                         loan={"kind": o.kind, "amount": o.amount, "months": o.months, "rate": o.rate})
        return self._cash_ok(sim, need, cand)

    def _repay_loans(self, sim: Sim):
        buffer = CASH_BUFFER_MONTHS * self._monthly_fixed(sim) * 2
        for l in sorted(sim.loans, key=lambda l: -l.eff_rate):
            rem = l.remaining_principal
            if sim.cash - rem > buffer and self._projected_min_cash(sim, rem) >= buffer / 2:
                sim.cash -= rem
                sim.loans.remove(l)
                sim.add_step(action="repay_loan", title=f"Досрочно погасить кредит {l.amount / 1e6:.0f} млн",
                             details=f"Остаток тела {rem:,.0f}; проценты за оставшиеся месяцы не начисляются."
                             .replace(",", " "), cost=round(rem),
                             loan={"kind": l.kind, "amount": l.amount, "months": l.months, "rate": l.rate})

    # ---------------- build
    def _build(self, sim: Sim, c: Candidate):
        ev = c.ev
        p, d = self.nodes[c.p], self.nodes[c.d]
        sim.cash -= ev.capex
        sim.built_pairs.add(c.pair_key())
        sim.stations.add((c.p, c.mode))
        sim.stations.add((c.d, c.mode))
        sim.supply[(c.p, c.cargo)] -= ev.volume
        sim.demand[(c.d, c.cargo)] -= ev.volume
        cons = ev.consist
        same = next((pr for pr in sim.projects if pr.cand.p == c.p and pr.cand.d == c.d
                     and pr.cand.mode == c.mode and pr.cand.cargo == c.cargo), None)
        if same is not None:
            # more volume on an existing line: just more vehicles
            same.volume += ev.volume
            same.income += ev.income
            same.running += ev.running
            same.n_consists += ev.n_consists
            same.vehicle_value += ev.vehicle_capex
            sim.add_step(action="buy_vehicles", kind=c.mode, cargo=c.cargo, line=same.id,
                         title=f"Докупить {ev.n_consists}× {cons.label} на линию {same.id}",
                         details=f"+{ev.volume:.0f} ед./год".replace(",", " "),
                         vehicles=[{"model": v.file.rsplit("/", 1)[-1], "name": v.name,
                                    "count": n * ev.n_consists, "price": round(v.price)} for v, n in cons.units],
                         cost=round(ev.capex), income_per_year=round(ev.income),
                         running_cost_per_year=round(ev.running),
                         roi_years=round(ev.payback_years, 1) if math.isfinite(ev.payback_years) else None)
            return
        pid = sim.new_line_id()
        proj = Project(pid, c, sim.month + 1, ev.volume, ev.income, ev.running, ev.maintenance,
                       [(v.id, n) for v, n in cons.units], ev.n_consists, ev.vehicle_capex, sim.month,
                       min(v.lifespan_years for v, _ in cons.units))
        proj.income_month = proj.start_month + _delay_months(0.5 * ev.trip_time_s, self.game.year_ms / 1000.0)
        sim.projects.append(proj)
        geometry = None
        segments = None
        demo_info = (None, [])
        if c.mode in ("rail", "road"):
            r = self.mat[c.mode].route(c.p, c.d)
            if r:
                from .routing import route_geometry, route_segments_json
                geometry = route_geometry(r)
                segments = route_segments_json(r)
                demo_info = _demolition_summary(getattr(r, "demolition_cost", 0), getattr(r, "demolition_m", 0),
                                                getattr(r, "demolition_buildings", 0),
                                                getattr(r, "demolition_towns", {}))
        elif c.mode == "water":
            r = self.water.route(c.p, c.d)
            if r:
                geometry = [[round(x, 1), round(y, 1)] for x, y in r.polyline[:: max(1, len(r.polyline) // 60)]]
        cargo_name = self.game.cargo_by_key[c.cargo].name
        mode_ru = {"rail": "Ж/д", "road": "Дорога", "water": "Судно"}[c.mode]
        sim.add_step(
            action="build_line", id_hint=pid, kind=c.mode, cargo=c.cargo,
            title=f"{mode_ru}: {cargo_name.lower()} {p.name} → {d.name}",
            details=(f"{ev.volume:.0f} ед./год, {ev.straight_m / 1000:.1f} км по прямой, маршрут "
                     f"{ev.route_m / 1000:.1f} км. Состав: {ev.n_consists}× {cons.label}. "
                     f"Станции: {ev.station_name} ({ev.station_each:,.0f} каждая), {ev.depot_name}. "
                     f"{_platform_text(ev, self.game)}"
                     f"Инфраструктура {ev.infra_capex:,.0f}, техника {ev.vehicle_capex:,.0f}.").replace(",", " "),
            **{"from": _endpoint_json(p), "to": _endpoint_json(d)},
            geometry=geometry, length_m=round(ev.route_m),
            **({"segments": segments} if segments else {}),
            **({"demolition": demo_info[0]} if demo_info[0] else {}),
            **({"warnings": demo_info[1]} if demo_info[1] else {}),
            vehicles=[{"model": v.file.rsplit("/", 1)[-1], "name": v.name, "count": n * ev.n_consists,
                       "price": round(v.price)} for v, n in cons.units],
            cost=round(ev.capex), income_per_year=round(ev.income),
            running_cost_per_year=round(ev.running + ev.maintenance),
            roi_years=round(ev.payback_years, 1) if math.isfinite(ev.payback_years) else None,
        )
        sim.steps[-1]["line"] = pid
        sim.steps[-1].pop("id_hint", None)
        note = self._deliver(sim, c.d, c.cargo, ev.volume)
        if note:
            sim.steps[-1]["details"] += note

    def _moved_out(self, sim: Sim, node: int, cargo: str) -> float:
        """Units/year already taken from (node, cargo) by built projects of any kind."""
        v = sum(pr.volume for pr in sim.projects if pr.cand.p == node and pr.cand.cargo == cargo)
        v += sum(g.uses_supply.get((node, cargo), 0) for g in sim.generic)
        # processing output carried by the same multi-stop line that delivered the inputs
        # (tours: chain legs) is not in uses_supply - count it, or it is offered twice
        v += sum((g.meta.get("chain_output") or {}).get((node, cargo), 0) for g in sim.generic)
        return v

    def _proc_scale(self, sim: Sim, node: int) -> float:
        """Capacity multiplier of a processing industry: productivity boosts (workers) x growth
        (level / initial level; livestock farms grow, factories have no levels)."""
        ind = self.ind[self.nodes[node].id]
        base = max(ind.level, 1)
        level = getattr(sim, "levels", {}).get(node, base)
        return getattr(sim, "cap_mult", {}).get(node, 1.0) * level / base

    def _refresh_processing(self, sim: Sim, node: int):
        """Free input demand and free output supply of a processing industry from what is delivered:
        output = min(scarcest input x recipe ratio, max production x scale) - already shipped."""
        ind = self.ind[self.nodes[node].id]
        scale = self._proc_scale(sim, node)
        for c_in in ind.inputs:
            if (node, c_in) in sim.demand:
                sim.demand[(node, c_in)] = max(0.0, max_production(self.game, ind, c_in) * scale
                                               - sim.delivered.get((node, c_in), 0))
        for c_out, needs in conversion(ind).items():
            # output limited by the scarcest input (all inputs of the rule are needed)
            possible = min(sim.delivered.get((node, ci), 0) * ratio for ci, ratio in needs.items())
            cap = max_production(self.game, ind, c_out) * scale
            free = min(possible, cap) - self._moved_out(sim, node, c_out)
            sim.supply[(node, c_out)] = max(0.0, free)

    def _apply_productivity(self, sim: Sim, node: int, factor: float):
        """Productivity modifier (workers, booster without a delivered cargo): primary industries
        produce `factor` x more; processing plants get `factor` x more CAPACITY (inputs and
        outputs) - they still need the inputs, nothing is produced from nothing."""
        ind = self.ind[self.nodes[node].id]
        if ind.inputs:
            if not hasattr(sim, "cap_mult"):
                sim.cap_mult = {}
            sim.cap_mult[node] = sim.cap_mult.get(node, 1.0) * factor
            self._refresh_processing(sim, node)
            return
        for c_out in ind.outputs:
            if (node, c_out) in sim.supply:
                sim.supply[(node, c_out)] += max_production(self.game, ind, c_out) * (factor - 1)

    def _deliver(self, sim: Sim, d_index: int, cargo: str, volume: float) -> str:
        """Effects of delivering `volume` units/year of `cargo` to node d_index:
        - booster cargo (fertilizer/tools/machines): main outputs grow by boostFactor x supplied fraction;
        - input of a processing plant: its outputs become available (recipe ratios, capped)."""
        d = self.nodes[d_index]
        if d.kind != "industry":
            return ""
        ind = self.ind[d.id]
        if cargo in self.boosters.get(d.id, {}):
            need, bf = self.boosters[d.id][cargo]
            before = min(1.0, sim.delivered.get((d_index, cargo), 0) / need) if need else 0
            sim.delivered[(d_index, cargo)] = sim.delivered.get((d_index, cargo), 0) + volume
            after = min(1.0, sim.delivered[(d_index, cargo)] / need) if need else 0
            for c_out in ind.outputs:
                base = max_production(self.game, ind, c_out)
                sim.supply[(d_index, c_out)] = sim.supply.get((d_index, c_out), 0) + base * bf * (after - before)
            return f" Бонус производства {d.name}: +{bf * (after - before):.0%}."
        if ind.inputs:
            sim.delivered[(d_index, cargo)] = sim.delivered.get((d_index, cargo), 0) + volume
            self._refresh_processing(sim, d_index)
            return f" {d.name} начинает выпуск продукции."
        return ""

    # ---------------- vehicles renewal
    def _renew(self, sim: Sim):
        for pr in sim.projects:
            c = pr.cand
            cargo = self.game.cargo_by_key[c.cargo]
            p, d = self.nodes[c.p], self.nodes[c.d]
            route_m = c.ev.route_m
            ev = evaluate_link(self.game, self.vehicles, cargo, _endpoint(p), _endpoint(d), c.mode, pr.volume,
                               sim.year, route_m=route_m, route_build_cost=0, route_maintenance=pr.maintenance)
            if not ev:
                continue
            age = (sim.month - pr.bought_month) / 12
            resale = RESALE_FACTOR * pr.vehicle_value * max(0.0, 1 - age / max(pr.lifespan_years, 1))
            gain = (ev.income - ev.running) - (pr.income - pr.running)
            swap = ev.vehicle_capex - resale
            old_ids = sorted(pr.consist_ids)
            new_ids = sorted((v.id, n) for v, n in ev.consist.units)
            end_of_life = age >= pr.lifespan_years
            voluntary = (new_ids != old_ids and age >= MIN_AGE_VOLUNTARY and gain > 0
                         and swap / gain < REPLACE_PAYBACK)
            if end_of_life or voluntary:
                if sim.cash < swap and not self._ensure_cash(sim, swap):
                    continue
                sim.cash -= swap
                pr.income, pr.running = ev.income, ev.running
                pr.consist_ids, pr.n_consists = new_ids, ev.n_consists
                pr.vehicle_value, pr.bought_month = ev.vehicle_capex, sim.month
                pr.lifespan_years = min(v.lifespan_years for v, _ in ev.consist.units)
                sim.add_step(action="replace_vehicles", kind=c.mode, cargo=c.cargo, line=pr.id,
                             title=f"Заменить технику на линии {pr.id}: {ev.n_consists}× {ev.consist.label}",
                             details=(f"Продажа старой ~{resale:,.0f}, покупка {ev.vehicle_capex:,.0f}. "
                                      f"Прибыль линии +{gain:,.0f}/год.").replace(",", " "),
                             vehicles=[{"model": v.file.rsplit("/", 1)[-1], "name": v.name,
                                        "count": n * ev.n_consists, "price": round(v.price)}
                                       for v, n in ev.consist.units],
                             cost=round(max(swap, 0)), income_per_year=round(gain),
                             roi_years=round(swap / gain, 1) if gain > 0 else None)

    # ---------------- generic candidates (passengers, multimodal, tours, efficiency)
    @staticmethod
    def _signature(pc: ProjectCandidate) -> tuple:
        """Identity of a project regardless of vehicle choice: kind, cargo and the set of nodes.
        Passenger bundles ("multi") and plain intercity lines between the same towns count as one."""
        kind = "passenger" if pc.kind == "passenger" else pc.kind
        return kind, pc.cargo, frozenset(pc.nodes)

    def _generic_candidates(self, sim: Sim) -> list:
        built = {self._signature(g) for g in sim.generic}
        out = []
        from . import capacity_generic, facilities
        cat = getattr(self.game, "_catalogue", None) or facilities.Catalogue(self.game)
        stations = set(sim.stations)
        remote = self._remote_generators(sim)
        for gen in self.generators:
            try:
                name = getattr(gen, "__name__", "")
                if name in remote:
                    res = remote[name]
                    if isinstance(res, Exception):
                        raise res
                    produced = res
                else:
                    produced = gen(self, sim, sim.year)
                for pc in produced or []:
                    if self._signature(pc) in built:
                        continue
                    # same capacity rules as direct lines: road/rail capacity, terminals, maintenance
                    pc = capacity_generic.check_candidate(self.game, cat, self._normalize_boost(pc), sim.year,
                                                          built_stations=stations, nodes=self.nodes,
                                                          vehicles=self.vehicles)
                    if pc is not None:
                        out.append(pc)
            except Exception as e:          # a broken generator must not stop the plan
                print(f"generator {getattr(gen, '__name__', gen)} failed: {e}")
        return out

    def _remote_generators(self, sim: Sim) -> dict:
        """Results of the generators run by the worker processes (parallel.py) for this state:
        {closure name: candidates | Exception}; {} when running serially."""
        pool = getattr(self, "_pool", None)
        if pool is None:
            return {}
        handle = getattr(self, "_pending", None)
        self._pending = None
        try:
            if handle is None or handle["month"] != sim.month:
                handle = pool.dispatch(self, sim)          # state not dispatched ahead: do it now
            return pool.collect(self, handle)
        except Exception as e:                  # broken worker: serial from now on
            print(f"parallel: workers failed ({e}); serial generators from now on", flush=True)
            try:
                pool.close()
            finally:
                self._pool = None
            return {}

    def _prune_generic(self, sim: Sim, cache: list, built: ProjectCandidate) -> list:
        """Drop candidates made obsolete by `built`: same stations / town loops, or exhausted supply."""
        taken = set(built.station_keys)
        sig = self._signature(built)
        out = []
        for g in cache:
            if g is built or self._signature(g) == sig:
                continue
            if taken & set(g.station_keys) and g.kind == built.kind and set(g.nodes) <= set(built.nodes):
                continue
            if any(sim.supply.get(k, 0) < 0.5 * v for k, v in g.uses_supply.items()):
                continue
            if any(sim.demand.get(k, 0) < 0.5 * v for k, v in g.uses_demand.items()):
                continue
            if (g.kind == "workers" and any((n, "workers") in getattr(sim, "boosted", set()) for n in g.nodes)):
                continue
            out.append(g)
        return out

    @staticmethod
    def _normalize_boost(pc: ProjectCandidate) -> ProjectCandidate:
        """A production boost is not cash income of the project that causes it: the planner adds the
        extra output to the supply (_deliver / _apply_productivity) and it earns money through the
        lines that carry it (buy_vehicles / new lines). Generators that put the boost value into
        `income` (efficiency boosters, passenger worker lines) are converted to the planner's
        convention: income = own transport income, boost value -> meta['value_bonus'] (ranking only),
        and the vehicles for the extra output are bought later by the carrying lines."""
        m = pc.meta
        if "value_bonus" in m or not pc.boosts:
            return pc
        bv = float(m.get("boost_value") or 0.0)
        if bv <= 0:
            return pc
        vcap = float(m.get("boost_vehicle_capex") or 0.0)
        pc.income = max(0.0, pc.income - bv)
        pc.vehicle_capex = max(0.0, pc.vehicle_capex - vcap)
        annuity = (1 - (1 + DISCOUNT) ** -NPV_YEARS) / DISCOUNT
        m["value_bonus"] = max(0.0, bv - vcap / annuity)
        m["boost_income_excluded"] = round(bv)
        return pc

    def boost_value(self, sim: Sim, node: int, factor: float) -> float:
        """Yearly profit gain from multiplying an industry's productivity by `factor`: extra units
        times the per-unit profit of its existing outbound lines (0 if nothing ships from it yet).
        Processing plants only gain where delivered inputs exceed their current capacity."""
        n = self.nodes[node]
        ind = self.ind.get(n.id) if n.kind == "industry" else None
        if ind is not None and ind.inputs:
            gain = 0.0
            scale = self._proc_scale(sim, node)
            for c_out, needs in conversion(ind).items():
                lines = [pr for pr in sim.projects if pr.cand.p == node and pr.cand.cargo == c_out and pr.volume > 0]
                vol = sum(pr.volume for pr in lines)
                if vol <= 0:
                    continue
                per_unit = max(0.0, sum(pr.income - pr.running for pr in lines) / vol)
                possible = min(sim.delivered.get((node, ci), 0) * r for ci, r in needs.items())
                cap = max_production(self.game, ind, c_out) * scale
                gain += per_unit * max(0.0, min(possible, cap * factor) - min(possible, cap))
            return gain
        gain = 0.0
        for pr in sim.projects:
            if pr.cand.p == node and pr.volume > 0:
                per_unit = (pr.income - pr.running) / pr.volume
                gain += max(0.0, per_unit) * pr.volume * (factor - 1)
        return gain

    def _any_score(self, c) -> float:
        """NPV per unit of capital; income that only starts after the first loaded arrival (half a
        round trip, > 1 year on long early lines) is lost for that time."""
        inc, _cost, delay = self._cand_flows(c)
        lost = inc * delay / 12
        if isinstance(c, ProjectCandidate):
            return (_npv(c.profit + c.meta.get("value_bonus", 0.0), c.capex) - lost) / max(c.capex, 1.0)
        return (_npv(c.ev.profit, c.ev.capex) - lost) / max(c.ev.capex, 1.0)

    def _build_generic(self, sim: Sim, pc: ProjectCandidate):
        sim.cash -= pc.capex
        pc.meta["start_month"] = sim.month + 1
        delay = _delay_months(_first_income_s(pc), self.game.year_ms / 1000.0)
        if pc.meta.get("action") == "prospect":
            delay += PROSPECT_DELAY_MONTHS
        pc.meta["income_month"] = sim.month + 1 + delay
        pc.meta["id"] = sim.new_line_id()
        sim.generic.append(pc)
        sim.built_pairs.update(pc.pair_keys)
        sim.stations.update(pc.station_keys)
        for k, v in pc.uses_supply.items():
            sim.supply[k] = max(0.0, sim.supply.get(k, 0) - v)
        for k, v in pc.uses_demand.items():
            sim.demand[k] = max(0.0, sim.demand.get(k, 0) - v)
        for k, v in pc.adds_supply.items():
            sim.supply[k] = sim.supply.get(k, 0) + v
        notes = "".join(self._deliver(sim, node, cargo, v) for (node, cargo), v in pc.uses_demand.items()
                        if isinstance(node, int) and node < len(self.nodes))
        # productivity boosts (e.g. workers): primary output x factor, processing capacity x factor
        boosted = getattr(sim, "boosted", set())
        for (node, what), factor in pc.boosts.items():
            n = self.nodes[node]
            if n.kind != "industry" or (node, what) in boosted:
                continue
            boosted.add((node, what))
            if (node, what) in pc.uses_demand and what in self.boosters.get(n.id, {}):
                continue        # delivered booster cargo: _deliver already added the extra output
            self._apply_productivity(sim, node, factor)
        sim.boosted = boosted
        ends = [self.nodes[i] for i in pc.nodes[:1] + pc.nodes[-1:]] if pc.nodes else []
        step = dict(action="build_line", kind=pc.mode, cargo=pc.cargo, title=pc.title, details=pc.details,
                    geometry=pc.geometry, project_kind=pc.kind,
                    vehicles=[{"model": v.file.rsplit("/", 1)[-1], "name": v.name, "count": v.count,
                               "price": round(v.price)} for v in pc.vehicles],
                    cost=round(pc.capex), income_per_year=round(pc.income),
                    running_cost_per_year=round(pc.running + pc.maintenance),
                    roi_years=round(pc.payback_years, 1) if math.isfinite(pc.payback_years) else None)
        if ends:
            step["from"] = _endpoint_json(ends[0])
            step["to"] = _endpoint_json(ends[-1])
        if len(pc.nodes) > 2:
            step["stops"] = [_endpoint_json(self.nodes[i]) for i in pc.nodes]
        if pc.legs:
            step["legs"] = pc.legs
        if pc.meta.get("segments"):
            step["segments"] = pc.meta["segments"]
            step["segment_totals"] = pc.meta.get("segment_totals")
        if pc.meta.get("pax"):                 # passenger stops / street path / depot (passengers.py)
            step["pax"] = pc.meta["pax"]
        tot = pc.meta.get("segment_totals") or {}
        demo, warns = _demolition_summary(tot.get("demolition_cost", 0), tot.get("demolition_m", 0),
                                          tot.get("demolition_buildings", 0), pc.meta.get("demolition_towns"))
        if demo:
            step["demolition"] = demo
        if warns:
            step["warnings"] = warns
        if pc.meta.get("capacity_check"):
            step["capacity_check"] = pc.meta["capacity_check"]
        sim.add_step(**step)
        sim.steps[-1]["line"] = pc.meta["id"]
        if notes:
            sim.steps[-1]["details"] = (sim.steps[-1].get("details") or "") + notes

    # ---------------- industry growth
    def _grow_industries(self, sim: Sim) -> bool:
        """Every half year an industry below its max level grows by one field with probability
        clamp((rating - 0.25) / 0.5, 0, 1), rating = productionRating x shipped/output
        (industries.script.tl); we take the expected value deterministically: certain at >= 75 %,
        never at <= 25 %. Output scales with level. Shipped counts every built project (direct
        links and generic lines: tours, chains, multimodal). Processing industries with levels
        (livestock farms) use shipped / capacity (production rating x shipped share) and grow their
        capacity (inputs and outputs); factories have no levels (max level 0)."""
        grew = False
        if not hasattr(sim, "levels"):
            sim.levels = {}
            sim.grow_acc = {}
        for n in self.nodes:
            if n.kind != "industry":
                continue
            ind = self.ind[n.id]
            if not ind.outputs:
                continue
            level = sim.levels.get(n.index, max(ind.level, 1))
            if level >= max(ind.max_level, level):
                continue
            outs = [c for c in ind.outputs if (n.index, c) in sim.supply]
            if not outs:
                continue
            ratios = []
            for c in outs:
                shipped = self._moved_out(sim, n.index, c)
                if ind.inputs:
                    total = max_production(self.game, ind, c) * self._proc_scale(sim, n.index)
                else:
                    total = shipped + sim.supply.get((n.index, c), 0)
                ratios.append(min(1.0, shipped / total) if total > 0 else 0)
            p = min(1.0, max(0.0, (max(ratios) - 0.25) / 0.5))
            acc = sim.grow_acc.get(n.index, 0.0) + p
            if acc >= 1.0:
                acc -= 1.0
                sim.levels[n.index] = level + 1
                if ind.inputs:
                    self._refresh_processing(sim, n.index)
                else:
                    for c in outs:
                        per_level = max_production(self.game, ind, c) / max(ind.level, 1)
                        sim.supply[(n.index, c)] = sim.supply.get((n.index, c), 0) + per_level
                grew = True
                if level + 1 in (max(ind.level, 1) + 3, ind.max_level):
                    sim.add_step(action="note", title=f"Рост: {n.name} достигла уровня {level + 1}",
                                 details="Вывоз ≥75 % продукции → индустрия растёт раз в полгода; "
                                         "добавьте технику на её линию.")
            sim.grow_acc[n.index] = acc
        return grew

    # ---------------- main loop
    def run(self) -> Sim:
        from . import parallel
        self._pool = parallel.make_pool(self)
        self._pending = None
        if self._pool is not None:
            print(f"parallel: {self._pool.n} worker processes", flush=True)
        try:
            return self._run()
        finally:
            if self._pool is not None:
                st = self._pool.stats
                if st:
                    print("parallel: " + ", ".join(f"{k} {v:.1f}" for k, v in st.items()), flush=True)
                self._pool.close()
                self._pool = None

    def _run(self) -> Sim:
        sim = self.initial_state()
        months = (self.horizon - self.game.year) * 12
        cand_cache: list[Candidate] | None = None
        gen_cache: list | None = None
        target: set | None = None
        rank_of = rank_schedule(self)
        for m in range(months):
            sim.month = m
            # monthly cash flow from running projects and loans; income only after the first loaded
            # arrival (income_month), scaled by the company-rank ticket multiplier of the year
            tmult = ticket_multiplier(self.game, rank_of(sim.year)) if RANK_INCOME else 1.0
            inc = (sum(p.income for p in sim.projects
                       if (p.income_month if p.income_month >= 0 else p.start_month) <= m)
                   + sum(g.income for g in sim.generic
                         if g.meta.get("income_month", g.meta.get("start_month", 0)) <= m)) / 12 * tmult
            cost = (sum(p.running + p.maintenance for p in sim.projects if p.start_month <= m)
                    + sum(g.running + g.maintenance for g in sim.generic if g.meta.get("start_month", 0) <= m)) / 12
            for l in list(sim.loans):
                pay = l.monthly_principal + l.monthly_interest
                cost += pay
                l.paid_months += 1
                if l.paid_months >= l.months:
                    sim.loans.remove(l)
            sim.cash += inc - cost
            sim.year_min_cash = min(getattr(sim, "year_min_cash", sim.cash), sim.cash)
            sim.year_income += inc
            sim.year_costs += cost
            for t in list(sim.offer_return):
                if t <= m:
                    sim.offer_return.remove(t)
                    sim.offers.append(_average_offer(sim.year))
            if m % 12 == 0 and m > 0:
                if getattr(self, "towns", None) is not None:
                    tg = self.towns.yearly_update(sim, sim.year)
                    for k, v in tg["demand_delta"].items():
                        sim.demand[k] = max(0.0, sim.demand.get(k, 0.0) + v)
                    self.towns.apply_to_game(self.game, town_objects=True)
                    self._towns_applied = True
                    for n in tg["notes"]:
                        sim.add_step(action="note", title=n["title"], details=n["details"])
                    gen_cache = None
                if UPGRADES:
                    from . import upgrades
                    applied = upgrades.yearly_upgrades(
                        self, sim, infrastructure=UPGRADE_INFRA,
                        # paid from own cash with buffer and cash projection; loans stay for new lines
                        ensure_cash=lambda s, need: self._cash_ok(s, need))
                    if applied:
                        gen_cache = None      # speeds/consists changed - regenerate candidates
                else:
                    self._renew(sim)
                cand_cache = None
            if m % 6 == 0 and m > 0 and self._grow_industries(sim):
                cand_cache = None
            if m % 12 == 0 and getattr(self, "_pool", None) is not None:
                # generators regenerate every January: start them in the workers now, the MILP
                # target and the direct-link candidates below run in this process meanwhile
                try:
                    self._pending = self._pool.dispatch(self, sim)
                except Exception as e:
                    print(f"parallel: dispatch failed ({e})", flush=True)
                    self._pending = None
            if MILP_GUIDED and m % 12 == 0:
                from . import flow
                des = flow.design(self, sim.year, built_keys=set(sim.built_pairs), sim=sim, time_limit=30)
                target = {(a, b, c, mo) for (a, b, c, mo) in des.flows}
                sim.target_objective = getattr(sim, "target_objective", {})
                sim.target_objective[sim.year] = des.objective
            # build as long as something worthwhile is affordable (with loans)
            for _ in range(10):
                if cand_cache is None:
                    cand_cache = self.candidates(sim)
                if gen_cache is None or m % 12 == 0 and getattr(sim, "_gen_month", -1) != m:
                    gen_cache = self._generic_candidates(sim)
                    sim._gen_month = m
                pool = [c for c in cand_cache if c.ev.payback_years < MAX_PAYBACK
                        and _npv(c.ev.profit, c.ev.capex) > 0
                        and (target is None or (c.p, c.d, c.cargo, c.mode) in target)]
                pool += [g for g in gen_cache
                         if _npv(g.profit + g.meta.get("value_bonus", 0.0), g.capex) > 0
                         and g.capex / max(g.profit + g.meta.get("value_bonus", 0.0), 1e-9) < MAX_PAYBACK]
                coverage = False
                if not pool and COVERAGE_PHASE:
                    # phase 2: connect what is left, as long as the line is not loss-making
                    pool = [c for c in cand_cache if self.not_loss_making(c)]
                    pool += [g for g in gen_cache if g.not_loss_making]
                    coverage = True
                if not pool:
                    break
                # best score first; when it is not affordable yet (cash, 4-loan limit, burn until its
                # first income) try the next ones instead of idling for years (ratio-greedy knapsack)
                best = None
                for cand in sorted(pool, key=self._any_score, reverse=True)[:AFFORD_TRIES]:
                    capex = cand.capex if isinstance(cand, ProjectCandidate) else cand.ev.capex
                    if coverage:
                        ok = self._cash_ok(sim, capex, cand)     # coverage projects: own cash, no loans
                    else:
                        ok = self._ensure_cash(sim, capex, cand)
                    if ok:
                        best = cand
                        break
                if best is None:
                    break
                if isinstance(best, ProjectCandidate):
                    self._build_generic(sim, best)
                    gen_cache = self._prune_generic(sim, gen_cache, best)
                else:
                    self._build(sim, best)
                if coverage and sim.steps and sim.steps[-1]["action"] in ("build_line", "buy_vehicles"):
                    sim.steps[-1]["phase"] = 2
                    sim.steps[-1]["title"] = "Охват: " + sim.steps[-1]["title"]
                cand_cache = None
            if m % 12 == 11:
                self._repay_loans(sim)
                print(f"progress {sim.year}/{self.horizon} money {sim.cash / 1e6:,.0f}M lines "
                      f"{len(sim.projects) + len(sim.generic)}", flush=True)
                sim.cashflow.append({"year": sim.year, "money": round(sim.cash), "income": round(sim.year_income),
                                     "costs": round(sim.year_costs),
                                     "loans": round(sum(l.remaining_principal for l in sim.loans)),
                                     "rank": rank_of(sim.year), "ticket_mult": round(tmult, 4),
                                     **({"population": round(self.towns.world_population()),
                                         "town_level_max": max(t.level for t in self.towns.towns)}
                                        if getattr(self, "towns", None) is not None else {}),
                                     "min_money": round(min(sim.year_min_cash, sim.cash)),
                                     "capex": round(sum(s["cost"] for s in sim.steps if s["year"] == sim.year and
                                                        s["action"] in ("build_line", "buy_vehicles",
                                                                        "replace_vehicles", "upgrade_track")))})
                sim.year_min_cash = sim.cash
                sim.year_income = sim.year_costs = 0.0
        return sim

    # ---------------- output
    def write(self, sim: Sim, path: Path = DATA / "plan.json") -> Path:
        lines = [s for s in sim.steps if s["action"] == "build_line"]
        summary = {
            "start_money": self.game.money,
            "final_money": sim.cashflow[-1]["money"] if sim.cashflow else sim.cash,
            "total_capex": sum(s["cost"] for s in sim.steps
                               if s["action"] in ("build_line", "replace_vehicles", "upgrade_track")),
            "lines": len(lines),
            "loans": sum(1 for s in sim.steps if s["action"] == "take_loan"),
            "horizon": [self.game.year, self.horizon],
            "note": ("Грузовые линии, кольцевые маршруты, перевалка через склады, пассажиры, рабочие и бустеры. "
                     "Доход в деньгах учитывает задержку первой доставки и понижающий множитель ранга компании "
                     "(ранг — предположение: +1 каждые " f"{RANK_YEARS:g}" " года); доход в шагах — без множителя. "
                     "Города и спрос не растут в модели — после исчерпания спроса план почти пуст. "
                     "Спрос городов и пассажиропоток — оценки, уточняются по игре. "
                     "План пересчитывается после каждой выгрузки из игры."),
        }
        plan = {"generated": datetime.now().isoformat(timespec="seconds"),
                "objective": f"Максимум прибыли (NPV {NPV_YEARS} лет, ставка {DISCOUNT:.0%})",
                "summary": summary, "steps": sim.steps, "cashflow": sim.cashflow}
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        routes = [{"name": s["title"], "kind": s["kind"], "cost": s["cost"], "length_m": s.get("length_m"),
                   "points": s["geometry"]} for s in lines if s.get("geometry")]
        (DATA / "routes.json").write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")
        return path


RANK_YEARS = 4.0          # ASSUMPTION: +1 company rank per this many years (rank = world population; calibrate)


def rank_schedule(pl: "Planner"):
    """Company rank by year: from the town model (world population) when enabled, otherwise the
    current rank from the export (v5 `company`) + 1 per RANK_YEARS."""
    if getattr(pl, "towns", None) is not None:
        return pl.towns.rank_of
    comp = pl.game.raw.get("company") or {}
    base = int(comp.get("level") or comp.get("potentialLevel") or 1)
    start = pl.game.year
    return lambda year: int(min(15, base + max(0, year - start) // RANK_YEARS))


WORKER_RADIUS_M = 4000.0        # town -> industry road distance for a worker line
WORKER_BOOST = 2.0              # expected workersBoostFactor (1.5 + U[0,1)) -> mean 2.0
WORKER_BUSES = 2                # players: one horse cart already brings >= 10 workers in 2 years


def worker_candidates(pl: "Planner", sim: Sim, year: int) -> list:
    """Bus lines bringing >= 10 residents to industries whose output is already shipped.
    Ticket income is ignored (small); the value is the production boost, passed as
    meta["value_bonus"] (ranking only) so it is not double counted: the extra output earns money
    through the vehicles later added to the industry's outbound lines."""
    from .candidates import ProjectCandidate, VehicleLine
    from . import facilities
    buses = [v for v in pl.vehicles if v.role == "bus" and v.available(year) and v.carries("passengers")]
    if not buses:
        return []
    bus = min(buses, key=lambda v: v.price + 5 * v.running_cost)
    road = pl.mat["road"]
    cat = getattr(pl.game, "_catalogue", None) or facilities.Catalogue(pl.game)
    stop = cat.construction(year, "small_stops/small_old") or cat.construction(year, "small_stops/small_mid") \
        or (45_000.0, 7_500.0)
    boosted = getattr(sim, "boosted", set())
    out = []
    towns = [n for n in pl.nodes if n.kind == "town"]
    for n in pl.nodes:
        if n.kind != "industry" or n.water_only or (n.index, "workers") in boosted:
            continue
        ind = pl.ind[n.id]
        shipped = sum(pl._moved_out(sim, n.index, c) for c in ind.outputs)
        if shipped <= 0:
            continue
        t = min(towns, key=lambda t: road.length[t.index, n.index])
        L = float(road.length[t.index, n.index])
        if not np.isfinite(L) or L > WORKER_RADIUS_M:
            continue
        value = pl.boost_value(sim, n.index, WORKER_BOOST)
        if value <= 0:
            continue
        free_depot = (t.index, "road") in sim.stations or (n.index, "road") in sim.stations
        dp = facilities.depot(cat, "road", year, WORKER_BUSES)
        build = float(road.build[t.index, n.index]) if np.isfinite(road.build[t.index, n.index]) else 0.0
        if (min(t.index, n.index), max(t.index, n.index), "road") in sim.built_pairs:
            build = 0.0
        infra = build + 2 * stop[0] + (0.0 if free_depot else dp.cost)
        maint = 2 * stop[1] + float(road.maint[t.index, n.index] if np.isfinite(road.maint[t.index, n.index]) else 0)
        title = f"Рабочие: {t.name} → {n.name}"
        details = (f"{WORKER_BUSES}× {bus.name} между остановкой в городе {t.name} и остановкой у предприятия "
                   f"({L / 1000:.1f} км по дороге). Нужно ≥10 разных жителей за 2 года → производство "
                   f"×1,5–2,5 (в среднем ×{WORKER_BOOST:g}). Ценность бонуса ≈ {value:,.0f}/год через рост "
                   f"вывоза продукции.").replace(",", " ")
        out.append(ProjectCandidate(
            kind="workers", mode="road", title=title, details=details, nodes=[t.index, n.index], cargo=None,
            volume=10.0, income=0.0, running=WORKER_BUSES * bus.running_cost, maintenance=maint,
            vehicle_capex=WORKER_BUSES * bus.price, infra_capex=infra,
            vehicles=[VehicleLine(bus.id, bus.name, bus.file, WORKER_BUSES, bus.price)],
            vehicle_life_years=bus.lifespan_years,
            pair_keys=[(min(t.index, n.index), max(t.index, n.index), "road")],
            station_keys=[(t.index, "road_pax"), (n.index, "road_pax")],
            boosts={(n.index, "workers"): WORKER_BOOST},
            meta={"value_bonus": value, "boost_value": value}))
    return out


def register_default_generators(pl: "Planner", passengers: bool = True, multimodal: bool = True,
                                efficiency: bool = True, tours: bool = True):
    """Attach the passenger and warehouse-chain generators (agents' modules) to a planner."""
    if passengers:
        from . import passengers as pax

        def gen_passengers(planner, sim, year):
            out = pax.generate(planner.game, planner.vehicles, planner, year, set(sim.built_pairs),
                               set(sim.stations),
                               boost_value=lambda node, factor: planner.boost_value(sim, node, factor))
            return [c for c in out if c.kind != "workers"]
        pl.generators.append(gen_passengers)
    pl.generators.append(worker_candidates)
    if multimodal:
        from . import multimodal as mm

        def gen_multimodal(planner, sim, year):
            return mm.generate(planner.game, planner.vehicles, planner, year, dict(sim.supply), dict(sim.demand),
                               set(sim.built_pairs), set(sim.stations))
        pl.generators.append(gen_multimodal)
    if tours:
        from . import tours as tr

        def gen_tours(planner, sim, year):
            keys = set(sim.built_pairs) | set(sim.stations)
            sup, dem = dict(sim.supply), dict(sim.demand)
            return (tr.milk_runs(planner, year, sup, dem, keys) or []) + \
                (tr.circuits(planner, year, sup, dem, keys) or [])
        pl.generators.append(gen_tours)
    if efficiency:
        from . import efficiency as eff
        # workers come from the passenger generator; boosters + prospecting from here
        pl.generators.append(eff.make_generator(rank_schedule(pl), include_workers=not passengers))


def main():
    import time
    t0 = time.time()
    import os
    # settings from the control center (tf3planner/control): data/settings.json -> env vars
    env = os.environ.get
    global COVERAGE_PHASE, UPGRADES
    COVERAGE_PHASE = env("TF3P_COVERAGE", "1") != "0"
    UPGRADES = env("TF3P_UPGRADES", "1") != "0"
    horizon = int(env("TF3P_HORIZON", "0") or 0) or None
    pl = Planner(horizon_year=horizon)
    rel = int(env("TF3P_HORIZON_REL", "0") or 0)          # quick plan: current game year + N
    if rel > 0 and not horizon:
        pl.horizon = min(pl.horizon, pl.game.year + rel)
    register_default_generators(pl, passengers=env("TF3P_PASSENGERS", "1") != "0",
                                multimodal=env("TF3P_MULTIMODAL", "1") != "0",
                                efficiency=env("TF3P_EFFICIENCY", "1") != "0",
                                tours=env("TF3P_TOURS", "1") != "0")
    print(f"planner: {pl.game.year}..{pl.horizon}, generators {len(pl.generators)}, coverage {COVERAGE_PHASE}",
          flush=True)
    sim = pl.run()
    path = pl.write(sim)
    print(f"plan -> {path} ({len(sim.steps)} steps, {time.time() - t0:.1f}s)", flush=True)
    if env("TF3P_DOSSIERS", "1") != "0":
        try:      # detailed per-line breakdowns for the web panel (data/dossiers.json)
            from . import dossier
            dossier.main([])
            print("dossiers -> data/dossiers.json", flush=True)
        except Exception as e:
            print(f"dossiers failed: {e}", flush=True)
    if env("TF3P_OVERLAY", "1") != "0":
        try:      # in-game blueprint (mod window "Чертёж" reads <userdata>/tf3_planner/plan_overlay.lua)
            from .overlay import write_overlay
            opath, _ov = write_overlay()
            print(f"overlay -> {opath}", flush=True)
        except Exception as e:
            print(f"overlay failed: {e}", flush=True)
    for s in sim.steps[:40]:
        print(f"{s['year']}-{s['month']:02d} {s['action']:16} {s['title'][:90]:90} cost {s['cost']/1e6:6.2f}M "
              f"inc {s['income_per_year']/1e6:5.2f}M/y payback {s['roi_years']} money {s['money_after']/1e6:7.2f}M")
    for r in sim.cashflow[::5]:
        print(r)


if __name__ == "__main__":
    main()
