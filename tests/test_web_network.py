"""Tests for GET /api/network payload (tf3planner.web.server.network_payload) on a synthetic export v6.
Run with pytest or directly: python tests/test_web_network.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_network import make_export  # noqa: E402
from tf3planner.network import TransportNetwork  # noqa: E402
from tf3planner.web import server  # noqa: E402

HW = "::/infrastructure/street/highway/highway_new_medium.street_template"


def _pts(flat):
    return list(zip(flat[0::2], flat[1::2]))


def test_chains_merge_and_classes():
    # country road 0-1-2-3 (3 edges, one chain), a junction at 1 with a town street 1-4,
    # player track 5-6-7 with a bridge on 6-7, a highway 8-9
    nodes = [(0, 0), (100, 0), (200, 0), (300, 0), (100, 100), (0, 500), (100, 500), (200, 500), (0, 900), (500, 900)]
    edges = [(0, 1, 0), (1, 2, 0), (2, 3, 0), (1, 4, 1), (5, 6, 2), (6, 7, 2), (8, 9, 0)]
    exp = make_export(nodes, edges, owned=[0, 0, 0, 0, 1, 1, 0], struct=[0, 0, 0, 0, 0, 1, 0])
    exp["network"]["templates"] = exp["network"]["templates"] + [{"name": HW, "roadType": 1, "speed": 27.8}]
    exp["network"]["edgeTemplate"][6] = len(exp["network"]["templates"]) - 1
    net = TransportNetwork.from_export(exp)
    p = server.network_payload(net)
    c = p["classes"]
    assert p["available"] and set(c) == {"country", "town", "track_player", "highway"}
    # country 0-1-2-3: node 1 has degree 2 within the country group (the town street is another group) -> one line
    country = c["country"]["normal"]
    assert len(country) == 1 and _pts(country[0]) in ([(0, 0), (300, 0)], [(300, 0), (0, 0)])   # collinear -> 2 pts
    assert c["town"]["normal"] == [[100, 0, 100, 100]] or c["town"]["normal"] == [[100, 100, 100, 0]]
    assert len(c["track_player"]["normal"]) == 1 and len(c["track_player"]["bridge"]) == 1
    assert len(c["highway"]["normal"]) == 1
    assert abs(p["km"]["country"] - 0.3) < 1e-9


def test_curves_get_interior_points_and_round():
    nodes = [(0, 0), (640, 640)]
    tangents = [1000, 0, 0, 0, 1000, 0]         # quarter-circle-like curve
    net = TransportNetwork.from_export(make_export(nodes, [(0, 1, 0)], tangents=tangents))
    line = server.network_payload(net)["classes"]["country"]["normal"][0]
    pts = _pts(line)
    assert 4 <= len(pts) <= 9 and all(isinstance(v, int) for v in line)
    # the curve bulges below/right of the chord y = x
    assert any(x - y > 50 for x, y in pts[1:-1])


def test_loop_and_unavailable():
    nodes = [(0, 0), (100, 0), (100, 100), (0, 100)]
    net = TransportNetwork.from_export(make_export(nodes, [(0, 1, 1), (1, 2, 1), (2, 3, 1), (3, 0, 1)]))
    lines = server.network_payload(net)["classes"]["town"]["normal"]
    assert len(lines) == 1 and lines[0][:2] == lines[0][-2:] and len(lines[0]) == 10
    r = server._network_unavailable({"version": 5})
    assert r["available"] is False and "5" in r["reason"]
    r = server._network_unavailable({"version": 6, "errors": [{"section": "networkGraph", "error": "boom\nstack"}]})
    assert r["reason"].endswith("boom")


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as ex:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(ex).__name__}: {ex}")
    sys.exit(1 if failed else 0)
