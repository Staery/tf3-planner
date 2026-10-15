"""Terrain routing engine: buildable rail/road routes, all-pairs network matrices,
shared-infrastructure (Steiner) networks and ship routes on lakes/rivers.

Grid and graph
--------------
* The 32 m game heightmap is resampled (bilinear) to a coarser node grid, ``cell`` metres
  (default 64 m, any value works). Nodes sit at ``origin + i * cell``.
* Every node is linked to its 16 neighbours (8 king moves + 8 knight moves), which gives
  route headings in steps of ~26.6 deg instead of 45 deg and therefore much less zig-zag.
  Edges are stored once per undirected direction (8 "canonical" directions) as arrays of
  shape (8, N) and assembled into a directed ``scipy.sparse`` CSR graph.
* Each edge is sampled along its length every ``sample_spacing`` metres (default 8 m) on the
  fine 32 m heightmap. Samples below the water level are water -> that part must be bridged.
  This catches 1-cell (32 m) rivers that a pure coarse grid would miss.

Cost model (all ASSUMPTIONS, to be calibrated against the game)
--------------------------------------------------------------
Heights used for grades are "effective" heights: terrain height on land, and
``water_level + water_clearance`` (default +5 m) on water nodes (a bridge deck, not the lake bed).

For an edge of horizontal length L, height change dz, allowed grade s = way.max_slope:

1. Track/road:  ``L * way.cost_per_m * (1 + grade_penalty * min(grade/s, 1)^2)``.
   The quadratic term (default +25 % at the limit grade) stands for earthworks the game
   does implicitly when a track follows sloped terrain.
2. Water: the wet length Lw is bridged with the chosen bridge type:
   ``Lw * bridge.cost_per_m * mean(max(1, h / threshold) ** exponent)`` over the wet samples.
   The game's type definitions describe bridge ``costFactors`` (x=10, y=2.5, z=1) as
   "threshold, exponent, offset within a curve"; we read it as a power-law height surcharge
   above a 10 m threshold (z, the curve offset, is ignored). h is the deck height above the
   water surface (``bridge_height_ref='water'``, default) or above the lake/river bed
   (``'ground'``). This choice matters a lot: lakes and rivers on this map are ~100 m deep
   (bed clamped at -100), so with 'ground' a deep crossing costs ~350x the per-metre price
   and routes go around lakes instead. Track cost is still paid on top of the bridge
   (TF charges track + bridge).
3. Excess grade: e = max(0, |dz| - s*L) metres must be absorbed. Options, cheapest wins
   (monotone in e and L):
   * earthworks (cut/fill): volume = Ld * (w*d + d^2) with d = e/2 (mean depth) and 1:1 side
     slopes, bed width w (rail 6 m, road 8 m); priced at ``terrain_cost_per_m3``
     (default mean(terrainRaise 6.75, terrainLower 7.875) = 7.31; base/mod.script.tl already
     multiplies these by the purchase-cost difficulty scale, so no extra x1.5).
     Whether the game charges terraforming for track embankments at all is UNKNOWN.
   * structure over the dry length Ld: a tunnel (Ld * tunnel.cost_per_m) when the sampled
     profile bulges above the chord (ridge), else a viaduct (Ld * bridge.cost_per_m *
     height surcharge for h = e/2) (valley / steep slope). Falls back to whichever exists.
   The per-edge view is a local approximation: a real line keeps its grade and carries the
   height deficit into the next cells; A* compensates by routing around steep terrain.
4. Maintenance per year: ``L * way.maintenance_per_m`` + bridge/viaduct length *
   bridge.maintenance_per_m + tunnel length * tunnel.maintenance_per_m
   (assumed per year and additive).
5. Travel time: ``(L - Lslow)/v + Lslow/min(v, bridge.speed_limit)`` (tunnels have no limit),
   times ``1 + slope_slowdown * uphill_grade`` (uphill grade capped at s). slope_slowdown
   defaults to 20 for rail (heavy trains crawl uphill: x3.6 at 13 %) and 0 for road.
   Curvature / min_radius is not modelled (16-connectivity keeps turns >= 26 deg anyway).
6. Routing objective (generalized cost, edge weight):
   ``build + maintenance_years * maintenance_per_year + value_of_time * time_s``
   (defaults: 10 years, value_of_time 4000 $/s). The graph is directed only because of the
   uphill/downhill time asymmetry.
7. Bridge type: cheapest per-metre generalized cost among available bridges for the
   transport kind with ``speed_limit >= bridge_min_speed`` (1900: trestle 225/m, 60 km/h).
   Tunnel: cheapest available tunnel for the kind. Keys containing ``_street``/``_car`` are
   treated as road-only, ``_rail`` as rail-only.
8. Way type: by default the cheapest available non-decorated way of the kind
   (rail: ``simple`` in 1900; road: cheapest ``country_*``/``highway_*`` road, so that 5 km/h
   town alleys are not picked). Override with ``RoutingParams.way_key``.
9. Endpoints: towns/industries are snapped to the nearest land node. Water industries (oil
   platforms, fishing grounds) are snapped to the nearest shore node and flagged
   ``water_only`` (a rail/road link to them is only a link to a harbour site). Station/port costs
   are NOT included; demolition of town buildings is (item 10).
10. Demolition (``RoutingParams.demolition``, default on; research in docs/demolition.md, mask in
   townmask.py). A NEW grid edge (not on an existing way of the kind, not a tunnel) demolishes the
   town buildings its corridor (way width = bed width) touches. Per edge: expected number of buildings,
   money (``bulldoze cost``: export v7 probes, else 150 $ per building = script bulldozeCost 100 x 1.5;
   ``demolition_usd_per_m2`` overrides it per m2 of plot) and town reputation penalty (capacity /
   district capacity / 0.405 per building, game town_util.tl). With export v7 buildings the edge gets
   each building's share = overlap of the edge with the chord of the building's reach disc (w/2 + plot
   radius), so a straight path counts every building once; without v7 the expected values come from the
   street-density / disc rasters. Money is part of the BUILD cost (cash); the penalty enters only the
   generalized cost as ``demolition_rating_usd`` per 1.0 (100 %) of one town's reputation.
   Existing streets/tracks used as they are demolish nothing. Route/segments/matrices report
   ``demolition_cost``, ``demolition_m`` (way length through demolished plots), ``demolition_buildings``,
   ``demolition_rating`` (sum over towns) and ``Route.demolition_towns`` {town: penalty}.

Ships
-----
Ship routes run on the fine 32 m water grid (h < water_level), 8-connected, with zero build
cost (ports are separate). Nodes get ports on every water body within ``port_radius`` metres.
Speed defaults to the fastest ship available in the year (from the vehicle export).

A* uses the admissible heuristic ``straight_distance * min generalized cost per metre``
(= way cost + years * way maintenance + value_of_time / way speed), so it returns the same
cost as Dijkstra. All-pairs use ``scipy.sparse.csgraph.dijkstra`` from every node.

Existing network (export v6, ``RoutingParams.use_existing``, default on)
-----------------------------------------------------------------------
The existing streets (road kind: town/country roads, highways, player roads, station and
industry access roads; not tram-only tracks or airport taxiways) or tracks (rail kind) are
rasterized onto the grid (``network.TransportNetwork.grid_segments``): every grid edge that an
existing way passes through between two 8-adjacent cells gets build cost 0, maintenance 0
(town/country roads are not maintained by the player; the player's own ways are paid anyway,
so the marginal maintenance is 0), no bridge/tunnel/earthworks (already built, valid even over
water) and travel time ``L / way speed`` of the existing way (max lane speed of its template).
Routes report the length on such edges in ``Route.reused_m`` and matrices in ``existing_m``.
A* then has a near-zero admissible heuristic, so ``route_xy`` switches to scipy Dijkstra.
NOTE: with value_of_time 0 the router follows existing roads even on long detours (build is
free, only travel time grows); the default 4000 $/s keeps road detours <= ~1.9x.

Route geometry and segments (``RoutePlanner.make_route``, ``NetworkMatrix.route``)
------------------------------------------------------------------------------
The grid path is a sequence of 64 m cell centres: along an existing road it zig-zags beside the
real road, and new parts look like a staircase. ``make_route`` therefore splits the path into
``Route.segments`` ``[{type, polyline, length_m, geo_m, way, build_cost[, upgrade_to]}]``:

* ``existing``: grid edges covered by an existing way of the kind. Drawn over the REAL way: the
  run's end cells are snapped to the curve geometry of the ways rasterized into the run
  (``TransportNetwork.snap_point``) and joined by the shortest network path between them
  (``path_between``, Hermite curves from the edge tangents). If that path leaves the grid run
  by more than ``SNAP_TOL_CELLS`` cells (another road, a gap in the network), the run is split in
  halves and retried; unmatched pieces keep the (simplified) grid geometry. Build cost 0.
  Short new gaps (<= ``GAP_MERGE_EDGES`` grid edges) between two runs on the same way are
  rasterization artefacts (the grid cut a corner of the road): they become existing too and
  their build/maintenance/structures are dropped from the Route (so Route.build_cost can be a
  little below the matrix value).
* ``upgrade``: existing way slower than ``UPGRADE_SPEED_RATIO`` x the planned way (not town
  streets unless ``UPGRADE_TOWN_STREETS``, never construction-internal ways). Travel time is
  still that of the old way (as routed); ``build_cost`` of the segment is the OPTIONAL upgrade
  price (ASSUMPTION: price difference of the templates, full price if the old template is
  unknown), summed in ``Route.upgrade_cost`` and NOT included in ``Route.build_cost``.
* ``new``: to build. Geometry: string pulling - consecutive anchors are joined straight when the
  straight piece stays within ``PULL_CORRIDOR`` cells of the grid path (that removes the 64 m
  staircase) or when the straightest lattice path between them (``_lattice``) is valid, crosses
  no bridge/tunnel and costs at most ``PULL_TOL`` more than the grid path (an equivalent way for
  the player); bridge/tunnel stretches keep their cells. Then corners are rounded (Chaikin with
  the cut bounded to 0.75 cell). Costs/lengths stay those of the grid path.
* ``shared``: built by an earlier route of the same ``build_network`` plan (build 0).

Invariants: sum(length_m) == Route.length_m (grid lengths; ``geo_m`` is the drawn length);
sum(build_cost of "new") == Route.build_cost; existing_m/upgrade_m/new_m/shared_m are the per-type
sums. ``Route.polyline`` is the concatenation (densified to <= one cell between points),
``Route.grid_polyline`` the raw cell centres. ``route_geometry``/``route_segments_json``/
``line_json`` produce compact JSON for plan steps (``step['geometry']``, ``step['segments']``).
``NetworkMatrix.route(i, j)`` is canonical by default: i -> j and j -> i are the same physical
way (the stored path of the lower index pair, reversed when needed), so loops and shuttles
draw out and back legs on one line.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import time as _time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from scipy import ndimage, sparse
from scipy.sparse import csgraph

from .model import DATA, Bridge, Game, Terrain, Way
from . import network as _network

__all__ = [
    "RoutingParams", "Route", "Node", "RoutePlanner", "NetworkMatrix", "NetworkPlan",
    "WaterBody", "WaterNetwork", "load_game", "network_nodes", "default_way", "pick_bridge",
    "pick_tunnel", "get_planner", "route", "network_matrix", "build_network", "water_network",
    "land_components", "route_geometry", "route_segments_json", "segment_totals", "line_json", "line_routes",
    "SEG_TYPES",
]

# canonical (undirected) neighbour offsets (dy, dx); the reverse direction is implied
CANON: list[tuple[int, int]] = [(0, 1), (1, 0), (1, 1), (1, -1), (1, 2), (2, 1), (1, -2), (2, -1)]
_LUT_DIR = np.full(25, -1, dtype=np.int64)
_LUT_SGN = np.zeros(25, dtype=np.int64)
for _d, (_dy, _dx) in enumerate(CANON):
    _LUT_DIR[(_dy + 2) * 5 + _dx + 2] = _d
    _LUT_SGN[(_dy + 2) * 5 + _dx + 2] = 1
    _LUT_DIR[(-_dy + 2) * 5 - _dx + 2] = _d
    _LUT_SGN[(-_dy + 2) * 5 - _dx + 2] = -1

# route shaping (Route.segments); module constants, NOT RoutingParams, so matrix caches stay valid
SEG_TYPES = ("existing", "upgrade", "new", "shared")
UPGRADE_SPEED_RATIO = 0.98    # existing way slower than this x planned way speed -> "upgrade"
UPGRADE_TOWN_STREETS = False  # town-owned streets are used as they are, never marked for upgrade
SNAP_TOL_CELLS = 1.5          # real way geometry must stay within this many cells of the grid path
GAP_MERGE_EDGES = 3           # new gaps of <= this many grid edges between two existing runs on one way
PULL_TOL = 0.002              # string pulling: a straight lattice path may cost <= +0.2 % of the grid path
PULL_CORRIDOR = 0.75          # ... and is always accepted while it stays within 0.75 cell of the grid path

DEFAULT_SLOPE_SLOWDOWN = {"rail": 20.0, "road": 0.0}
DEFAULT_BED_WIDTH = {"rail": 6.0, "road": 8.0}
_EPS = 1e-6
_CACHE_VERSION = 5   # bump when the cost model changes (3: demolition, 4: industry footprints, 5: road grade 0.12)

# Industry territory (export v8 footprints: subconstructions, fields): a new way must not run through it.
# Grid edges touching a footprint (+ margin) get a prohibitive search penalty (not a build cost, so the
# reported money stays real), and an endpoint inside a footprint is moved to an access cell just outside
# it, preferring a cell on an existing way (industries spawn with their own access road).
ROAD_MAX_GRADE = 0.12         # planned street grade limit (the mod builds with the same limit)
INDUSTRY_MARGIN_M = 8.0
INDUSTRY_PENALTY = 5.0e6          # search cost per grid edge touching an industry footprint
INDUSTRY_ACCESS_RADIUS_M = 400.0  # endpoint -> access cell search radius
INDUSTRY_ACCESS_ROAD_BONUS_M = 150.0  # an existing-way cell counts this much closer


# --------------------------------------------------------------------------- helpers

def load_game() -> Game:
    """Game.load(), tolerating export fields the model does not parse yet.

    Temporary workaround: some exports carry ``perCargo`` as a string/list, which the current
    model.py cannot read. In that case the field is blanked (routing does not need it)."""
    try:
        return Game.load()
    except (AttributeError, TypeError):
        export = json.loads((DATA / "export.json").read_text(encoding="utf-8"))
        recipes = json.loads((DATA / "industry_recipes.json").read_text(encoding="utf-8"))
        for ind in export.get("industries", []):
            if not isinstance(ind.get("perCargo"), dict):
                ind["perCargo"] = {}
        return Game(export, recipes)


def _bridge_fits_kind(b: Bridge, kind: str) -> bool:
    carriers = getattr(b, "carriers", ()) or ()
    if carriers:                                   # the game's own data (ROAD = 0, RAIL = 1)
        return (1 if kind == "rail" else 0) in carriers
    k = b.key.lower()
    if kind == "rail":
        return "_street" not in k and "_car" not in k
    return "_rail" not in k


def default_way(game: Game, kind: str, year: int | None = None) -> Way | None:
    """Cheapest plain (no barriers/trees/bus/tram/catenary) way of the kind available in year.
    Roads are restricted to country roads and highways."""
    ws = game.ways_available(kind, year)
    bad = ("barriers", "trees", "bus", "tram", "catenary")
    plain = [w for w in ws if not any(b in w.key for b in bad)]
    if kind == "road":
        plain = [w for w in plain if w.key.startswith(("country", "highway"))] or plain
    ws = plain or ws
    return min(ws, key=lambda w: (w.cost_per_m, -w.speed)) if ws else None


def pick_bridge(bridges: Iterable[Bridge], kind: str, year: int, way: Way,
                params: "RoutingParams") -> Bridge | None:
    cand = [b for b in bridges if not b.tunnel and b.available(year) and _bridge_fits_kind(b, kind)
            and (b.speed_limit <= 0 or b.speed_limit >= params.bridge_min_speed)]
    if not cand:
        return None

    def per_m(b: Bridge) -> float:
        v = min(way.speed, b.speed_limit) if b.speed_limit > 0 else way.speed
        return b.cost_per_m + params.maintenance_years * b.maintenance_per_m + params.value_of_time / v

    return min(cand, key=lambda b: (per_m(b), -b.speed_limit))


def pick_tunnel(bridges: Iterable[Bridge], kind: str, year: int) -> Bridge | None:
    cand = [b for b in bridges if b.tunnel and b.available(year) and _bridge_fits_kind(b, kind)]
    road_specific = lambda b: ("_car" in b.key or "_street" in b.key) == (kind == "road")
    return min(cand, key=lambda b: (b.cost_per_m, not road_specific(b), b.maintenance_per_m)) if cand else None


def _ship_speed(game: Game, year: int) -> float:
    best = 0.0
    for v in (getattr(game, "raw", None) or {}).get("vehicles", []) or []:
        md = v.get("metadata") or {}
        wv = md.get("waterVehicle")
        if not wv or "/ship/" not in str(v.get("name", "")):
            continue
        av = md.get("availability") or {}
        yf, yt = av.get("yearFrom", 0), av.get("yearTo", 0)
        if yf <= year and (yt == 0 or year < yt):
            best = max(best, float(wv.get("topSpeed") or 0))
    return best or 8.33


# --------------------------------------------------------------------------- data classes

@dataclass(frozen=True)
class RoutingParams:
    cell: float = 64.0                    # node spacing, m
    maintenance_years: float = 10.0       # years of maintenance added to the objective
    # money per second of one-way travel time (at the way's speed). 4000 = median over the 1900
    # profitable road links of 10 years x (running + capital charge of the fleet) per second of
    # trip (rail median ~8000). With 0, routes followed existing roads on 2.5x median / 32x max
    # detours because they are free to build (export v6 network); with 4000: median 1.25x, max 1.9x.
    value_of_time: float = 4000.0
    grade_penalty: float = 0.25           # extra build cost fraction at the max allowed grade
    terrain_cost_per_m3: float | None = None   # None -> mean(terrainRaise, terrainLower)
    bed_width: float | None = None        # None -> rail 6 m, road 8 m
    bridge_height_ref: str = "water"      # deck height measured above 'water' surface or 'ground' (bed)
    bridge_threshold: float | None = None # height surcharge threshold, None -> bridge costFactors.x
    bridge_exponent: float | None = None  # height surcharge exponent, None -> bridge costFactors.y
    water_clearance: float = 5.0          # bridge deck height above water level, m
    bridge_min_speed: float = 0.0         # m/s; bridges slower than this are not used
    allow_bridges: bool = True
    allow_tunnels: bool = True
    slope_slowdown: float | None = None   # None -> rail 20, road 0
    sample_spacing: float = 8.0           # edge sampling step on the fine heightmap, m
    way_key: str | None = None            # force a specific track/road template
    use_existing: bool = True             # existing roads/tracks (export v6 network) are free to build
    # demolition of town buildings by new ways (docs/demolition.md, townmask.py)
    demolition: bool = True
    # money equivalent of 1.0 (= 100 %) town reputation penalty, generalized cost only. ASSUMPTION
    # (docs/demolition.md): a 100 % loss takes 18 game months to recover (1/6 per 3 months) and cuts
    # the town's growth/XP multiplier to 0 meanwhile; one building costs ~2-5 % reputation.
    demolition_rating_usd: float = 2.0e6
    demolition_usd_per_m2: float | None = None   # money per m2 of building plot; None -> probes / 150 $ per building
    demolition_width: float | None = None        # corridor width of a new way, None -> bed_width

    def key(self) -> str:
        return hashlib.sha1(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]


@dataclass
class Node:
    index: int
    key: str            # "town:<id>" | "industry:<id>"
    id: int
    name: str
    kind: str           # "town" | "industry"
    type: str           # "town" or industry type, e.g. "oil_platform"
    x: float
    y: float
    z: float
    water_only: bool    # industry sits on water (rail/road reaches only the shore)


@dataclass
class Route:
    kind: str
    way_key: str
    polyline: list[tuple[float, float]]
    length_m: float
    build_cost: float
    maintenance_per_year: float
    travel_time_s: float                # start -> end
    travel_time_back_s: float           # end -> start
    generalized_cost: float
    max_grade: float                    # of the built line (<= way.max_slope)
    max_terrain_grade: float            # raw terrain grade along the path
    bridges: list[tuple[tuple[float, float], tuple[float, float], float]]  # (start, end, length)
    tunnels: list[tuple[tuple[float, float], tuple[float, float], float]]
    water_bridge_m: float
    viaduct_m: float
    tunnel_m: float
    earthworks_m3: float
    profile: list[tuple[float, float]]  # (distance along route m, track height m)
    straight_distance: float
    detour_factor: float
    cells: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.int64))
    reused_m: float = 0.0               # length on already built infrastructure (existing ways + build_network)
    # route shaping (see "Route geometry and segments" in the module doc). polyline above is the
    # shaped line: existing parts over the real road / track, new parts string-pulled and rounded.
    segments: list[dict] = field(default_factory=list)
    existing_m: float = 0.0             # grid length used as it is on existing ways (build 0)
    upgrade_m: float = 0.0              # on existing ways slower than the planned way (build 0, see upgrade_cost)
    new_m: float = 0.0                  # to build (sum of build_cost of "new" segments == build_cost)
    shared_m: float = 0.0               # built by an earlier route of the same build_network plan
    upgrade_cost: float = 0.0           # optional: upgrading the "upgrade" segments (NOT in build_cost)
    grid_polyline: list[tuple[float, float]] = field(default_factory=list, repr=False)  # raw cell centres
    # demolition of town buildings by the new parts (module doc item 10); money IS in build_cost
    demolition_cost: float = 0.0        # $, part of build_cost
    demolition_m: float = 0.0           # new way length running through demolished plots
    demolition_buildings: float = 0.0   # (expected) number of demolished town buildings
    demolition_rating: float = 0.0      # reputation penalty, sum over towns (0.25 = -25 % in one town)
    demolition_rating_cost: float = 0.0 # its money equivalent (generalized cost only, not cash)
    demolition_towns: dict = field(default_factory=dict)   # town name -> reputation penalty

    @property
    def start(self) -> tuple[float, float]:
        return self.polyline[0]

    @property
    def end(self) -> tuple[float, float]:
        return self.polyline[-1]

    def summary(self) -> str:
        reuse = (f", existing {self.existing_m / 1000:.2f} km, upgrade {self.upgrade_m / 1000:.2f} km, "
                 f"new {self.new_m / 1000:.2f} km") if self.segments else ""
        demo = (f", demolition {self.demolition_buildings:.1f} bldg / {self.demolition_m:.0f} m, "
                f"{self.demolition_cost:,.0f} $, reputation -{self.demolition_rating:.0%}"
                if self.demolition_buildings > 0.05 else "")
        return (f"{self.kind}/{self.way_key}: {self.length_m / 1000:.2f} km (x{self.detour_factor:.2f}), "
                f"build {self.build_cost:,.0f}, maint {self.maintenance_per_year:,.0f}/yr, "
                f"time {self.travel_time_s / 60:.1f} min, max grade {self.max_grade:.1%}, "
                f"bridges {self.water_bridge_m + self.viaduct_m:.0f} m, tunnels {self.tunnel_m:.0f} m" + reuse + demo)


# --------------------------------------------------------------------------- planner

class RoutePlanner:
    """Cost grid + graph for one transport kind, way, bridge and tunnel type."""

    def __init__(self, terrain: Terrain, way: Way, bridge: Bridge | None, tunnel: Bridge | None,
                 params: RoutingParams = RoutingParams(), kind: str | None = None,
                 terrain_cost_per_m3: float = 7.3125,
                 bridge_cost_factors: tuple[float, float, float] = (10.0, 2.5, 1.0),
                 network: "_network.TransportNetwork | None" = None,
                 townmask=None, industries=None):
        t0 = _time.perf_counter()
        self.terrain = terrain
        self.way = way
        self.kind = kind or way.kind
        self.params = params
        self.bridge = bridge if params.allow_bridges else None
        self.tunnel = tunnel if params.allow_tunnels else None
        self.terrain_cost = params.terrain_cost_per_m3 if params.terrain_cost_per_m3 is not None \
            else terrain_cost_per_m3
        self.bed_width = params.bed_width if params.bed_width is not None else DEFAULT_BED_WIDTH.get(self.kind, 6.0)
        self.slowdown = params.slope_slowdown if params.slope_slowdown is not None \
            else DEFAULT_SLOPE_SLOWDOWN.get(self.kind, 0.0)
        self.water_level = float(terrain.water_level)
        self.bridge_threshold = params.bridge_threshold if params.bridge_threshold is not None             else float(bridge_cost_factors[0])
        self.bridge_exponent = params.bridge_exponent if params.bridge_exponent is not None             else float(bridge_cost_factors[1])

        c = float(params.cell)
        self.cell = c
        self.nx = int(((terrain.nx - 1) * terrain.step) // c) + 1
        self.ny = int(((terrain.ny - 1) * terrain.step) // c) + 1
        self.N = self.nx * self.ny
        self.x0, self.y0 = float(terrain.origin_x), float(terrain.origin_y)
        iy, ix = np.mgrid[0:self.ny, 0:self.nx]
        self.z = ndimage.map_coordinates(terrain.h, [iy * c / terrain.step, ix * c / terrain.step],
                                         order=1, mode="nearest").astype(np.float64)
        self.water = self.z < self.water_level
        self.zeff = np.where(self.water, self.water_level + params.water_clearance, self.z)
        if self.water.any() and not self.water.all():
            idx = ndimage.distance_transform_edt(self.water, return_distances=False, return_indices=True)
            self._land_idx = (idx[0] * self.nx + idx[1]).ravel()
        else:
            self._land_idx = np.arange(self.N)

        self.ways: dict[str, Way] = {}       # all way templates by key (upgrade costs); set by from_game
        self.shape_routes = True             # make_route: segments + real road geometry (False: raw cells)
        self._mark_existing(network if params.use_existing else None)
        self.tm = townmask if params.demolition else None
        self.dem_width = params.demolition_width if params.demolition_width is not None else self.bed_width
        self.rating_usd = float(params.demolition_rating_usd)
        self._demolition_edges()
        self._industry_mask(industries)
        self._build_edges()
        Y, vot, v = params.maintenance_years, params.value_of_time, way.speed
        self.min_cost_per_m = way.cost_per_m + Y * way.maintenance_per_m + vot / v
        # admissible A* heuristic: existing edges cost only travel time
        self.h_cost_per_m = min(self.min_cost_per_m, vot / self.existing_vmax) if self.existing_vmax > 0 \
            else self.min_cost_per_m
        self.build_seconds = _time.perf_counter() - t0

    # ------------------------------------------------------------ construction
    @classmethod
    def from_game(cls, game: Game, kind: str, year: int | None = None,
                  params: RoutingParams = RoutingParams()) -> "RoutePlanner":
        year = game.year if year is None else year
        if params.way_key:
            way = next((w for w in game.ways if w.key == params.way_key), None)
            if way is None:
                raise ValueError(f"unknown way {params.way_key}")
        else:
            way = default_way(game, kind, year)
        if way is None:
            raise ValueError(f"no {kind} way available in {year}")
        costs = ((game.raw.get("baseConfig") or {}).get("costs") or {}) if hasattr(game, "raw") else {}
        tc = (costs.get("terrainRaise", 6.75) + costs.get("terrainLower", 7.875)) / 2
        bridge = pick_bridge(game.bridges, kind, year, way, params)
        cf = (10.0, 2.5, 1.0)
        if bridge is not None:
            for b in (game.raw.get("bridgeTypes") or []) if hasattr(game, "raw") else []:
                if b.get("name", "").endswith("/" + bridge.key):
                    f = (b.get("data") or {}).get("costFactors") or {}
                    cf = (f.get("x", 10.0), f.get("y", 2.5), f.get("z", 1.0))
        net = _network.for_game(game) if params.use_existing else None
        tm = None
        if params.demolition and kind in ("rail", "road"):
            from . import townmask as _townmask
            tm = _townmask.for_game(game)
        ifp = None
        if kind in ("rail", "road"):
            try:
                from .buildings import industry_footprints
                ifp = industry_footprints(game)
            except Exception:  # noqa: BLE001
                ifp = None
        p = cls(game.terrain, way, bridge, pick_tunnel(game.bridges, kind, year), params, kind, tc, cf, net, tm, ifp)
        p.year = year
        p.ways = {w.key: w for w in game.ways}
        return p

    def _mark_existing(self, net: "_network.TransportNetwork | None") -> None:
        """e_exist[d, anchor] / e_exspeed: grid edges covered by an existing way of this kind."""
        shape = (len(CANON), self.N)
        self.e_exist = np.zeros(shape, bool)
        self.e_exspeed = np.zeros(shape, np.float64)
        self.e_exedge = np.full(shape, -1, np.int32)   # fastest network edge covering the grid edge
        self.net = None
        self.existing_hash = None
        self.existing_vmax = 0.0
        self.existing_edges = 0
        if net is None or self.kind not in ("road", "rail"):
            return
        self.net = net
        u, v, sp, ee = net.grid_segments(self.kind, self.x0, self.y0, self.cell, self.nx, self.ny, return_edges=True)
        self.existing_hash = f"{net.hash()}:{self.kind}"
        if len(u) == 0:
            return
        uy, ux = np.divmod(u, self.nx)
        vy, vx = np.divmod(v, self.nx)
        code = (vy - uy + 2) * 5 + (vx - ux + 2)
        d = _LUT_DIR[code]
        ok = d >= 0
        anc = np.where(_LUT_SGN[code] > 0, u, v)
        d, anc, sp, ee = d[ok], anc[ok], sp[ok], ee[ok]
        self.e_exist[d, anc] = True
        np.maximum.at(self.e_exspeed, (d, anc), sp)
        order = np.argsort(sp, kind="stable")          # the fastest edge wins (written last)
        self.e_exedge[d[order], anc[order]] = ee[order]
        self.existing_vmax = float(sp.max()) if len(sp) else 0.0
        self.existing_edges = int(self.e_exist.sum())

    def _industry_mask(self, fp) -> None:
        """self.ind_half[2*iy + k, 2*ix + l]: half-cell lattice points inside an industry footprint
        (+ INDUSTRY_MARGIN_M); self.ind_cell: grid nodes inside one."""
        self.ind_half = None
        self.ind_cell = None
        self.industry_hash = None
        self._access_cache: dict[int, int] = {}
        if fp is None or not len(fp) or self.kind not in ("road", "rail"):
            return
        h = self.cell / 2
        H = np.zeros((2 * self.ny - 1, 2 * self.nx - 1), bool)
        m = INDUSTRY_MARGIN_M
        for x0, y0, x1, y1 in np.asarray(fp.rects, dtype=np.float64).tolist():
            j0 = max(0, int(math.ceil((x0 - m - self.x0) / h)))
            j1 = min(H.shape[1] - 1, int(math.floor((x1 + m - self.x0) / h)))
            i0 = max(0, int(math.ceil((y0 - m - self.y0) / h)))
            i1 = min(H.shape[0] - 1, int(math.floor((y1 + m - self.y0) / h)))
            if j1 >= j0 and i1 >= i0:
                H[i0:i1 + 1, j0:j1 + 1] = True
        if not H.any():
            return
        self.ind_half = H
        self.ind_cell = H[0::2, 0::2].ravel().copy()
        self.industry_hash = hashlib.sha1(np.round(np.asarray(fp.rects, dtype=np.float64), 1).tobytes()).hexdigest()[:16]

    def _demolition_edges(self) -> None:
        """Raw per-edge demolition (before existing / tunnel / water exclusions, applied in _build_edges):
        e_dcost money, e_dpen reputation penalty, e_dn buildings, e_dm length through plots."""
        shape = (len(CANON), self.N)
        self.e_dcost = np.zeros(shape)
        self.e_dpen = np.zeros(shape)
        self.e_dn = np.zeros(shape, np.float32)
        self.e_dm = np.zeros(shape, np.float32)
        self.demolition_hash = None
        self._dpairs = None
        tm = self.tm
        if tm is None:
            return
        P = self.params
        w = float(self.dem_width)
        self.demolition_hash = (f"{tm.hash()}:{w:.2f}:{P.demolition_usd_per_m2}")
        if tm.exact:
            d, anc, b, share, ov = tm.grid_pairs(self.x0, self.y0, self.cell, self.nx, self.ny, CANON, w)
            money = tm.bcost if P.demolition_usd_per_m2 is None else tm.barea * float(P.demolition_usd_per_m2)
            np.add.at(self.e_dcost, (d, anc), share * money[b])
            np.add.at(self.e_dpen, (d, anc), share * tm.bpen[b])
            dn = np.zeros(shape)
            np.add.at(dn, (d, anc), share)
            self.e_dn = dn.astype(np.float32)
            dm = np.zeros(shape)
            np.add.at(dm, (d, anc), ov)
            lens = np.array([self.cell * math.hypot(dy, dx) for dy, dx in CANON])
            self.e_dm = np.minimum(dm, lens[:, None]).astype(np.float32)   # union ~ sum, at most the edge
            key = d * self.N + anc
            order = np.argsort(key, kind="stable")
            self._dpairs = (key[order], b[order], share[order])
        else:
            ex = tm.grid_expected(self.x0, self.y0, self.cell, self.nx, self.ny, CANON, w)
            self.e_dcost = ex["cost"] if P.demolition_usd_per_m2 is None else ex["area"] * float(P.demolition_usd_per_m2)
            self.e_dpen = ex["pen"]
            self.e_dn = ex["n"].astype(np.float32)
            self.e_dm = ex["m"].astype(np.float32)

    def demolition_towns(self, d: np.ndarray, anc: np.ndarray, weight: np.ndarray | None = None) -> dict[str, float]:
        """{town name: reputation penalty} of the grid edges (d, anc), each scaled by weight (0 = skip)."""
        tm = self.tm
        out: dict[str, float] = {}
        if tm is None or len(d) == 0:
            return out
        wgt = np.ones(len(d)) if weight is None else np.asarray(weight, np.float64)
        if self._dpairs is not None:
            keys, bb, sh = self._dpairs
            for k, wk in zip((np.asarray(d) * self.N + np.asarray(anc)).tolist(), wgt.tolist()):
                if wk <= 0:
                    continue
                lo, hi = np.searchsorted(keys, k), np.searchsorted(keys, k, side="right")
                for b, s in zip(bb[lo:hi].tolist(), sh[lo:hi].tolist()):
                    t = tm.town_name(int(tm.btown[b]))
                    out[t] = out.get(t, 0.0) + wk * s * float(tm.bpen[b])
            return out
        pen = self.e_dpen[d, anc] * wgt
        if not (pen > 0).any():
            return out
        dy = np.array([CANON[k][0] for k in np.asarray(d).tolist()])
        dx = np.array([CANON[k][1] for k in np.asarray(d).tolist()])
        iy, ix = np.divmod(np.asarray(anc), self.nx)
        xs = self.x0 + (ix + dx / 2) * self.cell
        ys = self.y0 + (iy + dy / 2) * self.cell
        for t, p in zip(tm.town_at(xs, ys).tolist(), pen.tolist()):
            if p > 0 and t >= 0:
                nm = tm.town_name(int(t))
                out[nm] = out.get(nm, 0.0) + p
        return out

    def _build_edges(self) -> None:
        p, t, way = self.params, self.terrain, self.way
        nx, ny, N, c = self.nx, self.ny, self.N, self.cell
        wl, s = self.water_level, way.max_slope
        if self.kind == "road":
            # the street templates say 0.4, but the game refuses script-built streets that steep
            # ("Слишком большой наклон", 2026-10-05); its own generated roads stay under ~0.16 (p99)
            s = min(s, ROAD_MAX_GRADE)
        br, tn = self.bridge, self.tunnel
        zeff = self.zeff.ravel()
        Y, vot, v = p.maintenance_years, p.value_of_time, way.speed
        vb = min(v, br.speed_limit) if (br and br.speed_limit > 0) else v

        shape = (len(CANON), N)
        self.e_len = np.array([c * math.hypot(dy, dx) for dy, dx in CANON])
        self.e_valid = np.zeros(shape, bool)
        self.e_build = np.full(shape, np.nan)
        self.e_maint = np.full(shape, np.nan)
        self.e_lw = np.zeros(shape, np.float32)      # water bridge length
        self.e_lv = np.zeros(shape, np.float32)      # viaduct length
        self.e_lt = np.zeros(shape, np.float32)      # tunnel length
        self.e_ev = np.zeros(shape, np.float32)      # earthworks volume
        self.e_t0 = np.full(shape, np.nan, np.float32)  # flat travel time
        rows, cols, data = [], [], []

        for d, (dy, dx) in enumerate(CANON):
            iy0, iy1, ix0, ix1 = 0, ny - dy, max(0, -dx), nx - max(0, dx)
            if iy1 <= iy0 or ix1 <= ix0:
                continue
            IY, IX = np.mgrid[iy0:iy1, ix0:ix1]
            IY, IX = IY.ravel(), IX.ravel()
            a = IY * nx + IX
            b = a + dy * nx + dx
            L = self.e_len[d]
            ns = int(math.ceil(L / p.sample_spacing)) + 1
            ts = np.linspace(0.0, 1.0, ns)
            fy = ((IY[None, :] + ts[:, None] * dy) * c) / t.step
            fx = ((IX[None, :] + ts[:, None] * dx) * c) / t.step
            prof = ndimage.map_coordinates(t.h, [fy.ravel(), fx.ravel()], order=1,
                                           mode="nearest").reshape(ns, -1).astype(np.float64)
            del fy, fx
            wet = prof < wl
            wts = np.full(ns, 1.0 / (ns - 1))
            wts[[0, -1]] *= 0.5
            lw = (wts @ wet) * L
            za, zb = zeff[a], zeff[b]
            dz = zb - za
            chord = za[None, :] + ts[:, None] * dz[None, :]
            nwet = wet.sum(0)
            ref = wl if p.bridge_height_ref == "water" else prof
            hmult = self._height_mult(chord - ref)
            mw = np.where(nwet > 0, np.where(wet, hmult, 0.0).sum(0) / np.maximum(nwet, 1), 1.0)
            del hmult
            grade = np.abs(dz) / L
            e = np.maximum(np.abs(dz) - s * L, 0.0)
            ld = L - lw
            if ns > 2:
                ridge = (np.maximum(prof[1:-1], wl) - chord[1:-1]).mean(0) > 0
            else:
                ridge = np.zeros(a.size, bool)
            del prof, chord, wet

            earth_vol = ld * (self.bed_width * e / 2 + (e / 2) ** 2)
            earth_cost = self.terrain_cost * earth_vol
            via_cost = ld * br.cost_per_m * self._height_mult(e / 2) if br else np.full(a.size, np.inf)
            tun_cost = ld * tn.cost_per_m if tn else np.full(a.size, np.inf)
            if br and tn:
                is_tun = ridge
            else:
                is_tun = np.full(a.size, tn is not None)
            struct_cost = np.where(is_tun, tun_cost, via_cost)
            use_struct = (e > 0) & (struct_cost < earth_cost)
            lv = np.where(use_struct & ~is_tun, ld, 0.0)
            lt = np.where(use_struct & is_tun, ld, 0.0)
            ev = np.where(use_struct, 0.0, earth_vol)

            build = L * way.cost_per_m * (1 + p.grade_penalty * np.minimum(grade / s, 1.0) ** 2)
            build = build + np.where(use_struct, struct_cost, earth_cost)
            maint = np.full(a.size, L * way.maintenance_per_m) + lt * (tn.maintenance_per_m if tn else 0)
            if br:
                build = build + lw * br.cost_per_m * mw
                maint = maint + (lw + lv) * br.maintenance_per_m
            slow = lw + lv
            t0 = (L - slow) / v + slow / vb
            valid = np.ones(a.size, bool) if br else (lw <= 0)
            ex = self.e_exist[d, a]
            # demolition: only the dry, not tunnelled part of a NEW edge (bridges over water demolish
            # nothing; viaducts are assumed to clear the ground like a track - conservative)
            dfac = np.where((lt > 0) | ex, 0.0, np.clip(1.0 - lw / L, 0.0, 1.0))
            for q in (self.e_dcost, self.e_dpen, self.e_dn, self.e_dm):
                q[d, a] = q[d, a] * dfac
            dcost = self.e_dcost[d, a]
            build = build + dcost
            if ex.any():
                # existing way: nothing to build or maintain, its own speed, valid even over water
                build = np.where(ex, 0.0, build)
                maint = np.where(ex, 0.0, maint)
                lw, lv, lt, ev = (np.where(ex, 0.0, q) for q in (lw, lv, lt, ev))
                t0 = np.where(ex, L / np.maximum(self.e_exspeed[d, a], 0.1), t0)
                valid = valid | ex

            self.e_valid[d, a] = valid
            self.e_build[d, a] = build
            self.e_maint[d, a] = maint
            self.e_lw[d, a] = lw
            self.e_lv[d, a] = lv
            self.e_lt[d, a] = lt
            self.e_ev[d, a] = ev
            self.e_t0[d, a] = t0

            base = build + Y * maint + self.rating_usd * self.e_dpen[d, a]
            if self.ind_half is not None:
                # new ways through industry territory: prohibitive (search only, not a build cost)
                Hh = self.ind_half
                touch = Hh[2 * IY, 2 * IX] | Hh[2 * (IY + dy), 2 * (IX + dx)] | Hh[2 * IY + dy, 2 * IX + dx]
                base = base + np.where(touch & ~ex, INDUSTRY_PENALTY, 0.0)
            up = np.clip(dz / L, 0, s)
            down = np.clip(-dz / L, 0, s)
            gf = base + vot * t0 * (1 + self.slowdown * up) + _EPS
            gb = base + vot * t0 * (1 + self.slowdown * down) + _EPS
            av, bv = a[valid], b[valid]
            rows += [av, bv]
            cols += [bv, av]
            data += [gf[valid], gb[valid]]

        rows_a, cols_a, data_a = np.concatenate(rows), np.concatenate(cols), np.concatenate(data)
        self.graph = sparse.csr_matrix((data_a, (rows_a, cols_a)), shape=(N, N))
        self.graph.sort_indices()
        self._indptr = memoryview(self.graph.indptr.astype(np.int64))
        self._indices = memoryview(self.graph.indices.astype(np.int64))

    def _height_mult(self, h: np.ndarray) -> np.ndarray:
        """Bridge cost multiplier for deck height h (power law above the threshold)."""
        return np.maximum(1.0, np.maximum(h, 0.0) / self.bridge_threshold) ** self.bridge_exponent

    # ------------------------------------------------------------ geometry
    def cell_xy(self, cell: int | np.ndarray):
        iy, ix = np.divmod(cell, self.nx)
        return self.x0 + ix * self.cell, self.y0 + iy * self.cell

    def nearest_cell(self, x: float, y: float) -> int:
        ix = int(np.clip(round((x - self.x0) / self.cell), 0, self.nx - 1))
        iy = int(np.clip(round((y - self.y0) / self.cell), 0, self.ny - 1))
        return iy * self.nx + ix

    def snap(self, x: float, y: float) -> int:
        """Nearest land node (water positions go to the nearest shore node); a position inside an
        industry footprint goes to its access cell (see _access_cell)."""
        cell = int(self._land_idx[self.nearest_cell(x, y)])
        if self.ind_cell is not None and self.ind_cell[cell]:
            return self._access_cell(x, y, cell)
        return cell

    def _access_cell(self, x: float, y: float, cell: int) -> int:
        """Node just outside the industry footprint around (x, y): the nearest one, an existing way
        node counting INDUSTRY_ACCESS_ROAD_BONUS_M closer."""
        got = self._access_cache.get(cell)
        if got is not None:
            return got
        r = int(math.ceil(INDUSTRY_ACCESS_RADIUS_M / self.cell))
        iy, ix = divmod(cell, self.nx)
        y0, y1 = max(0, iy - r), min(self.ny - 1, iy + r)
        x0, x1 = max(0, ix - r), min(self.nx - 1, ix + r)
        IY, IX = np.mgrid[y0:y1 + 1, x0:x1 + 1]
        cells = (IY * self.nx + IX).ravel()
        ok = ~self.ind_cell[cells] & ~self.water.ravel()[cells]
        if not ok.any():
            self._access_cache[cell] = cell
            return cell
        cells = cells[ok]
        cx, cy = self.cell_xy(cells)
        dist = np.hypot(cx - x, cy - y)
        if self.existing_edges:
            on_way = self._existing_cells()[cells]
            dist = dist - INDUSTRY_ACCESS_ROAD_BONUS_M * on_way
        best = int(cells[int(np.argmin(dist))])
        self._access_cache[cell] = best
        return best

    def _existing_cells(self) -> np.ndarray:
        """Grid nodes touched by an existing way of this kind."""
        ec = getattr(self, "_ex_cells", None)
        if ec is None:
            ec = np.zeros(self.N, bool)
            for d, (dy, dx) in enumerate(CANON):
                a = np.nonzero(self.e_exist[d])[0]
                if len(a):
                    ec[a] = True
                    ec[a + dy * self.nx + dx] = True
            self._ex_cells = ec
        return ec

    # ------------------------------------------------------------ search
    def astar(self, s: int, t: int, weights: np.ndarray | None = None) -> tuple[list[int] | None, float]:
        """A* on the grid graph. ``weights`` optionally replaces graph.data (same CSR order);
        the heuristic is only admissible if no weight is below the default per-metre minimum."""
        if s == t:
            return [s], 0.0
        indptr, indices = self._indptr, self._indices
        data = memoryview(np.ascontiguousarray(weights if weights is not None else self.graph.data, dtype=np.float64))
        nx = self.nx
        hk = self.h_cost_per_m * self.cell
        ty, tx = divmod(t, nx)
        hyp = math.hypot
        push, pop = heapq.heappush, heapq.heappop
        g = {s: 0.0}
        parent = {s: -1}
        closed = set()
        sy, sx = divmod(s, nx)
        heap = [(hyp(sx - tx, sy - ty) * hk, s)]
        found = False
        while heap:
            _, u = pop(heap)
            if u in closed:
                continue
            if u == t:
                found = True
                break
            closed.add(u)
            gu = g[u]
            for k in range(indptr[u], indptr[u + 1]):
                w = indices[k]
                if w in closed:
                    continue
                nd = gu + data[k]
                if nd < g.get(w, math.inf):
                    g[w] = nd
                    parent[w] = u
                    wy, wx = divmod(w, nx)
                    push(heap, (nd + hyp(wx - tx, wy - ty) * hk, w))
        if not found:
            return None, math.inf
        path = [t]
        while path[-1] != s:
            path.append(parent[path[-1]])
        path.reverse()
        return path, g[t]

    def dijkstra_path(self, s: int, t: int, graph: sparse.csr_matrix | None = None) -> tuple[list[int] | None, float]:
        dist, pred = csgraph.dijkstra(graph if graph is not None else self.graph, directed=True,
                                      indices=s, return_predecessors=True)
        if not np.isfinite(dist[t]):
            return None, math.inf
        return _walk(pred, s, t), float(dist[t])

    # ------------------------------------------------------------ routes
    def edge_attrs(self, path: Sequence[int]) -> dict[str, np.ndarray]:
        p = np.asarray(path, dtype=np.int64)
        u, v = p[:-1], p[1:]
        uy, ux = np.divmod(u, self.nx)
        vy, vx = np.divmod(v, self.nx)
        code = (vy - uy + 2) * 5 + (vx - ux + 2)
        if np.any((np.abs(vy - uy) > 2) | (np.abs(vx - ux) > 2)) or np.any(_LUT_DIR[code] < 0):
            raise ValueError("path contains non-adjacent cells")
        d = _LUT_DIR[code]
        anc = np.where(_LUT_SGN[code] > 0, u, v)
        L = self.e_len[d]
        zf = self.zeff.ravel()
        dz = zf[v] - zf[u]
        s = self.way.max_slope
        t0 = self.e_t0[d, anc].astype(np.float64)
        return dict(
            L=L, build=self.e_build[d, anc], maint=self.e_maint[d, anc],
            lw=self.e_lw[d, anc].astype(np.float64), lv=self.e_lv[d, anc].astype(np.float64),
            lt=self.e_lt[d, anc].astype(np.float64), ev=self.e_ev[d, anc].astype(np.float64),
            t_fwd=t0 * (1 + self.slowdown * np.clip(dz / L, 0, s)),
            t_bwd=t0 * (1 + self.slowdown * np.clip(-dz / L, 0, s)),
            grade=np.abs(dz) / L, valid=self.e_valid[d, anc], exist=self.e_exist[d, anc],
            exedge=self.e_exedge[d, anc].astype(np.int64),
            dcost=self.e_dcost[d, anc].copy(), dpen=self.e_dpen[d, anc].copy(),
            dn=self.e_dn[d, anc].astype(np.float64), dm=self.e_dm[d, anc].astype(np.float64),
            d=d, anc=anc,
        )

    def edge_gen(self, a: dict, fwd: bool = True) -> np.ndarray:
        """Generalized cost per edge of edge_attrs (build incl. demolition money + maintenance years +
        reputation equivalent + value of time)."""
        P = self.params
        return (a["build"] + P.maintenance_years * a["maint"] + self.rating_usd * a["dpen"]
                + P.value_of_time * (a["t_fwd"] if fwd else a["t_bwd"]))

    def make_route(self, path: Sequence[int], reused: np.ndarray | None = None, shape: bool | None = None) -> Route:
        """Route object for a node path. ``reused[k]`` marks edge k as already built
        (its build and maintenance cost are then 0). ``shape`` (default ``self.shape_routes``):
        split into existing / upgrade / new / shared segments and draw existing parts over the
        real road / track geometry (see the module doc)."""
        p = np.asarray(path, dtype=np.int64)
        xs, ys = self.cell_xy(p)
        poly = list(zip(xs.tolist(), ys.tolist()))
        zf = self.zeff.ravel()[p]
        if p.size < 2:
            return Route(self.kind, self.way.key, poly, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                         [], [], 0.0, 0.0, 0.0, 0.0, [(0.0, float(zf[0]))] if p.size else [], 0.0, 1.0, p,
                         grid_polyline=list(poly))
        a = self.edge_attrs(p)
        build, maint = a["build"].copy(), a["maint"].copy()
        lw, lv, lt, ev = (a[k].copy() for k in ("lw", "lv", "lt", "ev"))
        dcost, dpen, dn, dm = (a[k].copy() for k in ("dcost", "dpen", "dn", "dm"))
        exist = a["exist"].copy()
        shared = np.zeros(len(build), bool)
        if reused is not None and np.any(reused):
            shared = np.asarray(reused, bool) & ~exist
            for q in (build, maint, dcost, dpen, dn, dm):
                q[shared] = 0.0
        shape = self.shape_routes if shape is None else shape
        segs: list[dict] = []
        shaped = poly
        if shape:
            segs, shaped, merged = self._shape(p, a, shared)
            if merged.any():
                # grid shortcut next to an existing way (rasterization artefact): use the way itself
                for q in (build, maint, lw, lv, lt, ev, dcost, dpen, dn, dm):
                    q[merged] = 0.0
                exist |= merged
        reused_mask = exist | shared
        reused_m = float(a["L"][reused_mask].sum())
        P = self.params
        length = float(a["L"].sum())
        tf, tb = float(a["t_fwd"].sum()), float(a["t_bwd"].sum())
        bcost, mcost = float(build.sum()), float(maint.sum())
        rating = float(dpen.sum())
        gen = bcost + P.maintenance_years * mcost + self.rating_usd * rating + P.value_of_time * tf
        towns = self.demolition_towns(a["d"], a["anc"], (dpen > 0).astype(np.float64)) if rating > 0 else {}
        straight = float(math.hypot(xs[-1] - xs[0], ys[-1] - ys[0]))
        cum = np.concatenate([[0.0], np.cumsum(a["L"])])
        raw = float(a["grade"].max())
        bridges, tunnels = _spans(lw + lv, poly), _spans(lt, poly)
        if shape and len(shaped) >= 2:
            S = np.asarray(shaped, np.float64)
            bridges = [(_project(S, s0), _project(S, s1), ln) for s0, s1, ln in bridges]
            tunnels = [(_project(S, s0), _project(S, s1), ln) for s0, s1, ln in tunnels]
        tot = {t: sum(g["length_m"] for g in segs if g["type"] == t) for t in SEG_TYPES}
        return Route(
            kind=self.kind, way_key=self.way.key, polyline=shaped, length_m=length, build_cost=bcost,
            maintenance_per_year=mcost, travel_time_s=tf, travel_time_back_s=tb, generalized_cost=gen,
            max_grade=min(raw, self.way.max_slope), max_terrain_grade=raw,
            bridges=bridges, tunnels=tunnels,
            water_bridge_m=float(lw.sum()), viaduct_m=float(lv.sum()),
            tunnel_m=float(lt.sum()), earthworks_m3=float(ev.sum()),
            profile=list(zip(cum.tolist(), zf.tolist())), straight_distance=straight,
            detour_factor=length / straight if straight > 0 else 1.0, cells=p, reused_m=reused_m,
            segments=segs, existing_m=tot["existing"], upgrade_m=tot["upgrade"], new_m=tot["new"],
            shared_m=tot["shared"], upgrade_cost=sum(g["build_cost"] for g in segs if g["type"] == "upgrade"),
            grid_polyline=poly, demolition_cost=float(dcost.sum()), demolition_m=float(dm.sum()),
            demolition_buildings=float(dn.sum()), demolition_rating=rating,
            demolition_rating_cost=self.rating_usd * rating,
            demolition_towns={k: round(v, 4) for k, v in sorted(towns.items(), key=lambda kv: -kv[1])})

    # ------------------------------------------------------------ route shaping
    def _lattice(self, a: int, b: int) -> np.ndarray:
        """Straightest grid path a -> b with this graph's moves (king + knight): the cells nearest
        to the straight line, consecutive straight + diagonal steps merged into knight moves."""
        nx = self.nx
        ay, ax = divmod(int(a), nx)
        by, bx = divmod(int(b), nx)
        dy, dx = by - ay, bx - ax
        n = max(abs(dy), abs(dx))
        if n == 0:
            return np.array([a], np.int64)
        t = np.arange(n + 1) / n
        ys = np.rint(ay + t * dy).astype(np.int64)
        xs = np.rint(ax + t * dx).astype(np.int64)
        diag = ((np.diff(ys) != 0) & (np.diff(xs) != 0)).tolist()
        keep = np.ones(n + 1, bool)
        k = 0
        while k < n - 1:
            if diag[k] != diag[k + 1]:
                keep[k + 1] = False
                k += 2
            else:
                k += 1
        return (ys * nx + xs)[keep]

    def _pull(self, cells: np.ndarray, gen: np.ndarray, free: np.ndarray) -> list[int]:
        """String pulling: indices of ``cells`` kept as polyline vertices. cells[i] -> cells[j] is
        drawn straight when the straight lattice path between them is valid, has no bridge / tunnel
        and costs (generalized) at most PULL_TOL more than the grid path it replaces - i.e. it is an
        equivalent way the player can build. Edges with free[k] False (structures) are kept as is."""
        m = len(cells) - 1
        if m <= 1:
            return list(range(m + 1))
        cum = np.concatenate([[0.0], np.cumsum(gen)])

        xs, ys = self.cell_xy(np.asarray(cells))
        C = np.column_stack([xs, ys]).astype(np.float64)
        corridor = PULL_CORRIDOR * self.cell

        def ok(i: int, j: int) -> bool:
            if j - i <= 1:
                return True
            if not free[i:j].all():
                return False
            # inside the grid corridor: the straight piece IS the grid path without its staircase
            if _seg_dist(C[i + 1:j], C[i], C[j]).max() <= corridor:
                return True
            lat = self._lattice(cells[i], cells[j])
            at = self.edge_attrs(lat)
            if not at["valid"].all() or (at["lw"] + at["lv"] + at["lt"]).any():
                return False
            c = float(self.edge_gen(at).sum())
            return c <= (cum[j] - cum[i]) * (1.0 + PULL_TOL) + 1e-6

        out, i = [0], 0
        while i < m:
            if ok(i, m):
                j = m
            else:
                lo, hi = i + 1, m
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    if ok(i, mid):
                        lo = mid
                    else:
                        hi = mid
                j = lo
            out.append(j)
            i = j
        return out

    def _snap_range(self, grid: np.ndarray, exedge: np.ndarray, k0: int, k1: int, L: np.ndarray,
                    depth: int = 0) -> list[tuple[np.ndarray, float, int]]:
        """Existing-way grid edges k0..k1-1 (cells k0..k1) as chunks over the real way geometry:
        [(xy polyline, grid length share, network edge or -1 for raw grid geometry)]. Splits the
        range in halves when the network path does not match the grid path (other road, gap)."""
        net = self.net
        gl = float(L[k0:k1].sum())
        G = grid[k0:k1 + 1]
        res = None
        if net is not None:
            ea = [e for e in exedge[k0:min(k1, k0 + 3)].tolist() if e >= 0]
            eb = [e for e in exedge[max(k0, k1 - 3):k1].tolist() if e >= 0]
            sa = net.snap_point(G[0, 0], G[0, 1], self.kind, edges=ea) if ea else None
            sb = net.snap_point(G[-1, 0], G[-1, 1], self.kind, edges=eb) if eb else None
            tol = SNAP_TOL_CELLS * self.cell
            if sa and sb and sa[2] <= tol and sb[2] <= tol:
                res = net.path_between(sa[:2], sb[:2], self.kind, max_length=1.5 * gl + 2 * self.cell)
            if res is not None:
                pts = np.concatenate([q for _, q in res[0]])
                if _hausdorff(pts, G) > tol:
                    res = None
        if res is None:
            if k1 - k0 >= 4 and depth < 12:
                mid = (k0 + k1) // 2
                return (self._snap_range(grid, exedge, k0, mid, L, depth + 1)
                        + self._snap_range(grid, exedge, mid, k1, L, depth + 1))
            return [(G.copy(), gl, -1)]
        pieces = res[0]
        geo = np.array([_poly_len(q) for _, q in pieces])
        w = geo / geo.sum() if geo.sum() > 0 else np.full(len(pieces), 1.0 / len(pieces))
        return [(np.asarray(q, np.float64), float(gl * wk), int(e)) for (e, q), wk in zip(pieces, w)]

    def _upgrade(self, e: int) -> tuple[bool, float]:
        """(mark network edge e for upgrade, upgrade cost per m). ASSUMPTION: the game upgrades a
        street / track in place for the price difference of the two templates; unknown old
        template -> full price of the planned way."""
        net, way = self.net, self.way
        if e < 0 or net is None or net.speed[e] >= UPGRADE_SPEED_RATIO * way.speed:
            return False, 0.0
        if net.owner[e] == _network.TOWN and not UPGRADE_TOWN_STREETS:
            return False, 0.0
        full = net.templates[int(net.template[e])].name.lower()
        key = net.template_key(e)
        if any(t in full for t in ("/constructions/", "/depots/", "industry_track", "/airport/")) or key.startswith("ind_"):
            return False, 0.0            # construction-internal ways (stations, depots, industries)
        old = self.ways.get(key)
        per_m = max(0.0, way.cost_per_m - old.cost_per_m) if old is not None else way.cost_per_m
        return True, per_m

    def _shape(self, p: np.ndarray, a: dict, shared: np.ndarray) -> tuple[list[dict], list, np.ndarray]:
        """Segments + shaped polyline of a grid path; also the mask of grid edges re-classified
        as existing (short shortcuts between two runs on the same existing way).
        Direction-independent: a reversed path gives the reversed result (string pulling is
        greedy from one end, so the path is always shaped from its lower-numbered end)."""
        n = len(p) - 1
        if n >= 1 and p[-1] < p[0]:
            pr = p[::-1].copy()
            segs, line, merged = self._shape(pr, self.edge_attrs(pr), np.asarray(shared, bool)[::-1])
            for g in segs:
                g["polyline"] = g["polyline"][::-1]
            return segs[::-1], line[::-1], merged[::-1].copy()
        xs, ys = self.cell_xy(p)
        grid = np.column_stack([xs, ys]).astype(np.float64)
        L = a["L"]
        exist = a["exist"]
        exedge = a["exedge"]
        merged = np.zeros(n, bool)

        def runs_of(lab):
            out, k = [], 0
            while k < n:
                j = k
                while j + 1 < n and lab[j + 1] == lab[k]:
                    j += 1
                out.append((int(lab[k]), k, j + 1))
                k = j + 1
            return out

        lab = np.where(exist, 1, np.where(shared, 2, 0))
        runs = runs_of(lab)
        if self.net is not None:
            for r in range(1, len(runs) - 1):
                t, k0, k1 = runs[r]
                if t == 1 or runs[r - 1][0] != 1 or runs[r + 1][0] != 1 or k1 - k0 > GAP_MERGE_EDGES:
                    continue
                w0, w1 = max(runs[r - 1][1], k0 - 3), min(runs[r + 1][2], k1 + 3)
                ch = self._snap_range(grid, exedge, w0, w1, L, depth=99)   # no splitting: all or nothing
                if ch and all(e >= 0 for _, _, e in ch):
                    merged[k0:k1] = True
            if merged.any():
                lab = np.where(exist | merged, 1, lab)
                runs = runs_of(lab)
        gen = self.edge_gen(a)
        geoms = []      # per run: list of chunks (xy, grid length, network edge; -2 = new part)
        for t, k0, k1 in runs:
            if t == 1:
                geoms.append(self._snap_range(grid, exedge, k0, k1, L))
            else:
                # structures and demolishing edges keep their cells: a pulled straight piece could cut
                # through houses the grid path went around
                free = ((a["lw"][k0:k1] + a["lv"][k0:k1] + a["lt"][k0:k1] == 0) & ~exist[k0:k1]
                        & (a["dn"][k0:k1] <= 0.01))
                keep = self._pull(p[k0:k1 + 1], gen[k0:k1], free)
                pts = _simplify(grid[k0 + np.asarray(keep)], 0.25 * self.cell)
                geoms.append([(pts, float(L[k0:k1].sum()), -2)])
        # joints: a new part starts / ends exactly where the neighbouring existing part does
        for r, (t, k0, k1) in enumerate(runs):
            if t == 1:
                continue
            pts = geoms[r][0][0].copy()
            if r > 0:
                pts[0] = geoms[r - 1][-1][0][-1]
            if r + 1 < len(runs):
                pts[-1] = geoms[r + 1][0][0][0]
            geoms[r][0] = (_round_corners(pts, 0.75 * self.cell), geoms[r][0][1], -2)
        segs: list[dict] = []
        for r, (t, k0, k1) in enumerate(runs):
            if t != 1:
                typ = "new" if t == 0 else "shared"
                segs.append({"type": typ, "polyline": geoms[r][0][0], "length_m": float(L[k0:k1].sum()),
                             "way": self.way.key,
                             "build_cost": float(a["build"][k0:k1].sum()) if typ == "new" else 0.0})
                if typ == "new" and float(a["dn"][k0:k1].sum()) > 0:
                    segs[-1].update(demolition_cost=float(a["dcost"][k0:k1].sum()),
                                    demolition_m=float(a["dm"][k0:k1].sum()),
                                    demolition_buildings=float(a["dn"][k0:k1].sum()),
                                    demolition_rating=float(a["dpen"][k0:k1].sum()))
                continue
            for pts, gl, e in geoms[r]:
                if e == -1:      # raw grid geometry on an existing way (could not match the real way)
                    ee = [x for x in exedge[k0:k1].tolist() if x >= 0]
                    e_way = max(set(ee), key=ee.count) if ee else -1
                    up, per_m = False, 0.0
                    pts = _simplify(pts, 0.25 * self.cell)
                else:
                    e_way = e
                    up, per_m = self._upgrade(e)
                typ = "upgrade" if up else "existing"
                name = self.net.template_key(e_way) if (self.net is not None and e_way >= 0) else None
                if segs and segs[-1]["type"] == typ and segs[-1]["way"] == name:
                    g = segs[-1]
                    last = np.asarray(g["polyline"][-1], np.float64)
                    tail = pts if math.hypot(*(pts[0] - last)) > 0.5 else pts[1:]
                    g["polyline"] = np.concatenate([np.asarray(g["polyline"], np.float64), tail])
                    g["length_m"] += gl
                    g["build_cost"] += gl * per_m
                else:
                    if segs:
                        last = np.asarray(segs[-1]["polyline"][-1], np.float64)
                        if math.hypot(*(pts[0] - last)) > 0.5:
                            pts = np.concatenate([last[None], pts])     # connector over a split
                    segs.append({"type": typ, "polyline": pts, "length_m": gl, "way": name,
                                 "build_cost": gl * per_m})
                    if typ == "upgrade":
                        segs[-1]["upgrade_to"] = self.way.key
        line: list[np.ndarray] = []
        for g in segs:
            P = np.asarray(g["polyline"], np.float64).reshape(-1, 2)
            if g["type"] in ("existing", "upgrade"):
                P = _simplify(P, 1.0)        # real way geometry: 8 m samples -> ~1 m accuracy
            if len(P) == 1:
                P = np.concatenate([P, P])
            if line and math.hypot(*(P[0] - line[-1][-1])) < 0.5:
                if len(P) > 1:
                    line.append(P[1:])
            else:
                line.append(P)
            g["polyline"] = [(float(x), float(y)) for x, y in P]
            g["geo_m"] = _poly_len(P)
        merged_line = _densify(np.concatenate(line), self.cell) if line else grid
        return segs, [(float(x), float(y)) for x, y in merged_line], merged

    def route_xy(self, a: tuple[float, float], b: tuple[float, float], method: str = "astar") -> Route | None:
        s, t = self.snap(*a), self.snap(*b)
        if method == "astar" and self.h_cost_per_m < 0.2 * self.min_cost_per_m:
            method = "dijkstra"   # free existing ways make the A* heuristic useless; scipy is faster
        path, _ = self.astar(s, t) if method == "astar" else self.dijkstra_path(s, t)
        return None if path is None else self.make_route(path)

    # ------------------------------------------------------------ edge lookup (for reuse)
    def csr_positions(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        indptr, indices = self.graph.indptr, self.graph.indices
        out = np.empty(len(u), np.int64)
        for k, (a, b) in enumerate(zip(np.asarray(u).tolist(), np.asarray(v).tolist())):
            lo, hi = indptr[a], indptr[a + 1]
            out[k] = lo + int(np.searchsorted(indices[lo:hi], b))
        return out


def _walk(pred: np.ndarray, s: int | None, t: int) -> list[int]:
    path = [int(t)]
    cur = int(t)
    while True:
        nxt = int(pred[cur])
        if nxt < 0:
            break
        path.append(nxt)
        cur = nxt
    path.reverse()
    if s is not None and path[0] != s:
        raise RuntimeError("broken predecessor chain")
    return path


def _spans(lengths: np.ndarray, poly: list[tuple[float, float]]):
    out = []
    on = lengths > 0
    k, n = 0, len(lengths)
    while k < n:
        if not on[k]:
            k += 1
            continue
        j = k
        while j + 1 < n and on[j + 1]:
            j += 1
        out.append((poly[k], poly[j + 1], float(lengths[k:j + 1].sum())))
        k = j + 1
    return out


def _poly_len(P) -> float:
    P = np.asarray(P, np.float64)
    return float(np.hypot(*np.diff(P, axis=0).T).sum()) if len(P) > 1 else 0.0


def _pt_poly_dist(pts: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Distance of every point to the polyline (vectorized, chunked)."""
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    poly = np.asarray(poly, np.float64).reshape(-1, 2)
    if len(poly) == 1:
        return np.hypot(pts[:, 0] - poly[0, 0], pts[:, 1] - poly[0, 1])
    A, AB = poly[:-1], np.diff(poly, axis=0)
    l2 = np.maximum((AB ** 2).sum(1), 1e-12)
    out = np.empty(len(pts))
    step = max(1, 200000 // max(1, len(A)))
    for c in range(0, len(pts), step):
        q = pts[c:c + step, None, :]
        t = np.clip(((q - A[None]) * AB[None]).sum(2) / l2[None], 0.0, 1.0)
        d = q - (A[None] + t[..., None] * AB[None])
        out[c:c + step] = np.sqrt((d ** 2).sum(2).min(1))
    return out


def _seg_dist(pts: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Distance of points to the segment a-b."""
    ab = b - a
    l2 = float(ab @ ab)
    t = np.clip(((pts - a) @ ab) / l2, 0.0, 1.0) if l2 > 1e-12 else np.zeros(len(pts))
    d = pts - (a + t[:, None] * ab)
    return np.sqrt((d * d).sum(1))


def _hausdorff(P: np.ndarray, Q: np.ndarray, step: float = 4.0) -> float:
    """Symmetric Hausdorff distance of two polylines (both densified to step, KD-tree; error <= step/2)."""
    from scipy.spatial import cKDTree
    A, B = _densify(P, step), _densify(Q, step)
    return float(max(cKDTree(B).query(A)[0].max(), cKDTree(A).query(B)[0].max()))


def _project(poly: np.ndarray, pt) -> tuple[float, float]:
    """Nearest point of the polyline to pt."""
    A, AB = poly[:-1], np.diff(poly, axis=0)
    l2 = np.maximum((AB ** 2).sum(1), 1e-12)
    q = np.asarray(pt, np.float64)
    t = np.clip(((q - A) * AB).sum(1) / l2, 0.0, 1.0)
    P = A + t[:, None] * AB
    k = int(np.argmin(((P - q) ** 2).sum(1)))
    return float(P[k, 0]), float(P[k, 1])


def _simplify(P, tol: float) -> np.ndarray:
    """Douglas-Peucker (iterative); keeps both ends."""
    P = np.asarray(P, np.float64)
    if len(P) <= 2 or tol <= 0:
        return P.copy()
    keep = np.zeros(len(P), bool)
    keep[[0, -1]] = True
    stack = [(0, len(P) - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        ab = P[j] - P[i]
        l = math.hypot(ab[0], ab[1])
        q = P[i + 1:j] - P[i]
        d = np.abs(q[:, 0] * ab[1] - q[:, 1] * ab[0]) / l if l > 1e-9 else np.hypot(q[:, 0], q[:, 1])
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return P[keep]


def _round_corners(P, r: float, iters: int = 2) -> np.ndarray:
    """Chaikin corner cutting with the cut limited to r metres from each corner (long straight
    pieces stay straight, a corner moves at most ~r/2 inwards); both ends are fixed."""
    P = np.asarray(P, np.float64)
    for _ in range(iters):
        if len(P) < 3:
            break
        out = [P[0]]
        for k in range(1, len(P) - 1):
            a, v, b = P[k - 1], P[k], P[k + 1]
            da, db = a - v, b - v
            la, lb = math.hypot(*da), math.hypot(*db)
            if la < 1e-9 or lb < 1e-9 or abs(da[0] * db[1] - da[1] * db[0]) < 1e-6 * la * lb:
                out.append(v)            # degenerate or straight: nothing to round
                continue
            out.append(v + da * min(0.25, r / la))
            out.append(v + db * min(0.25, r / lb))
        out.append(P[-1])
        P = np.array(out)
        r *= 0.5
    return P


def _densify(P: np.ndarray, step: float) -> np.ndarray:
    """Insert points so that no polyline piece is longer than step (keeps nearest-vertex lookups
    and down-sampling by index as accurate as on the raw grid polyline)."""
    P = np.asarray(P, np.float64).reshape(-1, 2)
    if len(P) < 2:
        return P
    d = np.hypot(*np.diff(P, axis=0).T)
    n = np.maximum(1, np.ceil(d / step).astype(np.int64))
    seg = np.repeat(np.arange(len(d)), n)
    start = np.concatenate([[0], np.cumsum(n)[:-1]])
    t = (np.arange(int(n.sum())) - start[seg] + 1) / n[seg]
    pts = P[seg] + (P[seg + 1] - P[seg]) * t[:, None]
    return np.concatenate([P[:1], pts])


# --------------------------------------------------------------------------- JSON for plans / map

def route_geometry(r, tol: float = 2.0, digits: int = 1) -> list[list[float]]:
    """Compact map polyline [[x, y], ...] of a Route (Douglas-Peucker, tol metres). Unlike
    down-sampling by index (poly[::k]) it is direction-independent: a reversed route gives the
    reversed list, so out-and-back legs coincide on the map."""
    pts = getattr(r, "polyline", None) or []
    if len(pts) < 2:
        return [[round(float(x), digits), round(float(y), digits)] for x, y in pts]
    return [[round(float(x), digits), round(float(y), digits)] for x, y in _simplify(np.asarray(pts, np.float64), tol)]


def route_segments_json(r, leg: int | None = None, tol: float = 2.0) -> list[dict]:
    """Route.segments as JSON for plan steps / the map:
    ``[{type, polyline, length_m, way, build_cost[, upgrade_to][, leg]}]``; type is
    "existing" (use the existing way as it is), "upgrade" (existing way slower than the planned
    one; build_cost = optional upgrade price, not part of Route.build_cost), "new" (build) or
    "shared" (built by another route of the same network plan). "new" segments through towns also
    carry ``demolition_cost`` ($, INCLUDED in build_cost), ``demolition_m`` (way length through
    demolished plots), ``demolition_buildings`` and ``demolition_rating`` (town reputation penalty)."""
    out = []
    for g in getattr(r, "segments", None) or []:
        d = {"type": g["type"],
             "polyline": [[round(float(x), 1), round(float(y), 1)] for x, y in _simplify(np.asarray(g["polyline"]), tol)],
             "length_m": round(float(g["length_m"])), "way": g.get("way"), "build_cost": round(float(g["build_cost"]))}
        if g.get("upgrade_to"):
            d["upgrade_to"] = g["upgrade_to"]
        if g.get("demolition_buildings"):
            # build_cost of a "new" segment INCLUDES demolition_cost
            d.update(demolition_cost=round(float(g["demolition_cost"])), demolition_m=round(float(g["demolition_m"])),
                     demolition_buildings=round(float(g["demolition_buildings"]), 1),
                     demolition_rating=round(float(g["demolition_rating"]), 4))
        if leg is not None:
            d["leg"] = leg
        out.append(d)
    return out


def segment_totals(segments: Iterable[dict]) -> dict:
    """{existing_m, upgrade_m, new_m, shared_m, build_cost (new only, incl. demolition), upgrade_cost, repeat_m,
    demolition_cost, demolition_m, demolition_buildings} of segments. Length marked ``repeat_m`` (the same physical way already counted by an earlier
    segment of the line, e.g. the return leg of a loop) is counted once; costs pro rata."""
    segs = list(segments or [])
    tot = {f"{t}_m": 0.0 for t in SEG_TYPES}
    tot.update(build_cost=0.0, upgrade_cost=0.0, repeat_m=0.0, demolition_cost=0.0, demolition_m=0.0,
               demolition_buildings=0.0)
    for g in segs:
        L = float(g["length_m"])
        rep = min(L, float(g.get("repeat_m") or 0.0))
        f = (L - rep) / L if L > 0 else 1.0
        tot[f"{g['type']}_m"] += L - rep
        tot["repeat_m"] += rep
        if g["type"] == "new":
            tot["build_cost"] += f * float(g["build_cost"])
            for k in ("demolition_cost", "demolition_m", "demolition_buildings"):
                tot[k] += f * float(g.get(k) or 0.0)
        elif g["type"] == "upgrade":
            tot["upgrade_cost"] += f * float(g["build_cost"])
    out = {k: round(v) for k, v in tot.items()}
    out["demolition_buildings"] = round(tot["demolition_buildings"], 1)
    return out


def line_json(routes: Sequence, tol: float = 2.0, repeat_tol: float = 10.0) -> dict:
    """Geometry + segments of a line made of consecutive legs (None legs are skipped):
    ``{"geometry": [[x, y], ...], "segments": [... with "leg": k and "repeat_m"], "segment_totals": {...}}``.
    Legs run in the given order. ``repeat_m`` of a segment = its length running over the geometry of
    earlier segments of the line (within repeat_tol m): loops / shuttles use ONE way out and back
    (NetworkMatrix.route is canonical), it is built once and the map draws it once;
    ``segment_totals`` count it once."""
    from scipy.spatial import cKDTree
    geom: list[list[float]] = []
    segs: list[dict] = []
    for k, r in enumerate(routes):
        if r is None:
            continue
        g = route_geometry(r, tol)
        geom += g[1:] if geom and g and geom[-1] == g[0] else g
        segs += route_segments_json(r, leg=k, tol=tol)
    seen: list[np.ndarray] = []
    for g in segs:
        P = _densify(np.asarray(g["polyline"], np.float64), 5.0)
        rep = 0.0
        if seen and len(P) > 1:
            d = cKDTree(np.concatenate(seen)).query(P)[0]
            seg = np.hypot(*np.diff(P, axis=0).T)
            close = (d[1:] < repeat_tol) & (d[:-1] < repeat_tol)
            far = np.cumsum(seg) > 2 * repeat_tol          # not the joint with the previous segment
            if far.sum() >= 2:
                seg, close = seg[far], close[far]
            rep = float(seg[close].sum() / max(seg.sum(), 1e-9)) * float(g["length_m"])
        g["repeat_m"] = round(rep)
        seen.append(P)
    return {"geometry": geom or None, "segments": segs, "segment_totals": segment_totals(segs)}


# --------------------------------------------------------------------------- game-level API

_PLANNERS: dict[tuple, RoutePlanner] = {}


def get_planner(game: Game, kind: str, year: int | None = None,
                params: RoutingParams | None = None) -> RoutePlanner:
    """Cached RoutePlanner for (game, kind, year, params)."""
    params = params or RoutingParams()
    year = game.year if year is None else year
    key = (id(game), kind, year, params.key())
    if key not in _PLANNERS:
        if len(_PLANNERS) > 6:
            _PLANNERS.pop(next(iter(_PLANNERS)))
        _PLANNERS[key] = RoutePlanner.from_game(game, kind, year, params)
    return _PLANNERS[key]


def network_nodes(game: Game) -> list[Node]:
    """All towns followed by all industries."""
    nodes = []
    for t in game.towns:
        nodes.append(Node(len(nodes), f"town:{t.id}", t.id, t.name, "town", "town", t.x, t.y, t.z, False))
    for i in game.industries:
        nodes.append(Node(len(nodes), f"industry:{i.id}", i.id, i.name, "industry", i.type, i.x, i.y, i.z,
                          bool(i.on_water)))
    return nodes


def _resolve(nodes: list[Node], item) -> Node | tuple[float, float]:
    if isinstance(item, Node):
        return item
    if isinstance(item, str):
        for n in nodes:
            if n.key == item or n.name == item:
                return n
        raise KeyError(item)
    if isinstance(item, (int, np.integer)):
        for n in nodes:
            if n.id == int(item):
                return n
        raise KeyError(item)
    if hasattr(item, "x") and hasattr(item, "y"):
        for n in nodes:
            if n.id == getattr(item, "id", None):
                return n
        return (float(item.x), float(item.y))
    x, y = item
    return (float(x), float(y))


def _xy(r) -> tuple[float, float]:
    return (r.x, r.y) if isinstance(r, Node) else r


def route(game: Game, kind: str, a, b, year: int | None = None, params: RoutingParams | None = None,
          method: str = "astar") -> Route | None:
    """Point-to-point route. a/b: Node, entity id, node key/name, Town/Industry or (x, y)."""
    pl = get_planner(game, kind, year, params)
    nodes = network_nodes(game)
    return pl.route_xy(_xy(_resolve(nodes, a)), _xy(_resolve(nodes, b)), method)


# --------------------------------------------------------------------------- all-pairs

_MATRIX_FIELDS = ("gen", "build", "maint", "length", "time", "water_bridge_m", "viaduct_m",
                  "tunnel_m", "earthworks_m3", "straight", "existing_m", "demolition_cost", "demolition_m",
                  "demolition_rating")


@dataclass
class NetworkMatrix:
    kind: str
    year: int
    way_key: str
    params: RoutingParams
    nodes: list[Node]
    cells: np.ndarray            # snapped grid cell per node
    gen: np.ndarray              # generalized cost i -> j
    build: np.ndarray
    maint: np.ndarray            # maintenance per year
    length: np.ndarray
    time: np.ndarray             # travel time i -> j, s
    water_bridge_m: np.ndarray
    viaduct_m: np.ndarray
    tunnel_m: np.ndarray
    earthworks_m3: np.ndarray
    straight: np.ndarray         # straight distance between snapped cells
    existing_m: np.ndarray       # length on existing roads/tracks (export v6 network), m
    demolition_cost: np.ndarray  # $ of demolished town buildings, INCLUDED in build
    demolition_m: np.ndarray     # new way length through demolished plots, m
    demolition_rating: np.ndarray  # town reputation penalty (sum over towns; in gen via demolition_rating_usd)
    path_flat: np.ndarray = field(repr=False)
    path_offsets: np.ndarray = field(repr=False)
    compute_seconds: float = 0.0
    _game: Game | None = field(default=None, repr=False)
    _routes: dict = field(default_factory=dict, repr=False)

    @property
    def detour(self) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self.straight > 0, self.length / self.straight, 1.0)

    def index(self, item) -> int:
        n = _resolve(self.nodes, item)
        if not isinstance(n, Node):
            raise KeyError(item)
        return n.index

    def _idx(self, item) -> int:
        """Small ints are node indices; anything else (entity id, key, name, Node) is resolved."""
        if isinstance(item, (int, np.integer)) and 0 <= int(item) < len(self.nodes):
            return int(item)
        return self.index(item)

    def path_cells(self, i: int, j: int) -> np.ndarray:
        k = i * len(self.nodes) + j
        return self.path_flat[self.path_offsets[k]:self.path_offsets[k + 1]]

    def route(self, i, j, canonical: bool = True) -> Route | None:
        """Full Route for a node pair (indices, ids or keys), rebuilt from the stored path, with
        ``segments`` (existing / upgrade / new) and the shaped polyline.

        ``canonical`` (default): a road / track between two nodes is ONE physical way used in both
        directions, so for i > j the stored j -> i path is returned reversed (travel time and
        grades are evaluated in the i -> j direction). Without it the directed matrices may give
        slightly different cell paths out and back (uphill/downhill time asymmetry, ties), which
        drew loops / shuttles as two crossing lines. build/length then equal the [min, max] pair
        of the matrices. Routes are cached per (i, j)."""
        i, j = self._idx(i), self._idx(j)
        key = (i, j, bool(canonical))
        if key in self._routes:
            return self._routes[key]
        p = self.path_cells(i, j)
        if canonical and i > j:
            q = self.path_cells(j, i)
            if q.size:
                p = q[::-1]
        if p.size == 0 and i != j:
            return None
        if self._game is None:
            raise RuntimeError("NetworkMatrix has no game attached")
        pl = get_planner(self._game, self.kind, self.year, self.params)
        r = pl.make_route(p if p.size else self.cells[[i]])
        if len(self._routes) > 4096:
            self._routes.clear()
        self._routes[key] = r
        return r

    def symmetric(self, name: str = "build") -> np.ndarray:
        m = getattr(self, name)
        return np.minimum(m, m.T)

    def mst_pairs(self, name: str = "build", include_water_only: bool = False) -> list[tuple[int, int]]:
        idx = [n.index for n in self.nodes if include_water_only or not n.water_only]
        m = self.symmetric(name)[np.ix_(idx, idx)].copy()
        m[~np.isfinite(m)] = 0
        m = np.where(m > 0, m, 0) + 0.0
        # tiny positive weight for co-located nodes so MST keeps them connected
        off = ~np.eye(len(idx), dtype=bool) & (m == 0)
        m[off] = 1e-3
        tree = csgraph.minimum_spanning_tree(sparse.csr_matrix(m)).tocoo()
        return [(idx[a], idx[b]) for a, b in zip(tree.row.tolist(), tree.col.tolist())]


def _cache_path(kind: str, year: int, cell: float) -> Path:
    return DATA / f"routes_{kind}_{year}_{int(round(cell))}.npz"


def _signature(pl: RoutePlanner, nodes: list[Node], cells: np.ndarray) -> str:
    s = json.dumps({"v": _CACHE_VERSION, "params": pl.params.key(), "way": asdict(pl.way),
                    "bridge_full": asdict(pl.bridge) if pl.bridge else None,
                    "tunnel_full": asdict(pl.tunnel) if pl.tunnel else None,
                    "terrain_cost": pl.terrain_cost, "bridge_curve": [pl.bridge_threshold, pl.bridge_exponent],
                    "bridge": pl.bridge.key if pl.bridge else None,
                    "tunnel": pl.tunnel.key if pl.tunnel else None,
                    "terrain": float(np.asarray(pl.terrain.h, dtype=np.float64).sum()),
                    "existing": pl.existing_hash, "demolition": pl.demolition_hash,
                    "industries": getattr(pl, "industry_hash", None),
                    "nodes": [(n.key, int(c)) for n, c in zip(nodes, cells.tolist())]}, sort_keys=True)
    return hashlib.sha1(s.encode()).hexdigest()


def network_matrix(game: Game, kind: str, year: int | None = None, params: RoutingParams | None = None,
                   cache: bool = True, nodes: list[Node] | None = None, chunk: int = 8,
                   verbose: bool = False) -> NetworkMatrix:
    """All-pairs routes between towns and industries (Dijkstra from every node).

    Returns generalized/build/maintenance/length/time matrices [i, j] (i -> j) and stored paths.
    Cached to data/routes_<kind>_<year>_<cell>.npz (invalidated on any parameter/node change)."""
    params = params or RoutingParams()
    year = game.year if year is None else year
    pl = get_planner(game, kind, year, params)
    nodes = nodes or network_nodes(game)
    cells = np.array([pl.snap(n.x, n.y) for n in nodes], dtype=np.int64)
    sig = _signature(pl, nodes, cells)
    cpath = _cache_path(kind, year, params.cell)
    if cache and cpath.exists():
        try:
            z = np.load(cpath, allow_pickle=False)
            if str(z["signature"]) == sig:
                return NetworkMatrix(kind, year, pl.way.key, params, nodes, cells,
                                     **{f: z[f] for f in _MATRIX_FIELDS},
                                     path_flat=z["path_flat"], path_offsets=z["path_offsets"],
                                     compute_seconds=float(z["compute_seconds"]), _game=game)
        except Exception:  # corrupt/old cache -> recompute
            pass

    t0 = _time.perf_counter()
    n = len(nodes)
    mats = {f: np.full((n, n), np.inf) for f in _MATRIX_FIELDS}
    for f in _MATRIX_FIELDS:
        np.fill_diagonal(mats[f], 0.0)
    xs, ys = pl.cell_xy(cells)
    mats["straight"] = np.hypot(xs[:, None] - xs[None, :], ys[:, None] - ys[None, :])
    paths: list[np.ndarray] = [np.zeros(0, np.int64)] * (n * n)
    Y, vot = params.maintenance_years, params.value_of_time
    for c0 in range(0, n, chunk):
        src = list(range(c0, min(n, c0 + chunk)))
        dist, pred = csgraph.dijkstra(pl.graph, directed=True, indices=cells[src], return_predecessors=True)
        for r, i in enumerate(src):
            for j in range(n):
                if j == i:
                    continue
                if cells[j] == cells[i]:
                    for f in _MATRIX_FIELDS:
                        if f != "straight":
                            mats[f][i, j] = 0.0
                    paths[i * n + j] = np.array([cells[i]], np.int64)
                    continue
                if not np.isfinite(dist[r, cells[j]]):
                    continue
                p = np.array(_walk(pred[r], int(cells[i]), int(cells[j])), dtype=np.int64)
                paths[i * n + j] = p
                a = pl.edge_attrs(p)
                bsum, msum, tsum = a["build"].sum(), a["maint"].sum(), a["t_fwd"].sum()
                psum = a["dpen"].sum()
                mats["build"][i, j] = bsum
                mats["maint"][i, j] = msum
                mats["time"][i, j] = tsum
                mats["gen"][i, j] = bsum + Y * msum + vot * tsum + pl.rating_usd * psum
                mats["demolition_cost"][i, j] = a["dcost"].sum()
                mats["demolition_m"][i, j] = a["dm"].sum()
                mats["demolition_rating"][i, j] = psum
                mats["length"][i, j] = a["L"].sum()
                mats["water_bridge_m"][i, j] = a["lw"].sum()
                mats["viaduct_m"][i, j] = a["lv"].sum()
                mats["tunnel_m"][i, j] = a["lt"].sum()
                mats["earthworks_m3"][i, j] = a["ev"].sum()
                mats["existing_m"][i, j] = a["L"][a["exist"]].sum()
        if verbose:
            print(f"  {kind}: dijkstra {min(n, c0 + chunk)}/{n} ({_time.perf_counter() - t0:.1f} s)")
    lens = np.array([len(p) for p in paths], dtype=np.int64)
    offsets = np.concatenate([[0], np.cumsum(lens)])
    flat = np.concatenate(paths) if offsets[-1] else np.zeros(0, np.int64)
    secs = _time.perf_counter() - t0
    nm = NetworkMatrix(kind, year, pl.way.key, params, nodes, cells, **mats,
                       path_flat=flat.astype(np.int32), path_offsets=offsets, compute_seconds=secs, _game=game)
    if cache:
        try:
            np.savez_compressed(cpath, signature=np.array(sig), compute_seconds=np.array(secs),
                                path_flat=nm.path_flat, path_offsets=offsets, **mats)
        except OSError:
            pass
    return nm


# --------------------------------------------------------------------------- shared network

@dataclass
class NetworkPlan:
    routes: list[Route]
    total_build_cost: float
    total_maintenance_per_year: float
    total_length_m: float
    components: list[list[str]]          # terminal labels per connected component
    method: str

    def __iter__(self):  # allows: routes, total = build_network(...)
        return iter((self.routes, self.total_build_cost))


def _label(nd) -> str:
    return nd.name if isinstance(nd, Node) else f"({nd[0]:.0f},{nd[1]:.0f})"


def build_network(game: Game, kind: str, year: int | None, pairs: Iterable[tuple],
                  params: RoutingParams | None = None, method: str = "best") -> NetworkPlan:
    """Cheap shared network connecting every given pair (pair endpoints: Node, entity id,
    key/name or (x, y)).

    Pairs are grouped into connected components of terminals; each component is joined by
    * ``steiner`` (default): Takahashi-Matsuyama shortest-path heuristic - grow a tree from one
      terminal, repeatedly attach the terminal nearest to the tree (multi-source Dijkstra from
      all tree cells), so branches can start anywhere on existing track; or
    * ``mst``: minimum spanning tree over terminal-to-terminal costs, edges routed cheapest
      first;
    * ``best`` (default): run both and keep the plan with the lower build + maintenance cost.
    In all cases once a grid edge is built its build and maintenance cost become 0 for later
    segments (only travel time still counts). Returned routes are the built segments."""
    pairs = list(pairs)
    if method == "best":
        plans = [build_network(game, kind, year, pairs, params, m) for m in ("steiner", "mst")]
        Y = (params or RoutingParams()).maintenance_years
        return min(plans, key=lambda pl_: pl_.total_build_cost + Y * pl_.total_maintenance_per_year)
    pl = get_planner(game, kind, year, params)
    nodes = network_nodes(game)
    P = pl.params
    # resolve terminals
    term: dict[int, object] = {}
    adj: list[tuple[int, int]] = []
    for a, b in pairs:
        ra, rb = _resolve(nodes, a), _resolve(nodes, b)
        ca, cb = pl.snap(*_xy(ra)), pl.snap(*_xy(rb))
        term.setdefault(ca, ra)
        term.setdefault(cb, rb)
        adj.append((ca, cb))
    if not adj:
        return NetworkPlan([], 0.0, 0.0, 0.0, [], method)
    cl = sorted(term)
    pos = {c: k for k, c in enumerate(cl)}
    g = sparse.coo_matrix((np.ones(len(adj)), ([pos[a] for a, _ in adj], [pos[b] for _, b in adj])),
                          shape=(len(cl), len(cl)))
    ncomp, lab = csgraph.connected_components(g, directed=False)
    comps = [[cl[k] for k in range(len(cl)) if lab[k] == ci] for ci in range(ncomp)]

    weights = pl.graph.data.copy()
    built: set[tuple[int, int]] = set()
    graph = pl.graph.copy()
    routes: list[Route] = []

    # time-only weight for an already built edge
    def mark_built(path: list[int]):
        p = np.asarray(path, np.int64)
        if p.size < 2:
            return
        u, v = p[:-1], p[1:]
        a = pl.edge_attrs(p)
        pf = pl.csr_positions(u, v)
        pb = pl.csr_positions(v, u)
        weights[pf] = P.value_of_time * a["t_fwd"] + _EPS
        weights[pb] = P.value_of_time * a["t_bwd"] + _EPS
        for x, y in zip(u.tolist(), v.tolist()):
            built.add((min(x, y), max(x, y)))
        graph.data = weights

    def add_route(path: list[int]):
        p = np.asarray(path, np.int64)
        reused = np.array([(min(x, y), max(x, y)) in built for x, y in zip(p[:-1].tolist(), p[1:].tolist())],
                          dtype=bool) if p.size > 1 else None
        r = pl.make_route(p, reused)
        routes.append(r)
        mark_built(path)

    for comp in comps:
        if len(comp) < 2:
            continue
        if method == "steiner":
            tree = {comp[0]}
            remaining = set(comp[1:])
            while remaining:
                tl = sorted(tree)
                dist, pred, _ = csgraph.dijkstra(graph, directed=True, indices=tl, min_only=True,
                                                 return_predecessors=True)
                rem = sorted(remaining)
                k = int(np.argmin(dist[rem]))
                tgt = rem[k]
                if not np.isfinite(dist[tgt]):
                    raise RuntimeError(f"terminal {_label(term[tgt])} unreachable")
                path = _walk(pred, None, tgt)
                add_route(path)
                tree.update(path)
                remaining -= set(path)
        elif method == "mst":
            dist, _ = csgraph.dijkstra(graph, directed=True, indices=comp, return_predecessors=True)
            m = dist[:, comp]
            m = np.minimum(m, m.T)
            m = np.where(np.eye(len(comp), dtype=bool), 0, np.maximum(m, 1e-3))
            tree = csgraph.minimum_spanning_tree(sparse.csr_matrix(m)).tocoo()
            for k in np.argsort(tree.data):
                s, t = comp[tree.row[k]], comp[tree.col[k]]
                path, _ = pl.dijkstra_path(s, t, graph)
                if path is None:
                    raise RuntimeError("unreachable terminal")
                add_route(path)
        else:
            raise ValueError(method)

    return NetworkPlan(routes, sum(r.build_cost for r in routes), sum(r.maintenance_per_year for r in routes),
                       sum(r.length_m - r.reused_m for r in routes),
                       [[_label(term[c]) for c in comp] for comp in comps], method)


def line_routes(nm: NetworkMatrix, legs: Sequence[tuple]) -> list[Route | None]:
    """Routes of the consecutive legs of ONE line (loop A -> B -> C -> A, shuttle A -> B -> A,
    multi-stop), built as one shared way: the first leg is ``nm.route`` (canonical), every later
    leg is routed with the grid edges of the earlier legs already built (build/maintenance 0,
    only travel time counts; they appear as "shared" segments). So a loop whose legs run along
    the same corridor uses ONE new road instead of two parallel ones 40 m apart, and the return
    leg of a shuttle follows the outbound way. Build costs of later legs are therefore lower than
    in the (independent, all-pairs) matrices. legs: (i, j) node indices / ids / keys."""
    if nm._game is None:
        raise RuntimeError("NetworkMatrix has no game attached")
    pl = get_planner(nm._game, nm.kind, nm.year, nm.params)
    P = pl.params
    weights = pl.graph.data.copy()
    graph = pl.graph.copy()
    built: set[tuple[int, int]] = set()
    out: list[Route | None] = []
    for a, b in legs:
        i, j = nm._idx(a), nm._idx(b)
        if not built:
            r = nm.route(i, j)
            path = None if r is None else r.cells
        else:
            path, _ = pl.dijkstra_path(int(nm.cells[i]), int(nm.cells[j]), graph)
            r = None
            if path is not None:
                p = np.asarray(path, np.int64)
                reused = np.array([(min(x, y), max(x, y)) in built for x, y in zip(p[:-1].tolist(), p[1:].tolist())],
                                  dtype=bool) if p.size > 1 else None
                r = pl.make_route(p, reused)
        out.append(r)
        if path is None or len(path) < 2:
            continue
        p = np.asarray(path, np.int64)
        u, v = p[:-1], p[1:]
        at = pl.edge_attrs(p)
        weights[pl.csr_positions(u, v)] = P.value_of_time * at["t_fwd"] + _EPS
        weights[pl.csr_positions(v, u)] = P.value_of_time * at["t_bwd"] + _EPS
        graph.data = weights
        built.update((min(x, y), max(x, y)) for x, y in zip(u.tolist(), v.tolist()))
    return out


def land_components(game: Game, kind: str = "rail", year: int | None = None,
                    params: RoutingParams | None = None, nodes: list[Node] | None = None) -> dict[int, list[Node]]:
    """Groups of nodes mutually reachable WITHOUT bridges (i.e. separated by water otherwise)."""
    params = replace(params or RoutingParams(), allow_bridges=False)
    pl = get_planner(game, kind, year, params)
    nodes = nodes or network_nodes(game)
    _, lab = csgraph.connected_components(pl.graph, directed=True, connection="weak")
    out: dict[int, list[Node]] = {}
    for nd in nodes:
        out.setdefault(int(lab[pl.snap(nd.x, nd.y)]), []).append(nd)
    return out


# --------------------------------------------------------------------------- water

@dataclass
class WaterBody:
    label: int
    cells: int
    area_km2: float
    bbox: tuple[float, float, float, float]     # xmin, ymin, xmax, ymax
    max_depth: float
    mean_depth: float
    touches_map_edge: bool
    nodes: list[str] = field(default_factory=list)


@dataclass
class WaterNetwork:
    bodies: list[WaterBody]
    nodes: list[Node]                            # all network nodes (same order as land matrices)
    ports: dict[int, list[tuple[int, int, float]]]  # node index -> [(body label, fine cell, distance to water m)]
    length: np.ndarray                           # ship path length, inf if no common water body
    time: np.ndarray
    body: np.ndarray                             # body label used for the pair, -1 if none
    ship_speed: float
    _graph: sparse.csr_matrix = field(repr=False, default=None)
    _terrain: Terrain = field(repr=False, default=None)

    def route(self, i: int, j: int) -> Route | None:
        """Ship route between node indices (build cost 0)."""
        if i == j or not np.isfinite(self.length[i, j]):
            return None
        si = [c for _, c, _ in self.ports[i]]
        tj = {c for _, c, _ in self.ports[j]}
        dist, pred, _ = csgraph.dijkstra(self._graph, directed=False, indices=si, min_only=True,
                                         return_predecessors=True)
        t = min(tj, key=lambda c: dist[c])
        p = np.array(_walk(pred, None, t), np.int64)
        tr = self._terrain
        iy, ix = np.divmod(p, tr.nx)
        xs, ys = tr.origin_x + ix * tr.step, tr.origin_y + iy * tr.step
        poly = list(zip(xs.tolist(), ys.tolist()))
        seg = np.hypot(np.diff(xs), np.diff(ys))
        L = float(seg.sum())
        straight = float(math.hypot(xs[-1] - xs[0], ys[-1] - ys[0]))
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        tt = L / self.ship_speed
        return Route("ship", "water", poly, L, 0.0, 0.0, tt, tt, 0.0, 0.0, 0.0, [], [], 0.0, 0.0, 0.0, 0.0,
                     list(zip(cum.tolist(), [tr.water_level] * len(cum))), straight,
                     L / straight if straight > 0 else 1.0, p)


def _water_graph(water: np.ndarray, step: float) -> sparse.csr_matrix:
    ny, nx = water.shape
    N = nx * ny
    rows, cols, data = [], [], []
    for dy, dx in [(0, 1), (1, 0), (1, 1), (1, -1)]:
        iy1, ix0, ix1 = ny - dy, max(0, -dx), nx - max(0, dx)
        A = water[0:iy1, ix0:ix1]
        B = water[dy:dy + iy1, ix0 + dx:ix1 + dx]
        IY, IX = np.nonzero(A & B)
        IX = IX + ix0
        a = IY * nx + IX
        rows.append(a)
        cols.append(a + dy * nx + dx)
        data.append(np.full(a.size, step * math.hypot(dy, dx)))
    return sparse.csr_matrix((np.concatenate(data), (np.concatenate(rows), np.concatenate(cols))), shape=(N, N))


def water_network(game: Game, year: int | None = None, port_radius: float = 600.0,
                  min_body_area_km2: float = 0.05, ship_speed: float | None = None,
                  connectivity: int = 8, nodes: list[Node] | None = None) -> WaterNetwork:
    """Ship routes on lakes/rivers between water-adjacent nodes.

    Water = fine heightmap cells below water level, ``connectivity`` 8 (diagonal contacts
    connect, as for ship movement) or 4. Bodies smaller than ``min_body_area_km2`` get no ports.
    A node has a port on each body whose nearest water cell is within ``port_radius``
    (water industries always get their own cell). length[i, j] = shortest water path between
    any port cells of i and j (inf if they share no water body)."""
    tr = game.terrain
    year = game.year if year is None else year
    nodes = nodes or network_nodes(game)
    speed = ship_speed or _ship_speed(game, year)
    wl = tr.water_level
    W = tr.h < wl
    struct = np.ones((3, 3), bool) if connectivity == 8 else ndimage.generate_binary_structure(2, 1)
    labels, nlab = ndimage.label(W, structure=struct)
    step = tr.step
    bodies: dict[int, WaterBody] = {}
    if nlab:
        idx = np.arange(1, nlab + 1)
        counts = ndimage.sum_labels(np.ones_like(tr.h), labels, idx)
        depth = np.where(W, wl - tr.h, 0)
        maxd = ndimage.maximum(depth, labels, idx)
        meand = ndimage.mean(depth, labels, idx)
        slices = ndimage.find_objects(labels)
        for k, lb in enumerate(idx.tolist()):
            sl = slices[k]
            area = counts[k] * step * step / 1e6
            if area < min_body_area_km2:
                continue
            y0, y1, x0, x1 = sl[0].start, sl[0].stop - 1, sl[1].start, sl[1].stop - 1
            edge = y0 == 0 or x0 == 0 or y1 == tr.ny - 1 or x1 == tr.nx - 1
            bodies[lb] = WaterBody(lb, int(counts[k]), float(area),
                                   (tr.origin_x + x0 * step, tr.origin_y + y0 * step,
                                    tr.origin_x + x1 * step, tr.origin_y + y1 * step),
                                   float(maxd[k]), float(meand[k]), bool(edge))

    R = int(math.ceil(port_radius / step))
    ports: dict[int, list[tuple[int, int, float]]] = {}
    for nd in nodes:
        fx, fy = (nd.x - tr.origin_x) / step, (nd.y - tr.origin_y) / step
        cx, cy = int(np.clip(round(fx), 0, tr.nx - 1)), int(np.clip(round(fy), 0, tr.ny - 1))
        rad = R if not nd.water_only else max(R, 8)
        ya, yb, xa, xb = max(0, cy - rad), min(tr.ny, cy + rad + 1), max(0, cx - rad), min(tr.nx, cx + rad + 1)
        win = labels[ya:yb, xa:xb]
        WY, WX = np.nonzero(win > 0)
        if WY.size == 0:
            continue
        d = np.hypot(WY + ya - fy, WX + xa - fx) * step
        best: dict[int, tuple[float, int]] = {}
        for k in np.argsort(d).tolist():
            if d[k] > port_radius and not nd.water_only:
                break
            lb = int(win[WY[k], WX[k]])
            if lb in bodies and lb not in best:
                best[lb] = (float(d[k]), int((WY[k] + ya) * tr.nx + WX[k] + xa))
                if nd.water_only:
                    break   # platforms/fishing grounds: only the body they sit in
        if best:
            ports[nd.index] = [(lb, c, dd) for lb, (dd, c) in sorted(best.items(), key=lambda kv: kv[1][0])]
            for lb in best:
                bodies[lb].nodes.append(nd.name)

    graph = _water_graph(W, step) if connectivity == 8 else _water_graph_4(W, step)
    n = len(nodes)
    length = np.full((n, n), np.inf)
    body = np.full((n, n), -1, dtype=np.int64)
    np.fill_diagonal(length, 0.0)
    pidx = sorted(ports)
    for i in pidx:
        src = [c for _, c, _ in ports[i]]
        dist = csgraph.dijkstra(graph, directed=False, indices=src, min_only=True)
        for j in pidx:
            if j == i:
                continue
            best = (np.inf, -1)
            for lb, c, _ in ports[j]:
                if dist[c] < best[0]:
                    best = (float(dist[c]), lb)
            length[i, j], body[i, j] = best
    return WaterNetwork(sorted(bodies.values(), key=lambda b: -b.area_km2), nodes, ports, length,
                        length / speed, body, speed, graph, tr)


def _water_graph_4(water: np.ndarray, step: float) -> sparse.csr_matrix:
    ny, nx = water.shape
    rows, cols = [], []
    for dy, dx in [(0, 1), (1, 0)]:
        A = water[0:ny - dy, 0:nx - dx]
        B = water[dy:, dx:]
        IY, IX = np.nonzero(A & B)
        a = IY * nx + IX
        rows.append(a)
        cols.append(a + dy * nx + dx)
    r = np.concatenate(rows)
    return sparse.csr_matrix((np.full(r.size, step), (r, np.concatenate(cols))), shape=(nx * ny, nx * ny))
