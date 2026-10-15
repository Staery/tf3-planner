"""Built-up area of towns and the price of demolishing town buildings (routing / siting).

Research and sources: docs/demolition.md. Short version (TF3 build 40408 game files):

* Money: the export v7 probes the game's own removal proposals (``townBuildings.bulldozeSamples``,
  48 buildings of every land use / level / parcel count, 1900, VeryHard): the price is
  ``~25 300 $ x building person capacity`` (std 1.2 %), i.e. 25 k - 600 k per building - NOT the
  script ``bulldozeCost = 100`` of townbuildingutil.lua (that x1.5 is only a small part). Fitted here
  as cost = a x capacity + b (``cost_model``); without probes DEFAULT_USD_PER_CAPACITY.
  ``RoutingParams.demolition_usd_per_m2`` overrides it (price per m2 of building plot).
* Reputation ("urban care"): every removed building adds ``capacity / district capacity`` of its land
  use to ``fractionDistrictRemoved``; the town reputation penalty is ``fractionDistrictRemoved / 0.405``
  (game_mechanics/towns/town_util.tl calculateProposalImpact). Removing a uniform x % of a whole town
  (all three districts) therefore costs ~7.4 x % reputation. Recovery 1/6 per 3 game months
  (towns.script.tl). The router prices it with ``RoutingParams.demolition_rating_usd`` per 1.0 (100 %).

Data sources of the mask, best first:
1. export v7 ``townBuildings`` (buildings.py): real buildings. Plot = (parcels x 8 m + 4) x (depth x 8 m + 4)
   (8 m parcels + 2 m border, townbuildingutil.lua), centre = bbox centre, treated as a disc of equal
   area (the exported AABB of a rotated plot is ~2x too large).
2. export v6 street network: town-owned streets; ground covered by plots = street length density x
   STREET_BAND_M (two rows of plots), capped at COVER_MAX, only within BUILT_BUFFER_M of a street.
3. town discs: radius 10 m x sqrt(total capacity) + TOWN_MARGIN_M, cover COVER_MAX x (1 - (d/R)^2)
   (denser centre).
2./3. use synthetic buildings of FALLBACK_PLOT_M2 and the per-town penalty of a uniform mix of the
three land uses (n_landuses / (0.405 x built plot area of the town) per m2).

Corridor model. A way of width w along a polyline demolishes every building it touches, i.e. a building
of plot size s whose centre lies within (w + s) / 2 of the line. With the per-building density
c_i(x) = 1 / area_i on its plot, the expected sum of a per-building quantity q per metre of way is
``w * Q + QS`` with the rasters ``Q = sum c_i q_i`` and ``QS = sum c_i q_i s_i`` (s_i = plot diameter).
Rasters (``RASTER_M`` cells) exist for q = 1 (count), plot area, money and reputation penalty.
``corridor()`` counts real buildings exactly when the export has them.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

__all__ = ["TownMask", "TownRef", "for_game", "from_points", "from_discs", "from_streets", "from_buildings", "IMPACT_PER_FRACTION", "FALLBACK_PLOT_M2", "PARCEL_M",
           "plot_area"]

PARCEL_M = 8.0                     # townbuildingutil.lua parcelSize
PLOT_BORDER_M = 2.0                # townbuildingutil.lua borderSize (each side)
IMPACT_PER_FRACTION = 1.0 / 0.405  # town_util.tl: reputation penalty per fractionDistrictRemoved
RASTER_M = 32.0
FALLBACK_PLOT_M2 = 600.0           # synthetic building plot (median real plot ~1000 m2; small houses at the edge)
COVER_MAX = 0.6                    # max share of ground covered by plots (streets, gaps)
STREET_BAND_M = 30.0               # plots per metre of street: 2 rows x ~20 m deep x 0.75 filled
BUILT_BUFFER_M = 45.0              # street mask: plots stand within this distance of a town street
TOWN_MARGIN_M = 60.0               # disc mask: houses stand a bit beyond the radius
TOWN_RADIUS_K = 10.0               # R = k * sqrt(capacity) (same as passengers.town_infos)
BUILT_COVER = 0.10                 # ground with >= 10 % plots counts as built-up (demolition_m, siting)
DEFAULT_USD_PER_CAPACITY = 25300.0  # export v7 probes 1900 (VeryHard): 25 056 .. 26 895 $ per capacity unit
_Q = ("n", "area", "cost", "pen")  # per-building quantities with rasters


def plot_area(parcels, depth) -> np.ndarray:
    p = np.maximum(np.asarray(parcels, np.float64), 1)
    d = np.maximum(np.asarray(depth, np.float64), 1)
    return (p * PARCEL_M + 2 * PLOT_BORDER_M) * (d * PARCEL_M + 2 * PLOT_BORDER_M)


@dataclass
class TownRef:
    index: int
    id: int
    name: str
    x: float
    y: float
    radius: float
    caps: tuple[float, float, float]     # residential, commercial, industrial capacity (district sizes)


@dataclass
class TownMask:
    source: str                          # "buildings (export v7)" | "town streets (export v6)" | "town discs"
    x0: float
    y0: float
    step: float
    nx: int
    ny: int
    R: dict                              # q -> (Q, QS) float32 rasters (ny, nx), q in _Q
    town: np.ndarray                     # (ny, nx) int32 town index, -1 = none
    towns: list[TownRef]
    # exact buildings (export v7): centres (M, 2), equal-area disc radius, plot area, cost, penalty, town index
    bxy: np.ndarray | None = None
    br: np.ndarray | None = None
    barea: np.ndarray | None = None
    bcost: np.ndarray | None = None
    bpen: np.ndarray | None = None
    btown: np.ndarray | None = None
    cost_source: str = "default"         # how money per building was derived
    usd_per_capacity: float = DEFAULT_USD_PER_CAPACITY
    _tree: cKDTree | None = field(default=None, repr=False)
    _hash: str | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ basics
    @property
    def exact(self) -> bool:
        return self.bxy is not None and len(self.bxy) > 0

    def hash(self) -> str:
        if self._hash is None:
            h = hashlib.sha1(self.source.encode())
            for q in _Q:
                for a in self.R[q]:
                    h.update(np.round(a.astype(np.float64), 9).tobytes())
            h.update(self.town.tobytes())
            if self.exact:
                for a in (self.bxy, self.br, self.bcost, self.bpen, self.btown):
                    h.update(np.round(np.asarray(a, np.float64), 6).tobytes())
            self._hash = h.hexdigest()[:16]
        return self._hash

    def summary(self) -> dict:
        cover = self.R["area"][0]
        out = {"source": self.source, "cost_source": self.cost_source, "raster_m": self.step,
               "built_km2": round(float((cover >= BUILT_COVER).sum()) * self.step ** 2 / 1e6, 2),
               "towns": len(self.towns)}
        if self.exact:
            out.update(buildings=int(len(self.bxy)), median_cost=round(float(np.median(self.bcost)), 1),
                       median_penalty=round(float(np.median(self.bpen)), 5))
        return out

    def _fidx(self, xs, ys):
        return (np.asarray(ys, np.float64) - self.y0) / self.step, (np.asarray(xs, np.float64) - self.x0) / self.step

    def sample(self, xs, ys, q: str) -> tuple[np.ndarray, np.ndarray]:
        """Bilinear (Q, QS) of quantity q at points."""
        fy, fx = self._fidx(xs, ys)
        Q, QS = self.R[q]
        return (ndimage.map_coordinates(Q, [fy, fx], order=1, mode="constant", cval=0.0),
                ndimage.map_coordinates(QS, [fy, fx], order=1, mode="constant", cval=0.0))

    def per_m(self, xs, ys, width: float, q: str) -> np.ndarray:
        """Expected sum of q over buildings demolished per metre of a way of width w at the points."""
        Q, QS = self.sample(xs, ys, q)
        return width * Q + QS

    def cover(self, xs, ys) -> np.ndarray:
        """Share of the ground covered by building plots (0..1)."""
        return np.minimum(self.sample(xs, ys, "area")[0], 1.0)

    def town_at(self, xs, ys) -> np.ndarray:
        fy, fx = self._fidx(xs, ys)
        iy = np.clip(np.rint(fy), 0, self.ny - 1).astype(np.int64)
        ix = np.clip(np.rint(fx), 0, self.nx - 1).astype(np.int64)
        return self.town[iy, ix]

    def built(self, x: float, y: float) -> bool:
        return bool(self.cover([x], [y])[0] >= BUILT_COVER)

    def built_mask_grid(self, x0: float, y0: float, cell: float, nx: int, ny: int, dilate: int = 2) -> np.ndarray:
        """(ny, nx) bool: routing grid nodes near any building (edges touching them need sampling)."""
        iy, ix = np.mgrid[0:ny, 0:nx]
        xs, ys = x0 + ix * cell, y0 + iy * cell
        fy, fx = self._fidx(xs.ravel(), ys.ravel())
        any_ = (self.R["n"][0] > 0) | (self.R["n"][1] > 0)
        # max-filter at raster resolution so thin features are not missed between grid nodes
        k = max(1, int(math.ceil(cell / self.step)))
        any_ = ndimage.maximum_filter(any_.astype(np.uint8), size=2 * k + 1) > 0
        m = ndimage.map_coordinates(any_.astype(np.float32), [fy, fx], order=0, mode="constant", cval=0.0) > 0
        m = m.reshape(ny, nx)
        return ndimage.binary_dilation(m, iterations=dilate) if dilate else m

    # ------------------------------------------------------------------ corridor / footprint
    def corridor(self, polyline, width: float, usd_per_m2: float | None = None, step: float = 4.0) -> dict:
        """Buildings demolished by a way of ``width`` along ``polyline``:
        {count, area_m2, cost, penalty, towns: {town index: penalty}, length_m (way length running
        through demolished plots), exact: bool, idx: building indices (exact only)}."""
        P = np.asarray([(float(p[0]), float(p[1])) for p in polyline], np.float64).reshape(-1, 2)
        empty = {"count": 0.0, "area_m2": 0.0, "cost": 0.0, "penalty": 0.0, "towns": {}, "length_m": 0.0,
                 "exact": self.exact, "idx": np.zeros(0, np.int64)}
        if len(P) < 2:
            return empty
        D = _densify(P, step)
        seg = np.hypot(*np.diff(D, axis=0).T)
        if self.exact:
            if self._tree is None:
                self._tree = cKDTree(self.bxy)
            reach = width / 2.0 + self.br
            rmax = float(reach.max()) if len(reach) else 0.0
            cand = set()
            for lst in self._tree.query_ball_point(D, rmax + step):
                cand.update(lst)
            if not cand:
                return empty
            cand = np.fromiter(sorted(cand), np.int64)
            from .routing import _pt_poly_dist
            d = _pt_poly_dist(self.bxy[cand], D)
            hit = cand[d <= reach[cand]]
            if len(hit) == 0:
                return empty
            # length of the way inside the reach of a demolished building
            tr = cKDTree(self.bxy[hit])
            mid = (D[1:] + D[:-1]) / 2
            dd, kk = tr.query(mid)
            inside = dd <= (width / 2.0 + self.br[hit][kk])
            cost = (self.barea[hit] * usd_per_m2) if usd_per_m2 is not None else self.bcost[hit]
            towns: dict[int, float] = {}
            for t, p in zip(self.btown[hit].tolist(), self.bpen[hit].tolist()):
                towns[t] = towns.get(t, 0.0) + p
            return {"count": float(len(hit)), "area_m2": float(self.barea[hit].sum()), "cost": float(cost.sum()),
                    "penalty": float(self.bpen[hit].sum()), "towns": towns,
                    "length_m": float(seg[inside].sum()), "exact": True, "idx": hit}
        mid = (D[1:] + D[:-1]) / 2
        xs, ys = mid[:, 0], mid[:, 1]
        n = self.per_m(xs, ys, width, "n") * seg
        if n.sum() <= 1e-9:
            return empty
        area = self.per_m(xs, ys, width, "area") * seg
        cost = area * usd_per_m2 if usd_per_m2 is not None else self.per_m(xs, ys, width, "cost") * seg
        pen = self.per_m(xs, ys, width, "pen") * seg
        tw = self.town_at(xs, ys)
        towns = {}
        for t in np.unique(tw[(pen > 0) & (tw >= 0)]).tolist():
            towns[int(t)] = float(pen[tw == t].sum())
        built = self.cover(xs, ys) >= BUILT_COVER
        return {"count": float(n.sum()), "area_m2": float(area.sum()), "cost": float(cost.sum()),
                "penalty": float(pen.sum()), "towns": towns, "length_m": float(seg[built].sum()),
                "exact": False, "idx": np.zeros(0, np.int64)}

    def footprint(self, x: float, y: float, radius: float, usd_per_m2: float | None = None) -> dict:
        """Buildings demolished by a construction covering a disc of ``radius`` at (x, y) (same keys as
        corridor, length_m = 0)."""
        if self.exact:
            if self._tree is None:
                self._tree = cKDTree(self.bxy)
            cand = np.asarray(self._tree.query_ball_point([x, y], radius + float(self.br.max())), np.int64)
            if len(cand):
                d = np.hypot(self.bxy[cand, 0] - x, self.bxy[cand, 1] - y)
                hit = cand[d <= radius + self.br[cand]]
            else:
                hit = cand
            cost = (self.barea[hit] * usd_per_m2) if usd_per_m2 is not None else self.bcost[hit]
            towns: dict[int, float] = {}
            for t, p in zip(self.btown[hit].tolist(), self.bpen[hit].tolist()):
                towns[t] = towns.get(t, 0.0) + p
            return {"count": float(len(hit)), "area_m2": float(self.barea[hit].sum()), "cost": float(cost.sum()),
                    "penalty": float(self.bpen[hit].sum()), "towns": towns, "length_m": 0.0, "exact": True,
                    "idx": hit}
        # expected: a disc of radius r touches plots whose centre is within r + s/2:
        # sum_i c_i q_i pi (r + s_i/2)^2 ~ pi r^2 Q + pi r QS + pi/4 QS s_mean  (s_mean = QS/Q)
        rr = np.linspace(0, radius, 4)
        ang = np.linspace(0, 2 * math.pi, 9)[:-1]
        xs = np.concatenate([[x], (x + rr[1:, None] * np.cos(ang)).ravel()])
        ys = np.concatenate([[y], (y + rr[1:, None] * np.sin(ang)).ravel()])
        out = {}
        for q in _Q:
            Q, QS = (float(a.mean()) for a in self.sample(xs, ys, q))
            sm = QS / Q if Q > 1e-12 else 0.0
            out[q] = math.pi * radius ** 2 * Q + math.pi * radius * QS + 0.25 * math.pi * QS * sm
        cost = out["area"] * usd_per_m2 if usd_per_m2 is not None else out["cost"]
        t = int(self.town_at([x], [y])[0])
        return {"count": out["n"], "area_m2": out["area"], "cost": cost, "penalty": out["pen"],
                "towns": ({t: out["pen"]} if t >= 0 and out["pen"] > 0 else {}), "length_m": 0.0,
                "exact": False, "idx": np.zeros(0, np.int64)}

    def grid_pairs(self, x0: float, y0: float, cell: float, nx: int, ny: int, canon, width: float):
        """Exact (export v7) building hits of every grid edge: arrays (d, anchor, building, share, overlap_m).

        Edge d at anchor a runs from node a to a + canon[d] (dy, dx). A building is hit when the edge
        passes within its reach rho = width / 2 + plot radius; ``share`` = length of the edge inside the
        reach disc / full chord of the edge's line through that disc, so a straight way through (or
        tangentially past) a building sums to exactly 1 over consecutive edges; ``overlap_m`` = that
        length inside the disc."""
        if not self.exact:
            z = np.zeros(0, np.int64)
            return z, z, z, np.zeros(0), np.zeros(0)
        P = self.bxy
        rho = width / 2.0 + self.br
        lmax = cell * max(math.hypot(dy, dx) for dy, dx in canon)
        K = int(math.ceil((float(rho.max()) + lmax) / cell))
        off = np.arange(-K, K + 1)
        oy, ox = np.meshgrid(off, off, indexing="ij")
        oy, ox = oy.ravel(), ox.ravel()
        cy = np.rint((P[:, 1] - y0) / cell).astype(np.int64)
        cx = np.rint((P[:, 0] - x0) / cell).astype(np.int64)
        IY = cy[:, None] + oy[None, :]
        IX = cx[:, None] + ox[None, :]
        B = np.broadcast_to(np.arange(len(P))[:, None], IY.shape)
        out = ([], [], [], [], [])
        for d, (dy, dx) in enumerate(canon):
            ok = (IY >= 0) & (IY + dy < ny) & (IX >= 0) & (IX < nx) & (IX + dx >= 0) & (IX + dx < nx)
            iy, ix, b = IY[ok], IX[ok], B[ok]
            ax, ay = x0 + ix * cell, y0 + iy * cell
            L = cell * math.hypot(dy, dx)
            ux, uy = dx * cell / L, dy * cell / L
            px, py = P[b, 0] - ax, P[b, 1] - ay
            t = px * ux + py * uy
            h = np.abs(px * uy - py * ux)
            r = rho[b]
            hit = h < r
            half = np.sqrt(np.maximum(r * r - h * h, 0.0))
            ov = np.clip(np.minimum(t + half, L) - np.maximum(t - half, 0.0), 0.0, None)
            hit &= ov > 1e-6
            out[0].append(np.full(int(hit.sum()), d, np.int64))
            out[1].append((iy * nx + ix)[hit])
            out[2].append(b[hit].astype(np.int64))
            out[3].append(ov[hit] / np.maximum(2 * half[hit], 1e-9))
            out[4].append(ov[hit])
        return tuple(np.concatenate(o) for o in out)

    def grid_expected(self, x0: float, y0: float, cell: float, nx: int, ny: int, canon, width: float,
                      samples: int = 5) -> dict:
        """Expected per-edge totals from the rasters (no exact buildings): {n, area, cost, pen, m} arrays
        (len(canon), nx * ny); m = edge length on ground with >= BUILT_COVER plots."""
        N = nx * ny
        res = {k: np.zeros((len(canon), N)) for k in ("n", "area", "cost", "pen", "m")}
        iy, ix = np.divmod(np.arange(N), nx)
        ts = (np.arange(samples) + 0.5) / samples
        for d, (dy, dx) in enumerate(canon):
            ok = (iy + dy < ny) & (ix + dx >= 0) & (ix + dx < nx)
            a = np.nonzero(ok)[0]
            L = cell * math.hypot(dy, dx)
            xs = (x0 + (ix[a][None, :] + ts[:, None] * dx) * cell).ravel()
            ys = (y0 + (iy[a][None, :] + ts[:, None] * dy) * cell).ravel()
            for q in ("n", "area", "cost", "pen"):
                res[q][d, a] = self.per_m(xs, ys, width, q).reshape(samples, -1).mean(0) * L
            res["m"][d, a] = (self.cover(xs, ys) >= BUILT_COVER).reshape(samples, -1).mean(0) * L
        return res

    def town_name(self, k: int) -> str:
        return self.towns[k].name if 0 <= k < len(self.towns) else "?"


# --------------------------------------------------------------------------- construction

def _densify(P: np.ndarray, step: float) -> np.ndarray:
    d = np.hypot(*np.diff(P, axis=0).T)
    n = np.maximum(1, np.ceil(d / step).astype(np.int64))
    seg = np.repeat(np.arange(len(d)), n)
    start = np.concatenate([[0], np.cumsum(n)[:-1]])
    t = (np.arange(int(n.sum())) - start[seg] + 1) / n[seg]
    return np.concatenate([P[:1], P[seg] + (P[seg + 1] - P[seg]) * t[:, None]])


def _town_refs(game) -> list[TownRef]:
    cur = {}
    for raw in (getattr(game, "raw", None) or {}).get("towns", []) or []:
        cu = raw.get("capacityUsage")
        if isinstance(cu, list) and len(cu) >= 3 and all(isinstance(c, dict) for c in cu[:3]):
            cur[raw["id"]] = tuple(float(c.get("capacity", 0) or 0) for c in cu[:3])
    stats = (((getattr(game, "raw", None) or {}).get("towns_stats") or {}).get("town2personCapacities") or {})
    out = []
    for k, t in enumerate(game.towns):
        caps = stats.get(str(t.id)) or stats.get(t.id)
        if isinstance(caps, list) and len(caps) >= 3:
            caps = tuple(float(c) for c in caps[:3])
        else:
            caps = cur.get(t.id) or (float(t.capacities.get("residential", 0)), float(t.capacities.get("commercial", 0)),
                                     float(t.capacities.get("industrial", 0)))
        out.append(TownRef(k, t.id, t.name, float(t.x), float(t.y),
                           TOWN_RADIUS_K * math.sqrt(max(sum(caps), 1.0)), caps))
    return out


def _empty(terrain, step: float):
    W = (terrain.nx - 1) * terrain.step
    H = (terrain.ny - 1) * terrain.step
    nx, ny = int(W // step) + 1, int(H // step) + 1
    return float(terrain.origin_x), float(terrain.origin_y), nx, ny


def _splat(x0, y0, step, nx, ny, cx, cy, side, weights: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Spread every square (centre cx, cy, side) over raster cells by area overlap; weights[q] is the
    per-building value already divided by its area (density); returns rasters."""
    out = {q: np.zeros((ny, nx), np.float64) for q in weights}
    gx = (cx - x0) / step + 0.5          # cell k spans [k - 0.5, k + 0.5] around node k -> shift
    gy = (cy - y0) / step + 0.5
    hs = side / 2 / step
    lo_x, hi_x, lo_y, hi_y = gx - hs, gx + hs, gy - hs, gy + hs
    span = int(np.ceil(np.max(2 * hs))) + 1 if len(cx) else 0
    bx, by = np.floor(lo_x).astype(np.int64), np.floor(lo_y).astype(np.int64)
    cell_area = step * step
    for oy in range(span + 1):
        iy = by + oy
        ovy = np.clip(np.minimum(hi_y, iy + 1) - np.maximum(lo_y, iy), 0, None)
        for ox in range(span + 1):
            ix = bx + ox
            ovx = np.clip(np.minimum(hi_x, ix + 1) - np.maximum(lo_x, ix), 0, None)
            a = ovx * ovy * cell_area                       # overlap area, m2
            ok = (a > 0) & (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
            if not ok.any():
                continue
            for q, w in weights.items():
                np.add.at(out[q], (iy[ok], ix[ok]), w[ok] * a[ok] / cell_area)
    return out


def cost_model(tb) -> tuple[float, float, bool]:
    """(a, b, from_probes): removal price of a town building = a x person capacity + b, least squares over
    the in-game probes of export v7 (b clipped >= 0); DEFAULT_USD_PER_CAPACITY without probes."""
    pts = [(float(s["capacity"]), float(s["costs"])) for s in (tb.bulldoze_samples if tb is not None else [])
           if isinstance(s.get("costs"), (int, float)) and s["costs"] > 0 and not s.get("error")
           and isinstance(s.get("capacity"), (int, float)) and s["capacity"] > 0]
    if len(pts) >= 3 and len({c for c, _ in pts}) >= 2:
        C, Y = np.array(pts).T
        a, b = np.polyfit(C, Y, 1)
        if b < 0:
            a, b = float((C * Y).sum() / (C * C).sum()), 0.0
        return float(a), float(b), True
    if pts:
        return float(np.mean([y / c for c, y in pts])), 0.0, True
    return DEFAULT_USD_PER_CAPACITY, 0.0, False


def from_buildings(game, tb, step: float = RASTER_M) -> TownMask:
    towns = _town_refs(game)
    tidx = {t.id: t.index for t in towns}
    cxy = (tb.bbox[:, :2] + tb.bbox[:, 2:]) / 2
    area = plot_area(tb.parcels, tb.depth)
    a, b, probes = cost_model(tb)
    cost = a * np.maximum(np.asarray(tb.capacity, np.float64), 1.0) + b
    btown = np.array([tidx.get(int(t), -1) for t in tb.town.tolist()], np.int64)
    lu = np.asarray(tb.land_use, np.int64)
    cap = np.maximum(np.asarray(tb.capacity, np.float64), 0.0)
    # district sizes: the game's getLandUsePersonCapacities (towns_stats), else the sum of the buildings
    dist = np.zeros((len(towns), 3))
    for k, t in enumerate(towns):
        dist[k] = t.caps
    sums = np.zeros((len(towns), 3))
    ok = (btown >= 0) & (lu >= 0) & (lu <= 2)
    np.add.at(sums, (btown[ok], lu[ok]), cap[ok])
    dist = np.where(dist > 0, dist, sums)
    pen = np.zeros(len(cap))
    pen[ok] = IMPACT_PER_FRACTION * cap[ok] / np.maximum(dist[btown[ok], lu[ok]], 1.0)
    tm = from_points(game.terrain, cxy, area, cost, pen, btown, towns, "buildings (export v7)", step,
                     cost_source=f"probes: {a:,.0f} $ x capacity + {b:,.0f}" if probes else
                     f"default {a:,.0f} $ x capacity")
    tm.usd_per_capacity = a
    return tm


def from_points(terrain, cxy, area, cost, pen, btown, towns: list[TownRef], source: str = "buildings",
                step: float = RASTER_M, cost_source: str = "given") -> TownMask:
    """Mask of explicit buildings: plot centres (M, 2), plot areas, removal money, reputation penalty and
    town index per building (also used by tests)."""
    x0, y0, nx, ny = _empty(terrain, step)
    cxy = np.asarray(cxy, np.float64).reshape(-1, 2)
    area = np.asarray(area, np.float64)
    cost = np.asarray(cost, np.float64)
    pen = np.asarray(pen, np.float64)
    btown = np.asarray(btown, np.int64)
    side = np.sqrt(area)
    s = 2 * np.sqrt(area / math.pi)                          # equal-area disc diameter
    w = {"n": 1 / area, "area": np.ones_like(area), "cost": cost / area, "pen": pen / area}
    ws = {q + "S": v * s for q, v in w.items()}
    r = _splat(x0, y0, step, nx, ny, cxy[:, 0], cxy[:, 1], side, {**w, **ws})
    R = {q: (r[q].astype(np.float32), r[q + "S"].astype(np.float32)) for q in _Q}
    # town raster: nearest building's town within 3 cells
    town = np.full((ny, nx), -1, np.int32)
    if len(cxy):
        iy, ix = np.mgrid[0:ny, 0:nx]
        pts = np.column_stack([x0 + ix.ravel() * step, y0 + iy.ravel() * step])
        d, k = cKDTree(cxy).query(pts, distance_upper_bound=3 * step)
        got = np.isfinite(d)
        tt = np.full(len(pts), -1, np.int64)
        tt[got] = btown[k[got]]
        town = tt.reshape(ny, nx).astype(np.int32)
    return TownMask(source, x0, y0, step, nx, ny, R, town, towns,
                    bxy=cxy, br=s / 2, barea=area, bcost=cost, bpen=pen, btown=btown, cost_source=cost_source)


def _synthetic(game, cover: np.ndarray, town: np.ndarray, towns: list[TownRef], x0, y0, step, source) -> TownMask:
    """Rasters of synthetic buildings (FALLBACK_PLOT_M2) for a plot-cover raster. Money per m2 of plot:
    usd_per_capacity x town capacity / built plot area of the town (probes of the export if any)."""
    try:
        from . import buildings
        a_cap, b_cap, probes = cost_model(buildings.for_game(game))
    except Exception:                                     # pragma: no cover
        a_cap, b_cap, probes = DEFAULT_USD_PER_CAPACITY, 0.0, False
    a0 = FALLBACK_PLOT_M2
    s0 = 2 * math.sqrt(a0 / math.pi)
    cover = np.clip(cover, 0.0, COVER_MAX)
    built_area = np.zeros(len(towns))
    m = town >= 0
    np.add.at(built_area, town[m], cover[m] * step * step)
    nlu = np.array([max(1, sum(1 for c in t.caps if c > 0)) for t in towns], np.float64)
    pen_m2 = np.zeros_like(cover)
    pen_m2[m] = IMPACT_PER_FRACTION * nlu[town[m]] / np.maximum(built_area[town[m]], 1.0)
    capt = np.array([sum(t.caps) for t in towns], np.float64)
    usd_m2 = np.zeros_like(cover)
    usd_m2[m] = a_cap * capt[town[m]] / np.maximum(built_area[town[m]], 1.0)
    n = cover / a0
    money = cover * usd_m2 + n * b_cap
    R = {"n": (n, n * s0), "area": (cover, cover * s0), "cost": (money, money * s0),
         "pen": (cover * pen_m2, cover * pen_m2 * s0)}
    R = {q: (a.astype(np.float32), b.astype(np.float32)) for q, (a, b) in R.items()}
    ny, nx = cover.shape
    tm = TownMask(source, x0, y0, step, nx, ny, R, town.astype(np.int32), towns,
                  cost_source=f"{'probes' if probes else 'default'} {a_cap:,.0f} $ x capacity, spread over the plots")
    tm.usd_per_capacity = a_cap
    return tm


def _nearest_town(towns: list[TownRef], xs, ys) -> np.ndarray:
    if not towns:
        return np.full(len(xs), -1, np.int64)
    tc = np.array([[t.x, t.y] for t in towns])
    tr = np.array([t.radius for t in towns])
    d = np.hypot(xs[:, None] - tc[None, :, 0], ys[:, None] - tc[None, :, 1]) - tr[None]
    return np.argmin(d, axis=1)


def from_streets(game, net, step: float = RASTER_M) -> TownMask | None:
    towns = _town_refs(game)
    x0, y0, nx, ny = _empty(game.terrain, step)
    m = net.mask("road", owners="town")
    if not m.any():
        return None
    _, xs, ys = net.edge_samples(m, spacing=8.0)
    ix = np.rint((xs - x0) / step).astype(np.int64)
    iy = np.rint((ys - y0) / step).astype(np.int64)
    ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
    S = np.zeros((ny, nx))
    np.add.at(S, (iy[ok], ix[ok]), 8.0)                      # street metres per cell
    rk = 48.0
    k = int(math.ceil(rk / step))
    yy, xx = np.mgrid[-k:k + 1, -k:k + 1] * step
    disc = (np.hypot(xx, yy) <= rk + step / 2).astype(np.float64)
    rho = ndimage.convolve(S, disc, mode="constant") / (disc.sum() * step * step)   # m street per m2
    cover = np.minimum(rho * STREET_BAND_M, COVER_MAX)
    near, (ky, kx) = ndimage.distance_transform_edt(S <= 0, sampling=step, return_indices=True)
    cover[near > BUILT_BUFFER_M] = 0.0
    # town of the nearest street sample
    tw = _nearest_town(towns, xs[ok], ys[ok])
    T = np.full((ny, nx), -1, np.int64)
    T[iy[ok], ix[ok]] = tw
    town = T[ky, kx]
    town[cover <= 0] = -1
    return _synthetic(game, cover, town, towns, x0, y0, step, "town streets (export v6)")


def from_discs(game, step: float = RASTER_M) -> TownMask:
    towns = _town_refs(game)
    x0, y0, nx, ny = _empty(game.terrain, step)
    iy, ix = np.mgrid[0:ny, 0:nx]
    X, Y = x0 + ix * step, y0 + iy * step
    cover = np.zeros((ny, nx))
    town = np.full((ny, nx), -1, np.int64)
    for t in towns:
        R = t.radius + TOWN_MARGIN_M
        d = np.hypot(X - t.x, Y - t.y)
        c = COVER_MAX * np.clip(1 - (d / R) ** 2, 0, 1)
        better = c > cover
        cover = np.where(better, c, cover)
        town[better] = t.index
    return _synthetic(game, cover, town, towns, x0, y0, step, "town discs")


_BY_GAME: dict[int, tuple[object, TownMask | None]] = {}


def for_game(game) -> TownMask | None:
    """Built-up mask of a model.Game (cached): v7 buildings, else v6 town streets, else discs."""
    raw = getattr(game, "raw", None)
    hit = _BY_GAME.get(id(game))
    if hit is not None and hit[0] is raw:
        return hit[1]
    tm = None
    try:
        from . import buildings
        tb = buildings.for_game(game)
        if tb is not None and len(tb):
            tm = from_buildings(game, tb)
    except Exception:                                     # pragma: no cover - broken export section
        tm = None
    if tm is None:
        try:
            from . import network
            net = network.for_game(game)
            tm = from_streets(game, net) if net is not None else None
        except Exception:                                 # pragma: no cover
            tm = None
    if tm is None and getattr(game, "towns", None):
        tm = from_discs(game)
    if len(_BY_GAME) > 4:
        _BY_GAME.pop(next(iter(_BY_GAME)))
    _BY_GAME[id(game)] = (raw, tm)
    return tm


if __name__ == "__main__":
    import json

    from .routing import load_game

    g = load_game()
    tm = for_game(g)
    print(json.dumps(tm.summary() if tm else None, ensure_ascii=False, indent=1))
