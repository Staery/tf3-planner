"""Tests for tf3planner.routing. Run with pytest or directly: python tests/test_routing.py"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner.model import Bridge, Terrain, Way  # noqa: E402
from tf3planner.network import TransportNetwork  # noqa: E402
from tf3planner.routing import (RoutePlanner, RoutingParams, build_network, land_components,  # noqa: E402
                                line_json, load_game, network_matrix, route, route_geometry,
                                route_segments_json, segment_totals, water_network)
from tf3planner.routing import _pt_poly_dist  # noqa: E402

RAIL = Way("test_rail", "test", "rail", 100.0, 10.0, 25.0, 0, 0, 0.13, 40, False)
TRESTLE = Bridge("trestle.bridge", "trestle", False, 200.0, 1.0, 16.0, 0, 0)
TUNNEL = Bridge("tunnel_a.tunnel", "tunnel", True, 1000.0, 8.0, 0.0, 0, 0)
SIZE = 129          # fine cells, 32 m -> 4096 m square map


def flat(h0: float = 10.0) -> np.ndarray:
    return np.full((SIZE, SIZE), h0, dtype=np.float32)


def terrain(h: np.ndarray) -> Terrain:
    return Terrain(0.0, 0.0, 32.0, h.astype(np.float32), 0.0)


def planner(h: np.ndarray, bridge=TRESTLE, tunnel=TUNNEL, network=None, townmask=None, **kw) -> RoutePlanner:
    return RoutePlanner(terrain(h), RAIL, bridge, tunnel, RoutingParams(**kw), network=network, townmask=townmask)


TRACK_T = "::/infrastructure/track/simple/simple.street_template"


def existing_track(points: list[tuple[float, float]], speed: float = 20.0) -> TransportNetwork:
    """Existing track polyline as an export v6 network."""
    m = len(points) - 1
    return TransportNetwork.from_export({"version": 6, "network": {
        "nodeIds": list(range(1, len(points) + 1)), "nodePos": [c for x, y in points for c in (x, y, 10.0)],
        "edgeIds": list(range(100, 100 + m)), "edgeNodes": [k for j in range(m) for k in (j, j + 1)],
        "edgeTemplate": [0] * m, "edgeRoadType": [0] * m, "edgeStruct": [0] * m, "edgeTypeIndex": [-1] * m,
        "edgeLength": [-1] * m, "edgeOwned": [1] * m, "roadTypeValues": {"TRACK": 0, "STREET": 1},
        "templates": [{"name": TRACK_T, "roadType": 0, "speed": speed}]}})


def coords():
    yy, xx = np.mgrid[0:SIZE, 0:SIZE] * 32.0
    return xx, yy


# ---------------------------------------------------------------- synthetic

def test_flat_straight_route():
    pl = planner(flat())
    r = pl.route_xy((512, 2048), (3584, 2048))
    assert r is not None
    assert abs(r.length_m - 3072) < 1e-6
    assert abs(r.build_cost - r.length_m * RAIL.cost_per_m) < 1e-3, r.build_cost
    assert abs(r.maintenance_per_year - r.length_m * RAIL.maintenance_per_m) < 1e-3
    assert abs(r.travel_time_s - r.length_m / RAIL.speed) < 1e-3
    assert all(abs(y - 2048) < 1e-9 for _, y in r.polyline)
    assert abs(r.detour_factor - 1.0) < 1e-9 and not r.bridges and not r.tunnels
    # oblique route: 16-connectivity keeps the detour small
    r2 = pl.route_xy((256, 256), (3840, 2048))
    assert 1.0 - 1e-9 <= r2.detour_factor < 1.03, r2.detour_factor
    assert abs(r2.build_cost - r2.length_m * RAIL.cost_per_m) < 1e-3


def test_astar_matches_dijkstra():
    rng = np.random.default_rng(1)
    h = flat() + np.asarray(rng.normal(0, 1, (SIZE, SIZE)), np.float32)
    from scipy import ndimage
    h = ndimage.gaussian_filter(h, 3) * 400 + 50     # smooth hills
    pl = planner(h.astype(np.float32))
    s, t = pl.snap(300, 400), pl.snap(3800, 3500)
    p1, c1 = pl.astar(s, t)
    p2, c2 = pl.dijkstra_path(s, t)
    assert p1 is not None and p2 is not None
    assert abs(c1 - c2) < 1e-6 * c2, (c1, c2)
    r = pl.make_route(p1)
    assert abs(r.generalized_cost - c1) < 1e-3 * c1


def ridge(gap_center: float | None) -> np.ndarray:
    xx, yy = coords()
    h = flat() + 200.0 * np.exp(-((xx - 2048) / 120.0) ** 2)
    if gap_center is not None:
        h = np.where(np.abs(yy - gap_center) < 250, 10.0, h)
    return h.astype(np.float32)


def test_ridge_forces_tunnel():
    pl = planner(ridge(None))
    r = pl.route_xy((1024, 2048), (3072, 2048))
    assert r.tunnel_m > 0 or r.viaduct_m > 0 or r.earthworks_m3 > 0
    assert r.tunnel_m > 0, r.summary()           # bulge above the chord -> tunnel
    assert r.detour_factor < 1.2
    assert r.build_cost > r.length_m * RAIL.cost_per_m * 1.5
    assert r.max_grade <= RAIL.max_slope + 1e-9


def test_ridge_with_gap_forces_detour():
    pl = planner(ridge(2700))
    r = pl.route_xy((1024, 2048), (3072, 2048))
    assert r.tunnel_m == 0, r.summary()
    assert r.detour_factor > 1.02
    assert max(y for _, y in r.polyline) > 2400       # went through the gap
    # without tunnels and without gap the route still exists (viaduct/earthworks)
    r2 = planner(ridge(None), tunnel=None).route_xy((1024, 2048), (3072, 2048))
    assert r2 is not None and r2.tunnel_m == 0


def lake(full_width: bool = False, ry: float = 1500.0, rx: float = 400.0) -> np.ndarray:
    """Elliptic lake (rx across the route, ry along y) or a river across the whole map."""
    xx, yy = coords()
    if full_width:
        wet = np.abs(xx - 2048) < 150
    else:
        wet = np.hypot((xx - 2048) / rx, (yy - 2048) / ry) < 1
    return np.where(wet, -20.0, 10.0).astype(np.float32)


def test_lake_bridge_or_detour():
    a, b = (1024, 2048), (3072, 2048)
    r = planner(lake()).route_xy(a, b)
    assert 700 < r.water_bridge_m < 900 and r.bridges, r.summary()  # bridge beats a long detour
    assert r.detour_factor < 1.05
    expensive = Bridge("stone.bridge", "stone", False, 5000.0, 3.0, 25.0, 0, 0)
    r2 = planner(lake(), bridge=expensive).route_xy(a, b)
    assert r2.water_bridge_m == 0, r2.summary()                       # detour around
    assert r2.detour_factor > 1.1
    r3 = planner(lake(), allow_bridges=False).route_xy(a, b)
    assert r3.water_bridge_m == 0 and r3.detour_factor > 1.1
    # a river across the whole map without bridges -> unreachable
    assert planner(lake(True), allow_bridges=False).route_xy(a, b) is None
    r4 = planner(lake(True)).route_xy(a, b)
    assert 250 < r4.water_bridge_m < 400, r4.water_bridge_m
    # bridge cost: track + bridge on the wet length
    assert r4.build_cost > r4.length_m * RAIL.cost_per_m + r4.water_bridge_m * TRESTLE.cost_per_m * 0.99


def test_bridge_height_reference():
    a, b = (1024, 2048), (3072, 2048)
    deep = np.where(lake(True) < 0, -100.0, 10.0).astype(np.float32)
    rw = planner(deep, bridge_height_ref="water").route_xy(a, b)
    rg = planner(deep, bridge_height_ref="ground").route_xy(a, b)
    assert rg.build_cost > 50 * rw.build_cost / 3


def test_value_of_time_prefers_faster():
    a, b = (1024, 2048), (3072, 2048)
    # round lake, very cheap but slow bridge (16 m/s vs 25 m/s track): bridged when time is
    # free, avoided when travel time is expensive
    cheap_slow = Bridge("trestle.bridge", "trestle", False, 20.0, 1.0, 16.0, 0, 0)
    pond = lake(ry=600, rx=600)
    r0 = planner(pond, bridge=cheap_slow, value_of_time=0.0).route_xy(a, b)
    r1 = planner(pond, bridge=cheap_slow, value_of_time=1e4).route_xy(a, b)
    assert r0.water_bridge_m > 1000, r0.summary()
    assert r1.water_bridge_m < r0.water_bridge_m and r1.travel_time_s < r0.travel_time_s, r1.summary()


# ---------------------------------------------------------------- existing network (export v6)

U_TRACK = [(512, 2048), (512, 3008), (3584, 3008), (3584, 2048)]     # a U-shaped detour A -> B


def test_existing_way_is_free_and_followed():
    a, b = (512, 2048), (3584, 2048)
    net = existing_track(U_TRACK)
    direct = planner(flat()).route_xy(a, b)
    pl = planner(flat(), network=net)
    assert pl.existing_edges > 0
    r = pl.route_xy(a, b)
    assert r.build_cost < 0.01 * direct.build_cost, r.summary()
    assert r.maintenance_per_year < 0.01 * direct.maintenance_per_year
    assert r.reused_m > 0.98 * r.length_m
    assert max(y for _, y in r.polyline) > 2990                      # follows the U
    assert 4900 < r.length_m < 5100
    assert abs(r.travel_time_s - r.length_m / 20.0) < 1.0           # existing way speed, not RAIL.speed
    # use_existing=False ignores it; the A* path (existing edges) matches Dijkstra
    assert planner(flat(), network=net, use_existing=False).route_xy(a, b).build_cost == direct.build_cost
    s, t = pl.snap(*a), pl.snap(*b)
    (p1, c1), (p2, c2) = pl.astar(s, t), pl.dijkstra_path(s, t)
    assert abs(c1 - c2) < 1e-9 + 1e-9 * c2


def test_existing_way_vs_value_of_time():
    a, b = (512, 2048), (3584, 2048)
    net = existing_track(U_TRACK, speed=10.0)
    # travel time expensive: building the direct line beats the slow 5 km detour
    r = planner(flat(), network=net, value_of_time=1e4).route_xy(a, b)
    assert r.length_m < 3200 and r.build_cost > 0.9 * 3072 * RAIL.cost_per_m, r.summary()


def test_existing_bridge_crosses_river():
    a, b = (1024, 2048), (3072, 2048)
    river = lake(True)
    assert planner(river, allow_bridges=False).route_xy(a, b) is None
    net = existing_track([(1700, 2048), (2400, 2048)])               # existing bridge over the river
    r = planner(river, allow_bridges=False, network=net).route_xy(a, b)
    assert r is not None and r.water_bridge_m == 0
    assert 600 < r.reused_m < 800
    assert abs(r.build_cost - (r.length_m - r.reused_m) * RAIL.cost_per_m) < 0.02 * r.build_cost


# ---------------------------------------------------------------- route shaping (segments)

def hausdorff(P, Q) -> float:
    return max(_pt_poly_dist(np.asarray(P), np.asarray(Q)).max(), _pt_poly_dist(np.asarray(Q), np.asarray(P)).max())


def curved_track(p0, p1, t0, t1, speed: float = 25.0) -> TransportNetwork:
    """One existing track edge p0 -> p1 as a cubic Hermite curve with tangents t0 / t1."""
    return TransportNetwork.from_export({"version": 6, "network": {
        "nodeIds": [1, 2], "nodePos": [p0[0], p0[1], 10.0, p1[0], p1[1], 10.0],
        "edgeIds": [100], "edgeNodes": [0, 1], "edgeTemplate": [0], "edgeRoadType": [0], "edgeStruct": [0],
        "edgeTypeIndex": [-1], "edgeLength": [-1], "edgeOwned": [1], "roadTypeValues": {"TRACK": 0, "STREET": 1},
        "edgeTangents": [t0[0], t0[1], 0.0, t1[0], t1[1], 0.0],
        "templates": [{"name": TRACK_T, "roadType": 0, "speed": speed}]}})


def turning(P) -> float:
    """Sum of absolute heading changes along a polyline, radians (zig-zag measure)."""
    d = np.diff(np.asarray(P, np.float64), axis=0)
    d = d[np.hypot(d[:, 0], d[:, 1]) > 1e-6]
    h = np.arctan2(d[:, 1], d[:, 0])
    return float(np.abs((np.diff(h) + np.pi) % (2 * np.pi) - np.pi).sum())


def zigzags(P, min_turn_deg: float = 15.0, max_gap: float = 150.0) -> int:
    """Number of left/right alternations: two consecutive sharp turns of opposite sign closer than
    max_gap metres (the staircase of king / knight moves of a raw grid path)."""
    P = np.asarray(P, np.float64)
    d = np.diff(P, axis=0)
    ln = np.hypot(d[:, 0], d[:, 1])
    d, ln = d[ln > 1e-6], ln[ln > 1e-6]
    h = np.arctan2(d[:, 1], d[:, 0])
    t = (np.diff(h) + np.pi) % (2 * np.pi) - np.pi
    sharp = np.abs(t) > math.radians(min_turn_deg)
    return int(sum(1 for k in range(len(t) - 1)
                   if sharp[k] and sharp[k + 1] and t[k] * t[k + 1] < 0 and ln[k + 1] < max_gap))


def check_invariants(r):
    assert abs(sum(g["length_m"] for g in r.segments) - r.length_m) < 1e-6 * max(1.0, r.length_m)
    assert abs(sum(g["build_cost"] for g in r.segments if g["type"] == "new") - r.build_cost) < 1e-6 * max(1.0, r.build_cost)
    assert abs(r.existing_m + r.upgrade_m + r.new_m + r.shared_m - r.length_m) < 1e-6 * max(1.0, r.length_m)
    assert abs(sum(g["build_cost"] for g in r.segments if g["type"] == "upgrade") - r.upgrade_cost) < 1e-6 * max(1.0, r.upgrade_cost)
    # segments are continuous and form the polyline
    for g0, g1 in zip(r.segments, r.segments[1:]):
        assert math.dist(g0["polyline"][-1], g1["polyline"][0]) < 0.5
    assert math.dist(r.polyline[0], r.segments[0]["polyline"][0]) < 0.5
    assert math.dist(r.polyline[-1], r.segments[-1]["polyline"][-1]) < 0.5


def test_route_on_existing_way_snaps_to_its_geometry():
    # S-curved existing track (Hermite tangents along +x) between two grid nodes
    p0, p1 = (512.0, 1024.0), (3584.0, 3072.0)
    net = curved_track(p0, p1, (3000.0, 0.0), (3000.0, 0.0))
    _, x, y = net.edge_samples(spacing=2.0)
    road = np.column_stack([x, y])
    pl = planner(flat(), network=net)
    r = pl.route_xy(p0, p1)
    check_invariants(r)
    raw = hausdorff(r.grid_polyline, road)
    on = [g for g in r.segments if g["type"] == "existing"]
    assert on and sum(g["length_m"] for g in on) > 0.95 * r.length_m, [(g["type"], g["length_m"]) for g in r.segments]
    assert r.build_cost < 0.01 * r.length_m * RAIL.cost_per_m and r.upgrade_m == 0
    # the drawn line is the real track, not the cell centres beside it
    shaped = hausdorff(r.polyline, road)
    assert shaped < 15.0, (shaped, raw)
    assert raw > shaped
    for g in on:
        assert _pt_poly_dist(np.asarray(g["polyline"]), road).max() < 15.0
        assert g["way"] == "simple" and g["build_cost"] == 0
    # merged polyline: no long pieces (densified to <= one cell)
    P = np.asarray(r.polyline)
    assert np.hypot(*np.diff(P, axis=0).T).max() <= pl.cell + 1e-6


def test_mixed_route_segment_totals():
    a, b = (512, 2048), (3584, 2048)
    net = existing_track([(512, 2048), (2048, 2048)], speed=RAIL.speed)    # same speed: use as it is
    r = planner(flat(), network=net).route_xy(a, b)
    check_invariants(r)
    assert [g["type"] for g in r.segments] == ["existing", "new"], [(g["type"], g["length_m"]) for g in r.segments]
    assert abs(r.existing_m - 1536) < 1 and abs(r.new_m - 1536) < 1 and r.upgrade_m == 0
    assert abs(r.reused_m - r.existing_m) < 1e-6
    assert abs(r.build_cost - r.new_m * RAIL.cost_per_m) < 1e-3
    assert r.segments[0]["build_cost"] == 0 and r.segments[1]["way"] == RAIL.key
    assert all(abs(y - 2048) < 1e-6 for _, y in r.polyline)


def test_slower_existing_way_is_marked_for_upgrade():
    a, b = (512, 2048), (3584, 2048)
    net = existing_track(U_TRACK, speed=20.0)                  # 20 < 0.98 x 25 m/s of the planned track
    pl = planner(flat(), network=net)
    r = pl.route_xy(a, b)
    check_invariants(r)
    assert r.upgrade_m > 0.98 * r.length_m and r.existing_m == 0 and r.build_cost < 1.0
    assert abs(r.upgrade_cost - r.upgrade_m * RAIL.cost_per_m) < 1e-3       # unknown old template: full price
    assert r.segments[0]["upgrade_to"] == RAIL.key
    pl.ways = {"simple": Way("simple", "simple", "rail", 40.0, 5.0, 20.0, 0, 0, 0.13, 40, False)}
    r2 = pl.make_route(r.cells)
    assert abs(r2.upgrade_cost - r2.upgrade_m * 60.0) < 1e-3                # price difference
    assert abs(r2.build_cost - r.build_cost) < 1e-9                        # upgrades are optional


def test_new_parts_have_no_zigzag():
    # flat oblique route: the grid path is a staircase of king / knight moves, the drawn line is straight
    pl = planner(flat())
    r = pl.route_xy((256, 256), (3840, 1408))          # slope 0.32: not a lattice direction
    check_invariants(r)
    assert [g["type"] for g in r.segments] == ["new"]
    P = np.asarray(r.segments[0]["polyline"])
    assert len(P) == 2
    assert _pt_poly_dist(np.asarray(r.polyline), P[[0, -1]]).max() < 1.0
    assert turning(r.grid_polyline) > 1.0 and turning(r.polyline) < 1e-6 and zigzags(r.grid_polyline) > 3
    # hills: the shaped line stays in the grid corridor (bridges/tunnels where the grid path has them)
    rng = np.random.default_rng(3)
    from scipy import ndimage
    h = (ndimage.gaussian_filter(rng.normal(0, 1, (SIZE, SIZE)), 4) * 600 + 60).astype(np.float32)
    ph = planner(h)
    rh = ph.route_xy((300, 400), (3800, 3500))
    check_invariants(rh)
    G, S = np.asarray(rh.grid_polyline), np.asarray(rh.polyline)
    assert _pt_poly_dist(S, G).max() < 1.0 * ph.cell, _pt_poly_dist(S, G).max()
    assert zigzags(G) >= 5 and zigzags(S) <= 1, (zigzags(S), zigzags(G))   # 1: a real one-cell S-bend
    assert abs(rh.build_cost - ph.make_route(rh.cells, shape=False).build_cost) < 1e-6 * rh.build_cost


def test_shaping_is_direction_independent():
    # a loop A -> B -> A must draw the return leg exactly over the outbound one
    rng = np.random.default_rng(5)
    from scipy import ndimage
    h = (ndimage.gaussian_filter(rng.normal(0, 1, (SIZE, SIZE)), 4) * 500 + 60).astype(np.float32)
    net = existing_track([(1024, 512), (1024, 2048), (2560, 2560)], speed=RAIL.speed)
    pl = planner(h, network=net)
    r = pl.route_xy((300, 300), (3800, 3600))
    back = pl.make_route(r.cells[::-1])
    assert hausdorff(r.polyline, back.polyline) < 5.0
    assert [g["type"] for g in back.segments] == [g["type"] for g in r.segments][::-1]
    for g, q in zip(r.segments, back.segments[::-1]):
        assert hausdorff(g["polyline"], q["polyline"]) < 5.0
    assert abs(r.build_cost - back.build_cost) < 1e-6 * max(1.0, r.build_cost)


def test_segments_json_helpers():
    net = existing_track([(512, 2048), (2048, 2048)], speed=RAIL.speed)
    pl = planner(flat(), network=net)
    r = pl.route_xy((512, 2048), (3584, 2048))
    back = pl.make_route(r.cells[::-1])
    js = line_json([r, back])
    assert js["geometry"][0] == js["geometry"][-1] == [512.0, 2048.0]
    assert [(g["type"], g["leg"]) for g in js["segments"]] == [("existing", 0), ("new", 0), ("new", 1), ("existing", 1)]
    # the return leg is the same physical way: marked as repeat, counted (and built) once
    assert [g["repeat_m"] for g in js["segments"]] == [0, 0, 1536, 1536]
    t = js["segment_totals"]
    assert t["existing_m"] == 1536 and t["new_m"] == 1536 and t["repeat_m"] == 2 * 1536
    assert t["build_cost"] == round(r.build_cost)
    assert route_geometry(back) == route_geometry(r)[::-1]
    assert segment_totals(route_segments_json(r))["new_m"] == 1536


# ---------------------------------------------------------------- demolition of town buildings

BLDG_COST = 100_000.0      # $ per building (real probes: ~25 k x capacity)
BLDG_PEN = 0.002           # reputation penalty per building


def synthetic_town(kind: str = "disc", cx: float = 2048.0, cy: float = 2048.0, r: float = 400.0,
                   spacing: float = 24.0):
    """Town buildings (20 x 20 m plots every `spacing` m) as a townmask.TownMask: a disc of radius r,
    or a north-south band x in [cx - r/8, cx + r/8] over the whole map (no way around)."""
    from tf3planner import townmask as TM
    g = np.arange(0.0, 4096.0 + 1, spacing)
    X, Y = np.meshgrid(g, g)
    X, Y = X.ravel(), Y.ravel()
    keep = (np.hypot(X - cx, Y - cy) <= r) if kind == "disc" else (np.abs(X - cx) <= r / 8)
    xy = np.column_stack([X[keep], Y[keep]])
    n = len(xy)
    towns = [TM.TownRef(0, 1, "Testtown", cx, cy, r, (n / 3, n / 3, n / 3))]
    return TM.from_points(terrain(flat()), xy, np.full(n, 400.0), np.full(n, BLDG_COST),
                          np.full(n, BLDG_PEN), np.zeros(n, np.int64), towns, "test buildings")


def test_demolition_detour_around_town():
    a, b = (512, 2048), (3584, 2048)
    tm = synthetic_town()
    plain = planner(flat()).route_xy(a, b)                      # demolition ignored: straight through
    assert plain.length_m < 3100 and plain.demolition_cost == 0
    # what the straight line would demolish under the demolition model
    straight = planner(flat(), townmask=tm).make_route(plain.cells)
    assert straight.demolition_buildings > 20 and straight.demolition_cost > 20 * BLDG_COST
    assert straight.demolition_towns.get("Testtown", 0) > 20 * BLDG_PEN
    pl = planner(flat(), townmask=tm)
    r = pl.route_xy(a, b)
    check_invariants(r)
    assert r.demolition_buildings < 1.0, r.summary()            # goes around the centre
    assert r.length_m > plain.length_m + 100
    assert r.generalized_cost < straight.generalized_cost
    assert min(abs(y - 2048) for x, y in r.polyline if abs(x - 2048) < 50) > 380
    # demolition switched off -> back to the straight line
    assert planner(flat(), townmask=tm, demolition=False).route_xy(a, b).length_m == plain.length_m


def test_demolition_through_town_when_no_way_around():
    """A band of houses across the whole map: demolition is a cost, not a ban - the route crosses
    it on the shortest crossing and the money is part of build_cost / segments / totals."""
    a, b = (512, 2048), (3584, 2048)
    tm = synthetic_town("band")
    pl = planner(flat(), townmask=tm)
    r = pl.route_xy(a, b)
    check_invariants(r)
    assert 2 < r.demolition_buildings < 12, r.summary()
    assert abs(r.demolition_cost - r.demolition_buildings * BLDG_COST) < 1e-6 * r.demolition_cost
    assert abs(r.build_cost - (r.length_m * RAIL.cost_per_m + r.demolition_cost)) < 1e-6 * r.build_cost
    assert 40 < r.demolition_m < 160
    assert abs(r.demolition_rating - r.demolition_buildings * BLDG_PEN) < 1e-9
    assert abs(r.generalized_cost - (r.build_cost + 10 * r.maintenance_per_year + 2e6 * r.demolition_rating
                                     + 4000 * r.travel_time_s)) < 1e-6 * r.generalized_cost
    new = [g for g in r.segments if g["type"] == "new" and g.get("demolition_buildings")]
    assert new and abs(sum(g["demolition_cost"] for g in new) - r.demolition_cost) < 1e-6 * r.demolition_cost
    js = route_segments_json(r)
    assert sum(g.get("demolition_cost", 0) for g in js) == pytest_approx(r.demolition_cost, 2)
    t = segment_totals(js)
    assert abs(t["demolition_cost"] - r.demolition_cost) <= 2 and t["build_cost"] == round(r.build_cost)
    assert t["demolition_m"] == round(r.demolition_m) or abs(t["demolition_m"] - r.demolition_m) <= 2
    # a per-m2 price override
    r2 = planner(flat(), townmask=tm, demolition_usd_per_m2=10.0).make_route(r.cells)
    assert abs(r2.demolition_cost - r.demolition_buildings * 400 * 10.0) < 1e-6 * r2.demolition_cost


def pytest_approx(v, tol):
    class _A:
        def __eq__(self, o):
            return abs(o - v) <= tol
    return _A()


def test_demolition_zero_on_existing_street():
    """Using an existing track/street through the town demolishes nothing."""
    a, b = (512, 2048), (3584, 2048)
    tm = synthetic_town()
    net = existing_track([a, (2048, 2048), b], speed=30.0)      # existing way straight through the centre
    r = planner(flat(), network=net, townmask=tm).route_xy(a, b)
    assert r.existing_m > 0.98 * r.length_m
    assert r.demolition_cost == 0 and r.demolition_buildings == 0 and r.demolition_m == 0
    assert all("demolition_cost" not in g for g in route_segments_json(r))


def test_demolition_cache_signature():
    from tf3planner.routing import _signature, network_nodes  # noqa: F401
    from tf3planner.routing import Node
    nodes = [Node(0, "town:1", 1, "A", "town", "town", 512, 2048, 10, False),
             Node(1, "town:2", 2, "B", "town", "town", 3584, 2048, 10, False)]
    p0 = planner(flat())
    p1 = planner(flat(), townmask=synthetic_town())
    p2 = planner(flat(), townmask=synthetic_town(r=300))
    p3 = planner(flat(), townmask=synthetic_town(), demolition_rating_usd=1e5)
    cells = np.array([p0.snap(n.x, n.y) for n in nodes])
    sigs = {_signature(p, nodes, cells) for p in (p0, p1, p2, p3)}
    assert len(sigs) == 4
    assert _signature(p1, nodes, cells) == _signature(planner(flat(), townmask=synthetic_town()), nodes, cells)


# ---------------------------------------------------------------- real map

_GAME = None


def game():
    global _GAME
    if _GAME is None:
        _GAME = load_game()
    return _GAME


def test_real_route_sanity():
    g = game()
    towns = g.towns
    t0 = time.perf_counter()
    r = route(g, "rail", towns[0], towns[1])
    dt = time.perf_counter() - t0
    assert r is not None
    assert r.length_m >= r.straight_distance - 1e-6
    assert r.detour_factor < 2.5
    assert dt < 5.0 + 10.0, dt          # includes one-time grid build
    t0 = time.perf_counter()
    far = max(towns[1:], key=lambda t: math.hypot(t.x - towns[0].x, t.y - towns[0].y))
    r2 = route(g, "rail", towns[0], far)
    assert time.perf_counter() - t0 < 5.0
    assert r2.detour_factor < 2.5 and r2.length_m > 10000
    rr = route(g, "road", towns[0], towns[1])
    assert rr is not None and rr.detour_factor < 2.5


def test_real_all_pairs():
    g = game()
    t0 = time.perf_counter()
    nm = network_matrix(g, "rail", cache=False)
    dt = time.perf_counter() - t0
    assert dt < 120, dt
    n = len(nm.nodes)
    off = ~np.eye(n, dtype=bool)
    assert np.isfinite(nm.build[off]).all()
    assert (nm.length[off] >= nm.straight[off] - 1e-6).all()
    assert np.percentile(nm.detour[off], 95) < 1.5
    # stored paths rebuild into consistent Routes
    i, j = 0, n - 1
    r = nm.route(i, j)
    assert abs(r.build_cost - nm.build[i, j]) < 1e-3 * nm.build[i, j]
    # water-only industries are flagged
    assert any(nd.water_only for nd in nm.nodes)


def test_real_build_network_reuse():
    g = game()
    towns = g.towns
    pairs = [(towns[0], towns[k]) for k in range(1, 6)]
    plan = build_network(g, "rail", None, pairs)
    routes, total = plan
    nm = network_matrix(g, "rail")
    star = sum(nm.build[nm.index(a.id), nm.index(b.id)] for a, b in pairs)
    assert len(plan.components) == 1
    assert total <= star * 1.0001, (total, star)


def test_real_route_segments_and_out_and_back():
    """Export v6: existing parts are drawn over the real roads, loops / shuttles draw one way."""
    import pytest
    from tf3planner.network import for_game
    g = game()
    net = for_game(g)
    if net is None:
        pytest.skip(f"data/export.json is v{g.raw.get('version')} without network (needs export v6)")
    nm = network_matrix(g, "road")
    tree = net._samples("road")["tree"]
    from tf3planner.routing import _densify
    n = len(nm.nodes)
    checked, on_m, near_m = 0, 0.0, 0.0
    t0 = time.perf_counter()
    for i in range(0, n, 5):
        for j in range(i + 1, n, 7):
            r = nm.route(i, j)
            if r is None or nm.nodes[i].water_only or nm.nodes[j].water_only:
                continue
            check_invariants(r)
            assert r.build_cost <= nm.build[i, j] + 1e-6 * nm.build[i, j] + 1e-6
            back = nm.route(j, i)
            assert hausdorff(r.polyline, back.polyline) < 5.0           # one physical way out and back
            assert abs(back.build_cost - r.build_cost) < 1e-6 * max(1.0, r.build_cost)
            for s in r.segments:
                if s["type"] in ("existing", "upgrade"):
                    P = _densify(np.asarray(s["polyline"]), 8.0)
                    d = tree.query(P)[0]
                    seg = np.hypot(*np.diff(P, axis=0).T)
                    on_m += seg.sum()
                    near_m += seg[(d[1:] < 15) & (d[:-1] < 15)].sum()
            checked += 1
    assert checked > 20
    assert near_m > 0.95 * on_m, near_m / on_m                         # drawn over the real road
    assert (time.perf_counter() - t0) / (2 * checked) < 0.2


def test_real_line_routes_loop_and_shuttle():
    """A circuit / shuttle is ONE way: the return leg coincides with the outbound one (< 5 m) and
    builds nothing; legs of a loop reuse each other's new road instead of running in parallel."""
    from tf3planner.routing import line_routes
    g = game()
    nm = network_matrix(g, "road")
    land = [nd.index for nd in nm.nodes if not nd.water_only and nd.kind == "industry"]
    # three industries a few km apart
    a = land[0]
    near = sorted(land[1:], key=lambda k: nm.length[a, k])
    b, c = near[2], near[4]
    out, back = line_routes(nm, [(a, b), (b, a)])
    assert out is not None and back is not None
    assert hausdorff(out.polyline, back.polyline) < 5.0
    assert back.new_m == 0 and back.build_cost < 1e-6
    check_invariants(back)
    js = line_json([out, back])
    assert abs(js["segment_totals"]["new_m"] - out.new_m) <= 2     # per-segment rounding in JSON
    loop = line_routes(nm, [(a, b), (b, c), (c, a)])
    indep = [nm.route(a, b), nm.route(b, c), nm.route(c, a)]
    assert sum(r.new_m for r in loop) <= sum(r.new_m for r in indep) + 1e-6
    assert sum(r.build_cost for r in loop) <= sum(r.build_cost for r in indep) + 1e-6
    for r in loop:
        check_invariants(r)


def test_real_water_and_land_components():
    g = game()
    wn = water_network(g)
    assert wn.bodies and wn.bodies[0].area_km2 > 1
    plat = [nd for nd in wn.nodes if nd.water_only]
    assert all(nd.index in wn.ports for nd in plat)
    comps = land_components(g, "rail")
    assert len(comps) >= 1


def test_real_existing_roads():
    """Export v6: town-to-town road links along existing roads are (nearly) free."""
    import pytest
    from tf3planner.network import for_game
    g = game()
    net = for_game(g)
    if net is None:
        pytest.skip(f"data/export.json is v{g.raw.get('version')} without network (needs export v6)")
    towns = g.towns
    t0 = time.perf_counter()
    # value_of_time 0: existing roads are free and followed whatever the detour
    with_net = network_matrix(g, "road", params=RoutingParams(value_of_time=0.0), cache=False)
    dt = time.perf_counter() - t0
    assert dt < 180, dt
    without = network_matrix(g, "road", params=RoutingParams(use_existing=False, value_of_time=0.0), cache=False)
    # default value_of_time: existing roads reused only on moderate detours (audit 2026-10-02)
    dflt = network_matrix(g, "road", cache=False)
    offn = ~np.eye(len(dflt.nodes), dtype=bool)
    det = dflt.length[offn] / np.maximum(dflt.straight[offn], 1)
    assert np.percentile(det[np.isfinite(det)], 99) < 2.5
    assert np.median(dflt.existing_m[offn] / np.maximum(dflt.length[offn], 1)) > 0.3
    n = len(towns)
    off = ~np.eye(n, dtype=bool)
    b1, b0 = with_net.build[:n, :n][off], without.build[:n, :n][off]
    fin = np.isfinite(b1) & np.isfinite(b0)
    assert (b1[fin] <= b0[fin] + 1e-6).all()
    # towns connected by the existing road network: build share small for most pairs
    pm = net.path_matrix([(t.x, t.y) for t in towns], "road")
    conn = np.isfinite(pm["length"]) & off
    if conn.any():
        share = with_net.build[:n, :n][conn] / np.maximum(without.build[:n, :n][conn], 1)
        assert np.median(share) < 0.25, np.median(share)
        assert np.median(with_net.existing_m[:n, :n][conn] / with_net.length[:n, :n][conn]) > 0.7


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            t0 = time.perf_counter()
            try:
                fn()
                print(f"PASS {name} ({time.perf_counter() - t0:.1f} s)")
            except Exception as ex:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(ex).__name__}: {ex}")
    sys.exit(1 if failed else 0)


def test_real_demolition():
    """Export v7 town buildings: the router avoids demolition where a detour is cheap, matrices and
    routes agree, and siting prices (instead of forbidding) sites inside towns."""
    import pytest
    from tf3planner import townmask
    g = game()
    tm = townmask.for_game(g)
    if tm is None:
        pytest.skip("no towns")
    # rail: Тетиев farm -> Бердичев (the 2026-10-03 plan crossed Черногородка: ~17 houses, ~3 M$)
    try:
        a, b = R_node(g, 36479), R_node(g, 193836)
    except KeyError:
        pytest.skip("plan endpoints not on this map")
    old_pl = R_planner(g, "rail", demolition=False)
    new_pl = R_planner(g, "rail")
    old = new_pl.make_route(old_pl.route_xy(a, b).cells)
    new = new_pl.route_xy(a, b)
    check_invariants(new)
    assert old.demolition_cost > 1e6, old.summary()
    assert new.demolition_cost < 0.3 * old.demolition_cost and new.generalized_cost < old.generalized_cost
    assert new.length_m < 1.1 * old.length_m
    nm = network_matrix(g, "rail", cache=False)
    n = len(nm.nodes)
    assert (nm.demolition_cost <= nm.build + 1e-6).all() and np.isfinite(nm.demolition_m[0, 1:]).all()
    for i, j in ((0, n - 1), (1, 7), (5, 20)):
        r = nm.route(i, j)
        if r is not None and r.demolition_cost > 0:
            assert abs(r.demolition_cost - nm.demolition_cost[min(i, j), max(i, j)]) <= 1e-3 * r.demolition_cost + 1
    from tf3planner import siting
    S = siting.for_game(g)
    t = g.towns[0]
    assert S.demolition(t.x, t.y, "station_road")["cost"] > 0          # the centre is built up
    d = S.depot((t.x, t.y), [(t.x, t.y), (t.x + 3000, t.y)], 400, "road")
    assert d.demolition_cost == 0                                       # free ground within 400 m wins


def R_node(g, eid):
    for nd in network_nodes_(g):
        if nd.id == eid:
            return (nd.x, nd.y)
    raise KeyError(eid)


def network_nodes_(g):
    from tf3planner.routing import network_nodes
    return network_nodes(g)


def R_planner(g, kind, **kw):
    from tf3planner.routing import get_planner
    return get_planner(g, kind, 1913, RoutingParams(**kw))
