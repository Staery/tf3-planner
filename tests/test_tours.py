"""Tests for tf3planner.tours. Run with pytest or directly: python tests/test_tours.py

Pure solver tests need only numpy (OR-Tools / elkai parts are skipped when not installed);
map tests need data/export.json and are skipped without it."""
from __future__ import annotations

import itertools
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tf3planner import tours as T  # noqa: E402


def euclid(n: int, seed: int) -> np.ndarray:
    p = np.random.default_rng(seed).random((n, 2)) * 20000
    return np.sqrt(((p[:, None] - p[None]) ** 2).sum(-1))


def is_perm(order, n) -> bool:
    return sorted(order) == list(range(n))


def brute_path(d, s=None, e=None) -> float:
    n = len(d)
    best = math.inf
    for p in itertools.permutations(range(n)):
        if (s is None or p[0] == s) and (e is None or p[-1] == e):
            best = min(best, T.tour_cost(d, list(p), False))
    return best


# --------------------------------------------------------------------------- solvers

def test_held_karp_matches_brute_force():
    rng = np.random.default_rng(7)
    for it in range(30):
        n = int(rng.integers(3, 9))
        d = rng.random((n, n)) * 100                   # asymmetric
        np.fill_diagonal(d, 0)
        hk, tour = T.held_karp(d)
        bf, _ = T.brute_force(d)
        assert abs(hk - bf) < 1e-9 and abs(T.tour_cost(d, tour) - hk) < 1e-9
        assert abs(T.held_karp_small(d)[0] - hk) < 1e-9


def test_lk_matches_held_karp():
    """Own Lin-Kernighan (single run from nearest neighbour, no kicks) finds the optimum on small
    Euclidean instances; chained LK with kicks as well."""
    for seed in range(40):
        n = 5 + seed % 8                               # 5..12
        d = euclid(n, seed)
        opt, _ = T.held_karp(d)
        lk = T.lin_kernighan(d)
        assert is_perm(lk, n)
        assert T.tour_cost(d, lk) <= opt * (1 + 1e-9), (seed, T.tour_cost(d, lk) / opt)
        clk = T.lin_kernighan(d, kicks=30, seed=seed)
        assert T.tour_cost(d, clk) <= opt * (1 + 1e-9)


def test_local_search_valid_and_monotone():
    for seed in range(5):
        d = euclid(40, seed)
        nn = T.nearest_neighbour(d)
        t2 = T.two_opt(d, nn)
        t3 = T.or_opt(d, t2)
        lk = T.lin_kernighan(d, nn)
        for t in (nn, t2, t3, lk):
            assert is_perm(t, 40)
        assert T.tour_cost(d, t2) <= T.tour_cost(d, nn) + 1e-6
        assert T.tour_cost(d, t3) <= T.tour_cost(d, t2) + 1e-6
        assert T.tour_cost(d, lk) <= T.tour_cost(d, nn) + 1e-6


def test_solve_tour_open_and_fixed_ends():
    rng = np.random.default_rng(11)
    methods = ["auto", "held_karp", "brute"] + (["lkh"] if T.HAVE_ELKAI else []) + (["ortools"] if T.HAVE_ORTOOLS else [])
    for it in range(20):
        n = int(rng.integers(3, 8))
        d = rng.random((n, n)) * 100
        if it % 2:
            d = (d + d.T) / 2
        np.fill_diagonal(d, 0)
        s = int(rng.integers(n))
        e = (s + 1 + int(rng.integers(n - 1))) % n
        for S, E in ((None, None), (s, None), (None, e), (s, e)):
            for m in methods:
                if m == "ortools" and it >= 6:
                    continue                     # time-limited GLS: a few instances suffice
                r = T.solve_tour(d, open=True, fixed_start=S, fixed_end=E, method=m,
                                 time_limit=0.3 if m == "ortools" else 0.05)
                assert is_perm(r.order, n)
                assert S is None or r.order[0] == S
                assert E is None or r.order[-1] == E
                assert abs(r.cost - brute_path(d, S, E)) < 1e-6, (m, S, E)
        # closed with both ends fixed = path s..e + closing arc e->s
        r = T.solve_tour(d, fixed_start=s, fixed_end=e)
        assert r.order[0] == s and r.order[-1] == e
        assert abs(r.cost - (brute_path(d, s, e) + d[e, s])) < 1e-6
        r = T.solve_tour(d)
        assert abs(r.cost - T.brute_force(d)[0]) < 1e-6


def test_symmetric_heuristics_on_symmetric_paths():
    for seed in range(10):
        d = euclid(9, 100 + seed)
        for m in ("lk", "chained_lk"):
            r = T.solve_tour(d, open=True, fixed_start=0, method=m, time_limit=0.05)
            assert r.order[0] == 0 and abs(r.cost - brute_path(d, 0)) < 1e-6


def test_backends_match_optimum():
    for seed in range(8):
        d = euclid(11, 200 + seed)
        opt, _ = T.held_karp(d)
        if T.HAVE_ELKAI:
            assert abs(T.tour_cost(d, T.lkh(d)) - opt) < 1e-6 * opt
        if T.HAVE_ORTOOLS:
            assert T.tour_cost(d, T.ortools_tsp(d, 0.2)) <= opt * 1.02


def test_cvrp_feasible():
    rng = np.random.default_rng(5)
    for it in range(4):
        n = 15
        d = euclid(n, 300 + it)
        dem = [0.0] + list(rng.random(n - 1) * 50 + 10)
        cap, stops = 120.0, 4
        for m in (["savings"] + (["ortools"] if T.HAVE_ORTOOLS else [])):
            routes = T.cvrp(d, dem, cap, 0, stops, method=m, time_limit=0.3)
            served = sorted(c for r in routes for c in r)
            assert served == list(range(1, n)), m
            for r in routes:
                assert len(r) <= stops and sum(dem[c] for c in r) <= cap + 1e-6, m


def test_stop_order():
    sq = [(0, 0), (100, 0), (100, 100), (0, 100)]
    o = T.stop_order(sq)
    assert is_perm(o, 4) and abs(T.tour_cost(np.array([[math.dist(a, b) for b in sq] for a in sq]), o) - 400) < 1e-6


# --------------------------------------------------------------------------- map

_PL = {}


def planner():
    if not (ROOT / "data" / "export.json").exists():
        return None
    if "pl" not in _PL:
        from tf3planner.planner import Planner
        pl = Planner()
        _PL["pl"], _PL["sim"] = pl, pl.initial_state()
    return _PL["pl"]


def _chord_income(pl, f) -> float:
    a, b = pl.nodes[f["from"]], pl.nodes[f["to"]]
    return f["volume"] * pl.game.income_per_unit(pl.game.cargo_by_key[f["cargo"]], (a.x, a.y, a.z), (b.x, b.y, b.z))


def _check_candidate(pl, sim, c):
    year_s = pl.game.year_ms / 1000.0
    m = c.meta
    flows = m["flows"]
    # income: each unit pays the chord pickup -> drop-off, never more than separate direct links
    chord = sum(_chord_income(pl, f) for f in flows)
    tol = sum(0.0005 * f["income_per_unit"] for f in flows) + 1.0     # meta volumes rounded to 0.001
    assert c.income <= chord + tol
    assert abs(c.income - chord) <= tol
    # supply / demand respected
    for k, v in c.uses_supply.items():
        assert v <= sim.supply.get(k, 0) + 1e-6, k
    for k, v in c.uses_demand.items():
        assert v <= sim.demand.get(k, 0) + 1e-6, k
    # fleet: enough capacity for the peak leg (n * cap * year / trip >= peak), not oversized
    n, trip, peak = m["n_consists"], m["trip_s"], m["peak_load"]
    assert n >= 1 and trip > 0 and peak > 0
    by_id = {v.id: v for v in pl.vehicles}
    carrier = c.vehicles[-1]
    unit_cap = min(by_id[carrier.vehicle_id].carries(k) for k in m["cargos"])
    consist_cap = unit_cap * carrier.count / n
    assert unit_cap > 0 and all(l.count % n == 0 for l in c.vehicles)
    per_consist = consist_cap * year_s / trip
    assert n * per_consist >= peak * (1 - 1e-3), (n, per_consist, peak)
    assert (n - 1) * per_consist < peak * 1.01, (n, per_consist, peak)
    if c.mode == "rail":
        # wagons per train within the locomotive's tractive limit on the ruling grade (economics.best_consists)
        loco = by_id[c.vehicles[0].vehicle_id]
        wagon = by_id[carrier.vehicle_id]
        max_mass_t = loco.tractive_effort_kn * 1000 / (9.81 * (0.002 + T.RAIL_GRADE)) / 1000
        limit = max(1, int((max_mass_t - loco.weight_empty_t) // (wagon.weight_empty_t + wagon.weight_max_payload_t)))
        assert carrier.count / n <= min(30, limit), (carrier.count / n, limit)
    assert c.vehicle_capex > 0 and c.running > 0
    # profit / NPV bookkeeping and the comparison
    assert abs(c.profit - (c.income - c.running - c.maintenance)) < 1e-6
    assert m["gain_npv"] > 0 and m["npv"] > m["direct"]["npv"]
    assert m["tour"] == c.nodes and len(set(c.nodes)) == len(c.nodes)
    assert all(len(k) == 3 for k in c.pair_keys) and all(len(k) == 2 for k in c.station_keys)
    assert c.title and c.details and any("а" <= ch <= "я" for ch in c.details.lower())
    return year_s


def test_line_matches_evaluate_link():
    pl = planner()
    if pl is None:
        return
    from tf3planner.economics import Endpoint, evaluate_link
    sim = _PL["sim"]
    g = pl.game
    (p, c), vol = next((k, v) for k, v in sim.supply.items() if k[1] == "grain")
    d = next(n for (n, cc), v in sim.demand.items() if cc == "grain")
    ep = lambda n: Endpoint(n.kind, n.id, n.name, n.x, n.y, n.z)  # noqa: E731
    for mode in ("road", "rail"):
        mt = pl.mat[mode]
        ev = evaluate_link(g, pl.vehicles, g.cargo_by_key[c], ep(pl.nodes[p]), ep(pl.nodes[d]), mode, vol, g.year,
                           route_m=float(mt.length[p, d]), route_build_cost=float(mt.build[p, d]),
                           route_maintenance=float(mt.maint[p, d]), max_grade=0.01)
        le = T.evaluate_line(pl, g.year, mode, [p, d], [T.Flow(p, d, c, vol)], set(), "shuttle")
        assert ev is not None and le is not None
        assert abs(le.income - ev.income) < 1e-6 * ev.income
        assert le.n == ev.n_consists, (mode, le.n, ev.n_consists)
        assert abs(le.running - ev.running) < 1e-6 * ev.running
        assert abs(le.capex - ev.capex) < 1e-6 * ev.capex
        assert abs(le.maintenance - ev.maintenance) < 1e-6 * ev.maintenance


def test_milk_runs_sane():
    pl = planner()
    if pl is None:
        return
    sim = _PL["sim"]
    t = time.perf_counter()
    cands = T.milk_runs(pl, pl.game.year, sim.supply, sim.demand, set(), max_candidates=None)
    assert cands, "expected at least one milk-run beating direct links on the 1900 map"
    for c in cands:
        _check_candidate(pl, sim, c)
        assert len({f["from"] for f in c.meta["flows"]}) >= 2          # >= 2 pickup stops
        assert c.geometry and len(c.geometry) >= 2
    # second call hits the caches
    t1 = time.perf_counter()
    T.milk_runs(pl, pl.game.year, sim.supply, sim.demand, set(), geometry=False)
    assert time.perf_counter() - t1 < max(5.0, 0.5 * (t1 - t))


def test_milk_run_income_bounded_by_direct_chords():
    """Same flows, any order: line income equals the sum of direct chord incomes and never more,
    because cargo is paid producer -> consumer by the chord, not along the tour."""
    pl = planner()
    if pl is None:
        return
    sim = _PL["sim"]
    farms = [n for (n, c), v in sim.supply.items() if c == "grain"][:4]
    d = next(n for (n, cc), v in sim.demand.items() if cc == "grain")
    al = T.allocate(pl, farms + [d], sim.supply, sim.demand, {"grain"}, chain=False)
    direct = sum(f.volume * T._ipu(pl, f.cargo, f.p, f.d) for f in al.flows)
    for perm in itertools.permutations(farms + [d]):
        for topo in ("loop", "shuttle"):
            ev = T.evaluate_line(pl, pl.game.year, "road", list(perm), al.flows, set(), topo)
            if ev:
                assert ev.income <= direct * (1 + 1e-12)


def test_circuits_sane():
    pl = planner()
    if pl is None:
        return
    sim = _PL["sim"]
    cands = T.circuits(pl, pl.game.year, sim.supply, sim.demand, set(), max_candidates=None)
    assert cands
    for c in cands:
        _check_candidate(pl, sim, c)
        assert sum(1 for l in c.legs if l["load"] > 0) >= 2               # backhaul / chain leg
        assert c.meta["topology"] == "loop"


def test_built_keys_reduce_cost():
    pl = planner()
    if pl is None:
        return
    sim = _PL["sim"]
    farms = [n for (n, c), v in sim.supply.items() if c == "grain"][:2]
    d = next(n for (n, cc), v in sim.demand.items() if cc == "grain")
    al = T.allocate(pl, farms + [d], sim.supply, sim.demand, {"grain"}, chain=False)
    ev = T.evaluate_line(pl, pl.game.year, "rail", farms + [d], al.flows, set(), "loop")
    built = set(ev.pair_keys) | set(ev.station_keys)
    ev2 = T.evaluate_line(pl, pl.game.year, "rail", farms + [d], al.flows, built, "loop")
    assert ev2.infra_capex == 0 and ev2.maintenance < ev.maintenance and ev2.income == ev.income


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            t = time.perf_counter()
            try:
                fn()
                print(f"PASS {name} ({time.perf_counter() - t:.1f}s)")
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if fails else 0)
