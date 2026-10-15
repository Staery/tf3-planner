"""Era technology upgrades (tf3planner/upgrades.py): EAC defender/challenger, steam -> diesel era,
track/catenary/road availability years, road upgrade speeds up trips, apply_upgrade bookkeeping,
every line kind is seen. Uses the real export and the cached 1900 routing matrices.

Run: .venv/Scripts/python -X utf8 tests/test_upgrades.py   (or pytest tests/test_upgrades.py)
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tf3planner.planner as P                                    # noqa: E402
from tf3planner import upgrades as U                              # noqa: E402
from tf3planner.candidates import ProjectCandidate, VehicleLine   # noqa: E402
from tf3planner.economics import Consist, booster_info, evaluate_link   # noqa: E402
from tf3planner.routing import RoutingParams, load_game, network_matrix, network_nodes   # noqa: E402
from tf3planner.vehicles import load_vehicles                     # noqa: E402


@pytest.fixture(scope="module")
def pl():
    p = P.Planner.__new__(P.Planner)
    p.game = load_game()
    p.vehicles = load_vehicles(p.game)
    p.horizon = 2030
    p.nodes = network_nodes(p.game)
    p.by_key = {n.key: n for n in p.nodes}
    p.mat = {k: network_matrix(p.game, k, p.game.year, RoutingParams()) for k in ("rail", "road")}
    p.water = None
    p.ind = {i.id: i for i in p.game.industries}
    p.town = {t.id: t for t in p.game.towns}
    p.boosters = {i.id: booster_info(p.game, i) for i in p.game.industries}
    p.generators = []
    p.towns = None                     # static towns (planner.TOWN_GROWTH model not needed here)
    return p


def _veh(pl, prefix: str, year: int | None = None, carries: str | None = None):
    for v in pl.vehicles:
        if v.name.startswith(prefix) and v.price > 1000 and (year is None or v.available(year)) \
                and (carries is None or v.carries(carries)):
            return v
    raise LookupError(prefix)


def _pair(pl, mode: str, cargo: str, lo: float, hi: float):
    m = pl.mat[mode]
    src = [n for n in pl.nodes if n.kind == "industry" and cargo in pl.ind[n.id].outputs]
    dst = [n for n in pl.nodes if n.kind == "industry" and cargo in pl.ind[n.id].inputs]
    for f in src:
        for s in dst:
            L = float(m.length[f.index, s.index])
            if np.isfinite(L) and lo < L < hi:
                return f, s, L
    raise LookupError((mode, cargo))


def _line(pl, mode: str, cargo: str, year: int, units, n: int, volume: float, lo: float, hi: float):
    """A sim with one direct line built in `year` with the given consist."""
    f, s, L = _pair(pl, mode, cargo, lo, hi)
    g = pl.game
    sim = pl.initial_state()
    sim.month = (year - g.year) * 12
    sim.cash = 1e11
    ev = evaluate_link(g, pl.vehicles, g.cargo_by_key[cargo], P._endpoint(f), P._endpoint(s), mode, volume, year,
                       route_m=L, route_build_cost=0.0, route_maintenance=0.0)
    cap = sum(v.carries(cargo) * k for v, k in units)
    c = Consist(mode, units, cap, min(v.top_speed for v, _ in units), sum(v.price * k for v, k in units),
                sum(v.running_cost * k for v, k in units), min(v.load_speed for v, _ in units))
    ev.consist, ev.n_consists = c, n
    ev.vehicle_capex, ev.running = n * c.price, n * c.running_cost
    pl._build(sim, P.Candidate(f.index, s.index, cargo, mode, ev, None, 0.0, 0.0))
    return sim


def _set_year(sim, year: int):
    sim.month = (year - sim.game.year) * 12


# --------------------------------------------------------------------------- EAC logic

def test_eac_identical_fleet_never_replaced():
    price, run, life = 10e6, 1.5e6, 60
    for age in (0, 5, 20):
        mc = U.mc_keep(price, age, life, run, effect=2.0)
        eac = U.eac_new(price, run, life, 40, effect=2.0)
        assert eac > mc                       # a new copy of the same fleet loses the resale haircut
        assert not U.should_replace(mc, eac)


def test_eac_aging_raises_defender_cost():
    price, run, life = 10e6, 1.5e6, 60
    young = U.mc_keep(price, 10, life, run, effect=2.0)
    old = U.mc_keep(price, 70, life, run, effect=2.0)
    assert old > young
    assert U.aging_mult(29, life, 2.0) == pytest.approx(1.0)      # nothing before half the lifespan
    assert U.aging_mult(60, life, 2.0) == pytest.approx(1.3)      # VeryHard: +30 % at end of life
    assert U.aging_speed(60, life, 2.0) == pytest.approx(0.9)
    # a cheaper-to-run challenger wins only by more than the anti-churn margin
    assert U.should_replace(old, 0.5 * old)
    assert not U.should_replace(old, 0.99 * old)


def test_replacement_only_when_cheaper(pl):
    dmg = _veh(pl, "DMG Cannstatt")
    sim = _line(pl, "road", "clay", 1900, [(dmg, 1)], 250, 500.0, 8000, 15000)
    _set_year(sim, 1902)
    assert U.fleet_replacement(pl, sim, 1902) == []               # younger than MIN_FLEET_AGE_YEARS
    _set_year(sim, 1906)
    assert U.fleet_replacement(pl, sim, 1906) == []               # nothing better on the market yet
    _set_year(sim, 1925)                                          # AMO-F-15 (1924, 40 km/h, 12 units)
    cands = U.fleet_replacement(pl, sim, 1925)
    assert cands, "a 1924 truck must beat a horse-era DMG fleet"
    for c in cands:
        assert c.kind == "upgrade" and c.meta["upgrade"] == "fleet"
        assert c.meta["eac_new"] < c.meta["mc_keep"]
        assert c.meta["eac_saving"] > U.MIN_SAVING_ABS
        assert c.meta["n_to"] < c.meta["n_from"]                   # fewer, faster, bigger trucks
        assert c.vehicle_capex == pytest.approx(max(0.0, c.meta["purchase"] - c.meta["resale"]), rel=1e-6, abs=1)


def test_apply_fleet_updates_project_and_cash(pl):
    dmg = _veh(pl, "DMG Cannstatt")
    sim = _line(pl, "road", "clay", 1900, [(dmg, 1)], 250, 500.0, 8000, 15000)
    _set_year(sim, 1925)
    c = U.fleet_replacement(pl, sim, 1925)[0]
    pr = sim.projects[0]
    cash0, run0 = sim.cash, pr.running
    delta = U.apply_upgrade(pl, sim, c)
    assert sim.cash == pytest.approx(cash0 - c.meta["net_cost"])
    assert pr.consist_ids == sorted((v.id, k) for v, k in c.meta["_opt"].units)
    assert pr.n_consists == c.meta["n_to"] and pr.bought_month == sim.month
    assert pr.running == pytest.approx(run0 + c.running)
    assert sim.steps[-1]["action"] == "replace_vehicles" and sim.steps[-1]["line"] == pr.id
    assert delta["lines"][0]["n"] == c.meta["n_to"]
    _set_year(sim, 1929)
    assert not U.fleet_replacement(pl, sim, 1929)                 # fresh fleet is not replaced again


def test_no_obsolete_challengers(pl):
    """Horse cabs leaving the market in 1915 are never bought in 1909 (cannot be repurchased for growth)."""
    bus = _veh(pl, "L'Obéissante")
    sim = _line(pl, "road", "clay", 1900, [(_veh(pl, "DMG Cannstatt"), 1)], 50, 100.0, 8000, 15000)
    _set_year(sim, 1909)
    ctx = U._Ctx(pl, sim, 1909)
    seg = U._all_segments(ctx)[0]
    for units in U._candidate_units(ctx, seg, True, 100.0, None):
        assert all(v.year_to == 0 or v.year_to >= 1909 + U.MIN_MODEL_YEARS_LEFT for v, _ in units)
    assert not U._model_ok(ctx, [(_veh(pl, "Дрожки"), 1)]) and U._model_ok(ctx, [(bus, 1)])


# --------------------------------------------------------------------------- steam -> diesel / electric

def test_steam_to_diesel_in_plausible_era(pl):
    loco = _veh(pl, "Тип 4-6-0")
    wagon = _veh(pl, "Грузовой вагон 1913", carries="logs")
    sim = _line(pl, "rail", "logs", 1913, [(loco, 1), (wagon, 20)], 2, 400.0, 15000, 25000)
    first_modern = None
    tractions = []
    for year in range(1914, 2031):
        _set_year(sim, year)
        for c in U.yearly_upgrades(pl, sim):
            if c.meta["upgrade"] == "fleet":
                tractions.append((year, c.meta["traction_to"]))
                if c.meta["traction_to"] in ("diesel", "electric") and first_modern is None:
                    first_modern = year
    assert first_modern is not None, f"steam kept until 2030: {tractions}"
    # first diesel locos 1924/1930; a 1913 steam fleet ages from 1963 (half of its 100-year life)
    assert 1940 <= first_modern <= 2030, tractions
    assert all(t == "steam" for y, t in tractions if y < 1924)


def test_electric_locos_need_catenary(pl):
    loco = _veh(pl, "Тип 4-6-0")
    wagon = _veh(pl, "Грузовой вагон 1913", carries="logs")
    sim = _line(pl, "rail", "logs", 1913, [(loco, 1), (wagon, 20)], 2, 400.0, 15000, 25000)
    _set_year(sim, 1950)
    ctx = U._Ctx(pl, sim, 1950)
    seg = U._all_segments(ctx)[0]
    need = U._need(ctx, seg)[0]
    plain = U._candidate_units(ctx, seg, U._electric_ok(ctx, seg, None), need, None)
    assert plain and not any(U.needs_catenary(pl.game, v) for units in plain for v, _ in units)
    pair = seg.legs[0].pair
    el = {pair: U.InfraState("simple", True)}
    assert U._electric_ok(ctx, seg, el)
    wired = U._candidate_units(ctx, seg, True, need, el)
    assert any(U.needs_catenary(pl.game, v) for units in wired for v, _ in units)


# --------------------------------------------------------------------------- infrastructure

def test_track_upgrades_respect_availability_years(pl):
    loco = _veh(pl, "Тип 4-6-0")
    wagon = _veh(pl, "Грузовой вагон 1913", carries="logs")
    sim = _line(pl, "rail", "logs", 1913, [(loco, 1), (wagon, 20)], 2, 400.0, 15000, 25000)
    for year, ways, elec in ((1915, set(), False), (1925, {"simple"}, True), (1935, {"simple", "standard"}, True),
                             (1979, {"simple", "standard"}, True), (1985, {"simple", "standard", "high_speed"}, True)):
        _set_year(sim, year)
        ctx = U._Ctx(pl, sim, year)
        ts = U._way_targets(ctx, "rail", U.InfraState("simple", False))
        assert {t.way for t in ts} == ways, (year, ts)
        assert any(t.electrified for t in ts) == elec
        assert all(ctx.way(t.way).year_from <= year for t in ts)
        for c in U.infrastructure_upgrades(pl, sim, year):
            assert c.kind == "upgrade" and c.meta["way_year_from"] <= year if "way_year_from" in c.meta else True
            if c.meta.get("electrified"):
                assert year >= pl.game.catenary_year
    # roads: only the old templates before 1940
    _set_year(sim, 1939)
    assert U._way_targets(U._Ctx(pl, sim, 1939), "road", U.InfraState("country_old_small")) == []
    _set_year(sim, 1941)
    assert U._way_targets(U._Ctx(pl, sim, 1941), "road", U.InfraState("country_old_small"))


def test_road_upgrade_improves_trip_time(pl):
    man = _veh(pl, "MAN 19.304", 1970, carries="clay")
    sim = _line(pl, "road", "clay", 1966, [(man, 1)], 5, 500.0, 8000, 15000)
    _set_year(sim, 1970)
    seg = U.lines(pl, sim)[0]
    pair = seg.legs[0].pair
    old = U.segment_cycle_s(pl, sim, seg)
    country = U.segment_cycle_s(pl, sim, seg, overrides={pair: U.InfraState("country_new_small")})
    highway = U.segment_cycle_s(pl, sim, seg, overrides={pair: U.InfraState("highway_new_small")})
    assert country < 0.85 * old and highway < 0.85 * old          # 60 -> 80 / 100 km/h limit
    assert highway == pytest.approx(country, rel=0.05)            # the truck itself tops out at 80 km/h
    dmg = _veh(pl, "DMG Cannstatt")
    slow = U.segment_cycle_s(pl, sim, seg, units=[(dmg, 1)])
    slow_hw = U.segment_cycle_s(pl, sim, seg, units=[(dmg, 1)], overrides={pair: U.InfraState("highway_new_small")})
    assert slow_hw == pytest.approx(slow, rel=0.01)               # a 25 km/h vehicle gains nothing


def test_road_upgrade_timed_by_first_year_return(pl):
    gaz = _veh(pl, "ГАЗ-3307", 1990, carries="clay")
    sim = _line(pl, "road", "clay", 1989, [(gaz, 1)], 30, 2000.0, 8000, 15000)
    _set_year(sim, 1993)
    cands = [c for c in U.infrastructure_upgrades(pl, sim, 1993) if c.meta["upgrade"] == "road"]
    assert cands, "90 km/h trucks on a 60 km/h road: the road upgrade must pay"
    c = cands[0]
    assert c.meta["first_year_return"] >= U.LAMBDA and c.meta["npv"] > 0
    assert c.infra_capex == pytest.approx(c.meta["construction"])
    U.apply_upgrade(pl, sim, c)
    pair = c.meta["pairs"][0]
    assert sim.infra[pair].way == c.meta["to_way"]
    assert sim.steps[-1]["action"] in ("upgrade_track", "replace_vehicles")
    assert not [x for x in U.infrastructure_upgrades(pl, sim, 1993) if x.meta["upgrade"] == "road"
                and x.meta["to_way"] == c.meta["to_way"]]


# --------------------------------------------------------------------------- every line kind

def test_generic_line_kinds_are_seen(pl):
    sim = pl.initial_state()
    sim.month = 48 * 12
    sim.cash = 1e10
    bus = _veh(pl, "Дилижанс")
    dmg = _veh(pl, "DMG Cannstatt")
    loco = _veh(pl, "Тип 4-6-0")
    wag = _veh(pl, "Грузовой вагон 1913", carries="logs")
    towns = [n for n in pl.nodes if n.kind == "town"]
    f, s, L = _pair(pl, "rail", "logs", 15000, 25000)
    base = dict(details="", infra_capex=0.0, maintenance=0.0)
    # passenger intercity bus line
    pax = ProjectCandidate(kind="passenger", mode="road", title="bus", nodes=[towns[0].index, towns[1].index],
                           cargo="passengers", volume=400.0, income=1e6, running=10 * bus.running_cost,
                           vehicle_capex=10 * bus.price, vehicles=[VehicleLine(bus.id, bus.name, bus.file, 10, bus.price)],
                           meta={"id": "L1", "line": "intercity", "route_m": 9000, "cycle_s": 3000, "start_month": 0},
                           **base)
    # multimodal chain: road feeder leg (step-format vehicles) + rail trunk leg
    mm = ProjectCandidate(kind="multimodal", mode="multi", title="mm", nodes=[f.index, s.index], cargo="logs",
                          volume=300.0, income=5e6, running=1e6, vehicle_capex=1e7, vehicles=[],
                          legs=[{"role": "feeder", "mode": "road", "from": {"key": f.index}, "to": {"key": "hub:1"},
                                 "length_m": 4000, "volume": 300, "n_consists": 40, "income": 1e6,
                                 "vehicles": [{"model": dmg.file.rsplit("/", 1)[-1], "name": dmg.name, "count": 40,
                                               "price": dmg.price}]},
                                {"role": "trunk", "mode": "rail", "from": {"key": "hub:1"}, "to": {"key": s.index},
                                 "length_m": L, "volume": 300, "n_consists": 2, "income": 4e6,
                                 "vehicles": [{"model": loco.file.rsplit("/", 1)[-1], "name": loco.name, "count": 2,
                                               "price": loco.price},
                                              {"model": wag.file.rsplit("/", 1)[-1], "name": wag.name, "count": 30,
                                               "price": wag.price}]}],
                          meta={"id": "L2", "start_month": 0}, **base)
    # rail tour over 2 legs
    tour = ProjectCandidate(kind="cargo", mode="rail", title="tour", nodes=[f.index, s.index], cargo="logs",
                            volume=400.0, income=6e6, running=1e6, vehicle_capex=1e7,
                            vehicles=[VehicleLine(loco.id, loco.name, loco.file, 2, loco.price),
                                      VehicleLine(wag.id, wag.name, wag.file, 40, wag.price)],
                            legs=[{"from": f.index, "to": s.index, "length_m": L, "load": 400},
                                  {"from": s.index, "to": f.index, "length_m": L, "load": 0}],
                            meta={"id": "L3", "generator": "tours", "n_consists": 2, "cargos": ["logs"],
                                  "peak_load": 400, "trip_s": 2000, "start_month": 0}, **base)
    # workers line (planner.worker_candidates shape)
    wk = ProjectCandidate(kind="workers", mode="road", title="w", nodes=[towns[0].index, f.index], cargo=None,
                          volume=10.0, income=0.0, running=2 * bus.running_cost, vehicle_capex=2 * bus.price,
                          vehicles=[VehicleLine(bus.id, bus.name, bus.file, 2, bus.price)],
                          meta={"id": "L4", "start_month": 0}, **base)
    sim.generic += [pax, mm, tour, wk]
    segs = U.lines(pl, sim)
    by = {(s_.line_id, s_.idx): s_ for s_ in segs}
    assert by[("L1", 0)].passenger and by[("L1", 0)].n == 10
    assert by[("L2", 0)].mode == "road" and by[("L2", 0)].n == 40
    assert by[("L2", 1)].mode == "rail" and by[("L2", 1)].n == 2 and dict((v.name, k) for v, k in by[("L2", 1)].units)[wag.name] == 15
    assert by[("L3", 0)].legs and len(by[("L3", 0)].legs) == 2 and not by[("L3", 0)].round_trip
    assert ("L4", 0) in by and by[("L4", 0)].passenger
    # 1948: the aged DMG feeder (25 km/h, 6 units) is replaced; the multimodal leg keeps its step format
    cands = {(c.meta["line"], c.meta["segment"]): c for c in U.fleet_replacement(pl, sim, sim.year)}
    assert ("L2", 0) in cands
    c = cands[("L2", 0)]
    U.apply_upgrade(pl, sim, c)
    leg = mm.legs[0]
    assert leg["n_consists"] == c.meta["n_to"] and all("model" in x for x in leg["vehicles"])
    assert mm.meta["fleet_bought"][0] == sim.month
    assert mm.legs[1]["n_consists"] == 2                           # the trunk leg is untouched


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-x"]))
