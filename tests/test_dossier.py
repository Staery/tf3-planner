"""Tests for tf3planner.dossier on the real export (skipped when data/export.json is missing).
Run with pytest or directly: python tests/test_dossier.py"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from tf3planner.model import DATA  # noqa: E402

pytestmark = pytest.mark.skipif(not (DATA / "export.json").exists(), reason="no export.json")

_CTX = None


def ctx():
    global _CTX
    if _CTX is None:
        from tf3planner.dossier import Context
        _CTX = Context()
    return _CTX


def _industry_pair(c, out_cargo: str, water_src: bool = False):
    """A producer of out_cargo and its nearest industry consumer."""
    g = c.game
    prods = [i for i in g.industries if out_cargo in i.outputs and bool(i.on_water) == water_src]
    cons = [i for i in g.industries if out_cargo in i.inputs and not i.on_water]
    best = min(((math.dist((p.x, p.y), (q.x, q.y)), p, q) for p in prods for q in cons), key=lambda t: t[0])
    return best[1], best[2]


def _vehicle(c, carrier, role, cargo=None, year=1900):
    vs = [v for v in c.vehicles if v.carrier == carrier and v.role == role and v.available(year) and v.price > 1000
          and v.year_from > 0 and (cargo is None or v.carries(cargo))]
    return max(vs, key=lambda v: v.power_kw if role not in ("wagon", "ship", "bus", "tram") else v.carries(cargo or ""))


def _vj(v, k):
    return {"model": v.file.rsplit("/", 1)[-1], "name": v.name, "count": k, "price": v.price}


def _ep(n):
    return {"type": n.kind, "id": n.id, "name": n.name, "x": n.x, "y": n.y}


def _step(c, mode, cargo, p, q, vehicles, n, line="T1"):
    return {"id": "s1", "line": line, "year": 1900, "month": 1, "order": 1, "action": "build_line", "kind": mode,
            "cargo": cargo, "title": f"test {mode}",
            "details": f"60 ед./год  1 км по прямой. Состав: {n}× тест.",
            "from": {"type": "industry", "id": p.id, "name": p.name, "x": p.x, "y": p.y},
            "to": {"type": "industry", "id": q.id, "name": q.name, "x": q.x, "y": q.y},
            "vehicles": [_vj(v, k) for v, k in vehicles],
            "cost": 1, "income_per_year": 1, "running_cost_per_year": 1, "roi_years": None, "money_after": 0}


def _check_instructions(d: dict, min_n: int = 5):
    ins = d["instructions"]
    assert len(ins) >= min_n and all(s.split(".", 1)[0].isdigit() for s in ins)
    assert [int(s.split(".", 1)[0]) for s in ins] == list(range(1, len(ins) + 1))


def _check_money(leg: dict):
    m = leg["money"]
    cx = m["capex"]
    assert abs(cx["route"] + cx["stations"] + cx["depots"] + cx["vehicles"] - cx["total"]) <= 2
    py = m["per_year"]
    assert abs(m["income_per_year"] - py["total_costs"] - m["profit_per_year"]) <= 2


def _check_leg(d: dict, leg: dict, mode: str | None = None, n_stations: int | None = 2):
    assert not leg.get("error")
    if mode:
        assert leg["mode"] == mode
    r = leg["route"]
    assert r["length_m"] > 0 and r["straight_m"] > 0
    t = leg["timing"]
    rt = t["round_trip_s"]
    if t.get("one_way"):                            # ring / tour: one pass + stops
        assert rt >= t["loaded"]["time_s"]
    else:
        assert r["length_m"] >= 0.95 * r["straight_m"]
        assert rt >= t["loaded"]["time_s"] + t["empty"]["time_s"]
    assert t["loaded"]["time_s"] >= t["loaded"]["ideal_time_s"] - 1e-6
    assert abs(t["trips_per_year"] - d["year_s"] / rt) < 1e-2
    _check_money(leg)
    if n_stations is not None:
        assert len(leg["stations"]) == n_stations
    assert leg["depots"]
    assert leg["chart"]["loaded"] and leg["chart"]["empty"] and leg["chart"]["track"]
    for s in leg["stations"] + leg["depots"]:
        assert isinstance(s["x"], (int, float)) and isinstance(s["y"], (int, float))


def _check_common(d: dict, mode: str):
    json.dumps(d)                                  # JSON-serializable
    assert d["legs"] and not d["legs"][0].get("error")
    leg = d["legs"][0]
    _check_leg(d, leg, mode)
    _check_instructions(d)
    return leg


def test_rail_dossier():
    from tf3planner.dossier import build_dossier
    c = ctx()
    p, q = _industry_pair(c, "logs")
    loco = _vehicle(c, "rail", "loco")
    wagon = _vehicle(c, "rail", "wagon", "logs")
    d = build_dossier(c.game, None, _step(c, "rail", "logs", p, q, [(loco, 2), (wagon, 10)], 2), ctx=c)
    leg = _check_common(d, "rail")
    assert d["kind"] == "cargo" and d["step_id"] == "s1"
    cons = leg["consist"]
    assert cons["n_consists"] == 2 and cons["units"] == 6           # 1 loco + 5 wagons per train
    assert cons["mass_loaded_t"] > cons["mass_empty_t"]
    assert cons["performance_loaded"][0]["max_kmh"] > 0
    assert leg["stations"][0]["platform_m"] >= cons["length_m"]
    assert leg["timing"]["ruling_grade"] is not None
    assert leg["timing"]["grade_variants"]
    assert any("Купить" in s for s in d["instructions"])
    assert any("пути" in s for s in d["instructions"])


def test_road_dossier_and_plan_reuse():
    from tf3planner.dossier import build_dossier
    c = ctx()
    p, q = _industry_pair(c, "logs")
    truck = _vehicle(c, "road", "truck", "logs")
    first = _step(c, "road", "logs", p, q, [(truck, 8)], 8, line="T1")
    second = dict(_step(c, "road", "logs", p, q, [(truck, 4)], 4, line="T2"), id="s2")
    d1 = build_dossier(c.game, None, first, [first, second], ctx=c)
    leg = _check_common(d1, "road")
    assert leg["capacity"]["consists_planned"] == 8
    d2 = build_dossier(c.game, None, second, [first, second], ctx=c)
    l2 = d2["legs"][0]
    assert l2["route"]["free"] and l2["money"]["capex"]["route"] == 0
    assert all(s["cost"] == 0 for s in l2["stations"])
    assert any("уже" in s for s in d2["instructions"])


def test_siting_rules():
    """Industry station inside the industry catchment; depot outside towns within its radius."""
    from tf3planner.dossier import build_dossier
    c = ctx()
    S = c.siting
    p, q = _industry_pair(c, "logs")
    truck = _vehicle(c, "road", "truck", "logs")
    d = build_dossier(c.game, None, _step(c, "road", "logs", p, q, [(truck, 3)], 3), ctx=c)
    leg = d["legs"][0]
    st = leg["stations"][0]
    assert st["dist_to_endpoint_m"] <= st["catchment_m"] and st["site_reason"]
    dp = leg["depots"][0]
    assert math.dist((dp["x"], dp["y"]), (st["x"], st["y"])) <= dp["radius_m"]
    assert S.built_depth(dp["x"], dp["y"]) == 0 and not S.water(dp["x"], dp["y"])
    # town station: at the edge on the route side, not in the centre
    t = S.towns[0]
    end = {"name": t.name, "kind": "town", "id": t.id, "x": t.x, "y": t.y, "key": None}
    site = S.town_station(end, (t.x + 3000.0, t.y), 175.0)
    d_c = math.dist((site.x, site.y), (t.x, t.y))
    assert 0.3 * t.radius <= d_c <= t.radius + 100 and site.x > t.x - 50


def test_plan_lines_if_present():
    """Every project kind present in data/plan.json gets a non-empty dossier."""
    from tf3planner.dossier import _step_kind, build_dossier
    plan_path = DATA / "plan.json"
    if not plan_path.exists():
        pytest.skip("no plan.json")
    steps = json.loads(plan_path.read_text(encoding="utf-8"))["steps"]
    c = ctx()
    done = set()
    for s in steps:
        k = (_step_kind(s), s.get("kind"))
        if s.get("action") != "build_line" or k in done:
            continue
        d = build_dossier(c.game, None, s, steps, ctx=c)
        json.dumps(d)
        assert d["step_id"] == s["id"] and d["kind"] == k[0]
        ok = [l for l in d["legs"] if not l.get("error")]
        assert ok, (s["id"], d["warnings"])
        for leg in ok:
            _check_leg(d, leg, n_stations=None)
        _check_instructions(d, 3)
        done.add(k)
    assert done


def test_multimodal_offshore_chain():
    """Oil platform -> ship -> harbor + warehouse -> trucks -> refinery: no harbor at the platform,
    warehouse in both catchments and >= 2x the largest consist calling there."""
    from tf3planner.dossier import build_dossier
    c = ctx()
    p, q = _industry_pair(c, "crude_oil", water_src=True)
    P, Q = c.by_key[f"industry:{p.id}"], c.by_key[f"industry:{q.id}"]
    hx, hy = c.siting.shore(p.x, p.y, 6000.0)
    hub = {"name": "порт узла", "key": f"hub:{round(hx)}:{round(hy)}", "x": hx, "y": hy}
    ship = _vehicle(c, "water", "ship", "crude_oil")
    truck = _vehicle(c, "road", "truck", "crude_oil")
    legs = [{"role": "feeder", "mode": "water", "from": {"name": P.name, "key": P.index, "x": P.x, "y": P.y},
             "to": hub, "length_m": math.dist((P.x, P.y), (hx, hy)), "volume": 50.0, "n_consists": 2,
             "vehicles": [_vj(ship, 2)], "income": 300000.0, "route_build": 0,
             "geometry": [[P.x, P.y], [hx, hy]]},
            {"role": "trunk", "mode": "road", "from": hub, "to": {"name": Q.name, "key": Q.index, "x": Q.x, "y": Q.y},
             "volume": 50.0, "n_consists": 10, "vehicles": [_vj(truck, 10)], "income": 900000.0, "route_build": 1}]
    step = {"id": "s7", "line": "L7", "year": 1905, "month": 1, "action": "build_line", "kind": "multi",
            "project_kind": "multimodal", "cargo": "crude_oil", "title": "Мультимодально: тест",
            "details": "50 ед./год. Склады: Склад (liquid), 500 ед. ×1 (узел, буфер 23 ед.). Перевалка через склад.",
            "from": _ep(P), "to": _ep(Q), "legs": legs, "vehicles": [_vj(ship, 2), _vj(truck, 10)],
            "cost": 1, "income_per_year": 1200000, "running_cost_per_year": 1}
    d = build_dossier(c.game, None, step, [step], ctx=c)
    json.dumps(d)
    assert d["kind"] == "multimodal" and len(d["legs"]) == 2
    feeder, trunk = d["legs"]
    _check_leg(d, feeder, "water")
    _check_leg(d, trunk, "road")
    assert feeder["stations"][0]["kind"] == "builtin_terminal" and feeder["stations"][0]["cost"] == 0
    assert feeder["stations"][1]["kind"] == "harbor" and feeder["stations"][1]["cost"] > 0
    assert not c.siting.water(feeder["stations"][1]["x"], feeder["stations"][1]["y"])
    assert c.siting.water(feeder["depots"][0]["x"], feeder["depots"][0]["y"])        # ship depot on water
    assert feeder["money"]["income_per_year"] == 300000 and trunk["money"]["income_per_year"] == 900000
    assert len(d["warehouses"]) == 1
    w = d["warehouses"][0]
    largest = max(feeder["consist"]["capacity"], trunk["consist"]["capacity"])
    assert w["capacity_units"] >= 2 * largest and w["modules"] >= 1 and w["cost"] > 0
    assert w["specialized"] and w["class"] == "liquid"
    for s in w["stations"]:
        if w["site_ok"]:
            assert math.dist((w["x"], w["y"]), (s["x"], s["y"])) <= s["catchment_m"]
    ins = " ".join(d["instructions"])
    assert "порт НЕ строить" in ins and "охвате ОБЕИХ" in ins and "со склада" in ins
    assert d["totals"]["capex"] >= sum(l["money"]["capex"]["total"] for l in d["legs"]) + w["cost"] - 2


def _towns_pair(c):
    ts = [n for n in c.nodes if n.kind == "town"]
    best = min(((math.dist((a.x, a.y), (b.x, b.y)), a, b) for a in ts for b in ts if a is not b and
                math.dist((a.x, a.y), (b.x, b.y)) > 2500), key=lambda t: t[0])
    return best[1], best[2]


def test_passenger_bundle():
    from tf3planner.dossier import build_dossier
    c = ctx()
    a, b = _towns_pair(c)
    bus = _vehicle(c, "road", "bus", "passengers")
    loop = {"title": f"Автобус по городу: {a.name}", "mode": "road", "kind": "passenger", "nodes": [a.index],
            "volume": 40.0, "income": 60000, "running": 1, "maintenance": 1, "vehicle_capex": 1, "infra_capex": 1,
            "vehicles": [{"vehicle_id": bus.id, "name": bus.name, "file": bus.file, "count": 2, "price": bus.price}],
            "geometry": [[a.x + 250 * math.cos(k * math.pi / 6), a.y + 250 * math.sin(k * math.pi / 6)] for k in range(13)],
            "details": f"Кольцевая линия по городу {a.name}: 4 остановок (малые, 45 000 каждая), кольцо ~1.5 км, "
                       f"круг 400 с, интервал 200 с. Средняя поездка 440 м по прямой. Пассажиров 40/год (спрос 45, "
                       f"провозная способность 50)."}
    inter = {"title": f"Автобус: {a.name} ↔ {b.name}", "mode": "road", "kind": "passenger", "nodes": [a.index, b.index],
             "volume": 70.0, "income": 900000, "running": 1, "maintenance": 1, "vehicle_capex": 1, "infra_capex": 1,
             "vehicles": [{"vehicle_id": bus.id, "name": bus.name, "file": bus.file, "count": 6, "price": bus.price}],
             "geometry": None,
             "details": "4.3 км по прямой, маршрут 4.4 км (дорога уже есть). Состав: 6× x, круг 1800 с, интервал 300 с. "
                        "Билет 15 000 за пассажира. Станции: 2 малые остановки в каждом городе. Пассажиров 70/год."}
    step = {"id": "s9", "line": "L9", "year": 1902, "month": 1, "action": "build_line", "kind": "multi",
            "project_kind": "passenger", "cargo": "passengers", "title": f"Пассажирская сеть: {a.name} ↔ {b.name}",
            "details": "", "from": _ep(a), "to": _ep(b), "legs": [loop, inter], "vehicles": [],
            "cost": 1, "income_per_year": 960000, "running_cost_per_year": 1}
    d = build_dossier(c.game, None, step, [step], ctx=c)
    json.dumps(d)
    assert d["kind"] == "passenger" and len(d["legs"]) == 2
    L0, L1 = d["legs"]
    _check_leg(d, L0, "road", n_stations=4)
    _check_leg(d, L1, "road", n_stations=4)
    assert L0["pax"] == "loop" and L1["pax"] == "intercity"
    for leg in d["legs"]:
        assert all(s["kind"] == "bus_stop" and "Грузов" not in s["building"] and "small_" in s["building"]
                   for s in leg["stations"])
        assert leg["passengers"]["passengers_per_year"] > 0 and leg["passengers"]["headway_s"] > 0
    assert L1["route"]["free"]                                     # towns are connected by road
    assert L1["stations"][0]["free_by"]                            # shares the loop stop in the same project
    assert L1["capacity"]["directions"] == 2.0
    assert L0["money"]["income_per_year"] == 60000
    ins = " ".join(d["instructions"])
    assert "кольцевую линию" in ins and "без «полной загрузки»" in ins.lower() or "БЕЗ «полной загрузки»" in ins


def test_workers_line():
    from tf3planner.dossier import build_dossier
    c = ctx()
    t = next(n for n in c.nodes if n.kind == "town")
    ind = min((n for n in c.nodes if n.kind == "industry" and not n.water_only),
              key=lambda n: math.dist((n.x, n.y), (t.x, t.y)))
    bus = _vehicle(c, "road", "bus", "passengers")
    step = {"id": "s5", "line": "L5", "year": 1904, "month": 1, "action": "build_line", "kind": "road",
            "project_kind": "workers", "cargo": "passengers", "title": f"Рабочие: {t.name} → {ind.name}",
            "details": f"Возить рабочих из {t.name} на «{ind.name}»: 2.4 км по прямой, маршрут 2.4 км. 1× x, круг 1151 с. "
                       f"Ожидается 4.4 поездок на работу в год, разных рабочих за 2 года ~8 (нужно ≥10). Пассажиров "
                       f"8/год, билеты 65 414/год. Две малые остановки. Рабочих меньше порога — бонуса не будет.",
            "from": _ep(t), "to": _ep(ind), "vehicles": [_vj(bus, 1)], "cost": 1, "income_per_year": 65414,
            "running_cost_per_year": 1}
    d = build_dossier(c.game, None, step, [step], ctx=c)
    leg = d["legs"][0]
    _check_leg(d, leg, "road")
    assert d["kind"] == "workers" and leg["pax"] == "workers"
    assert leg["passengers"]["distinct_workers_2y"] == 8 and leg["passengers"]["min_workers"] == 10
    assert not leg["passengers"]["boost"]
    assert any("≥10 РАЗНЫХ" in s for s in d["instructions"])
    assert any("бонуса производства не будет" in w for w in d["warnings"])


def test_tour_milk_run():
    """Circuit producer -> processor -> town -> producer: every stop with its load/unload."""
    from tf3planner.dossier import _short, build_dossier
    c = ctx()
    g = c.game
    p, q = _industry_pair(c, "logs")                              # forest -> sawmill
    P, Q = c.by_key[f"industry:{p.id}"], c.by_key[f"industry:{q.id}"]
    T = min((n for n in c.nodes if n.kind == "town"), key=lambda n: math.dist((n.x, n.y), (Q.x, Q.y)))
    truck = _vehicle(c, "road", "truck", "logs")
    legs = [{"from": P.index, "to": Q.index, "length_m": math.dist((P.x, P.y), (Q.x, Q.y)), "load": 40.0},
            {"from": Q.index, "to": T.index, "length_m": math.dist((Q.x, Q.y), (T.x, T.y)), "load": 30.0},
            {"from": T.index, "to": P.index, "length_m": math.dist((T.x, T.y), (P.x, P.y)), "load": 0.0}]
    logs, planks = g.cargo_by_key["logs"].name.lower(), g.cargo_by_key["planks"].name.lower()
    step = {"id": "s3", "line": "L3", "year": 1900, "month": 1, "action": "build_line", "kind": "road",
            "project_kind": "cargo", "cargo": "logs",
            "title": f"Кольцевой маршрут (дорога): {_short(P.name)} → {_short(Q.name)} → {T.name} ↺",
            "details": f"Маршрут (кольцо): x. Потоки: {_short(P.name)} → {_short(Q.name)}: {logs} 40; "
                       f"{_short(Q.name)} → {T.name}: {planks} 30. Каждая единица оплачивается по прямой. "
                       f"Состав: 10× тест.",
            "from": _ep(P), "to": _ep(T), "stops": [_ep(P), _ep(Q), _ep(T)], "legs": legs,
            "vehicles": [_vj(truck, 10)], "cost": 1, "income_per_year": 1, "running_cost_per_year": 1}
    d = build_dossier(c.game, None, step, [step], ctx=c)
    json.dumps(d)
    assert d["kind"] == "tour"
    leg = d["legs"][0]
    _check_leg(d, leg, "road", n_stations=3)
    tour = d["tour"]
    assert [s["n"] for s in tour["stops"]] == [1, 2, 3] and tour["topology"] == "loop"
    s1, s2, s3 = tour["stops"]
    assert s1["load"] == {"logs": 40} and s2["unload"] == {"logs": 40} and s2["load"] == {"planks": 30}
    assert s3["unload"] == {"planks": 30} and not s3["load"]
    assert len(tour["segments"]) == 3 and tour["segments"][2]["on_board_ru"] == "порожним"
    assert leg["money"]["income_per_year"] > 0 and len(leg["money"]["flows"]) == 2
    t = leg["timing"]
    assert t["one_way"] and t["round_trip_s"] >= t["loaded"]["time_s"]
    assert abs(leg["route"]["length_m"] - sum(sg["length_m"] for sg in tour["segments"])) <= 3
    ins = d["instructions"]
    assert any("Остановка 1" in s for s in ins) and any("Не ставить «полную загрузку»" in s for s in ins)


def test_prospect_step():
    from tf3planner.dossier import build_dossier
    c = ctx()
    p, q = _industry_pair(c, "clay")
    Q = c.by_key[f"industry:{q.id}"]
    town = next(n for n in c.nodes if n.kind == "town")
    truck = _vehicle(c, "road", "truck", "clay")
    step = {"id": "s11", "line": "L11", "year": 1908, "month": 3, "action": "build_line", "kind": "road",
            "project_kind": "industry_build", "cargo": "clay", "title": f"Разведка: глина у {town.name}",
            "details": f"Разведка ресурса «Глина» у города {town.name} (ранг 3, разрешение одноразовое; успех ~97%, "
                       f"~2.6 мес.). Ожидается глиняный карьер ~20 ед./год (старт ~уровень 1, при вывозе ≥75 % растёт "
                       f"на 1 уровень в полгода (в среднем до 6)). Первая линия: clay → {Q.name} (road), 20 ед./год, "
                       f"доход 1 924 743/год. Место — оценка.",
            "geometry": [[town.x + 600, town.y + 600], [Q.x, Q.y]], "from": _ep(town), "to": _ep(Q),
            "vehicles": [_vj(truck, 5)], "cost": 1, "income_per_year": 1, "running_cost_per_year": 1}
    d = build_dossier(c.game, None, step, [step], ctx=c)
    json.dumps(d)
    assert d["kind"] == "industry_build"
    pr = d["prospect"]
    assert pr["resource"] == "Глина" and pr["town"] == town.name and pr["rank"] == 3
    assert pr["expected"] == "глиняный карьер" and pr["first_line"]["volume"] == 20
    assert d["instructions"][0].startswith("1. Применить ранг компании ≥ 3")
    leg = d["legs"][0]
    _check_leg(d, leg, "road")
    assert leg["estimate"] and leg["from"]["x"] == town.x + 600


def test_project_candidate_input():
    from tf3planner.candidates import ProjectCandidate, VehicleLine
    from tf3planner.dossier import build_dossier
    c = ctx()
    p, q = _industry_pair(c, "logs")
    truck = _vehicle(c, "road", "truck", "logs")
    pi, qi = c.by_key[f"industry:{p.id}"].index, c.by_key[f"industry:{q.id}"].index
    cand = ProjectCandidate(kind="cargo", mode="road", title="кандидат", details="", nodes=[pi, qi], cargo="logs",
                            volume=40.0, income=1.0, running=1.0, maintenance=1.0, vehicle_capex=1.0, infra_capex=1.0,
                            vehicles=[VehicleLine(truck.id, truck.name, truck.file, 5, truck.price)])
    d = build_dossier(c.game, None, cand, ctx=c)
    _check_common(d, "road")
    # multimodal-like legs
    leg = {"role": "feeder", "mode": "road", "from": {"name": p.name, "key": pi, "x": p.x, "y": p.y},
           "to": {"name": q.name, "key": qi, "x": q.x, "y": q.y}, "volume": 40.0, "n_consists": 3,
           "vehicles": [{"model": truck.file.rsplit("/", 1)[-1], "name": truck.name, "count": 3}], "income": 1000.0}
    multi = ProjectCandidate(kind="multimodal", mode="multi", title="мульти", details="", nodes=[pi, qi], cargo="logs",
                             volume=40.0, income=1000.0, running=1.0, maintenance=1.0, vehicle_capex=1.0,
                             infra_capex=1.0, legs=[leg, dict(leg, role="trunk")],
                             meta={"warehouses": [{"name": "Склад", "hub": "узел", "x": 0, "y": 0, "modules": 1,
                                                   "buffer_units": 10, "cost": 1000, "maintenance": 100}]})
    dm = build_dossier(c.game, None, multi, ctx=c)
    json.dumps(dm)
    assert len(dm["legs"]) == 2 and dm["warehouses"]
    assert any("Склад" in s for s in dm["instructions"])


def test_build_all_duplicate_line_ids(tmp_path):
    """Older plans reuse line ids: every build step still gets its own dossier (key + step_id)."""
    from tf3planner.dossier import build_all
    c = ctx()
    p, q = _industry_pair(c, "logs")
    truck = _vehicle(c, "road", "truck", "logs")
    a = _step(c, "road", "logs", p, q, [(truck, 3)], 3, line="L1")
    b = dict(_step(c, "road", "logs", q, p, [(truck, 2)], 2, line="L1"), id="s2", cargo="logs")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"generated": "t", "steps": [a, b]}, ensure_ascii=False), encoding="utf-8")
    out = build_all(plan, game=c.game)
    keys = [k for k in out if not k.startswith("_")]
    assert sorted(keys) == ["L1", "L1@s2"]
    assert {out[k]["step_id"] for k in keys} == {"s1", "s2"}
    assert out["_meta"]["by_kind"]["cargo"] == {"steps": 2, "ok": 2}


if __name__ == "__main__":
    import inspect
    import tempfile
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and inspect.isfunction(f):
            if "tmp_path" in inspect.signature(f).parameters:
                f(Path(tempfile.mkdtemp()))
            else:
                f()
            print("ok", n)


# --------------------------------------------------------------------------- demolition (docs/demolition.md)

def _built_town(c):
    """The town with the most demolishable ground at its centre (needs a town mask)."""
    from tf3planner.dossier import _tm
    tm = _tm(c)
    if tm is None:
        pytest.skip("no town mask")
    return tm, max(c.game.towns, key=lambda t: float(tm.footprint(t.x, t.y, 60.0)["count"]))


def test_segment_demolition_split_and_parts():
    """A new segment through a town centre: totals kept, split by town, affected stretches inside it."""
    from tf3planner.dossier import _segment_demolition, _route_demolition, _mkleg
    c = ctx()
    tm, t = _built_town(c)
    poly = [[t.x - 600.0, t.y], [t.x + 600.0, t.y]]
    f = tm.corridor(poly, 6.0)
    assert f["count"] > 0
    seg = {"type": "new", "polyline": poly, "length_m": 1200, "build_cost": 1,
           "demolition_cost": round(f["cost"]), "demolition_m": round(f["length_m"]),
           "demolition_buildings": round(f["count"], 1), "demolition_rating": round(f["penalty"], 4)}
    sd = _segment_demolition(c, seg, 6.0)
    assert abs(sum(v["buildings"] for v in sd["towns"].values()) - seg["demolition_buildings"]) < 1e-6
    assert abs(sum(v["rating_loss"] for v in sd["towns"].values()) - seg["demolition_rating"]) < 1e-6
    assert t.name in sd["towns"]
    assert sd["parts"] and abs(sum(p["buildings"] for p in sd["parts"]) - seg["demolition_buildings"]) < 1e-6
    for p in sd["parts"]:
        assert len(p["polyline"]) >= 2 and abs(p["y"] - t.y) < 5 and t.x - 650 <= p["x"] <= t.x + 650
    # route section: per-segment text + total line, the money is reported as "в т.ч."
    leg = _mkleg(mode="rail", src={"name": "a", "x": t.x - 600, "y": t.y, "z": 0}, dst={"name": "b", "x": t.x + 600,
                                                                                     "y": t.y, "z": 0})
    sec = {"segments": [dict(seg)], "free": False}
    dem = _route_demolition(c, leg, type("R", (), {"polyline": poly, "length_m": 1200})(), None, sec)
    assert dem and dem["cost"] == seg["demolition_cost"] and dem["parts"]
    assert dem["total_ru"].startswith("в т.ч. снос") and "снос зданий:" in dem["segments_ru"][0]
    assert sec["segments"][0]["demolition_ru"].startswith("снос зданий:")


def test_reputation_warning_rule():
    from tf3planner.dossier import _rep_warning
    assert _rep_warning("Х", 0.5) is None
    w = _rep_warning("Х", 0.7)
    assert "игра может запретить снос/станции" in w and "«Плохо»" in w
    assert "«Очень плохо»" in _rep_warning("Х", 0.9)


def test_station_demolition_in_cost_and_instructions(monkeypatch):
    """A station whose site demolishes houses: its price includes the demolition, the instruction names it,
    the dossier sums it up per town; the planner's own demolition / warnings are carried over."""
    from tf3planner import siting as SIT
    from tf3planner.dossier import build_dossier, REP_WARN
    c = ctx()
    tm, t = _built_town(c)
    S = c.siting
    orig = S.industry_station
    calls = {}

    def fake(end, route_pt, catchment, mode="road"):
        site = orig(end, route_pt, catchment, mode)
        if "first" not in calls:                          # loading station: put it in the town centre
            calls["first"] = True
            f = tm.footprint(t.x, t.y, SIT.FOOTPRINT_M["station_road"])
            return SIT.Site(t.x, t.y, "test", demolition_cost=f["cost"], demolition_buildings=f["count"],
                            demolition_rating=f["penalty"])
        return site
    monkeypatch.setattr(S, "industry_station", fake)
    p, q = _industry_pair(c, "logs")
    truck = _vehicle(c, "road", "truck", "logs")
    step = _step(c, "road", "logs", p, q, [(truck, 2)], 2)
    step["demolition"] = {"cost": 123456, "m": 10, "buildings": 3, "towns": {"Тестоград": 0.8}}
    step["warnings"] = ["Тестоград: репутация −80 % (план)"]
    d = build_dossier(c.game, None, step, ctx=c)
    st = d["legs"][0]["stations"][0]
    dm = st["demolition"]
    assert dm and dm["buildings"] > 0 and t.name in dm["towns"]
    assert st["cost"] == st["full_cost"] and st["cost"] >= dm["cost"]
    assert any("СНОС под площадку" in s and t.name in s for s in d["instructions"])
    assert any("Без сноса" in s for s in d["instructions"])           # the other station / depot
    tot = d["demolition"]
    assert tot["cost"] >= dm["cost"] and t.name in tot["towns"]
    assert tot["plan"]["cost"] == 123456 and tot["plan"]["towns"]["Тестоград"] == 0.8
    assert "Тестоград: репутация −80 % (план)" in d["warnings"]
    if tot["towns"][t.name]["rating_loss"] > REP_WARN:
        assert any(t.name in w and "запретить" in w for w in d["warnings"])
    _check_money(d["legs"][0])
