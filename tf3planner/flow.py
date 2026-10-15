"""Network design as a MILP (fixed-charge, max-profit generalized flow with recipes).

Decides at once which producer feeds which consumer for every cargo and mode, respecting
production, processing recipes, boosters and consumer caps, with shared infrastructure:

  max  sum_a margin_a * x_a  -  sum_k fixed_k * y_k
  s.t. primary supply   sum_out x <= S(n,c) * (1 + bf * boost_in / need)      (boosters linear)
       processing       sum_out(n,c) x <= ratio(c,i) * sum_in(n,i) x  for every input i
                        sum_out(n,c) x <= maxprod(n,c)
       consumer caps    sum_in(n,c) x <= cap(n,c)            (industry consumption, town demand)
       linking          x_a <= U_a * y_k(a)                   (k = node pair + mode: one route/stations
                                                               serve all cargos between the two nodes)
       x >= 0, y in {0,1}

margin_a = income per unit - variable cost per unit (vehicle running + annualized vehicle capital),
fixed_k  = annualized route build + stations/depot + yearly route maintenance.
Capital is annualized with a capital recovery factor (rate LAMBDA, CAPITAL_YEARS) — the shadow
price of money (docs/real_logistics.md 6.1). Solved with HiGHS via scipy.optimize.milp.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from .economics import conversion, evaluate_link, max_production

LAMBDA = 0.15
CAPITAL_YEARS = 15


def crf(rate: float = LAMBDA, years: int = CAPITAL_YEARS) -> float:
    return rate / (1 - (1 + rate) ** -years)


@dataclass
class Arc:
    p: int
    d: int
    cargo: str
    mode: str
    margin: float          # per unit
    cap: float             # upper bound on flow
    key: tuple             # (min node, max node, mode)
    ev: object


@dataclass
class Design:
    flows: dict            # (p, d, cargo, mode) -> units/year
    keys: set              # chosen (a, b, mode)
    objective: float
    status: str
    arcs: list


def _demand(planner, sim_like=None) -> tuple[dict, dict, dict]:
    """Supply of primary producers, consumer caps and processing caps (max output)."""
    g = planner.game
    supply, caps, prod_cap = {}, {}, {}
    consumers = {c for i in g.industries for c in i.inputs} | \
                {c for t in g.towns for cs in t.needs.values() for c in cs} | \
                {c for b in planner.boosters.values() for c in b}
    from .planner import TOWN_DEMAND_PER_CAPACITY
    for n in planner.nodes:
        if n.kind == "industry":
            ind = planner.ind[n.id]
            if not ind.inputs:
                for c in ind.outputs:
                    if c in consumers:
                        supply[(n.index, c)] = max_production(g, ind, c)
            else:
                for c in ind.outputs:
                    if c in consumers:
                        prod_cap[(n.index, c)] = max_production(g, ind, c)
            for c in ind.inputs:
                caps[(n.index, c)] = max_production(g, ind, c)
            for c, (need, _bf) in planner.boosters[ind.id].items():
                caps[(n.index, c)] = need
        else:
            t = planner.town[n.id]
            for lu, cs in t.needs.items():
                for c in cs:
                    caps[(n.index, c)] = caps.get((n.index, c), 0) + TOWN_DEMAND_PER_CAPACITY * t.capacities.get(lu, 0)
    return supply, caps, prod_cap


def _state_capacities(planner, sim):
    """Total capacities from a running simulation: free amounts + volumes already moved."""
    supply, caps, prod_cap = _demand(planner)
    moved_out, moved_in = {}, {}
    for pr in sim.projects:
        c = pr.cand
        moved_out[(c.p, c.cargo)] = moved_out.get((c.p, c.cargo), 0) + pr.volume
        moved_in[(c.d, c.cargo)] = moved_in.get((c.d, c.cargo), 0) + pr.volume
    for k in list(supply):
        supply[k] = sim.supply.get(k, 0) + moved_out.get(k, 0)
    for k in list(caps):
        caps[k] = max(caps[k], sim.demand.get(k, 0) + moved_in.get(k, 0))
    for k, v in sim.demand.items():          # needs that appeared as towns grew (towns.TownModel)
        if k not in caps and planner.nodes[k[0]].kind == "town":
            caps[k] = v + moved_in.get(k, 0)
    return supply, caps, prod_cap


def build_arcs(planner, year: int, built_keys: set | None = None, sim=None) -> tuple[list[Arc], dict, dict, dict]:
    from .planner import _endpoint
    g = planner.game
    built_keys = built_keys or set()
    supply, caps, prod_cap = _state_capacities(planner, sim) if sim is not None else _demand(planner)
    sources = {**supply, **prod_cap}
    k_crf = crf()
    arcs: list[Arc] = []
    fixed: dict[tuple, float] = {}
    for (pi, c), s_cap in sources.items():
        if s_cap < 1:
            continue
        p = planner.nodes[pi]
        cargo = g.cargo_by_key[c]
        for (di, dc), d_cap in caps.items():
            if dc != c or di == pi or d_cap < 1:
                continue
            d = planner.nodes[di]
            vol = min(s_cap, d_cap)
            for mode in ("rail", "road", "water"):
                if mode == "water":
                    L = planner.water.length[pi, di]
                    if not np.isfinite(L) or not planner.water_ok(pi, di):
                        continue
                    route_m, build, maint = float(L), 0.0, 0.0
                else:
                    if p.water_only or d.water_only:
                        continue
                    m = planner.mat[mode]
                    if not np.isfinite(m.build[pi, di]):
                        continue
                    route_m, build, maint = float(m.length[pi, di]), float(m.build[pi, di]), float(m.maint[pi, di])
                ev = evaluate_link(g, planner.vehicles, cargo, _endpoint(p), _endpoint(d), mode, vol, year,
                                   route_m=route_m, route_build_cost=0.0, route_maintenance=0.0, max_grade=0.01)
                if ev is None or ev.volume <= 0:
                    continue
                var = (ev.running + ev.vehicle_capex * k_crf) / ev.volume
                margin = ev.income_per_unit - var
                if margin <= 0:
                    continue
                key = (min(pi, di), max(pi, di), mode)
                if key not in fixed:
                    capex = 0.0 if key in built_keys else build + 2 * ev.station_each + ev.depot_cost
                    upkeep = 0.0 if key in built_keys else maint + 2 * ev.station_maint_each
                    fixed[key] = capex * k_crf + upkeep
                arcs.append(Arc(pi, di, c, mode, margin, vol, key, ev))
    return arcs, fixed, (supply, caps, prod_cap), sources


def design(planner, year: int | None = None, built_keys: set | None = None,
           time_limit: float = 60.0, only_keys: set | None = None, sim=None) -> Design:
    """only_keys: restrict infrastructure to this set (evaluate a given network, e.g. a greedy plan)."""
    g = planner.game
    year = g.year if year is None else year
    arcs, fixed, (supply, caps, prod_cap), _ = build_arcs(planner, year, built_keys, sim)
    keys = sorted(fixed)
    kidx = {k: i for i, k in enumerate(keys)}
    na, nk = len(arcs), len(keys)
    n = na + nk
    cvec = np.zeros(n)
    for i, a in enumerate(arcs):
        cvec[i] = -a.margin
    for k, j in kidx.items():
        cvec[na + j] = fixed[k]

    rows, lo, hi = [], [], []

    def add_row(coefs: dict[int, float], lb: float, ub: float):
        rows.append(coefs)
        lo.append(lb)
        hi.append(ub)

    out_of = {}
    in_of = {}
    for i, a in enumerate(arcs):
        out_of.setdefault((a.p, a.cargo), []).append(i)
        in_of.setdefault((a.d, a.cargo), []).append(i)

    # primary supply with linear boosters
    for (pi, c), s in supply.items():
        coefs = {i: 1.0 for i in out_of.get((pi, c), [])}
        if not coefs:
            continue
        node = planner.nodes[pi]
        for bc, (need, bf) in planner.boosters.get(node.id, {}).items():
            if need > 0:
                for i in in_of.get((pi, bc), []):
                    coefs[i] = coefs.get(i, 0) - s * bf / need
        add_row(coefs, -np.inf, s)
    # processing recipes
    for (ni, c), cap in prod_cap.items():
        outs = out_of.get((ni, c), [])
        if not outs:
            continue
        add_row({i: 1.0 for i in outs}, -np.inf, cap)
        ind = planner.ind[planner.nodes[ni].id]
        needs = conversion(ind).get(c, {})
        for ci, ratio in needs.items():
            coefs = {i: 1.0 for i in outs}
            for i in in_of.get((ni, ci), []):
                coefs[i] = coefs.get(i, 0) - ratio
            add_row(coefs, -np.inf, 0.0)
    # consumer caps
    for (di, c), cap in caps.items():
        ins = in_of.get((di, c), [])
        if ins:
            add_row({i: 1.0 for i in ins}, -np.inf, cap)
    # linking
    for i, a in enumerate(arcs):
        add_row({i: 1.0, na + kidx[a.key]: -a.cap}, -np.inf, 0.0)

    A = lil_matrix((len(rows), n))
    for r, coefs in enumerate(rows):
        for j, v in coefs.items():
            A[r, j] = v
    integrality = np.r_[np.zeros(na), np.ones(nk)]
    ub = np.r_[[a.cap for a in arcs], np.ones(nk)]
    if only_keys is not None:
        for k, j in kidx.items():
            if k not in only_keys:
                ub[na + j] = 0
    res = milp(cvec, constraints=LinearConstraint(A.tocsr(), lo, hi), integrality=integrality,
               bounds=Bounds(np.zeros(n), ub), options={"time_limit": time_limit, "mip_rel_gap": 0.01})
    flows, chosen = {}, set()
    if res.x is not None:
        for i, a in enumerate(arcs):
            if res.x[i] > 0.5:
                flows[(a.p, a.d, a.cargo, a.mode)] = float(res.x[i])
        for k, j in kidx.items():
            if res.x[na + j] > 0.5:
                chosen.add(k)
    return Design(flows, chosen, -res.fun if res.fun is not None else 0.0, res.message, arcs)


if __name__ == "__main__":
    import time
    from .planner import Planner
    t0 = time.time()
    pl = Planner()
    t1 = time.time()
    des = design(pl)
    print(f"setup {t1 - t0:.1f}s, MILP {time.time() - t1:.1f}s, {len(des.arcs)} arcs, status: {des.status}")
    print(f"objective (annual profit after capital charge): {des.objective / 1e6:.1f}M/y, "
          f"{len(des.keys)} links, {len(des.flows)} flows")
    nodes = pl.nodes
    for (p, d, c, m), f in sorted(des.flows.items(), key=lambda kv: -kv[1]):
        print(f"  {m:5} {c:11} {nodes[p].name[:34]:34} -> {nodes[d].name[:34]:34} {f:6.0f} u/y")
