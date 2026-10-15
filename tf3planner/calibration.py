"""Calibrate the planner model against the player's running lines (export v5).

Usage:  python -m tf3planner.loader        (export.lua -> data/export.json)
        python -m tf3planner.calibration   (report + data/calibration.json)

For every player line the in-game figures of the last 12 months (or since the first vehicle
was bought, if the line is younger) are compared with what the model predicts for the same
stops and vehicles:

- income      actual = net balance (income - running costs, as the line window shows)
                       + vehicles' running costs; or the line's account chart when available
              model  = transported units * income_per_unit(stop -> stop straight distance)
                       * company ticket multiplier
- running     actual = sum of api.engine.util.vehicle.getRunningCost over the line's vehicles
              model  = sum of the vehicles' runningCosts from the catalogue
- trip time   actual = sum of TransportVehicle.sectionTimes (one round trip)
              model  = route / (topSpeed * SPEED_FACTOR) + per stop (capacity / loadSpeed + STOP_DELAY_S)
                       with route = straight stop-to-stop loop * DETOUR_DEFAULT  (economics.evaluate_link)
- throughput  model capacity = vehicles * capacity * year / trip  vs  transported per year

Fitted parameters (printed and written to data/calibration.json):
- income_factor          median(actual income / model income)
- running_factor         median(actual running / model running)
- speed_factor           effective fraction of top speed on the estimated route (model uses 0.85),
                         or a least-squares fit of trip = a*route/speed + b*loading + c*stops when
                         at least three lines with different geometry are available
- trips_per_resident     passengers per year on passenger lines / residents of the towns they serve
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .economics import DETOUR_DEFAULT, STOP_DELAY_S, Endpoint, evaluate_link
from .model import DATA, Game
from .vehicles import CARRIERS, Vehicle, load_vehicles

SPEED_FACTOR_MODEL = 0.85            # economics.evaluate_link: route / (speed * 0.85)
TOWN_RADIUS_M = 2000.0               # a stop serves the nearest town within this distance
MIN_AGE_YEARS = 0.05                 # younger lines are reported but not used for fitting

MODE_BY_NAME = {"ROAD": "road", "RAIL": "rail", "TRAM": "tram", "AIR": "air", "WATER": "water"}


# --------------------------------------------------------------------------- helpers

def as_list(x) -> list:
    """Loader turns empty Lua tables into {} and sparse arrays into string-keyed dicts."""
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        keys = list(x)
        if all(str(k).lstrip("-").isdigit() for k in keys):
            return [x[k] for k in sorted(keys, key=lambda k: int(k))]
    return []


def num(x, default=None):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else default


def ratio(a, b):
    a, b = num(a), num(b)
    if a is None or not b:
        return None
    return a / b


def median(xs):
    xs = [x for x in xs if num(x) is not None]
    return statistics.median(xs) if xs else None


def carrier_mode(c) -> str:
    if isinstance(c, str):
        return MODE_BY_NAME.get(c.upper(), c.lower())
    if isinstance(c, (int, float)):
        return CARRIERS.get(int(c), "?")
    return "?"


def pos3(p: dict | None, game: Game) -> tuple[float, float, float] | None:
    if not isinstance(p, dict) or "x" not in p or "y" not in p:
        return None
    z = p.get("z")
    if z is None:
        z = game.terrain.height(p["x"], p["y"])
    return (float(p["x"]), float(p["y"]), float(z))


def chart_series(chart) -> list[tuple[list, list]]:
    """ChartResult.series -> [(xs, ys), ...] (series[1] income, series[2] costs for accounts)."""
    out = []
    if not isinstance(chart, dict):
        return out
    for s in as_list(chart.get("series")):
        s = as_list(s)
        if len(s) >= 2:
            out.append((as_list(s[0]), as_list(s[1])))
    return out


def section_time_s(times: list) -> float | None:
    """Round-trip time from TransportVehicle.sectionTimes; values > 5000 are taken as ms."""
    ts = [t for t in times if num(t) and t > 0]
    if not ts:
        return None
    total = sum(ts)
    return total / 1000.0 if statistics.median(ts) > 5000 else float(total)


def departures_cycle_s(deps: list) -> float | None:
    """Cycle estimate from the last departure time (ms) at each stop."""
    ds = sorted(d for d in deps if num(d) and d > 0)
    if len(ds) < 2 or len(ds) != len(deps):
        return None
    span = ds[-1] - ds[0]
    return span * len(ds) / (len(ds) - 1) / 1000.0 if span > 0 else None


# --------------------------------------------------------------------------- per line

@dataclass
class LineCalib:
    id: int
    name: str
    mode: str
    cargo: str | None
    passenger: bool
    stops: int
    vehicles: int
    age_years: float
    window_years: float
    loop_straight_m: float = 0.0
    loop_route_m: float = 0.0
    transported_per_year: float | None = None
    income_actual: float | None = None
    income_source: str = ""
    income_model: float | None = None
    income_ratio: float | None = None
    ipu_model: float | None = None
    running_actual: float | None = None
    running_model: float | None = None
    running_ratio: float | None = None
    net_actual: float | None = None
    trip_actual_s: float | None = None
    trip_model_s: float | None = None
    trip_ratio: float | None = None
    speed_factor: float | None = None
    model_capacity_per_year: float | None = None
    capacity_used: float | None = None
    load_factor: float | None = None
    eval_vehicles: int | None = None
    eval_income: float | None = None
    eval_running: float | None = None
    eval_consist: str | None = None
    served_towns: list[int] = field(default_factory=list)
    regress: list[float] | None = None       # [route/speed, loading s, stops, trip_actual]
    notes: list[str] = field(default_factory=list)


def _line_cargo(game: Game, line: dict) -> tuple[str | None, bool]:
    rows = [r for r in as_list(line.get("capacityUsage")) + as_list(line.get("capacityUsageAll"))
            if isinstance(r, dict) and num(r.get("capacity"), 0) > 0]
    best = None
    for r in rows:
        try:
            c = game.cargos.get(int(r.get("cargo")))
        except (TypeError, ValueError):
            c = None
        if c is None:
            continue
        if c.is_passenger:
            return c.key, True
        if best is None or r["capacity"] > best[1]:
            best = (c, r["capacity"])
    if best:
        return best[0].key, False
    return None, False


def _nearest_town(game: Game, p: tuple[float, float, float]) -> int | None:
    best, bd = None, TOWN_RADIUS_M
    for t in game.towns:
        d = math.dist((t.x, t.y), (p[0], p[1]))
        if d < bd:
            best, bd = t.id, d
    return best


def analyse_line(game: Game, vby: dict[int, Vehicle], line: dict, ticket_mult: float) -> LineCalib:
    year_ms = game.year_ms
    year_s = year_ms / 1000.0
    money = line.get("money") if isinstance(line.get("money"), dict) else {}
    t1 = num(money.get("t1"), 0)
    first = num(line.get("firstPurchaseTime"))
    age = (t1 - first) / year_ms if first is not None and t1 else 1.0
    window = min(1.0, max(age, 1e-6))
    vehicles = [v for v in as_list(line.get("vehicles")) if isinstance(v, dict)]
    carriers = as_list(line.get("carriers")) or [v.get("carrier") for v in vehicles if v.get("carrier") is not None]
    mode = carrier_mode(carriers[0]) if carriers else "?"
    cargo_key, is_pass = _line_cargo(game, line)
    cargo = game.cargo_by_key.get(cargo_key) if cargo_key else None
    stops = [s for s in as_list(line.get("stops")) if isinstance(s, dict)]
    lc = LineCalib(int(line.get("id", -1)), str(line.get("name") or ""), mode, cargo_key, is_pass,
                   len(stops), len(vehicles), age, window)
    if age < MIN_AGE_YEARS:
        lc.notes.append(f"line is young ({age * 365.25:.0f} days), figures are noisy")

    # geometry
    pts = [pos3(s.get("pos"), game) for s in stops]
    pts = [p for p in pts if p]
    legs = list(zip(pts, pts[1:] + pts[:1])) if len(pts) >= 2 else []
    lc.loop_straight_m = sum(math.dist(a[:2], b[:2]) for a, b in legs)
    lc.loop_route_m = lc.loop_straight_m * DETOUR_DEFAULT
    lc.served_towns = sorted({t for t in (_nearest_town(game, p) for p in pts) if t is not None})
    if len(pts) < len(stops):
        lc.notes.append(f"{len(stops) - len(pts)} stops without position")

    # transported
    tpy = num(line.get("transportedPerYear"))
    if tpy is not None:
        lc.transported_per_year = tpy / window

    # money
    lc.running_actual = sum(num(v.get("runningCost"), 0) for v in vehicles) or None
    lb, vb = money.get("line") or {}, money.get("vehicles") or {}
    net = num(lb.get("maintIncome"))
    if not net:
        net = num(vb.get("maintIncome"), net)
    if net is not None:
        lc.net_actual = net / window
    chart = chart_series(money.get("lineChart"))
    if len(chart) >= 2 and age <= 2.0 and any(num(y, 0) for y in chart[0][1]):
        inc_total = sum(num(y, 0) for y in chart[0][1])
        lc.income_actual = inc_total / max(age, 1e-6) if age < 1 else inc_total / max(len(chart[0][1]), 1)
        lc.income_source = "account chart"
    elif lc.net_actual is not None:
        lc.income_actual = lc.net_actual + (lc.running_actual or 0)
        lc.income_source = "balance + running"

    # vehicles of the model catalogue
    models = []
    for v in vehicles:
        parts = [vby.get(p.get("modelId")) for p in as_list(v.get("parts")) if isinstance(p, dict)]
        parts = [p for p in parts if p]
        if parts:
            models.append((v, parts))
    if models:
        lc.running_model = sum(p.running_cost for _, parts in models for p in parts)
        lc.running_ratio = ratio(lc.running_actual, lc.running_model)
    elif vehicles:
        lc.notes.append("vehicle models not found in the catalogue")

    # income per unit and model income for the actual volume
    if cargo and len(pts) >= 2:
        pf = 0.5
        if models:
            pfs = [p.price_factor for _, parts in models for p in parts if p.carries(cargo.key)]
            pf = statistics.mean(pfs) if pfs else 0.5
        if is_pass:
            ipus = [game.income_per_unit(cargo, a, b, pf) for a, b in legs]
            ipu = statistics.mean(ipus)
        else:
            a, b = max(((a, b) for a in pts for b in pts), key=lambda ab: math.dist(ab[0][:2], ab[1][:2]))
            ipu = game.income_per_unit(cargo, a, b, pf)
        lc.ipu_model = ipu * ticket_mult
        if lc.transported_per_year is not None:
            lc.income_model = lc.transported_per_year * lc.ipu_model
            lc.income_ratio = ratio(lc.income_actual, lc.income_model)

    # trip time, speed and capacity
    trips, speed_fs, caps_year = [], [], []
    for v, parts in models:
        speed = min((p.top_speed for p in parts if p.top_speed > 0), default=0) or num(v.get("topSpeed"), 0)
        carriers_of = [p for p in parts if cargo and p.carries(cargo.key)]
        cap = sum(p.carries(cargo.key) for p in carriers_of) if cargo else 0
        load_speed = min((p.load_speed for p in carriers_of), default=1.0) or 1.0
        load_t = cap / max(load_speed, 0.1)
        if speed <= 0 or not legs:
            continue
        t_model = lc.loop_route_m / (speed * SPEED_FACTOR_MODEL) + len(pts) * (load_t + STOP_DELAY_S)
        t_act = section_time_s(as_list(v.get("sectionTimes"))) or departures_cycle_s(as_list(v.get("lineStopDepartures")))
        if cap:
            caps_year.append(cap * year_s / t_model * (2 if is_pass else 1))
        if t_act:
            trips.append((t_act, t_model))
            moving = t_act - len(pts) * (load_t + STOP_DELAY_S)
            if moving > 0:
                speed_fs.append(lc.loop_route_m / moving / speed)
            if lc.regress is None:
                lc.regress = [lc.loop_route_m / speed, len(pts) * load_t, float(len(pts)), t_act]
    if trips:
        lc.trip_actual_s = statistics.mean(t for t, _ in trips)
        lc.trip_model_s = statistics.mean(m for _, m in trips)
        lc.trip_ratio = ratio(lc.trip_actual_s, lc.trip_model_s)
    lc.speed_factor = median(speed_fs)
    if caps_year:
        lc.model_capacity_per_year = sum(caps_year)
    # capacity usage as reported by the game
    for r in as_list(line.get("capacityUsage")):
        if isinstance(r, dict) and cargo and str(r.get("cargo")) == str(cargo.id) and num(r.get("capacity"), 0) > 0:
            lc.capacity_used = num(r.get("used"))
            lc.load_factor = ratio(r.get("used"), r.get("capacity"))

    # what the model would build for this volume between the two farthest stops
    if cargo and len(pts) >= 2 and mode in ("road", "rail", "water") and lc.transported_per_year:
        try:
            a, b = max(((a, b) for a in pts for b in pts), key=lambda ab: math.dist(ab[0][:2], ab[1][:2]))
            kind = "town" if is_pass else "industry"
            ev = evaluate_link(game, list(vby.values()), cargo, Endpoint(kind, -1, "A", *a), Endpoint(kind, -2, "B", *b),
                               mode, lc.transported_per_year)
            if ev:
                lc.eval_vehicles = ev.n_consists
                lc.eval_income = ev.income * ticket_mult
                lc.eval_running = ev.running
                lc.eval_consist = ev.consist.label if ev.consist else None
        except Exception as e:  # noqa: BLE001 - diagnostics only
            lc.notes.append(f"evaluate_link failed: {e}")
    return lc


# --------------------------------------------------------------------------- fits

def fit(game: Game, export: dict, rows: list[LineCalib]) -> dict:
    usable = [r for r in rows if r.age_years >= MIN_AGE_YEARS]
    out: dict = {"lines_total": len(rows), "lines_used": len(usable)}
    out["income_factor"] = median([r.income_ratio for r in usable])
    out["running_factor"] = median([r.running_ratio for r in usable])
    out["trip_time_factor"] = median([r.trip_ratio for r in usable])
    out["speed_factor"] = median([r.speed_factor for r in usable])
    out["speed_factor_model"] = SPEED_FACTOR_MODEL
    out["load_factor"] = median([r.load_factor for r in usable])

    # trip = a * route/speed + b * loading + c * stops  (needs >= 3 different lines)
    reg = [r.regress for r in usable if r.regress]
    if len(reg) >= 3:
        try:
            import numpy as np
            A = np.array([x[:3] for x in reg], dtype=float)
            y = np.array([x[3] for x in reg], dtype=float)
            coef, _res, rank, _sv = np.linalg.lstsq(A, y, rcond=None)
            if rank == 3 and coef[0] > 0:
                out["regression"] = {"speed_factor": 1.0 / coef[0], "loading_multiplier": coef[1],
                                     "stop_delay_s": coef[2], "n": len(reg)}
        except Exception as e:  # noqa: BLE001
            out["regression_error"] = str(e)

    # passengers: trips per resident per year over the towns served by passenger lines
    res_by_town = {}
    usage_by_town = {}
    for t in as_list((export.get("towns_stats") or {}).get("towns")):
        if not isinstance(t, dict):
            continue
        caps = as_list(t.get("personCapacities"))
        if caps and num(caps[0]) is not None:
            res_by_town[t.get("id")] = caps[0]
        if num(t.get("lineUsage")) is not None:
            usage_by_town[t.get("id")] = t["lineUsage"]
    for t in game.towns:
        res_by_town.setdefault(t.id, t.capacities.get("residential", 0))
    pas = [r for r in usable if r.passenger and r.transported_per_year is not None]
    if pas:
        towns = sorted({t for r in pas for t in r.served_towns})
        residents = sum(res_by_town.get(t, 0) for t in towns)
        passengers = sum(r.transported_per_year for r in pas)
        out["passengers"] = {
            "lines": len(pas), "towns": towns, "residents": residents, "passengers_per_year": passengers,
            "trips_per_resident": passengers / residents if residents else None,
            "town_line_usage": {str(t): usage_by_town.get(t) for t in towns},
        }
    comp = export.get("company") or {}
    out["company"] = {k: comp.get(k) for k in ("level", "experience", "potentialLevel", "ticketPriceMultiplier",
                                               "worldPopulation", "basePopulation")}
    return out


# --------------------------------------------------------------------------- report

def _f(x, fmt="{:,.0f}"):
    return "-" if num(x) is None else fmt.format(x)


def run(export_path: Path = DATA / "export.json", out_path: Path | None = DATA / "calibration.json",
        stream=None) -> dict:
    stream = stream or sys.stdout

    def p(*a):
        print(*a, file=stream)

    export = json.loads(Path(export_path).read_text(encoding="utf-8"))
    version = export.get("version", 0)
    lines = [l for l in as_list(export.get("lines")) if isinstance(l, dict)]
    if version < 5:
        p(f"export v{version}: no line statistics (needs v5) - redeploy the mod, press export in game")
    if not lines:
        p("no lines yet")
        return {"version": version, "lines": [], "fits": None}

    game = Game(export, json.loads((DATA / "industry_recipes.json").read_text(encoding="utf-8"))
                if (DATA / "industry_recipes.json").exists() else {})
    vehicles = load_vehicles(game)
    vby = {v.id: v for v in vehicles}
    comp = export.get("company") or {}
    ticket_mult = num(comp.get("ticketPriceMultiplier"), 1.0) or 1.0

    rows = []
    for l in lines:
        if l.get("error"):
            p(f"line {l.get('id')}: export error {l['error']}")
            continue
        rows.append(analyse_line(game, vby, l, ticket_mult))

    p(f"Calibration: {len(rows)} lines, year {game.year}, ticket multiplier {ticket_mult:.3f}")
    p(f"{'line':28} {'mode':5} {'cargo':12} {'age d':>6} {'veh':>3} {'units/y':>8} "
      f"{'income/y':>10} {'model':>10} {'ratio':>6} {'run/y':>9} {'model':>9} {'trip s':>7} {'model':>7} {'spd f':>5} {'load':>5}")
    for r in rows:
        p(f"{r.name[:28]:28} {r.mode:5} {str(r.cargo)[:12]:12} {r.age_years * 365.25:6.0f} {r.vehicles:3d} "
          f"{_f(r.transported_per_year):>8} {_f(r.income_actual):>10} {_f(r.income_model):>10} "
          f"{_f(r.income_ratio, '{:.2f}'):>6} {_f(r.running_actual):>9} {_f(r.running_model):>9} "
          f"{_f(r.trip_actual_s):>7} {_f(r.trip_model_s):>7} {_f(r.speed_factor, '{:.2f}'):>5} "
          f"{_f(r.load_factor, '{:.2f}'):>5}")
        if r.eval_vehicles is not None:
            p(f"    model for this volume: {r.eval_vehicles}x {r.eval_consist}, income {_f(r.eval_income)}, "
              f"running {_f(r.eval_running)}; model capacity {_f(r.model_capacity_per_year)}/y")
        for n in r.notes:
            p(f"    note: {n}")

    fits = fit(game, export, rows)
    p("")
    p("Fitted parameters (median over lines older than "
      f"{MIN_AGE_YEARS * 365.25:.0f} days, {fits['lines_used']} of {fits['lines_total']}):")
    p(f"  income factor (actual / model)   {_f(fits['income_factor'], '{:.3f}')}")
    p(f"  running cost factor              {_f(fits['running_factor'], '{:.3f}')}")
    p(f"  trip time factor                 {_f(fits['trip_time_factor'], '{:.3f}')}")
    p(f"  effective speed factor           {_f(fits['speed_factor'], '{:.3f}')} (model {SPEED_FACTOR_MODEL})")
    p(f"  capacity load factor             {_f(fits['load_factor'], '{:.2f}')}")
    if "regression" in fits:
        g = fits["regression"]
        p(f"  regression (n={g['n']}): speed factor {g['speed_factor']:.3f}, loading x{g['loading_multiplier']:.2f}, "
          f"stop delay {g['stop_delay_s']:.1f} s")
    if "passengers" in fits:
        ps = fits["passengers"]
        p(f"  passengers: {_f(ps['passengers_per_year'])}/y over {_f(ps['residents'])} residents "
          f"-> {_f(ps['trips_per_resident'], '{:.3f}')} trips per resident per year")
    c = fits["company"]
    p(f"  company: level {c.get('level')}, xp {c.get('experience')}, world population {c.get('worldPopulation')}")

    result = {"version": version, "lines": [asdict(r) for r in rows], "fits": fits}
    if out_path:
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        p(f"-> {out_path}")
    return result


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")    # Cyrillic line names on a cp125x console
    except AttributeError:
        pass
    run()
