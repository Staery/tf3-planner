"""Tests for tf3planner.passengers (run as a script or with pytest)."""
from __future__ import annotations

import math
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner.passengers import (PassengerParams, TownInfo, capture, demand_matrix, gravity,  # noqa: E402
                                   intra_distance_pdf, light_planner, generate, street_planner, town_infos)

_PL = None
_CANDS: dict = {}


def _isolate_route_cache():
    """Routing matrices are cached in a temp dir (seeded from data/), never written to data/."""
    import shutil
    import tempfile
    from tf3planner import routing
    tmp = Path(tempfile.mkdtemp(prefix="tf3p_test_routes_"))
    for f in routing.DATA.glob("routes_*.npz"):
        shutil.copy2(f, tmp / f.name)
    routing._cache_path = lambda kind, year, cell: tmp / f"routes_{kind}_{year}_{int(round(cell))}.npz"


def planner():
    global _PL
    if _PL is None:
        _isolate_route_cache()
        _PL = light_planner()
    return _PL


def _towns(xs, residents=300.0):
    out = []
    for k, x in enumerate(xs):
        out.append(TownInfo(k, k, f"T{k}", x, 0.0, 0.0, residents, 300.0, 300.0, 300.0))
    return out


def _grav(towns, p=None):
    p = p or PassengerParams()
    xyz = np.array([[t.x, t.y, t.z] for t in towns])
    return gravity(towns, xyz, np.zeros(len(towns)), p)


# ---------------------------------------------------------------- demand

def test_disc_distance_pdf():
    d, w = intra_distance_pdf(100.0, 400)
    assert abs(w.sum() - 1) < 1e-9
    assert abs((d * w).sum() - 128 / (45 * math.pi) * 100) < 1.0      # mean 0.905 R


def test_totals_scale_with_population():
    p = PassengerParams()
    small = _grav(_towns([0, 4000, 9000], 200.0), p)
    big = _grav(_towns([0, 4000, 9000], 400.0), p)
    assert abs(small.sum() - 3 * 200 * p.trips_per_resident_year) < 1e-6
    assert abs(big.sum() / small.sum() - 2.0) < 1e-9
    g = planner().game
    nodes, M = demand_matrix(g, p)
    res = sum(t.residents for t in town_infos(g, p))
    scale = g.year_ms / 1000 / p.ref_year_s
    assert abs(M.sum() - res * p.trips_per_resident_year * scale) < 1e-6 * M.sum()
    # per-town totals are proportional to residents
    infos = town_infos(g, p)
    per = M[:len(infos)].sum(1) / np.array([t.residents for t in infos])
    assert np.allclose(per, per[0])
    # doubling the trip rate doubles demand
    _, M2 = demand_matrix(g, replace(p, trips_per_resident_year=2 * p.trips_per_resident_year))
    assert abs(M2.sum() / M.sum() - 2) < 1e-9


def test_intercity_decreases_with_distance():
    M = _grav(_towns([0, 3000, 8000, 14000]))
    assert M[0, 1] > M[0, 2] > M[0, 3] > 0
    assert M[0, 0] > M[0, 1]                       # most trips stay in town
    # real map: per-capita flow between town pairs falls with distance
    g = planner().game
    nodes, M = demand_matrix(g)
    infos = town_infos(g)
    xs, ys = [], []
    for a in infos:
        for b in infos:
            if a.index < b.index:
                dist = math.dist((a.x, a.y), (b.x, b.y))
                if dist < PassengerParams().max_trip_m:
                    xs.append(dist)
                    ys.append((M[a.index, b.index] + M[b.index, a.index]) / (a.residents * b.capacity))
    r = np.corrcoef(np.array(xs), np.log(np.array(ys)))[0, 1]
    assert r < -0.8, r


def test_capture_monotone():
    p = PassengerParams()
    d = np.array([200.0, 500.0, 1000.0, 3000.0])
    s_fast = capture(np.full(4, 200.0), d, 1900, p)
    s_slow = capture(np.full(4, 600.0), d, 1900, p)
    assert np.all(s_fast > s_slow)
    assert np.all(np.diff(s_fast) > 0)                # longer trips -> walking less attractive
    assert capture(300.0, 1000.0, 1990, p) < capture(300.0, 1000.0, 1900, p)   # cars compete later


def test_captured_matrix_below_potential():
    g = planner().game
    nodes, M = demand_matrix(g)
    _, C = demand_matrix(g, captured=True, nodes=nodes)
    assert np.all(C <= M + 1e-9) and C.sum() > 0
    nt = len(g.towns)
    assert np.allclose(C[:nt, nt:], M[:nt, nt:])      # industries: public transport only


# ---------------------------------------------------------------- candidates

def _cands(year=1900, **kw):
    pl = planner()
    if kw:
        return generate(pl.game, pl.vehicles, pl, year, set(), set(), **kw)
    if year not in _CANDS:
        _CANDS[year] = generate(pl.game, pl.vehicles, pl, year, set(), set())
    return _CANDS[year]


def test_candidates_1900():
    t0 = time.time()
    cs = _cands(1900)
    p = PassengerParams()
    assert cs, "no passenger candidates"
    lines = {c.meta.get("line") for c in cs}
    assert {"intra", "intercity", "workers", "bundle"} <= lines, lines
    nt = len(planner().game.towns)
    # every town gets an intra-town line candidate, and intercity lines exist
    assert len({c.nodes[0] for c in cs if c.meta.get("line") == "intra"}) == nt
    assert any(c.mode == "road" and c.meta.get("line") == "intercity" for c in cs)
    assert any(c.mode == "rail" and c.meta.get("line") == "intercity" for c in cs)
    for c in cs:
        assert c.kind in ("passenger", "workers")
        assert c.income > 0 and c.volume >= p.min_volume
        assert c.title and c.details and c.vehicles and c.geometry and len(c.geometry) >= 2
        n_road = sum(v.count for v in c.vehicles)
        assert n_road <= 3 * p.max_road_vehicles + p.max_trains * (p.max_wagons + 1), (c.title, n_road)
        assert isinstance(c.not_loss_making, bool)
        assert math.isfinite(c.depreciation)
        assert c.meta.get("capacity", c.volume) + 0.06 >= c.volume or c.meta.get("line") == "bundle"
    for c in cs:
        if c.meta.get("line") != "bundle":
            assert sum(v.count for v in c.vehicles) <= max(p.max_road_vehicles, p.max_trains * (p.max_wagons + 1))
    good = [c for c in cs if c.profit > 0]
    assert good and any(c.not_loss_making for c in good)
    assert any(c.payback_years < 10 for c in cs)
    print(f"  {len(cs)} candidates, {len(good)} profitable, {time.time() - t0:.1f}s")


def test_built_infrastructure_is_free():
    pl = planner()
    g = pl.game
    cs = _cands(1900)
    rail = min((c for c in cs if c.meta.get("line") == "intercity" and c.mode == "rail"), key=lambda c: c.payback_years)
    i, j = rail.nodes
    built = {(min(i, j), max(i, j), "rail")}
    stations = {(i, "rail_pax"), (j, "rail_pax"), (i, "rail")}
    again = [c for c in generate(g, pl.vehicles, pl, 1900, built, stations)
             if c.meta.get("line") == "intercity" and c.mode == "rail" and c.nodes == [i, j]]
    assert again and again[0].infra_capex < 1e-6 and not again[0].pair_keys
    # a built loop removes the town's intra candidates and makes it a feeder for intercity lines
    t = 0
    loop = {(t, "town_loop"), (t, "road_pax")}
    cs2 = generate(g, pl.vehicles, pl, 1900, set(), loop)
    assert not [c for c in cs2 if c.meta.get("line") == "intra" and c.nodes == [t]]
    fed = [c for c in cs2 if c.meta.get("line") == "intercity" and t in c.nodes and c.meta.get("feeders")]
    assert fed


def test_worker_candidates_within_range():
    pl = planner()
    p = PassengerParams()
    cs = _cands(1900, boost_value=lambda k, f: 300_000.0)
    w = [c for c in cs if c.kind == "workers"]
    assert w
    nt = len(pl.game.towns)
    for c in w:
        t, k = c.nodes
        assert t < nt <= k
        a, b = pl.nodes[t], pl.nodes[k]
        assert math.dist((a.x, a.y), (b.x, b.y)) <= p.worker_range_m
        assert not pl.nodes[k].water_only
        if c.boosts:
            assert c.boosts == {(k, "workers"): p.worker_boost_factor}
            assert c.meta["distinct_workers_2y"] >= p.min_workers
            assert abs(c.income - c.meta["ticket_income"] - 300_000.0) < 1e-6
        else:
            assert abs(c.income - c.meta["ticket_income"]) < 1e-6
    assert any(c.boosts for c in w)


# ---------------------------------------------------------------- lines on the existing streets (export v6+)

STREET_TOL_M = 10.0


def _streets():
    pl = planner()
    sp = street_planner(pl.game)
    if sp is None:
        import pytest
        pytest.skip("export without a street network (v6+)")
    return sp


def _dist_to_streets(sp, pts) -> np.ndarray:
    """Distance of points to the real (curved) road geometry of the network."""
    S = sp.net._samples("road", 2.0)
    d, _ = S["tree"].query(np.asarray(pts, float).reshape(-1, 2))
    return d


def _seg_pts(segs, types):
    out = []
    for g in segs or []:
        if g["type"] in types:
            P = np.asarray(g["polyline"], float)
            for a, b in zip(P[:-1], P[1:]):
                n = max(1, int(math.dist(a, b) // 5))
                out += [a + (b - a) * k / n for k in range(n + 1)]
    return np.array(out).reshape(-1, 2)


def test_town_loops_run_on_streets():
    sp = _streets()
    loops = [c for c in _cands(1900) if c.meta.get("line") == "intra"]
    nt = len(planner().game.towns)
    assert len({c.nodes[0] for c in loops}) == nt          # every town has streets for a loop
    for c in loops:
        pax = c.meta["pax"]
        # every stop lies on a street, away from junctions, and the stops are distinct
        st = np.array([[s["x"], s["y"]] for s in pax["stops"]])
        assert len(st) == c.meta["stops"] >= 2
        assert _dist_to_streets(sp, st).max() <= STREET_TOL_M, c.title
        assert min(s["junction_m"] for s in pax["stops"]) >= 10
        assert all(s["street"] and s["edge_id"] for s in pax["stops"])
        # the geometry follows the streets: every vertex within 10 m of a road edge
        G = np.asarray(c.geometry, float)
        assert len(G) >= 3 and _dist_to_streets(sp, G).max() <= STREET_TOL_M, c.title
        # bus loops build no road: only stops, depot and vehicles
        tot = c.meta["segment_totals"]
        if pax["streets_only"]:
            assert tot["new_m"] == 0 and tot["build_cost"] == 0
            assert c.meta["new_road_cost"] == 0
            assert all(g["type"] == "existing" for g in c.meta["segments"])
            P = _seg_pts(c.meta["segments"], ("existing",))
            assert _dist_to_streets(sp, P).max() <= STREET_TOL_M
            assert "Новых дорог не строим" in c.details
        assert "По улицам:" in c.details and pax["route_text"]
        # loop length = what the bus drives; distinct streets <= loop length
        assert abs(pax["length_m"] - c.meta["loop_m"]) <= 1 and pax["unique_m"] <= pax["length_m"] + 1
    road_loops = [c for c in loops if c.mode == "road"]
    streets_only = [c for c in road_loops if c.meta["pax"]["streets_only"]]
    assert len(streets_only) >= 0.9 * len(road_loops)
    assert min(c.infra_capex for c in road_loops) < 600_000      # stops (+ depot), no road construction


def test_loop_stops_cover_town():
    sp = _streets()
    p = PassengerParams()
    for t in town_infos(planner().game)[:10]:
        ts = sp.town_streets(t, p.stop_catchment_m, p.max_intra_stops)
        assert ts is not None and len(ts.order) >= 2
        assert all(b >= a - 1e-12 for a, b in zip(ts.cov, ts.cov[1:]))      # coverage grows with stops
        assert ts.cov[-1] >= 0.8, (t.name, ts.cov)
        xy = sp.S["xy"][ts.order]
        d = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
        assert d[~np.eye(len(xy), dtype=bool)].min() >= 100          # stops are spread out


def test_intercity_bus_on_existing_roads():
    sp = _streets()
    ic = [c for c in _cands(1900) if c.meta.get("line") == "intercity" and c.mode == "road"]
    assert ic
    ex = new = 0.0
    for c in ic:
        tot = c.meta["segment_totals"]
        assert c.meta["segments"] and c.geometry and len(c.geometry) >= 2
        ex += tot["existing_m"] + tot["upgrade_m"]
        new += tot["new_m"]
        # only genuinely new pieces cost money; the existing parts lie on the roads
        assert abs(c.meta["route_build"] - tot["build_cost"]) <= 2 + 1e-3 * tot["build_cost"], c.title
        if tot["new_m"] == 0:
            assert c.meta["route_build"] == 0 and not c.pair_keys
            P = _seg_pts(c.meta["segments"], ("existing",))
            assert _dist_to_streets(sp, P).max() <= STREET_TOL_M, c.title
        st = np.array([[s["x"], s["y"]] for s in c.meta["pax"]["stops"]])
        assert _dist_to_streets(sp, st).max() <= STREET_TOL_M, c.title
        for g in c.meta["segments"]:
            if g["type"] == "new":
                assert "demolition_cost" in g and g["build_cost"] >= g["demolition_cost"]
    assert ex > 0.6 * (ex + new), (ex, new)                  # mostly existing roads
    assert sum(1 for c in ic if c.meta["segment_totals"]["new_m"] == 0) >= 0.4 * len(ic)


def test_worker_stops_on_roads():
    sp = _streets()
    w = [c for c in _cands(1900) if c.kind == "workers" and c.meta.get("pax")]
    assert w
    for c in w:
        st = np.array([[s["x"], s["y"]] for s in c.meta["pax"]["stops"] if s.get("edge") is not None])
        assert len(st) and _dist_to_streets(sp, st).max() <= STREET_TOL_M, c.title


def test_depots_outside_built_up_area_next_to_road():
    sp = _streets()
    from tf3planner import siting
    S = siting.for_game(planner().game)
    seen = 0
    for c in _cands(1900):
        dep = (c.meta.get("pax") or {}).get("depot")
        if not dep:
            continue
        seen += 1
        assert S.built_depth(dep["x"], dep["y"]) == 0, c.title
        assert math.dist((dep["x"], dep["y"]), (dep["road_x"], dep["road_y"])) <= 40
        assert _dist_to_streets(sp, [[dep["road_x"], dep["road_y"]]]).max() <= STREET_TOL_M
    assert seen


def test_bundle_carries_leg_segments():
    b = [c for c in _cands(1900) if c.meta.get("line") == "bundle"]
    assert b
    for c in b[:20]:
        assert c.meta.get("segments") and c.meta.get("segment_totals")
        assert {g["leg"] for g in c.meta["segments"]} <= set(range(len(c.legs)))
        loops = [l for l in c.legs if len(l["nodes"]) == 1]
        assert all(l.get("pax", {}).get("stops") for l in loops)


def _step_of(c):
    """Plan step like planner._build_generic writes it (optionally without the 'pax' data)."""
    vs = [{"model": v.file.rsplit("/", 1)[-1], "name": v.name, "count": v.count, "price": v.price}
          for v in c.vehicles]
    return {"id": "t1", "line": "T1", "year": 1900, "month": 1, "action": "build_line", "kind": c.mode,
            "cargo": c.cargo, "title": c.title, "details": c.details, "geometry": c.geometry,
            "project_kind": c.kind, "vehicles": vs, "cost": c.capex, "income_per_year": c.income,
            "running_cost_per_year": c.running + c.maintenance, "legs": c.legs,
            "segments": c.meta.get("segments"), "segment_totals": c.meta.get("segment_totals"),
            "from": {"type": "town", "id": planner().nodes[c.nodes[0]].id},
            "to": {"type": "town", "id": planner().nodes[c.nodes[-1]].id}}


def test_dossier_lists_street_stops():
    sp = _streets()
    from tf3planner.dossier import Context, build_dossier
    pl = planner()
    ctx = Context(pl.game, None)
    cs = _cands(1900)
    loop = max((c for c in cs if c.meta.get("line") == "intra" and c.mode == "road"), key=lambda c: c.meta["stops"])
    inter = next(c for c in cs if c.meta.get("line") == "intercity" and c.mode == "road"
                 and c.meta["segment_totals"]["new_m"] == 0)
    for c in (loop, inter):
        for with_pax in (True, False):           # plan data, or rebuilt by the dossier from the street network
            step = _step_of(c)
            if with_pax:
                step["pax"] = c.meta["pax"]
            d = build_dossier(pl.game, None, step, [step], ctx=ctx)
            leg = d["legs"][0]
            st = leg["route"]["streets"]
            assert st and st["stops"] and st["streets_only"], c.title
            xy = [[s["x"], s["y"]] for s in leg["stations"]]
            assert len(xy) == len(c.meta["pax"]["stops"]) if c is loop else len(xy) >= 2
            assert _dist_to_streets(sp, xy).max() <= STREET_TOL_M, (c.title, with_pax)
            assert leg["route"]["free"] and leg["money"]["capex"]["route"] == 0
            assert leg["route"]["segment_totals"]["new_m"] == 0
            assert all("на улице:" in s["site_reason"] for s in leg["stations"])
            ins = " ".join(d["instructions"])
            if c is loop:
                assert "Маршрут по улицам:" in ins and "Новых дорог не строим" in ins
                assert [s["no"] for s in st["stops"]] == list(range(1, len(st["stops"]) + 1))
            else:
                assert "новых дорог не строим" in ins
            for dp in leg["depots"]:
                if with_pax and c.meta["pax"].get("depot"):
                    assert (dp["x"], dp["y"]) == (c.meta["pax"]["depot"]["x"], c.meta["pax"]["depot"]["y"])


def test_later_years_and_trams():
    cs = _cands(1925)
    assert any(c.mode == "tram" for c in cs)
    assert any(c.meta.get("line") == "bundle" and c.profit > 0 for c in cs)


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e!r}")
    print("all passed" if not failed else f"{failed} failed")
    sys.exit(1 if failed else 0)
