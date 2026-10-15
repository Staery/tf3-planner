"""Multimodal cargo chains through warehouses (feeder -> warehouse hub -> trunk -> consumer).

TF3 rules this module relies on (docs/mechanics.md 1.1-1.3, 2, 3):
- Cargo only waits in stocks. A transfer between two lines therefore needs a WAREHOUSE inside the
  catchment of the stations of both lines (station-only hubs are invalid plans).
- Payment is per leg on that leg's straight-line chord, but detours are not paid. We follow the
  conservative "origin->destination split" reading (docs/strategies.md, contradiction 2): a unit is
  paid min(chord income O->D, sum of leg chord incomes) in total, split between legs by their chord
  income share. A chain therefore never earns more than the direct chord; its gain comes from
  cheaper/faster transport, consolidation (shared stations, depots, trunk vehicles), no track on
  water, or making a link possible at all (water-only producers).
- Late cargo is still paid; lateness only hurts towns, so the delivery limit is only checked for
  town consumers (end-to-end, legs summed). Specialized warehouses extend the delivery time
  (players: the clock is paused in storage), so with a specialized warehouse the waiting time at
  the hub is not counted; with a universal one half a trunk headway is added.
- Catchment radii: harbor 250 m (big dock), road cargo station 175 m, rail cargo main building 150 m.

Hub sites (all are plain (x, y) points; hubs at an existing node reuse its matrices and keys)
- Harbor sites, per water body that has producers on/near it:
  (a) the shore cell nearest to every producer/consumer node with a port on that body
      (water_network.ports, <= PORT_RADIUS); aliased to the node when within HARBOR_CATCHMENT;
  (b) the shore cell nearest to every consumer within HARBOR_SITE_MAX_M (harbor + land trunk),
      only for cargos that water-only producers on that body produce;
  (c) the shore cell nearest to every land producer and producer-cluster centroid within
      FEEDER_MAX_M (truck feeder into a harbor).
  Sites closer than SITE_MERGE_M on the same body are merged (node sites (a) always kept).
- Land (rail) hubs: every land producer node, every processing plant, the TOP_TOWN_HUBS largest
  towns, and the centroids of producer clusters. Clusters: greedy leader clustering per cargo
  (largest producer seeds a cluster, every unassigned producer within CLUSTER_RADIUS_M joins,
  centroid = volume-weighted mean; one pass, deterministic, O(n^2) — k-means is not needed at
  ~15 producers per cargo).
- Land legs from arbitrary points use one Dijkstra tree per (hub, mode) on the routing grid
  (same graph as RoutePlanner.route_xy, ~0.07 s); feeder look-ups use a radius-limited tree first.
  Ship legs use a Dijkstra on the 32 m water grid. Trees are cached per Planner instance, so only
  the first generate() call pays for them.

Chains (ProjectCandidate kind "multimodal", mode "multi")
- feeders: road (land producers, route <= FEEDER_MAX_M) or ship (water-only producers, or land
  producers with their own port within HARBOR_CATCHMENT) into the hub's warehouse; producers inside
  the hub's catchment load directly at the trunk station;
- trunk: rail (any hub), road (harbor hub fed by ships: platform -> harbor -> truck -> refinery),
  or ship (harbor hub fed by trucks), ending at the consumer or, for ships, at a second harbor +
  warehouse near the consumer and a final road feeder (<= FEEDER_MAX_M).
- Producers are added greedily (forward selection) while the chain's advantage over the best
  direct links of the same producers to the same consumer grows (NPV over NPV_YEARS).
- Emitted only if profit > 0 and (NPV beats the sum of the best direct alternatives, or none of
  its producers has a profitable direct link to that consumer, e.g. oil platforms).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage
from scipy.sparse import csgraph
from scipy.spatial import cKDTree

from . import facilities
from .candidates import ProjectCandidate, VehicleLine
from .economics import MAX_OCCUPANCY, STOP_DELAY_S, VEHICLE_LENGTH_M, Endpoint, LinkEval, evaluate_link
from .routing import get_planner

# ----------------------------------------------------------------------------- parameters
FEEDER_MAX_M = 4000.0          # max road feeder route length (players: short truck shuttles)
HARBOR_CATCHMENT = 250.0       # big dock cargo radius (mechanics.md 3); same as planner.HARBOR_CATCHMENT
ROAD_CATCHMENT = 175.0         # modular road cargo station
RAIL_CATCHMENT = 150.0         # rail cargo main building 1
PORT_RADIUS = 1500.0           # water_network port radius used by the planner
HARBOR_SITE_MAX_M = 15000.0    # harbor site near a consumer: max straight distance to it
SITE_MERGE_M = 800.0           # harbor sites closer than this (same body) are merged
CLUSTER_RADIUS_M = 3000.0      # producer clustering radius for centroid hubs
TOP_TOWN_HUBS = 6              # largest towns used as land hub sites
MAX_DETOUR = 1.6               # (|P-H| + |H-D|) / |P-D| per member (detours are unpaid)
MIN_CHORD_M = 500.0            # ignore producer->consumer pairs closer than this
MAX_MEMBERS = 6                # producers per chain
MAX_PER_DEST = 4               # best chains kept per (cargo, consumer) ...
PER_PRODUCER = 2               # ... plus each producer's best chains to that consumer
MIN_VOL = 1.0                  # units/year
WAREHOUSE_MODULE = 500.0       # units per warehouse module (mechanics.md 2)
SAFETY_Z = 1.645               # 95 % service level for the safety stock (real_logistics.md 10.2)
WAREHOUSE_TWO_CONSISTS = 2.0   # warehouse holds >= 2x the largest consist calling at it (heuristic)
SPECIALIZED_WAREHOUSE = True   # one cargo per chain -> all cargos share a class -> specialized
NPV_YEARS = 15                 # same horizon / rate as planner.py
DISCOUNT = 0.05
LAND_GRADE = 0.01              # ruling grade passed to evaluate_link (as planner.py)

MODE_RU = {"road": "грузовики", "rail": "ж/д", "water": "судно"}
ROLE_RU = {"feeder": "Подвоз", "trunk": "Магистраль", "final": "Развоз"}


def _annuity(years: int = NPV_YEARS, r: float = DISCOUNT) -> float:
    return (1 - (1 + r) ** -years) / r


def npv(profit: float, capex: float) -> float:
    return profit * _annuity() - capex


# ----------------------------------------------------------------------------- data

@dataclass
class Hub:
    key: object                 # node index when the hub sits at a node, else "hub:<x>:<y>"
    kind: str                   # "harbor" | "land"
    x: float
    y: float
    z: float
    label: str                  # Russian, e.g. "порт у «Дмитриев»"
    node: int | None = None     # hub buildings are in this node's catchment (routes from matrices)
    body: int | None = None     # water body label (harbor)
    wcell: int | None = None    # fine water-grid cell of the harbor
    origin: str = ""            # which rule generated the site


@dataclass(eq=False)
class LegRoute:
    mode: str
    length: float
    build: float
    maint: float
    cells: np.ndarray | None    # path cells (routing grid for land, 32 m terrain grid for water)
    port_dist: float = 0.0      # water: distance from the node to its port cell


@dataclass(eq=False)
class Member:
    p: int                      # producer node
    vol: float                  # free supply units/year
    mode: str | None            # feeder mode, None = loads directly at the hub
    route: LegRoute | None


@dataclass
class ChainEval:
    cargo: str
    hub: Hub
    d: int
    trunk_mode: str
    hub2: Hub | None
    members: list[Member]
    vols: list[float]
    legs: list[dict]
    income: float
    running: float
    maintenance: float
    vehicle_capex: float
    infra_capex: float
    vehicles: dict              # vehicle id -> [Vehicle, count]
    life: float
    stations: list[tuple]       # station keys
    pair_keys: list[tuple]
    warehouses: list[dict]
    direct_chord_income: float
    direct_npv: float           # sum over members of max(0, best direct NPV)
    direct_any: bool            # some member has a profitable direct link to d
    direct_info: list[dict]
    max_time_s: float

    @property
    def profit(self) -> float:
        return self.income - self.running - self.maintenance

    @property
    def capex(self) -> float:
        return self.vehicle_capex + self.infra_capex

    @property
    def npv(self) -> float:
        return npv(self.profit, self.capex)

    @property
    def advantage(self) -> float:
        return self.npv - self.direct_npv


def _walk(pred: np.ndarray, s: int, t: int) -> list[int]:
    path = [int(t)]
    cur = int(t)
    while cur != s:
        cur = int(pred[cur])
        if cur < 0:
            return []
        path.append(cur)
    path.reverse()
    return path


def _pair_key(a, b, mode: str) -> tuple:
    if isinstance(a, (int, np.integer)) and isinstance(b, (int, np.integer)):
        return (min(int(a), int(b)), max(int(a), int(b)), mode)
    s = sorted([a, b], key=str)
    return (s[0], s[1], mode)


def _thin(pts: list[list[float]], n: int = 60) -> list[list[float]]:
    if len(pts) <= n:
        return pts
    step = max(1, len(pts) // n)
    out = pts[::step]
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


# ----------------------------------------------------------------------------- context (cached per planner)

class _Context:
    def __init__(self, planner, game):
        self.pl = planner
        self.game = game
        self.nodes = planner.nodes
        cat = getattr(game, "_catalogue", None)
        if cat is None:
            cat = game._catalogue = facilities.Catalogue(game)
        self.cat = cat
        self.rp = {m: get_planner(game, m, planner.mat[m].year, planner.mat[m].params) for m in ("rail", "road")}
        self.water = planner.water
        tr = game.terrain
        self.tr = tr
        W = tr.h < tr.water_level
        self.labels, _ = ndimage.label(W, structure=np.ones((3, 3), bool))   # same labelling as water_network
        shore = W & ~ndimage.binary_erosion(W, structure=ndimage.generate_binary_structure(2, 1), border_value=1)
        sy, sx = np.nonzero(shore)
        lab = self.labels[sy, sx]
        self.body_ok = {b.label for b in self.water.bodies}
        self.shore: dict[int, tuple[cKDTree, np.ndarray]] = {}
        for b in self.body_ok:
            k = lab == b
            if k.any():
                xy = np.column_stack([tr.origin_x + sx[k] * tr.step, tr.origin_y + sy[k] * tr.step])
                self.shore[b] = (cKDTree(xy), (sy[k] * tr.nx + sx[k]).astype(np.int64))
        self.land_trees: dict[tuple, dict[int, LegRoute]] = {}
        self.water_trees: dict[object, dict[int, LegRoute]] = {}
        self.hubs_cache: dict[tuple, list[Hub]] = {}
        self.leg_cache: dict[tuple, LinkEval | None] = {}
        self.direct_cache: dict[tuple, tuple] = {}
        town_cap = {}
        for t in game.towns:
            town_cap[t.id] = sum(t.capacities.values())
        self.town_cap = town_cap

    # ---------------- geometry helpers
    def wcell_xy(self, c: int) -> tuple[float, float]:
        iy, ix = divmod(int(c), self.tr.nx)
        return self.tr.origin_x + ix * self.tr.step, self.tr.origin_y + iy * self.tr.step

    def zat(self, x: float, y: float) -> float:
        return max(float(self.tr.height(x, y)), float(self.tr.water_level))

    def nearest_name(self, x: float, y: float) -> str:
        n = min((n for n in self.nodes if not n.water_only), key=lambda n: (n.x - x) ** 2 + (n.y - y) ** 2)
        return n.name

    def endpoint(self, loc) -> Endpoint:
        if isinstance(loc, Hub):
            return Endpoint("hub", -1, loc.label, loc.x, loc.y, loc.z)
        n = self.nodes[loc]
        return Endpoint(n.kind, n.id, n.name, n.x, n.y, n.z)

    # ---------------- routes
    def land_route(self, hub: Hub, n: int, mode: str, feeder: bool = False) -> LegRoute | None:
        if self.nodes[n].water_only:
            return None
        if hub.node is not None:
            if hub.node == n:
                return LegRoute(mode, 0.0, 0.0, 0.0, None)
            m = self.pl.mat[mode]
            if not np.isfinite(m.build[hub.node, n]):
                return None
            return LegRoute(mode, float(m.length[hub.node, n]), float(m.build[hub.node, n]),
                            float(m.maint[hub.node, n]), m.path_cells(hub.node, n))
        tkey = (hub.key, mode)
        if feeder and tkey not in self.land_trees:
            # short look-up: radius-limited tree (generalized cost of a FEEDER_MAX_M route with slack)
            tkey = (hub.key, mode, "feeder")
            if tkey not in self.land_trees:
                self.land_trees[tkey] = self._land_tree(hub, mode, FEEDER_MAX_M * self.rp[mode].min_cost_per_m * 4)
        if tkey not in self.land_trees:
            self.land_trees[tkey] = self._land_tree(hub, mode)
        return self.land_trees[tkey].get(n)

    def _land_tree(self, hub: Hub, mode: str, limit: float = np.inf) -> dict[int, LegRoute]:
        rp = self.rp[mode]
        s = rp.snap(hub.x, hub.y)
        dist, pred = csgraph.dijkstra(rp.graph, directed=True, indices=s, return_predecessors=True, limit=limit)
        cells = self.pl.mat[mode].cells
        out: dict[int, LegRoute] = {}
        for nd in self.nodes:
            if nd.water_only:
                continue
            c = int(cells[nd.index])
            if not np.isfinite(dist[c]):
                continue
            if c == s:
                out[nd.index] = LegRoute(mode, 0.0, 0.0, 0.0, np.array([c], np.int64))
                continue
            path = _walk(pred, s, c)
            if len(path) < 2:
                continue
            p = np.asarray(path, np.int64)
            a = rp.edge_attrs(p)
            out[nd.index] = LegRoute(mode, float(a["L"].sum()), float(a["build"].sum()), float(a["maint"].sum()), p)
        return out

    def water_route(self, hub: Hub, n: int) -> LegRoute | None:
        if hub.wcell is None:
            return None
        if hub.key not in self.water_trees:
            self.water_trees[hub.key] = self._water_tree(hub)
        return self.water_trees[hub.key].get(n)

    def _water_tree(self, hub: Hub) -> dict[int, LegRoute]:
        g = self.water._graph
        dist, pred = csgraph.dijkstra(g, directed=False, indices=hub.wcell, return_predecessors=True)
        out: dict[int, LegRoute] = {}
        for n, plist in self.water.ports.items():
            for b, c, dd in plist:
                if b != hub.body or not np.isfinite(dist[c]):
                    continue
                path = _walk(pred, hub.wcell, c) if c != hub.wcell else [c]
                if not path:
                    continue
                out[n] = LegRoute("water", float(dist[c]), 0.0, 0.0, np.asarray(path, np.int64), float(dd))
        return out

    def route_xy(self, r: LegRoute) -> list[list[float]]:
        if r is None or r.cells is None or len(r.cells) == 0:
            return []
        if r.mode == "water":
            pts = [list(self.wcell_xy(c)) for c in r.cells.tolist()]
        else:
            xs, ys = self.rp[r.mode].cell_xy(np.asarray(r.cells))
            pts = [[float(x), float(y)] for x, y in zip(np.atleast_1d(xs).tolist(), np.atleast_1d(ys).tolist())]
        return [[round(x, 1), round(y, 1)] for x, y in _thin(pts)]

    # ---------------- hub sites
    def hubs(self, prod: set[int], cons: set[int], prod_by_cargo: dict[str, list[tuple[int, float]]],
             cons_by_cargo: dict[str, list[tuple[int, float]]]) -> list[Hub]:
        key = (frozenset(prod), frozenset(cons))
        if key not in self.hubs_cache:
            self.hubs_cache[key] = self._make_hubs(prod, cons, prod_by_cargo, cons_by_cargo)
        return self.hubs_cache[key]

    def _clusters(self, prod_by_cargo) -> list[tuple[float, float, list[int]]]:
        """Greedy leader clustering of land producers per cargo (see module docstring)."""
        out = []
        for c, items in prod_by_cargo.items():
            land = [(p, v) for p, v in items if not self.nodes[p].water_only]
            land.sort(key=lambda pv: -pv[1])
            left = list(land)
            while left:
                seed = left[0]
                sn = self.nodes[seed[0]]
                grp = [(p, v) for p, v in left if math.hypot(self.nodes[p].x - sn.x, self.nodes[p].y - sn.y) <= CLUSTER_RADIUS_M]
                left = [pv for pv in left if pv not in grp]
                if len(grp) < 2:
                    continue
                w = sum(v for _, v in grp) or 1.0
                cx = sum(self.nodes[p].x * v for p, v in grp) / w
                cy = sum(self.nodes[p].y * v for p, v in grp) / w
                out.append((cx, cy, [p for p, _ in grp]))
        return out

    def _make_hubs(self, prod, cons, prod_by_cargo, cons_by_cargo) -> list[Hub]:
        nodes, water = self.nodes, self.water
        hubs: list[Hub] = []
        clusters = self._clusters(prod_by_cargo)
        # ---- bodies worth a harbor: a producer sits on it or within feeder range of its shore
        bodies = set()
        for p in prod:
            nd = self.nodes[p]
            for b, (tree, _) in self.shore.items():
                if nd.water_only:
                    if any(bb == b for bb, _c, _d in water.ports.get(p, [])):
                        bodies.add(b)
                elif tree.query((nd.x, nd.y))[0] <= FEEDER_MAX_M:
                    bodies.add(b)
        sites: dict[int, list[Hub]] = {b: [] for b in bodies}

        def add_site(b: int, cell: int, origin: str, alias: int | None = None, force: bool = False):
            x, y = self.wcell_xy(cell)
            if not force:
                for h in sites[b]:
                    if math.hypot(h.x - x, h.y - y) < SITE_MERGE_M:
                        return
            near = alias if alias is not None else None
            if alias is not None:
                key = alias
                label = f"порт у «{nodes[alias].name}»"
            else:
                key = f"hub:{round(x)}:{round(y)}"
                label = f"порт ({x / 1000:.1f}; {y / 1000:.1f} км) у «{self.nearest_name(x, y)}»"
            if any(h.key == key and h.kind == "harbor" for h in sites[b]):
                return
            sites[b].append(Hub(key, "harbor", x, y, self.zat(x, y), label, near, b, int(cell), origin))

        # (a) node port sites
        for nd in nodes:
            if nd.water_only or (nd.index not in prod and nd.index not in cons):
                continue
            for b, cell, dd in water.ports.get(nd.index, []):
                if b in sites:
                    add_site(b, cell, "node_port", nd.index if dd <= HARBOR_CATCHMENT else None, force=True)
        # (b) harbor near consumers, (c) near land producers and producer clusters
        # cargos produced by water-only producers per body -> consumers that need a harbor + land trunk
        wcons: dict[int, set[int]] = {b: set() for b in sites}
        for c, items in prod_by_cargo.items():
            for p, _v in items:
                if nodes[p].water_only:
                    for b, _c, _d in water.ports.get(p, []):
                        if b in wcons:
                            wcons[b] |= {d for d, _ in cons_by_cargo.get(c, []) if not nodes[d].water_only}
        pts = [(nodes[p].x, nodes[p].y, FEEDER_MAX_M, "producer", None) for p in prod if not nodes[p].water_only]
        pts += [(cx, cy, FEEDER_MAX_M, "cluster", None) for cx, cy, _ in clusters]
        pts += [(nodes[d].x, nodes[d].y, HARBOR_SITE_MAX_M, "consumer", d) for d in sorted(cons)
                if not nodes[d].water_only]
        for b in sorted(sites):
            tree, cells = self.shore[b]
            for x, y, rmax, origin, d in pts:
                if d is not None and d not in wcons[b]:
                    continue
                dist, k = tree.query((x, y))
                if dist <= rmax:
                    add_site(b, int(cells[k]), origin)
        for b in sorted(sites):
            hubs.extend(sites[b])
        # ---- land hubs
        land_nodes = set(p for p in prod if not nodes[p].water_only)
        land_nodes |= {nd.index for nd in nodes if nd.kind == "industry" and not nd.water_only
                       and self.pl.ind[nd.id].inputs}
        towns = sorted((nd for nd in nodes if nd.kind == "town"), key=lambda nd: -self.town_cap.get(nd.id, 0))
        land_nodes |= {nd.index for nd in towns[:TOP_TOWN_HUBS]}
        for i in sorted(land_nodes):
            nd = nodes[i]
            hubs.append(Hub(i, "land", nd.x, nd.y, nd.z, f"у «{nd.name}»", i, origin="node"))
        for cx, cy, members in clusters:
            if any(math.hypot(nodes[i].x - cx, nodes[i].y - cy) < 300 for i in land_nodes):
                continue
            key = f"hub:{round(cx)}:{round(cy)}"
            if any(h.key == key for h in hubs):
                continue
            hubs.append(Hub(key, "land", cx, cy, self.zat(cx, cy),
                            f"узел ({cx / 1000:.1f}; {cy / 1000:.1f} км) у «{self.nearest_name(cx, cy)}»",
                            origin="cluster"))
        return hubs

    # ---------------- members of a hub
    def members(self, hub: Hub, producers: list[tuple[int, float]]) -> list[Member]:
        out = []
        for p, vol in producers:
            nd = self.nodes[p]
            dxy = math.hypot(nd.x - hub.x, nd.y - hub.y)
            if hub.kind == "harbor":
                if nd.water_only:
                    r = self.water_route(hub, p)
                    if r is not None and r.length > HARBOR_CATCHMENT:
                        out.append(Member(p, vol, "water", r))
                    continue
                if hub.node == p or dxy <= HARBOR_CATCHMENT:
                    out.append(Member(p, vol, None, None))
                    continue
                r = self.land_route(hub, p, "road", feeder=True) if dxy <= FEEDER_MAX_M else None
                if r is not None and r.length <= FEEDER_MAX_M:
                    out.append(Member(p, vol, "road", r))
                    continue
                wr = self.water_route(hub, p)
                if wr is not None and wr.port_dist <= HARBOR_CATCHMENT and wr.length > HARBOR_CATCHMENT:
                    out.append(Member(p, vol, "water", wr))
            else:
                if nd.water_only:
                    continue
                if hub.node == p or dxy <= RAIL_CATCHMENT:
                    out.append(Member(p, vol, None, None))
                    continue
                if dxy > FEEDER_MAX_M:
                    continue
                r = self.land_route(hub, p, "road", feeder=True)
                if r is not None and r.length <= FEEDER_MAX_M:
                    out.append(Member(p, vol, "road", r))
        return out

    # ---------------- evaluations (cached)
    def leg(self, cargo, src, dst, mode: str, route: LegRoute, vol: float, year: int, built: bool) -> LinkEval | None:
        sk = src.key if isinstance(src, Hub) else src
        dk = dst.key if isinstance(dst, Hub) else dst
        key = (sk, dk, mode, cargo.key, round(vol, 2), year, built, round(route.length))
        if key not in self.leg_cache:
            build = 0.0 if built or mode == "water" else route.build
            maint = 0.0 if built or mode == "water" else route.maint
            self.leg_cache[key] = evaluate_link(self.game, self.pl.vehicles if self._vehicles is None else self._vehicles,
                                                cargo, self.endpoint(src), self.endpoint(dst), mode, vol, year,
                                                route_m=max(route.length, 1.0), route_build_cost=build,
                                                route_maintenance=maint,
                                                max_grade=0.0 if mode == "water" else LAND_GRADE)
        return self.leg_cache[key]

    _vehicles = None

    def direct(self, p: int, d: int, cargo, vol: float, year: int, built_keys: set, built_stations: set):
        """Best direct link p -> d (rail/road/water) as (npv, mode, LinkEval) or (None, None, None)."""
        nodes = self.nodes
        flags = tuple(_pair_key(p, d, m) in built_keys for m in ("rail", "road", "water"))
        sflags = tuple((n, m) in built_stations for n in (p, d) for m in ("rail", "road", "water"))
        key = (p, d, cargo.key, round(vol, 2), year, flags, sflags)
        if key in self.direct_cache:
            return self.direct_cache[key]
        best = (None, None, None)
        P, D = nodes[p], nodes[d]
        for mode, built in zip(("rail", "road", "water"), flags):
            if mode == "water":
                L = self.water.length[p, d]
                if not np.isfinite(L) or not self._water_ok(p, d):
                    continue
                route_m, rb, rm, grade = float(L), 0.0, 0.0, 0.0
            else:
                if P.water_only or D.water_only:
                    continue
                m = self.pl.mat[mode]
                if not np.isfinite(m.build[p, d]):
                    continue
                route_m, rb, rm, grade = float(m.length[p, d]), float(m.build[p, d]), float(m.maint[p, d]), LAND_GRADE
            if built:
                rb = rm = 0.0
            ev = evaluate_link(self.game, self._vehicles or self.pl.vehicles, cargo, self.endpoint(p),
                               self.endpoint(d), mode, vol, year, route_m=route_m, route_build_cost=rb,
                               route_maintenance=rm, max_grade=grade)
            if ev is None:
                continue
            free = sum(ev.station_each for n in (p, d) if (n, mode) in built_stations)
            free_m = sum(ev.station_maint_each for n in (p, d) if (n, mode) in built_stations)
            if (p, mode) in built_stations or (d, mode) in built_stations:
                free += ev.depot_cost
            capex = ev.vehicle_capex + max(0.0, ev.infra_capex - free)
            profit = ev.income - ev.running - max(0.0, ev.maintenance - free_m)
            v = npv(profit, capex)
            if best[0] is None or v > best[0]:
                best = (v, mode, ev)
        self.direct_cache[key] = best
        return best

    def _water_ok(self, i: int, j: int) -> bool:
        body = self.water.body[i, j]
        for n in (i, j):
            if self.nodes[n].water_only:
                continue
            dists = [dd for b, _c, dd in self.water.ports.get(n, []) if b == body]
            if not dists or min(dists) > HARBOR_CATCHMENT:
                return False
        return True


_CTX: dict[int, _Context] = {}


def _context(planner, game) -> _Context:
    k = id(planner)
    ctx = _CTX.get(k)
    if ctx is None or ctx.pl is not planner:
        if len(_CTX) > 2:
            _CTX.clear()
        ctx = _CTX[k] = _Context(planner, game)
    return ctx


# ----------------------------------------------------------------------------- chain evaluation

def _occupancy(ev: LinkEval) -> tuple[float, int]:
    c = ev.consist
    load_t = c.capacity / max(c.load_speed, 0.1)
    occ = ev.n_consists * (load_t + STOP_DELAY_S) / max(ev.trip_time_s, 1.0)
    return occ, sum(k for _, k in c.units)


def _one_way(ev: LinkEval) -> float:
    c = ev.consist
    return ev.route_m / (c.speed * 0.85) + c.capacity / max(c.load_speed, 0.1) + STOP_DELAY_S


def _eval_chain(ctx: _Context, cargo, hub: Hub, d: int, trunk_mode: str, hub2: Hub | None,
                members: list[Member], cap: float, year: int, built_keys: set, built_stations: set,
                trunk_route: LegRoute, final_route: LegRoute | None) -> ChainEval | None:
    game, nodes = ctx.game, ctx.nodes
    D = nodes[d]
    # ---- volumes (capped by the consumer's free demand)
    vols, rem = [], cap
    for m in members:
        v = min(m.vol, rem)
        if v < MIN_VOL:
            return None
        vols.append(v)
        rem -= v
    total = sum(vols)
    if not any(m.mode for m in members):
        return None                     # no transfer -> not a multimodal chain
    # ---- legs
    legs: list[tuple[str, object, object, str, LegRoute, LinkEval, float, Member | None]] = []
    for m, v in zip(members, vols):
        if m.mode is None:
            continue
        built = m.mode != "water" and _pair_key(m.p, hub.key, m.mode) in built_keys
        ev = ctx.leg(cargo, m.p, hub, m.mode, m.route, v, year, built)
        if ev is None or ev.consist is None:
            return None
        legs.append(("feeder", m.p, hub, m.mode, m.route, ev, v, m))
    end = hub2 if hub2 is not None else d
    end_key = hub2.key if hub2 is not None else d
    built = trunk_mode != "water" and _pair_key(hub.key, end_key, trunk_mode) in built_keys
    tev = ctx.leg(cargo, hub, end, trunk_mode, trunk_route, total, year, built)
    if tev is None or tev.consist is None:
        return None
    legs.append(("trunk", hub, end, trunk_mode, trunk_route, tev, total, None))
    fev = None
    if hub2 is not None:
        built = _pair_key(hub2.key, d, "road") in built_keys
        fev = ctx.leg(cargo, hub2, d, "road", final_route, total, year, built)
        if fev is None or fev.consist is None:
            return None
        legs.append(("final", hub2, d, "road", final_route, fev, total, None))

    def loc_key(loc):
        return loc.key if isinstance(loc, Hub) else loc

    def loc_xyz(loc):
        if isinstance(loc, Hub):
            return (loc.x, loc.y, loc.z)
        n = nodes[loc]
        return (n.x, n.y, n.z)

    # ---- delivery limit (towns only; specialized warehouses pause the clock)
    spec = SPECIALIZED_WAREHOUSE and facilities._special(cargo.classes) is not None
    t_trunk = _one_way(tev) + (_one_way(fev) if fev else 0.0)
    if not spec:
        t_trunk += 0.5 * tev.trip_time_s / max(tev.n_consists, 1)
        if fev:
            t_trunk += 0.5 * fev.trip_time_s / max(fev.n_consists, 1)
    max_t = 0.0
    for role, src, dst, mode, r, ev, v, m in legs:
        if role == "feeder":
            max_t = max(max_t, _one_way(ev) + t_trunk)
    if any(m.mode is None for m in members):
        max_t = max(max_t, t_trunk)
    if D.kind == "town" and cargo.time_to_deliver_s and max_t > cargo.time_to_deliver_s:
        return None

    # ---- income: O->D chord per member, split over its legs by leg chord income (no detour credit)
    ipu = game.income_per_unit
    t_ipu = ipu(cargo, (hub.x, hub.y, hub.z), loc_xyz(end))
    g_ipu = ipu(cargo, loc_xyz(hub2), (D.x, D.y, D.z)) if hub2 is not None else 0.0
    leg_income = [0.0] * len(legs)
    feeder_idx = {id(l[7]): k for k, l in enumerate(legs) if l[0] == "feeder"}
    t_idx = next(k for k, l in enumerate(legs) if l[0] == "trunk")
    g_idx = next((k for k, l in enumerate(legs) if l[0] == "final"), None)
    direct_chord = 0.0
    for m, v in zip(members, vols):
        P = nodes[m.p]
        od = ipu(cargo, (P.x, P.y, P.z), (D.x, D.y, D.z))
        direct_chord += v * od
        f_ipu = ipu(cargo, (P.x, P.y, P.z), (hub.x, hub.y, hub.z)) if m.mode else 0.0
        parts = f_ipu + t_ipu + g_ipu
        paid = min(od, parts)
        if parts <= 0:
            continue
        if m.mode:
            leg_income[feeder_idx[id(m)]] += v * paid * f_ipu / parts
        leg_income[t_idx] += v * paid * t_ipu / parts
        if g_idx is not None:
            leg_income[g_idx] += v * paid * g_ipu / parts
    income = sum(leg_income)

    # ---- stations: one per (location, mode), sized by the summed occupancy of its legs
    ends: dict[tuple, list[tuple[float, int]]] = {}
    for role, src, dst, mode, r, ev, v, m in legs:
        occ, units = _occupancy(ev)
        for loc in (src, dst):
            ends.setdefault((loc_key(loc), mode), []).append((occ, units))
    st_cost = st_maint = 0.0
    station_keys = []
    st_names = {}
    for (lk, mode), lst in ends.items():
        occ = sum(o for o, _ in lst)
        units = max(u for _, u in lst)
        terminals = max(1, math.ceil(occ / MAX_OCCUPANCY))
        st = facilities.station(ctx.cat, mode, year, cargo.classes, units * VEHICLE_LENGTH_M, terminals)
        # offshore industries (oil platforms, fishing grounds) have built-in ship terminals
        # (terminalConfig in their .con files): no harbor to build there
        if mode == "water" and isinstance(lk, int) and 0 <= lk < len(ctx.nodes) and ctx.nodes[lk].water_only:
            st_names[(lk, mode)] = "встроенный причал предприятия"
            continue
        station_keys.append((lk, mode))
        st_names[(lk, mode)] = st.name
        if (lk, mode) in built_stations:
            continue
        st_cost += st.cost
        st_maint += st.maintenance
    # ---- depots per leg group (feeders by mode at the hub, trunk, final)
    groups: dict[tuple, tuple[int, object]] = {}
    for role, src, dst, mode, r, ev, v, m in legs:
        gk = (role, mode)
        n0, anchor = groups.get(gk, (0, None))
        groups[gk] = (n0 + ev.n_consists, dst if role == "feeder" else src)
    dp_cost = dp_maint = 0.0
    depot_names = []
    for (role, mode), (n, anchor) in groups.items():
        dp = facilities.depot(ctx.cat, mode, year, n)
        depot_names.append(dp.name)
        if (loc_key(anchor), mode) in built_stations:
            continue
        dp_cost += dp.cost
        dp_maint += dp.maintenance
    # ---- warehouses: cycle stock (largest batch in or out) + safety stock over one trunk headway
    year_s = game.year_ms / 1000.0
    rate = total / year_s
    feeder_caps = [l[5].consist.capacity for l in legs if l[0] == "feeder"]
    wh_list = []
    wh_cost = wh_maint = 0.0
    classes = cargo.classes if SPECIALIZED_WAREHOUSE else ["UNIVERSAL"]
    for whub, batch_in, out_ev in ((hub, max(feeder_caps), tev),) + (((hub2, tev.consist.capacity, fev),) if hub2 else ()):
        headway = out_ev.trip_time_s / max(out_ev.n_consists, 1)
        buffer = max(out_ev.consist.capacity, batch_in) + SAFETY_Z * math.sqrt(headway * batch_in * rate)
        # rule of thumb from players: room for >= 2 of the largest consists so a train/ship can
        # always unload completely in one call even when the outbound line is late
        buffer = max(buffer, WAREHOUSE_TWO_CONSISTS * max(out_ev.consist.capacity, batch_in))
        modules = max(1, math.ceil(buffer / WAREHOUSE_MODULE))
        wh = facilities.warehouse(ctx.cat, year, classes)
        paid = modules - (1 if (whub.key, "warehouse") in built_stations else 0)
        wh_cost += paid * wh.cost
        wh_maint += paid * wh.maintenance
        station_keys.append((whub.key, "warehouse"))
        wh_list.append({"hub": whub.label, "hub_key": whub.key, "x": round(whub.x, 1), "y": round(whub.y, 1),
                        "name": wh.name, "modules": modules, "buffer_units": round(buffer, 1),
                        "cost": round(paid * wh.cost), "maintenance": round(paid * wh.maintenance),
                        "notes": wh.notes})
    # ---- routes, vehicles
    route_build = route_maint = 0.0
    pair_keys = []
    vehicles: dict[int, list] = {}
    leg_dicts = []
    life = math.inf
    for k, (role, src, dst, mode, r, ev, v, m) in enumerate(legs):
        if mode != "water":
            pk = _pair_key(loc_key(src), loc_key(dst), mode)
            pair_keys.append(pk)
            if pk not in built_keys:
                route_build += r.build
                route_maint += r.maint
        for veh, cnt in ev.consist.units:
            e = vehicles.setdefault(veh.id, [veh, 0])
            e[1] += cnt * ev.n_consists
            life = min(life, veh.lifespan_years)
        se, de = ctx.endpoint(src), ctx.endpoint(dst)
        leg_dicts.append({
            "role": role, "mode": mode,
            "from": {"name": se.name, "key": loc_key(src), "x": round(se.x, 1), "y": round(se.y, 1)},
            "to": {"name": de.name, "key": loc_key(dst), "x": round(de.x, 1), "y": round(de.y, 1)},
            "length_m": round(ev.route_m), "straight_m": round(ev.straight_m), "volume": round(v, 1),
            "consist": ev.consist.label, "n_consists": ev.n_consists,
            "vehicles": [{"model": veh.file.rsplit("/", 1)[-1], "name": veh.name, "count": cnt * ev.n_consists,
                          "price": round(veh.price)} for veh, cnt in ev.consist.units],
            "income": round(leg_income[k]), "running": round(ev.running), "vehicle_capex": round(ev.vehicle_capex),
            "route_build": round(0.0 if (mode != "water" and pair_keys and pair_keys[-1] in built_keys) else r.build),
            "trip_time_s": round(ev.trip_time_s, 1), "one_way_s": round(_one_way(ev), 1),
            "station_from": st_names.get((loc_key(src), mode)), "station_to": st_names.get((loc_key(dst), mode)),
            "_route": r,
        })
    # ---- direct alternatives of the same producers to the same consumer
    dnpv, dany, dinfo = 0.0, False, []
    for m, v in zip(members, vols):
        val, mode, ev = ctx.direct(m.p, d, cargo, v, year, built_keys, built_stations)
        if val is not None and ev is not None and ev.profit > 0:
            dany = True
        dnpv += max(0.0, val) if val is not None else 0.0
        dinfo.append({"producer": m.p, "name": nodes[m.p].name, "mode": mode,
                      "npv": None if val is None else round(val),
                      "profit": None if ev is None else round(ev.profit),
                      "capex": None if ev is None else round(ev.capex)})
    return ChainEval(cargo.key, hub, d, trunk_mode, hub2, list(members), vols, leg_dicts, income,
                     running=sum(l[5].running for l in legs),
                     maintenance=route_maint + st_maint + dp_maint + wh_maint,
                     vehicle_capex=sum(l[5].vehicle_capex for l in legs),
                     infra_capex=route_build + st_cost + dp_cost + wh_cost,
                     vehicles=vehicles, life=life if math.isfinite(life) else 30.0,
                     stations=station_keys, pair_keys=pair_keys, warehouses=wh_list,
                     direct_chord_income=direct_chord, direct_npv=dnpv, direct_any=dany, direct_info=dinfo,
                     max_time_s=max_t)


def _greedy(ctx, cargo, hub, d, trunk_mode, hub2, pool, cap, year, built_keys, built_stations,
            trunk_route, final_route) -> ChainEval | None:
    """Forward selection of producers maximizing the advantage over their direct links."""
    best: ChainEval | None = None
    selected: list[Member] = []
    while len(selected) < MAX_MEMBERS:
        step: ChainEval | None = None
        for m in pool:
            if m in selected:
                continue
            trial = selected + [m]
            if not any(t.mode for t in trial):
                continue
            ce = _eval_chain(ctx, cargo, hub, d, trunk_mode, hub2, trial, cap, year, built_keys,
                             built_stations, trunk_route, final_route)
            if ce is not None and (step is None or ce.advantage > step.advantage):
                step = ce
        if step is None or (best is not None and step.advantage <= best.advantage):
            break
        best = step
        selected = step.members
    return best


# ----------------------------------------------------------------------------- candidates

def _to_candidate(ctx: _Context, ce: ChainEval, game) -> ProjectCandidate:
    nodes = ctx.nodes
    cargo = game.cargo_by_key[ce.cargo]
    D = nodes[ce.d]
    names = [nodes[m.p].name for m in ce.members]
    src = names[0] if len(names) == 1 else f"{len(names)} источника ({'; '.join(names)})"
    modes = []
    for l in ce.legs:
        if l["role"] == "feeder":
            if MODE_RU[l["mode"]] not in modes:
                modes.append(MODE_RU[l["mode"]])
    chain_modes = "/".join(modes) + " → склад → " + MODE_RU[ce.trunk_mode] + (" → склад → грузовики" if ce.hub2 else "")
    title = f"Мультимодально ({chain_modes}): {cargo.name.lower()} {src} → {D.name}"
    geometry: list[list[float]] = []
    for l in ce.legs:
        pts = ctx.route_xy(l.pop("_route"))
        l["geometry"] = pts
        geometry.extend(pts)
    fmt = lambda v: f"{v:,.0f}".replace(",", " ")
    lines = [f"{ROLE_RU[l['role']]} ({MODE_RU[l['mode']]}): {l['from']['name']} → {l['to']['name']}, "
             f"{l['length_m'] / 1000:.1f} км, {l['volume']:.0f} ед./год, {l['n_consists']}× {l['consist']}, "
             f"доход {fmt(l['income'])}/год" for l in ce.legs]
    wh = "; ".join(f"{w['name']} ×{w['modules']} ({w['hub']}, буфер {w['buffer_units']:.0f} ед.)" for w in ce.warehouses)
    alt = [f"{a['name']}: {MODE_RU.get(a['mode'], a['mode'])} NPV {fmt(a['npv'])}" if a["npv"] is not None
           else f"{a['name']}: прямой связи нет" for a in ce.direct_info]
    details = (f"{sum(ce.vols):.0f} ед./год. " + ". ".join(lines) + f". Склады: {wh}. "
               f"Перевалка через склад обязательна (станции не хранят груз). Оплата — по хорде "
               f"источник→потребитель, делится между плечами. Инфраструктура {fmt(ce.infra_capex)}, "
               f"техника {fmt(ce.vehicle_capex)}. NPV {NPV_YEARS} лет {fmt(ce.npv)}; прямые альтернативы: "
               + "; ".join(alt) + ".")
    station_keys = list(dict.fromkeys(ce.stations))
    return ProjectCandidate(
        kind="multimodal", mode="multi", title=title, details=details,
        nodes=[ce.members[0].p, ce.d] + [m.p for m in ce.members[1:]],
        cargo=ce.cargo, volume=sum(ce.vols), income=ce.income, running=ce.running,
        maintenance=ce.maintenance, vehicle_capex=ce.vehicle_capex, infra_capex=ce.infra_capex,
        vehicles=[VehicleLine(v.id, v.name, v.file, n, v.price) for v, n in ce.vehicles.values()],
        vehicle_life_years=ce.life, geometry=geometry or None, legs=ce.legs,
        pair_keys=list(dict.fromkeys(ce.pair_keys)), station_keys=station_keys,
        uses_supply={(m.p, ce.cargo): v for m, v in zip(ce.members, ce.vols)},
        uses_demand={(ce.d, ce.cargo): sum(ce.vols)},
        meta={"hub": (round(ce.hub.x, 1), round(ce.hub.y, 1)), "hub_key": ce.hub.key, "hub_kind": ce.hub.kind,
              "hub_label": ce.hub.label, "hub_origin": ce.hub.origin,
              "hub2": None if ce.hub2 is None else (round(ce.hub2.x, 1), round(ce.hub2.y, 1)),
              "trunk_mode": ce.trunk_mode, "warehouse": ce.warehouses[0], "warehouses": ce.warehouses,
              "npv": round(ce.npv), "direct_npv": round(ce.direct_npv), "advantage": round(ce.advantage),
              "direct_any": ce.direct_any, "direct_alternatives": ce.direct_info,
              "direct_chord_income": round(ce.direct_chord_income, 2), "max_delivery_s": round(ce.max_time_s, 1)},
    )


def generate(game, vehicles, planner, year: int, supply: dict, demand: dict,
             built_keys: set | None = None, built_stations: set | None = None,
             stats: dict | None = None) -> list[ProjectCandidate]:
    """Multimodal chain candidates for the current state (see module docstring).

    supply/demand: (node, cargo) -> free units/year. built_keys: infrastructure pair keys already
    built (route part free); built_stations: (location, mode) station keys already built
    (location = node index or hub key; mode may also be "warehouse")."""
    parts = generate_parts(game, vehicles, planner, year, supply, demand, built_keys, built_stations, stats=stats)
    return merge_parts(parts)


def merge_parts(parts: list[tuple[str, list[ProjectCandidate]]]) -> list[ProjectCandidate]:
    """generate() output from generate_parts() pieces (any split by cargo): cargo order, then NPV."""
    out = [pc for _c, lst in sorted(parts, key=lambda x: x[0]) for pc in lst]
    out.sort(key=lambda pc: -pc.meta["npv"])
    return out


def cargo_keys(game, supply: dict, demand: dict) -> list[str]:
    """Cargos generate_parts() iterates over (for splitting the work between processes)."""
    prod = {c for (_p, c), v in supply.items() if v >= MIN_VOL and c in game.cargo_by_key}
    return sorted({c for (_d, c), v in demand.items() if v >= MIN_VOL and c in prod})


def generate_parts(game, vehicles, planner, year: int, supply: dict, demand: dict,
                   built_keys: set | None = None, built_stations: set | None = None,
                   stats: dict | None = None, cargos=None) -> list[tuple[str, list[ProjectCandidate]]]:
    """generate() per cargo: [(cargo, candidates in generate() order)]. cargos: only these cargos
    (None = all). Cargos are independent (every cache key holds the cargo), so the union of
    the parts of any split equals the full run."""
    t0 = time.perf_counter()
    only = set(cargos) if cargos is not None else None
    parts, info = chain_parts(game, vehicles, planner, year, supply, demand, built_keys, built_stations,
                              own=None if only is None else (lambda key: key[0] in only))
    out = select_chains(_context(planner, game), game, parts)
    if stats is not None:
        stats.update({**info, "candidates": sum(len(lst) for _c, lst in out), "seconds": time.perf_counter() - t0})
    return out


def chain_parts(game, vehicles, planner, year: int, supply: dict, demand: dict,
                built_keys: set | None = None, built_stations: set | None = None, own=None) -> tuple[list, dict]:
    """The evaluation loop of generate(), split per (cargo, hub #): [((cargo, hub #), [(key, best
    ChainEval)])] in generate() order. own(key) -> bool selects the parts to compute (None = all):
    every (cargo, hub) is independent, select_chains() makes the per-consumer choice afterwards."""
    built_keys = built_keys or set()
    built_stations = built_stations or set()
    ctx = _context(planner, game)
    ctx._vehicles = vehicles
    nodes = ctx.nodes
    prod_by_cargo: dict[str, list[tuple[int, float]]] = {}
    for (p, c), v in supply.items():
        if v >= MIN_VOL and c in game.cargo_by_key:
            prod_by_cargo.setdefault(c, []).append((p, v))
    cons_by_cargo: dict[str, list[tuple[int, float]]] = {}
    for (d, c), v in demand.items():
        if v >= MIN_VOL and c in prod_by_cargo:
            cons_by_cargo.setdefault(c, []).append((d, v))
    prod_by_cargo = {c: sorted(v) for c, v in prod_by_cargo.items() if c in cons_by_cargo}
    prod = {p for items in prod_by_cargo.values() for p, _ in items}
    cons = {d for items in cons_by_cargo.values() for d, _ in items}
    hubs = ctx.hubs(prod, cons, prod_by_cargo, cons_by_cargo)
    n_eval = 0
    parts: list = []
    for c in sorted(prod_by_cargo):
        cargo = game.cargo_by_key[c]
        producers = prod_by_cargo[c]
        for hi, hub in enumerate(hubs):
            if own is not None and not own((c, hi)):
                continue
            best: dict[tuple, ChainEval] = {}
            parts.append(((c, hi), best))
            members = ctx.members(hub, producers)
            if not any(m.mode for m in members):
                continue
            has_ship_feeder = any(m.mode == "water" for m in members)
            for d, cap in sorted(cons_by_cargo[c]):
                D = nodes[d]
                if hub.node == d:
                    continue
                dh = math.hypot(D.x - hub.x, D.y - hub.y)
                if dh <= (HARBOR_CATCHMENT if hub.kind == "harbor" else RAIL_CATCHMENT):
                    continue        # feeders could deliver directly: not a transfer chain
                pool = []
                for m in members:
                    P = nodes[m.p]
                    if m.p == d:
                        continue
                    pd = math.hypot(P.x - D.x, P.y - D.y)
                    ph = math.hypot(P.x - hub.x, P.y - hub.y)
                    if pd < MIN_CHORD_M or (ph + dh) > MAX_DETOUR * pd:
                        continue
                    pool.append(m)
                if not any(m.mode for m in pool):
                    continue
                options = []
                if hub.kind == "harbor":
                    wr = ctx.water_route(hub, d)
                    land_pool = [m for m in pool if m.mode != "water"]
                    if wr is not None and wr.length > HARBOR_CATCHMENT and any(m.mode for m in land_pool):
                        if D.water_only or wr.port_dist <= HARBOR_CATCHMENT:
                            options.append(("water", None, wr, None, land_pool))
                        elif wr.port_dist <= FEEDER_MAX_M:
                            cell = wr.cells[-1]
                            x2, y2 = ctx.wcell_xy(cell)
                            h2 = Hub(f"hub:{round(x2)}:{round(y2)}", "harbor", x2, y2, ctx.zat(x2, y2),
                                     f"порт у «{D.name}»", None, hub.body, int(cell), "consumer_port")
                            fr = ctx.land_route(h2, d, "road", feeder=True)
                            if fr is not None and fr.length <= FEEDER_MAX_M:
                                options.append(("water", h2, wr, fr, land_pool))
                    if not D.water_only:
                        for mode in ("rail", "road"):
                            if mode == "road" and not has_ship_feeder:
                                continue        # truck -> warehouse -> truck never beats a direct truck
                            r = ctx.land_route(hub, d, mode)
                            if r is not None and r.length > 0:
                                options.append((mode, None, r, None, pool))
                else:
                    if not D.water_only:
                        r = ctx.land_route(hub, d, "rail")
                        if r is not None and r.length > 0:
                            options.append(("rail", None, r, None, pool))
                for mode, h2, tr, fr, opool in options:
                    if not any(m.mode for m in opool):
                        continue
                    ce = _greedy(ctx, cargo, hub, d, mode, h2, opool, cap, year, built_keys, built_stations, tr, fr)
                    n_eval += 1
                    if ce is None or ce.profit <= 0:
                        continue
                    if not (ce.advantage > 0 or not ce.direct_any):
                        continue
                    key = (d, hub.key, hub.kind)
                    if key not in best or ce.advantage > best[key].advantage:
                        best[key] = ce
    info = {"hubs": len(hubs), "harbor_hubs": sum(h.kind == "harbor" for h in hubs),
            "land_hubs": sum(h.kind == "land" for h in hubs), "options": n_eval}
    return [(k, list(best.items())) for k, best in parts], info


def select_chains(ctx, game, parts: list) -> list[tuple[str, list[ProjectCandidate]]]:
    """chain_parts() pieces (any split) -> [(cargo, candidates)] as generate_parts(): best chain per
    (consumer, hub), then the per-consumer selection."""
    by_cargo: dict[str, dict] = {}
    for (c, _hi), items in sorted(parts, key=lambda x: x[0]):
        best = by_cargo.setdefault(c, {})
        for key, ce in items:
            if key not in best or ce.advantage > best[key].advantage:
                best[key] = ce
    out = []
    for c in sorted(by_cargo):
        best = by_cargo[c]
        kept: list[ChainEval] = []
        by_dest: dict[int, list[ChainEval]] = {}
        for ce in best.values():
            by_dest.setdefault(ce.d, []).append(ce)
        for d, lst in by_dest.items():
            # keep the MAX_PER_DEST best chains per consumer plus, for every producer, its
            # PER_PRODUCER best chains to that consumer (small producers are not crowded out)
            lst.sort(key=lambda ce: -ce.advantage)
            seen, per_p = set(), {}
            for k, ce in enumerate(lst):
                sig = (frozenset(m.p for m in ce.members), ce.trunk_mode)
                if sig in seen:
                    continue
                if k >= MAX_PER_DEST and all(per_p.get(m.p, 0) >= PER_PRODUCER for m in ce.members):
                    continue
                seen.add(sig)
                kept.append(ce)
                for m in ce.members:
                    per_p[m.p] = per_p.get(m.p, 0) + 1
        out.append((c, [_to_candidate(ctx, ce, game) for ce in kept]))
    return out


# ----------------------------------------------------------------------------- CLI

def main():
    from .planner import Planner
    t0 = time.perf_counter()
    pl = Planner()
    sim = pl.initial_state()
    t1 = time.perf_counter()
    stats: dict = {}
    cands = generate(pl.game, pl.vehicles, pl, sim.year, sim.supply, sim.demand, set(), set(), stats)
    t2 = time.perf_counter()
    stats2: dict = {}
    generate(pl.game, pl.vehicles, pl, sim.year, sim.supply, sim.demand, set(), set(), stats2)
    print(f"planner {t1 - t0:.1f}s; generate {t2 - t1:.1f}s (warm {stats2['seconds']:.1f}s); {stats}")
    fmt = lambda v: "—" if v is None else f"{v / 1e6:7.2f}M"
    print(f"{'#':>2} {'chain':95} {'vol':>5} {'income':>8} {'profit':>8} {'capex':>8} {'NPV':>8} | best direct NPV")
    for k, c in enumerate(cands[:15], 1):
        print(f"{k:2} {c.title[:95]:95} {c.volume:5.0f} {fmt(c.income)} {fmt(c.profit)} {fmt(c.capex)} "
              f"{fmt(c.meta['npv'])} | {fmt(c.meta['direct_npv']) if c.meta['direct_any'] else 'нет прибыльной прямой'}")
        for l in c.legs:
            print(f"     {l['role']:6} {l['mode']:5} {l['from']['name'][:38]:38} -> {l['to']['name'][:38]:38} "
                  f"{l['length_m'] / 1000:5.1f} km {l['volume']:5.0f} u {l['n_consists']:3}x {l['consist'][:34]:34} "
                  f"inc {l['income'] / 1e6:5.2f}M")
        w = c.meta["warehouse"]
        print(f"     warehouse: {w['name']} x{w['modules']} buffer {w['buffer_units']:.0f} u, cost {w['cost']:,}")


if __name__ == "__main__":
    main()
