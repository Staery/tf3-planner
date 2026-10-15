"""Sanity tests for tf3planner.capacity. Run with pytest or directly: python tests/test_capacity.py"""
from __future__ import annotations

import math
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner import capacity as C  # noqa: E402
from tf3planner.economics import Endpoint, evaluate_link  # noqa: E402

Y = C.YEAR_S_DEFAULT


@lru_cache(maxsize=1)
def _data():
    from tf3planner.model import Game
    from tf3planner.vehicles import load_vehicles
    g = Game.load()
    return g, load_vehicles(g)


def _link(mode: str, year: int, km: float, volume: float):
    g, vs = _data()
    src = Endpoint("industry", -1, "a", 0.0, 0.0, 0.0)
    dst = Endpoint("industry", -2, "b", km * 1000.0, 0.0, 0.0)
    return evaluate_link(g, vs, g.cargo_by_key["coal"], src, dst, mode, volume, year, route_m=km * 1250.0)


# --------------------------------------------------------------------------- loading / terminals

def test_loading_time_model():
    base = C.loading_time_s(48, 6, 0.0625)
    assert math.isclose(base, 128.0)                                  # mechanics.md §4.2 example
    assert math.isclose(C.loading_time_s(48, 6, 0.0625, True), base / 2)
    assert math.isclose(C.loading_time_s(48, 6, 0.0625, False, True), base / 2)
    assert math.isclose(C.loading_time_s(48, 6, 0.0625, True, True), base / 4)
    assert math.isclose(C.loading_time_s(48, 6, 0.0625, True, class_mismatch=True), base * 4)
    assert C.loading_time_s(96, 6) > C.loading_time_s(48, 6) > C.loading_time_s(24, 6)
    # wagons load in parallel: 12 wagons x 8 = one wagon's time; overlength is 4x slower
    assert math.isclose(C.loading_time_s(96, 2, n_units=12), C.loading_time_s(8, 2))
    assert math.isclose(C.loading_time_s(96, 2, n_units=12, overlength_fraction=0.1), 4 * C.loading_time_s(8, 2))
    assert C.loading_time_s(0, 6) == 0.0


def test_terminals():
    occ = [C.terminal_occupancy(n, 600, 40) for n in range(1, 60)]
    assert all(b > a for a, b in zip(occ, occ[1:]))
    assert C.terminals_needed(0.0) == 1
    assert C.terminals_needed(0.7) == 1
    assert C.terminals_needed(0.71) == 2
    assert C.terminals_needed(2.1, 0.7) == 3
    ts = [C.terminals_needed(o) for o in occ]
    assert all(b >= a for a, b in zip(ts, ts[1:]))


# --------------------------------------------------------------------------- road

def test_road_speed_factor_monotone():
    sf = [C.road_speed_factor(x / 10) for x in range(0, 30)]
    assert sf[0] == 1.0 and all(b < a for a, b in zip(sf[1:], sf[2:]))
    assert C.road_speed_factor(1.0, 2.0) < C.road_speed_factor(1.0, 1.0)
    assert C.road_lane_capacity(20) > C.road_lane_capacity(5)


def test_road_link_small_ok_500_fails():
    small = C.road_link_ok(5, 900, 4000, 8.0, dwell_s=40)
    assert small.ok, small.reason
    big = C.road_link_ok(500, 900, 4000, 8.0, dwell_s=40)
    assert not big.ok
    # 500 trucks fail even on a long route with tiny dwell (fleet cap)
    assert not C.road_link_ok(500, 20000, 50000, 8.0, dwell_s=10).ok
    # more vehicles never improve the check
    a = C.road_link_ok(40, 900, 4000, 8.0, dwell_s=40)
    b = C.road_link_ok(80, 900, 4000, 8.0, dwell_s=40)
    assert b.vc > a.vc and b.speed_factor <= a.speed_factor and b.terminals >= a.terminals


def test_no_500_truck_link_passes_apply_capacity():
    g, vs = _data()
    for year, km, vol in [(1900, 12, 1000), (1900, 24, 300), (1925, 48, 3000), (2000, 48, 3000)]:
        ev = _link("road", year, km, vol)
        adj = C.apply_capacity(ev, None, g)
        if adj["n_vehicles"] >= 500:
            assert not adj["feasible"], (year, km, vol, adj["n_vehicles"])
        if adj["feasible"]:
            assert adj["n_vehicles"] <= C.MAX_ROAD_VEHICLES
    ev = _link("road", 1900, 12, 1000)
    adj = C.apply_capacity(ev, None, g)
    assert ev.n_consists > 500 and not adj["feasible"] and adj["reason"]


# --------------------------------------------------------------------------- rail

def test_single_track_saturates():
    L, v, tl = 20_000.0, 13.9, 180.0
    caps = [C.single_track_capacity(L, v, k, tl) for k in range(0, 200)]
    assert all(b > a for a, b in zip(caps, caps[1:]))                 # more loops -> more capacity
    limit = C.MAX_OCC * Y / (2 * C.crossing_margin_s(v, tl))
    assert caps[-1] < limit                                           # bounded by the crossing margin
    assert C.single_track_limit(L, v, tl) <= limit
    assert C.loops_needed(1e6, L, v, tl) is None                      # saturates
    assert C.double_track_capacity(v, tl) > C.single_track_limit(L, v, tl)


def test_loops_needed_monotone():
    L, v, tl = 30_000.0, 13.9, 180.0
    prev = 0
    for tpy in (0.5, 1, 2, 3, 4, 5):
        k = C.loops_needed(tpy, L, v, tl)
        if k is None:
            break
        assert k >= prev
        assert C.single_track_capacity(L, v, k, tl) >= tpy - 1e-9
        if k > 0:
            assert C.single_track_capacity(L, v, k - 1, tl) < tpy
        prev = k
    # each section holds one train: 6 trains running need >= 5 loops
    assert (C.loops_needed(0.1, L, v, tl, trains_running=6) or 0) >= 5
    # platoons raise capacity for the same loops
    assert C.single_track_capacity(L, v, 5, tl, platoon=2) > C.single_track_capacity(L, v, 5, tl)


def test_double_track_threshold_and_plan():
    L, v, tl = 20_000.0, 13.9, 180.0
    thr = C.double_track_threshold(L, v, tl, cost_per_m=90)
    assert 0 < thr < C.double_track_capacity(v, tl)
    low = C.rail_line_plan(0.5 * thr, L, v, tl, 90, 15, n_trains=3, running_share=0.8)
    high = C.rail_line_plan(1.5 * thr, L, v, tl, 90, 15, n_trains=30, running_share=0.8)
    assert low.kind in ("single", "single+loops")
    assert high.kind in ("double", "infeasible")
    lc, _ = C.loop_cost(90, tl)
    assert lc == (tl + C.LOOP_MARGIN_M + 2 * C.SWITCH_ZONE_M) * 90
    assert C.rail_line_plan(100, L, v, tl, 90, n_trains=60).kind == "infeasible"
    assert C.rail_line_plan(0.2, L, v, tl, 90, n_trains=1).kind == "single"


# --------------------------------------------------------------------------- maintenance

def test_maintenance_plan():
    g, _ = _data()
    from tf3planner import facilities
    cat = facilities.Catalogue(g)
    plans = [C.maintenance_plan(cat, "road", 1900, n, 5000) for n in (1, 12, 13, 40, 150)]
    assert all(p.depots >= 1 for p in plans)
    assert all(p.pool >= n for p, n in zip(plans, (1, 12, 13, 40, 150)))
    assert all(b.capex >= a.capex for a, b in zip(plans, plans[1:]))
    assert plans[0].maint_buildings == 0
    full = C.maintenance_plan(cat, "road", 1900, 5, 24_000, coverage="full")
    assert full.depots + full.maint_buildings >= math.ceil(24_000 / (2 * 600))
    rail = C.maintenance_plan(cat, "rail", 1900, 20, 10_000)
    assert rail.pool >= 20
    c, u = C.maint_building_price(cat, "road", 1900)
    assert c > 0 and u > 0


def test_maintenance_matches_facilities_and_dossier_rule():
    """One price list and one counting rule: capacity.maintenance_plan == facilities.depot
    (what tours / multimodal / passengers / the dossier use). Script prices x1.5 VeryHard:
    road maintenance building 60 000 -> 90 000 (the export's menu attribute 180 000 is wrong)."""
    g, _ = _data()
    from tf3planner import facilities
    cat = facilities.Catalogue(g)
    assert C.maint_building_price(cat, "road", 1900) == facilities.maint_building(cat, "road", 1900) == (90_000, 15_000)
    assert C.maint_building_price(cat, "rail", 1900) == (360_000, 60_000)
    for mode, n in (("road", 1), ("road", 12), ("road", 13), ("road", 34), ("road", 64), ("rail", 6), ("rail", 20),
                    ("water", 23)):
        mp = C.maintenance_plan(cat, mode, 1900, n, 5000)
        dp = facilities.depot(cat, mode, 1900, n)
        assert (mp.depots, mp.maint_buildings) == facilities.maintenance_counts(mode, n), (mode, n)
        assert math.isclose(mp.capex, dp.cost) and math.isclose(mp.upkeep, dp.maintenance), (mode, n)
    assert facilities.maintenance_counts("road", 34) == (1, 2)
    assert facilities.depot(cat, "road", 1900, 34).name == "Дорожное депо + 2 здание(я) техобслуживания"


def test_truck_station_price_two_platforms_per_terminal():
    """street.zip modular_street_station: a terminal = 2 cargo_platform modules, one
    entrance_exit per station -> 1 universal terminal 66 000 x 1.5 = 99 000 (upkeep 16 500)."""
    g, _ = _data()
    from tf3planner import facilities
    cat = facilities.Catalogue(g)
    for t, cost, upkeep in ((1, 99_000, 16_500), (2, 189_000, 31_500), (4, 369_000, 61_500)):
        st = facilities.station(cat, "road", 1900, ["UNIVERSAL", "BULK"], 80, t)
        assert (st.cost, st.maintenance) == (cost, upkeep), (t, st)
    # specialized platforms: both modules of every terminal (60 000 vs 30 000 at scale 1)
    pc, pm = C._special_premium(cat, "road", 1900, "bulk", 1, 1)
    assert pc == 2 * (90_000 - 45_000) and pm == 2 * (15_000 - 7_500)
    assert facilities.road_pax_station(cat, 1900, 1)[0] == 9_000 + 2 * 90_000


def test_apply_capacity_recovers_economics_trip():
    """apply_capacity must rebuild the running time with economics' own load model: with no
    congestion and no way-speed limit its trip = economics trip + manoeuvre at both ends."""
    g, _ = _data()
    for km, vol in ((6, 30), (12, 60)):
        ev = _link("road", 1900, km, vol)
        adj = C.apply_capacity(ev, None, g, platform="universal")
        if any("ограничена" in n for n in adj["notes"]) or adj["speed_factor"] < 0.999:
            continue
        assert math.isclose(adj["trip_time_s"], ev.trip_time_s + 2 * C.MANOEUVRE_S["road"], rel_tol=1e-3), \
            (km, vol, adj["trip_time_s"], ev.trip_time_s)
        assert adj["n_vehicles"] >= ev.n_consists


# --------------------------------------------------------------------------- apply_capacity

def test_apply_capacity_rail_and_apply_to():
    g, _ = _data()
    ev = _link("rail", 1900, 24, 300)
    adj = C.apply_capacity(ev, None, g)
    for k in ("feasible", "reason", "speed_factor", "extra_infra_capex", "extra_maintenance", "n_vehicles",
              "terminals", "track", "maintenance_plan", "score"):
        assert k in adj
    assert 0 < adj["speed_factor"] <= 1
    assert adj["n_vehicles"] >= 1 and adj["terminals"] >= 1
    assert adj["track"]["kind"] in ("single", "single+loops", "double")
    if adj["n_vehicles"] > 1:
        assert adj["extra_track_capex"] > 0
    ev2 = C.apply_to(ev, adj)
    assert ev2.n_consists == adj["n_vehicles"]
    assert math.isclose(ev2.infra_capex, ev.infra_capex + adj["extra_infra_capex"])
    assert ev.n_consists == _link("rail", 1900, 24, 300).n_consists       # input not mutated


def test_apply_capacity_volume_monotone():
    g, _ = _data()
    prev_n, prev_t = 0, 0
    for vol in (50, 100, 300, 1000):
        adj = C.apply_capacity(_link("road", 1950, 6, vol), None, g, platform="universal")
        assert adj["n_vehicles"] >= prev_n and adj["terminals"] >= prev_t
        prev_n, prev_t = adj["n_vehicles"], adj["terminals"]


def test_breakeven_table():
    g, vs = _data()
    rows = C.breakeven_table(g, vs, 1900, volumes=(50, 1000), distances_km=(3, 24))
    assert len(rows) == 4
    cell = {(r["dist_km"], r["volume"]): r for r in rows}
    assert cell[(3, 50)]["winner"] == "road"                             # tiny volume: trucks
    assert cell[(24, 1000)]["winner"] == "rail"                          # long and heavy: rail
    assert not cell[(24, 1000)]["road"]["feasible"]
    summ = C.breakeven_summary(rows)
    assert summ[24] == 1000 and summ[3] in (1000, None)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
