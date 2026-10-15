"""Tests for tf3planner.buildings (town buildings, export v7). Run with pytest or directly."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tf3planner.buildings import FALLBACK_BULLDOZE_COST, TownBuildings, buildings_in_corridor  # noqa: E402
from tf3planner.model import DATA  # noqa: E402


def export(boxes, land_use=None, parcels=None, level=None, samples=None, lu_values=True):
    """boxes: [(xmin, ymin, xmax, ymax) or None (unknown -> pos at (x, y) given as ('p', x, y))]"""
    M = len(boxes)
    bbox, pos = [], []
    for b in boxes:
        if b[0] == "p":
            bbox += [0] * 6
            pos += [b[1], b[2], 5]
        else:
            bbox += [b[0], b[1], 0, b[2], b[3], 10]
            pos += [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2, 5]
    t = {"version": 1, "count": M, "ids": [800 + i for i in range(M)], "town": [200] * M,
         "construction": [900 + i for i in range(M)], "file": [0] * M, "files": ["::/buildings/a/r1/1x1_01.con"],
         "landUse": land_use or [0] * M, "capacity": [10] * M, "level": level or [1] * M,
         "parcels": parcels or [1] * M, "depth": [1] * M, "height": [8.0] * M, "bbox": bbox, "pos": pos,
         "timeBuilt": [0] * M, "enumeration": "town2BuildingMap", "failed": 0}
    if lu_values:
        t["landUseValues"] = {"RESIDENTIAL": 0, "COMMERCIAL": 1, "INDUSTRIAL": 2}
    if samples is not None:
        t["bulldozeSamples"] = samples
    return {"version": 7, "townBuildings": t}


def test_old_export():
    assert TownBuildings.from_export({"version": 6}) is None
    assert len(buildings_in_corridor(TownBuildings.from_export(export([(0, 0, 1, 1)])), [(5, 5)], 0)) == 0


def test_corridor():
    tb = TownBuildings.from_export(export([
        (0, -5, 10, 5),          # 0: on the line y=0
        (20, 8, 30, 18),         # 1: 8 m above the line -> inside a 20 m corridor, outside 10 m
        (40, 30, 50, 40),        # 2: far
        ("p", 100, 4),           # 3: unknown bbox -> 6 m half size around (100, 4) -> touches y=0 within 0 m
        (200, -3, 210, 3),       # 4: beyond the end (x=150) by 50 m
        (148, 60, 152, 70),      # 5: near the second segment (150,0)->(150,100)
    ]))
    line = [(-10, 0), (150, 0), (150, 100)]
    idx, d = tb.in_corridor(line, 20.0, return_distance=True)
    assert list(idx) == [0, 1, 3, 5], idx
    assert d[0] == 0 and abs(d[1] - 8) < 1e-9 and d[2] == 0
    assert list(tb.in_corridor(line, 10.0)) == [0, 3, 5]
    # diagonal segment passing near a corner: distance = corner-to-line distance
    tb2 = TownBuildings.from_export(export([(10, 0, 20, 10)]))
    _, dd = tb2.in_corridor([(-30, 0), (30, 60)], 100, return_distance=True)      # line y = x + 30
    assert abs(dd[0] - 30 / math.sqrt(2)) < 1e-9                                     # corner (10, 10)
    # segment crossing a box without endpoints inside
    tb3 = TownBuildings.from_export(export([(0, 0, 10, 10)]))
    assert list(tb3.in_corridor([(-5, 5), (15, 5)], 0.0)) == [0]
    assert list(tb3.in_corridor([(-5, 20), (15, 20)], 2.0)) == []


def test_bulldoze_costs():
    samples = [{"landUse": 0, "level": 1, "parcels": 1, "costs": 1000},
               {"landUse": 0, "level": 2, "parcels": 2, "costs": 3000},
               {"landUse": 1, "level": 1, "parcels": 1, "costs": -5},     # unusable
               {"landUse": 2, "level": 1, "parcels": 1, "error": "x"}]
    tb = TownBuildings.from_export(export([(0, 0, 1, 1)] * 5, land_use=[0, 0, 0, 1, 2], parcels=[1, 2, 4, 1, 1],
                                          level=[1, 2, 3, 1, 1], samples=samples))
    c = tb.bulldoze_cost()
    assert c[0] == 1000 and c[1] == 3000
    assert c[2] == pytest.approx(4 * np.mean([1000, 1500]))          # same land use, per parcel
    assert c[3] == c[4] == np.median([1000, 3000])                     # no usable sample of that land use
    assert tb.demolition_cost([0, 1]) == 4000
    tb0 = TownBuildings.from_export(export([(0, 0, 1, 1)]))
    assert tb0.bulldoze_cost()[0] == FALLBACK_BULLDOZE_COST


def test_raw_land_use_without_enum():
    tb = TownBuildings.from_export(export([(0, 0, 1, 1)] * 3, land_use=[1, 2, 3], lu_values=False))
    assert list(tb.land_use) == [0, 1, 2]


def test_from_fake_game_export():
    """The offline fake-API export (test_dump) loads into TownBuildings."""
    from test_dump import _fake_lua, _run_export
    from tf3planner.loader import _to_py
    _ok, _s, saved = _run_export(_fake_lua())
    tb = TownBuildings.from_export(_to_py(saved))
    assert len(tb) == 3 and list(tb.ids) == [800, 801, 802]
    assert list(tb.land_use) == [0, 1, 2] and tb.file_name(2).endswith("2x2_01.con")
    assert np.allclose(tb.bbox[0], [95, 195, 105, 205])
    assert list(tb.bulldoze_cost()) == [150, 300, 450]
    assert list(buildings_in_corridor(tb, [(0, 200), (400, 200)], 20)) == [0]


def test_industry_footprints():
    from tf3planner.buildings import DEFAULT_INDUSTRY_HALF, IndustryFootprints
    exp = {"industries": [
        {"id": 1, "pos": {"x": 900, "y": 0}, "footprint": {"ind": [895, -5, 905, 5], "con": [840, -60, 960, 60],
                                                           "parts": [850, -50, 900, 0, 900, 0, 950, 50],
                                                           "fields": [1000, -100, 1200, 100]}},
        {"id": 2, "pos": {"x": 0, "y": 0}, "footprint": {"fields": {1: 1000, 2: -100, 3: 1200, 4: 100}}},  # same field
        {"id": 3, "pos": {"x": -500, "y": 0}}]}
    fp = IndustryFootprints.from_export(exp)
    assert fp.exact
    assert fp.kind.count("field") == 1 and "con" not in fp.kind[:3]          # parts replace the construction box
    assert fp.kind.count("square") == 2                                     # industries 2 and 3: no own footprint
    assert fp.clearance(925, 25) < 0 and abs(fp.clearance(925, 80) - 30) < 1e-9
    assert fp.clear(925, 120, 22) and not fp.clear(925, 70, 22)
    d, owner, kind = fp.nearest(1100, 0)
    assert d < 0 and owner == -1 and kind == "field"
    assert abs(fp.clearance(-500 + DEFAULT_INDUSTRY_HALF + 10, 0) - 10) < 1e-9
    old = IndustryFootprints.from_export({"industries": [{"id": 5, "pos": {"x": 0, "y": 0}}]})
    assert not old.exact and len(old) == 1


def test_siting_station_next_to_industry():
    """The cargo station is placed beside the industry, never on its footprint."""
    import numpy as np
    from types import SimpleNamespace
    from tf3planner.buildings import IndustryFootprints
    from tf3planner.model import Terrain
    from tf3planner.siting import FOOTPRINT_M, Siting
    S = Siting.__new__(Siting)
    S.t = Terrain(-2000.0, -2000.0, 32.0, np.full((130, 130), 10.0, dtype=np.float32), 0.0)
    S.tm, S.plot_tree, S.street_tree = None, None, None
    S.towns, S.by_id, S.tc, S.tr = [], {}, np.zeros((0, 2)), np.zeros(0)
    S.rating_usd = 0.0
    S.ifp = IndustryFootprints.from_export({"industries": [
        {"id": 7, "pos": {"x": 0, "y": 0}, "footprint": {"parts": [-120, -80, 120, 80]}}]})
    site = S.industry_station({"x": 0, "y": 0, "id": 7, "name": "Карьер"}, (1000, 0), 175.0, "road")
    gap = S.industry_gap(7, site.x, site.y)
    assert site.ok and gap >= FOOTPRINT_M["station_road"] + 5, (site, gap)
    assert gap <= 0.6 * 175 and site.x > 120                    # adjacent, on the route side (east)
    assert "вплотную" in site.reason


def test_real_town_buildings():
    path = DATA / "export.json"
    if not path.exists():
        pytest.skip("no data/export.json")
    exp = json.loads(path.read_text(encoding="utf-8"))
    if (exp.get("version") or 0) < 7:
        pytest.skip(f"data/export.json is v{exp.get('version')} (< 7): restart the game and export again")
    tb = TownBuildings.from_export(exp)
    if tb is None:
        pytest.skip("export v7 without town buildings: " + str([e for e in (exp.get("errors") or [])]))
    assert len(tb) > 100 and tb.failed < 0.01 * len(tb)
    assert (tb.land_use >= 0).mean() > 0.95
    w = tb.bbox[:, 2] - tb.bbox[:, 0]
    assert 3 < np.median(w) < 80
    # buildings are near their town centres
    towns = {t["id"]: t for t in exp["towns"]}
    d = [math.hypot(tb.pos[i, 0] - towns[t]["pos"]["x"], tb.pos[i, 1] - towns[t]["pos"]["y"])
         for i, t in enumerate(tb.town.tolist()) if t in towns and towns[t].get("pos")]
    assert np.median(d) < 2000


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except pytest.skip.Exception as ex:
                print(f"SKIP {name}: {ex}")
            except Exception as ex:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(ex).__name__}: {ex}")
    sys.exit(1 if failed else 0)
