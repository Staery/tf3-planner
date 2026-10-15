"""Tests for tf3planner.network (existing street/track graph, export v6).
Run with pytest or directly: python tests/test_network.py"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner.model import DATA  # noqa: E402
from tf3planner.network import COUNTRY, PLAYER, RAIL, ROAD, TOWN, TRAM, OTHER, TransportNetwork  # noqa: E402

T_COUNTRY = "::/infrastructure/street/country/country_old_small.street_template"
T_TOWN = "::/infrastructure/street/town/town_old_small.street_template"
T_TRACK = "::/infrastructure/track/simple/simple.street_template"
T_TRAM = "::/infrastructure/street/tram/tram_old.street_template"
T_TAXI = "::/infrastructure/street/airport/airport_taxiway_old_grass.street_template"
TEMPLATES = [
    {"name": T_COUNTRY, "id": 29, "roadType": 1, "speed": 16.67, "lanes": 3},
    {"name": T_TOWN, "id": 75, "roadType": 1, "speed": 11.11, "lanes": 3},
    {"name": T_TRACK, "id": 86, "roadType": 0, "speed": 27.777, "lanes": 1},
    {"name": T_TRAM, "id": 82, "roadType": 1, "speed": 11.11, "lanes": 2},
    {"name": T_TAXI, "id": 8, "roadType": 1, "speed": 8.33, "lanes": 3},
]


def make_export(nodes: list[tuple[float, float]], edges: list[tuple[int, int, int]], owned=None, struct=None,
                tangents=None, street_templates: bool = False) -> dict:
    """nodes [(x, y)], edges [(n0, n1, template index)]"""
    m = len(edges)
    net = {
        "graphVersion": 1, "nodes": len(nodes), "edges": m,
        "nodeIds": [5000 + k for k in range(len(nodes))],
        "nodePos": [c for x, y in nodes for c in (x, y, 10.0)],
        "edgeIds": [9000 + j for j in range(m)],
        "edgeNodes": [k for a, b, _ in edges for k in (a, b)],
        "edgeTemplate": [t for _, _, t in edges],
        "edgeRoadType": [TEMPLATES[t]["roadType"] for _, _, t in edges],
        "edgeStruct": struct or [0] * m, "edgeTypeIndex": [-1] * m,
        "edgeLength": [-1] * m, "edgeOwned": owned or [0] * m,
        "templates": TEMPLATES,
        "roadTypeValues": {"STREET": 1, "TRACK": 0}, "structValues": {"NORMAL": 0, "BRIDGE": 1, "TUNNEL": 2},
    }
    if tangents is not None:
        net["edgeTangents"] = tangents
    exp = {"version": 6, "network": net}
    if street_templates:
        exp["streetTemplates"] = [{"name": T_COUNTRY, "data": {"roadType": 1, "laneConfigs": [{"speed": 20.0}]}}]
    return exp


def test_old_export_has_no_network():
    assert TransportNetwork.from_export({"version": 5, "network": {"lines": [], "stations": []}}) is None
    assert TransportNetwork.from_export({}) is None


def test_classification():
    nodes = [(0, 0), (100, 0), (200, 0), (300, 0), (400, 0), (500, 0)]
    edges = [(0, 1, 0), (1, 2, 1), (2, 3, 2), (3, 4, 3), (4, 5, 4), (0, 2, 0)]
    net = TransportNetwork.from_export(make_export(nodes, edges, owned=[0, 0, 1, 0, 0, 1], struct=[0, 1, 0, 2, 0, 0],
                                                   street_templates=True))
    assert list(net.kind) == [ROAD, ROAD, RAIL, TRAM, OTHER, ROAD]
    assert list(net.owner) == [COUNTRY, TOWN, PLAYER, COUNTRY, COUNTRY, PLAYER]
    assert list(net.struct) == [0, 1, 0, 2, 0, 0]
    # export-wide streetTemplates win over the sampled lane speed; others use the sample
    assert net.speed[0] == 20.0 and abs(net.speed[1] - 11.11) < 1e-9 and abs(net.speed[2] - 27.777) < 1e-9
    assert np.allclose(net.length, [100, 100, 100, 100, 100, 200])
    s = net.summary()
    assert s["km"]["road"]["town"] == 0.1 and s["km"]["road"]["bridges"] == 0.1
    assert s["km"]["rail"]["player"] == 0.1


def test_paths_components_and_matrix():
    # road A: 0-1-2-3 straight slow town street (300 m @ 11.11) and a fast detour 0-4-3 (2 x 180 m @ 16.67)
    nodes = [(0, 0), (100, 0), (200, 0), (300, 0), (150, 100), (5000, 0), (5100, 0)]
    edges = [(0, 1, 1), (1, 2, 1), (2, 3, 1), (0, 4, 0), (4, 3, 0), (5, 6, 0)]
    net = TransportNetwork.from_export(make_export(nodes, edges))
    by_len = net.shortest_path((0, -5), (300, 5), "road", "length")
    assert list(by_len.nodes) == [0, 1, 2, 3] and abs(by_len.length_m - 300) < 1e-9
    assert abs(by_len.snap_a_m - 5) < 1e-9
    by_time = net.shortest_path((0, 0), (300, 0), "road", "time")
    assert list(by_time.nodes) == [0, 4, 3] and by_time.time_s < by_len.time_s
    assert abs(by_time.length_m - 2 * math.hypot(150, 100)) < 1e-6
    assert net.shortest_path((0, 0), (5100, 0), "road") is None
    lab = net.components("road")
    assert lab[0] == lab[3] == lab[4] and lab[5] == lab[6] and lab[0] != lab[5]
    assert lab[0] == 0                                   # largest component first
    k, d = net.nearest_node(5040, 10, "road")
    assert k == 5 and abs(d - math.hypot(40, 10)) < 1e-9
    assert net.nearest_node(0, 0, "rail") == (-1, math.inf)
    pm = net.path_matrix([(0, 0), (300, 0), (5000, 0)], "road")
    assert abs(pm["time"][0, 1] - by_time.time_s) < 1e-6 and abs(pm["length"][0, 1] - 2 * math.hypot(150, 100)) < 1e-6
    assert np.isinf(pm["length"][0, 2]) and pm["component"][0] != pm["component"][2]
    pl = net.path_matrix([(0, 0), (300, 0)], "road", weight="length")
    assert abs(pl["length"][0, 1] - 300) < 1e-6 and abs(pl["time"][0, 1] - 300 / 11.11) < 1e-6


def test_parallel_edges_keep_cheapest():
    nodes = [(0, 0), (100, 0)]
    net = TransportNetwork.from_export(make_export(nodes, [(0, 1, 1), (0, 1, 0)]))
    p = net.shortest_path(0, 1, "road", "time")
    assert list(p.edges) == [1] and abs(p.time_s - 100 / 16.67) < 1e-9


def test_grid_segments_straight_and_curved():
    nodes = [(0, 0), (640, 0), (0, 640), (640, 1280)]
    edges = [(0, 1, 0), (2, 3, 2)]
    # edge 2->3: S-curve via tangents (both pointing +x)
    tangents = [640, 0, 0, 640, 0, 0, 1200, 0, 0, 1200, 0, 0]
    net = TransportNetwork.from_export(make_export(nodes, edges, tangents=tangents))
    u, v, sp = net.grid_segments("road", 0.0, 0.0, 64.0, 30, 30)
    assert len(u) == 10 and set(u.tolist()) == set(range(10)) and (v - u == 1).all()
    assert np.allclose(sp, 16.67)
    ur, vr, _ = net.grid_segments("rail", 0.0, 0.0, 64.0, 30, 30)
    iy, ix = np.divmod(np.concatenate([ur, vr]), 30)
    assert iy.min() == 10 and iy.max() == 20 and ix.min() == 0 and ix.max() >= 10
    # a curve: not a straight diagonal (some horizontal moves at the ends)
    dyy = np.abs(vr // 30 - ur // 30)
    assert (dyy == 0).any() and (dyy == 1).any()
    # adjacency is always king-move
    assert (np.abs(vr % 30 - ur % 30) <= 1).all() and (dyy <= 1).all()


def test_hash_changes_with_network():
    a = TransportNetwork.from_export(make_export([(0, 0), (100, 0)], [(0, 1, 0)]))
    b = TransportNetwork.from_export(make_export([(0, 0), (100, 0)], [(0, 1, 1)]))
    assert a.hash() != b.hash() and a.hash() == TransportNetwork.from_export(make_export([(0, 0), (100, 0)], [(0, 1, 0)])).hash()


# ---------------------------------------------------------------- real map (export v6)

def _real():
    path = DATA / "export.json"
    if not path.exists():
        pytest.skip("no data/export.json")
    exp = json.loads(path.read_text(encoding="utf-8"))
    if (exp.get("version") or 0) < 6:
        pytest.skip(f"data/export.json is v{exp.get('version')} (< 6): restart the game and export again")
    net = TransportNetwork.from_export(exp)
    if net is None:
        pytest.skip("export v6 without network graph")
    return exp, net


def test_real_network_sanity():
    exp, net = _real()
    assert net.n_edges > 100 and net.n_nodes > 100
    assert net.failed_edges < 0.01 * net.n_edges
    bb = exp["meta"]["bbox"]
    assert (net.xyz[:, 0] >= bb["min"]["x"] - 100).all() and (net.xyz[:, 0] <= bb["max"]["x"] + 100).all()
    road = net.kind == ROAD
    assert road.sum() > 0.5 * net.n_edges
    # edges are short pieces: curve length >= chord, not absurdly longer
    p0, p1 = net.xyz[net.edges[:, 0]], net.xyz[net.edges[:, 1]]
    chord = np.linalg.norm(p1 - p0, axis=1)
    assert (net.length >= chord - 0.2).all()
    assert np.median(net.length / np.maximum(chord, 1)) < 1.2
    # towns sit on their streets
    from tf3planner.routing import load_game, network_nodes
    g = load_game()
    towns = [n for n in network_nodes(g) if n.kind == "town"]
    t0 = time.perf_counter()
    pm = net.path_matrix(towns, "road")
    assert time.perf_counter() - t0 < 30
    assert np.median(pm["snap_dist"]) < 200, pm["snap_dist"]


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
