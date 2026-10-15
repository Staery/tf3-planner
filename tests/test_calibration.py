"""Tests for tf3planner.calibration. Run with pytest or directly: python tests/test_calibration.py"""
from __future__ import annotations

import io
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner import calibration  # noqa: E402
from tf3planner.model import DATA, Game  # noqa: E402
from tf3planner.vehicles import available, load_vehicles  # noqa: E402

EXPORT = DATA / "export.json"


def test_no_lines():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "export.json"
        p.write_text(json.dumps({"version": 5, "lines": {}}), encoding="utf-8")
        out = io.StringIO()
        r = calibration.run(p, None, stream=out)
        assert "no lines yet" in out.getvalue()
        assert r["lines"] == [] and r["fits"] is None
        # v4 export: explains that a re-export is needed
        p.write_text(json.dumps({"version": 4}), encoding="utf-8")
        out = io.StringIO()
        calibration.run(p, None, stream=out)
        assert "needs v5" in out.getvalue() and "no lines yet" in out.getvalue()


def _synthetic_export() -> tuple[dict, dict]:
    """Real map export + one bus line between the two closest towns, with plausible game figures."""
    export = json.loads(EXPORT.read_text(encoding="utf-8"))
    recipes = json.loads((DATA / "industry_recipes.json").read_text(encoding="utf-8"))
    game = Game(export, recipes)
    buses = [v for v in available(load_vehicles(game), game.year, "road", "bus") if v.capacity.get("passengers")]
    bus = buses[0]
    t1, t2 = min(((a, b) for a in game.towns for b in game.towns if a.id < b.id),
                 key=lambda ab: math.dist((ab[0].x, ab[0].y), (ab[1].x, ab[1].y)))
    pid = next(c.id for c in game.cargos.values() if c.is_passenger)
    year_ms = game.year_ms
    now = 3 * year_ms
    dist = math.dist((t1.x, t1.y), (t2.x, t2.y))
    trip_s = 2 * dist * 1.3 / (bus.top_speed * 0.8) + 2 * (bus.capacity["passengers"] / bus.load_speed + 4)
    vehicle = {
        "id": 1, "carrier": "ROAD", "runningCost": bus.running_cost * 1.1,
        "parts": [{"modelId": bus.id, "purchaseTime": now - year_ms, "maintenanceState": 1.0}],
        "purchaseTime": now - year_ms, "sectionTimes": [trip_s / 2, trip_s / 2],
    }
    transported = 2000
    ipu = game.income_per_unit(game.cargos[pid], (t1.x, t1.y, t1.z), (t2.x, t2.y, t2.z), bus.price_factor)
    income = transported * ipu * 0.9
    line = {
        "id": 400, "name": "Test bus", "carriers": ["ROAD"], "firstPurchaseTime": now - year_ms,
        "stops": [{"pos": {"x": t1.x, "y": t1.y}}, {"pos": {"x": t2.x, "y": t2.y}}],
        "vehicles": [vehicle, dict(vehicle, id=2)],
        "money": {"t0": now - year_ms, "t1": now,
                  "line": {"maintIncome": income - 2 * vehicle["runningCost"]}, "vehicles": {}},
        "capacityUsage": [{"cargo": pid, "used": 20, "capacity": 2 * bus.capacity["passengers"]}],
        "transportedPerYear": transported,
    }
    export.update(version=5, lines=[line],
                  company={"ticketPriceMultiplier": 1.0, "level": 1, "experience": 10, "worldPopulation": 1000},
                  towns_stats={"towns": [{"id": t1.id, "personCapacities": [300, 0, 0], "lineUsage": 0.2},
                                         {"id": t2.id, "personCapacities": [200, 0, 0], "lineUsage": 0.1}]})
    expect = {"income": income, "transported": transported, "residents": 500, "towns": sorted([t1.id, t2.id]),
              "capacity": 2 * bus.capacity["passengers"]}
    return export, expect


def test_synthetic_line():
    if not EXPORT.exists():
        return
    export, expect = _synthetic_export()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "export.json"
        p.write_text(json.dumps(export), encoding="utf-8")
        out = io.StringIO()
        r = calibration.run(p, Path(d) / "calibration.json", stream=out)
        text = out.getvalue()
        assert "Test bus" in text and "Fitted parameters" in text, text
        row = r["lines"][0]
        assert row["passenger"] and row["mode"] == "road" and row["vehicles"] == 2
        assert abs(row["income_actual"] - expect["income"]) < 1e-6 * max(1, expect["income"])
        assert abs(row["income_ratio"] - 0.9) < 1e-3         # passengers: mean of both directions (climb term)
        assert abs(row["running_ratio"] - 1.1) < 1e-6
        assert 0.75 < row["speed_factor"] < 0.85           # trips were generated at 0.8 of top speed
        assert row["trip_ratio"] > 1.0
        assert row["capacity_used"] == 20 and abs(row["load_factor"] - 20 / expect["capacity"]) < 1e-9
        fits = r["fits"]
        assert abs(fits["income_factor"] - 0.9) < 1e-3
        ps = fits["passengers"]
        assert ps["towns"] == expect["towns"] and ps["residents"] == expect["residents"]
        assert abs(ps["trips_per_resident"] - expect["transported"] / expect["residents"]) < 1e-9
        assert json.loads((Path(d) / "calibration.json").read_text(encoding="utf-8"))["fits"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
