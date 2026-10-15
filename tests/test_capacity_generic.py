"""Tests for tf3planner.capacity_generic (capacity layer for tours, chains, passengers, ...) and the
plan/dossier consistency of depot and maintenance-building counts.
Run with pytest or directly: python tests/test_capacity_generic.py"""
from __future__ import annotations

import collections
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from tf3planner import capacity as C  # noqa: E402
from tf3planner import capacity_generic as CG  # noqa: E402
from tf3planner import facilities  # noqa: E402
from tf3planner.candidates import ProjectCandidate, VehicleLine  # noqa: E402
from tf3planner.model import DATA  # noqa: E402

pytestmark = pytest.mark.skipif(not (DATA / "export.json").exists(), reason="no export.json")

YEAR = 1900


@lru_cache(maxsize=1)
def _data():
    from tf3planner.model import Game
    from tf3planner.vehicles import load_vehicles
    g = Game.load()
    vs = load_vehicles(g)
    return g, vs, facilities.Catalogue(g)


def _truck(cargos=("clay", "bricks")):
    _, vs, _ = _data()
    ok = [v for v in vs if v.carrier == "road" and v.role == "truck" and v.available(YEAR) and v.price > 1000
          and all(v.carries(c) for c in cargos)]
    return min(ok, key=lambda v: v.price)


def _bus():
    _, vs, _ = _data()
    ok = [v for v in vs if v.carrier == "road" and v.role == "bus" and v.available(YEAR) and v.price > 1000
          and v.carries("passengers")]
    return min(ok, key=lambda v: v.price)


def _rail_set(cargo="coal"):
    _, vs, _ = _data()
    loco = max((v for v in vs if v.carrier == "rail" and v.role == "loco" and v.available(YEAR) and v.price > 1000),
               key=lambda v: v.power_kw)
    wagon = max((v for v in vs if v.carrier == "rail" and v.role == "wagon" and v.available(YEAR) and v.carries(cargo)),
                key=lambda v: v.carries(cargo))
    return loco, wagon


def _tour(n: int, volume: float, trip_s: float = 4000.0, cycle_m: float = 20_000.0) -> ProjectCandidate:
    """A road loop tour 0 -> 1 -> 2 -> 0 like tours.to_candidate makes (clay 0->1, bricks 1->2)."""
    v = _truck()
    flows = [{"from": 0, "to": 1, "cargo": "clay", "volume": volume, "income_per_unit": 1000},
             {"from": 1, "to": 2, "cargo": "bricks", "volume": volume, "income_per_unit": 1000}]
    return ProjectCandidate(
        kind="cargo", mode="road", title="test tour", details=f"Маршрут. Состав: {n}× {v.name}, пиковая загрузка.",
        nodes=[0, 1, 2], cargo="bricks", volume=2 * volume, income=1e6, running=n * v.running_cost,
        maintenance=100_000.0, vehicle_capex=n * v.price, infra_capex=1_000_000.0,
        vehicles=[VehicleLine(v.id, v.name, v.file, n, v.price)],
        legs=[{"from": 0, "to": 1, "length_m": 8000, "load": volume}, {"from": 1, "to": 2, "length_m": 8000, "load": volume},
              {"from": 2, "to": 0, "length_m": 4000, "load": 0.0}],
        pair_keys=[(0, 1, "road"), (1, 2, "road"), (0, 2, "road")], station_keys=[(0, "road"), (1, "road"), (2, "road")],
        meta={"generator": "tours", "n_consists": n, "consist": v.name, "cargos": ["bricks", "clay"],
              "trip_s": trip_s, "cycle_m": cycle_m, "visits": [0, 1, 2], "tour": [0, 1, 2], "flows": flows,
              "peak_load": volume, "topology": "loop"})


def _check(pc, **kw):
    g, vs, cat = _data()
    return CG.check_candidate(g, cat, pc, YEAR, vehicles=vs, **kw)


# --------------------------------------------------------------------------- maintenance

def test_tour_free_depot_still_needs_maintenance_buildings():
    """tours.evaluate_line drops the whole depot (incl. maintenance buildings) when one stop already
    has a station; the pool of 34 trucks still needs 1 depot + 2 maintenance buildings."""
    pc = _tour(34, 5.0)
    own = _check(pc)                                        # depot priced by the generator: nothing to add
    m = own.meta["capacity_check"]["maintenance"][0]
    assert (m["vehicles"], m["depots"], m["maint_buildings"], m["depot_free"]) == (34, 1, 2, False)
    assert own.capex == pytest.approx(pc.capex)
    shared = _check(pc, built_stations={(0, "road")})       # existing depot reused: buildings are not free
    m = shared.meta["capacity_check"]["maintenance"][0]
    assert (m["depots"], m["maint_buildings"], m["depot_free"]) == (1, 2, True)
    mc, mu = facilities.maint_building(None, "road", YEAR)
    assert shared.infra_capex - pc.infra_capex == pytest.approx(2 * mc)
    assert shared.maintenance - pc.maintenance == pytest.approx(2 * mu)
    assert any("техобслуживания" in n for n in shared.meta["capacity_notes"])
    assert "Пропускная способность" in shared.details


def test_passenger_bundle_part_with_shared_depot():
    """Intercity part of a bundle sees the loop's stop as built (passengers.bundle_candidates), so it
    priced no depot; 16 buses still need a maintenance building (the dossier counts it too)."""
    b = _bus()
    loop = {"title": "loop", "mode": "road", "kind": "passenger", "nodes": [3], "volume": 50.0,
            "vehicles": [vars(VehicleLine(b.id, b.name, b.file, 1, b.price))],
            "details": "Кольцевая линия по городу X: 3 остановок (малые, 45 000 каждая), кольцо ~1.5 км, круг 600 с, "
                       "интервал 600 с. Пассажиров 50/год"}
    inter = {"title": "inter", "mode": "road", "kind": "passenger", "nodes": [3, 4], "volume": 30.0,
             "vehicles": [vars(VehicleLine(b.id, b.name, b.file, 16, b.price))],
             "details": "маршрут 6.0 км (дорога уже есть). Состав: 16× x, круг 2700 с, интервал 170 с. "
                        "Станции: малая остановка в каждом городе. Пассажиров 30/год"}
    pc = ProjectCandidate(kind="passenger", mode="multi", title="bundle", details="", nodes=[3, 4], cargo="passengers",
                          volume=80, income=1e5, running=1e4, maintenance=1e4, vehicle_capex=17 * b.price,
                          infra_capex=500_000, vehicles=[VehicleLine(b.id, b.name, b.file, 17, b.price)],
                          legs=[loop, inter], meta={"line": "bundle"})
    new = _check(pc)
    groups = new.meta["capacity_check"]["maintenance"]
    assert [(g["vehicles"], g["maint_buildings"], g["depot_free"]) for g in groups] == [(1, 0, False), (16, 1, True)]
    assert new.infra_capex - pc.infra_capex == pytest.approx(facilities.maint_building(None, "road", YEAR)[0])


# --------------------------------------------------------------------------- terminals, limits, rail

def test_busy_stop_gets_more_terminals():
    """The brick plant of a tour unloads clay and loads bricks: twice the handling of the ends."""
    quiet = _check(_tour(4, 5.0))
    busy_pc = _tour(45, 100.0)                              # plant: 200 units handled + 2 calls per lap
    busy = _check(busy_pc)
    assert quiet.infra_capex == pytest.approx(_tour(4, 5.0).infra_capex)
    pl = {p["place"]: p for p in busy.meta["capacity_check"]["places"]}
    assert pl["1"]["terminals"] >= 2 and pl["1"]["terminals"] >= pl["0"]["terminals"]
    assert busy.infra_capex > busy_pc.infra_capex
    assert any("терминал" in n for n in busy.meta["capacity_notes"])


def test_road_limits_make_project_infeasible():
    assert _check(_tour(C.MAX_ROAD_VEHICLES + 50, 10.0)) is None


def test_rail_shuttle_leg_gets_passing_loops():
    """A 3-train rail trunk of a warehouse chain on 20 km single track needs loops (or a second
    track): capex goes up and the layout is reported."""
    loco, wagon = _rail_set("coal")
    n, k = 3, 8
    leg = {"role": "trunk", "mode": "rail", "from": {"key": "hub:1:1", "name": "hub"}, "to": {"key": 7, "name": "D"},
           "length_m": 20_000, "volume": 900.0, "n_consists": n, "consist": "x",
           "vehicles": [{"model": loco.file.rsplit("/", 1)[-1], "name": loco.name, "count": n, "price": loco.price},
                        {"model": wagon.file.rsplit("/", 1)[-1], "name": wagon.name, "count": n * k, "price": wagon.price}],
           "trip_time_s": 3600.0, "station_from": "Ж/д грузовая станция: 1×160 м", "station_to": "Ж/д грузовая станция: 1×160 м"}
    pc = ProjectCandidate(kind="multimodal", mode="multi", title="chain", details="", nodes=[5, 7], cargo="coal",
                          volume=900, income=5e6, running=1e5, maintenance=1e5, vehicle_capex=1e6, infra_capex=2e6,
                          vehicles=[VehicleLine(loco.id, loco.name, loco.file, n, loco.price),
                                    VehicleLine(wagon.id, wagon.name, wagon.file, n * k, wagon.price)], legs=[leg])
    new = _check(pc)
    assert new is not None
    s = new.meta["capacity_check"]["services"][0]
    assert s["track"][0]["kind"] in ("single+loops", "double")
    assert new.infra_capex > pc.infra_capex
    if s["n"] > n:                                           # crossing waits -> extra trains, leg updated too
        assert new.legs[0]["n_consists"] == s["n"] and pc.legs[0]["n_consists"] == n


def test_checked_once_and_input_not_mutated():
    pc = _tour(34, 5.0)
    before = (pc.capex, pc.maintenance, pc.details, [v.count for v in pc.vehicles], dict(pc.meta))
    new = _check(pc, built_stations={(0, "road")})
    assert (pc.capex, pc.maintenance, pc.details, [v.count for v in pc.vehicles], dict(pc.meta)) == before
    assert _check(new, built_stations={(0, "road")}) is new
    unknown = ProjectCandidate(kind="upgrade", mode="road", title="x", details="", nodes=[], cargo=None, volume=0,
                               income=0, running=0, maintenance=0, vehicle_capex=0, infra_capex=0)
    out = _check(unknown)
    assert out.meta["capacity_check"] == {"checked": False} and out.capex == 0


# --------------------------------------------------------------------------- plan <-> dossier consistency

def _plan_steps():
    p = DATA / "plan.json"
    if not p.exists():
        pytest.skip("no plan.json")
    return json.loads(p.read_text(encoding="utf-8"))["steps"]


def test_plan_and_dossier_report_same_maintenance():
    """For lines of data/plan.json the plan (vehicles of the step, same grouping and counting rule
    as check_candidate; and the depot text in `details` when present) and the dossier give the same
    depot / maintenance-building counts per mode. Covers the first lines of each kind and every
    line that needs maintenance buildings (tours, chains, prospecting, bundles, direct links)."""
    from tf3planner.dossier import Context, _step_kind, build_dossier
    steps = _plan_steps()
    ctx = Context()
    kinds_seen = collections.Counter()
    checked = []
    for s in steps:
        if s.get("action") != "build_line":
            continue
        plan = CG.plan_step_maintenance(ctx.game, s, ctx.vehicles)
        kind = _step_kind(s)
        if not any(g["maint_buildings"] for g in plan) and kinds_seen[kind] >= 1:
            continue
        kinds_seen[kind] += 1
        d = build_dossier(ctx.game, None, s, steps, ctx=ctx)
        dos = CG.dossier_maintenance(d)
        pm, dm = collections.Counter(), collections.Counter()
        for g in plan:
            pm[g["mode"]] += g["maint_buildings"]
        for g in dos:
            dm[g["mode"]] += g["maint_buildings"]
        assert +pm == +dm, (s["line"], kind, plan, dos)
        # every mode the plan uses has a depot in the dossier (bought there or shared)
        assert {g["mode"] for g in plan} <= {g["mode"] for g in dos if g["depots"] >= 1}, (s["line"], plan, dos)
        m = re.search(r"(?:Дорожное|Ж/д|Судовое) депо \+ (\d+) здание\(я\) техобслуживания", s.get("details") or "")
        if m and kind in ("tour", "cargo"):
            assert int(m.group(1)) == sum(dm.values()), (s["line"], m.group(0), dos)
        checked.append((s["line"], kind, sum(pm.values())))
    assert len(checked) >= 5
    assert any(k for _, _, k in checked), "no line with maintenance buildings checked"


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
