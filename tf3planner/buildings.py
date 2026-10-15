"""Town buildings of the map (export v7, section ``townBuildings``) for pricing demolition.

The mod (mod/tf3_planner_1/content/tf3_planner/townbuildings.lua) exports every town building
as flat arrays: entity, town, construction, construction file, land use, person capacity,
level, #parcels, depth, height, axis-aligned bounding box and centre, plus a few removal-cost
probes (``bulldozeSamples``: ``api.engine.util.proposal.makeProposalData`` on a removal proposal
of one building per (land use, level, parcels) class - computed in the game, never applied).

API
---
* ``TownBuildings.from_export(export)`` / ``for_game(game)`` -> TownBuildings | None (exports < v7).
* ``tb.in_corridor(polyline, width)`` -> indices of buildings whose footprint (AABB) comes within
  width/2 of the polyline (optionally with distances); ``buildings_in_corridor(game_or_tb, ...)``.
* ``tb.bulldoze_cost()`` -> per-building cost estimate (from the probes of the same class, else
  the same land use, else the median probe, else FALLBACK_BULLDOZE_COST); ``tb.demolition_cost(idx)``.
* arrays: ids, town, construction, file (index into files), land_use (0 res, 1 com, 2 ind,
  -1 unknown), capacity, level, parcels, depth, height, bbox (M, 4: xmin, ymin, xmax, ymax),
  zmin/zmax, pos (M, 3), time_built.

Footprints are axis-aligned bounding boxes of possibly rotated buildings, i.e. slightly larger
than the real footprint (conservative for demolition).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

__all__ = ["LAND_USES", "FALLBACK_BULLDOZE_COST", "TownBuildings", "for_game", "buildings_in_corridor",
           "IndustryFootprints", "industry_footprints"]

LAND_USES = ("residential", "commercial", "industrial")
# townbuildingutil.lua: construction bulldozeCost = 100, x infrastructureCostScale (1.5 on VeryHard)
# by construction.script.tl. Used only when the export has no usable in-game probe.
FALLBACK_BULLDOZE_COST = 150.0
UNKNOWN_HALF_SIZE = 6.0      # half size of a footprint when the bbox is unknown, m


@dataclass
class TownBuildings:
    ids: np.ndarray
    town: np.ndarray
    construction: np.ndarray
    file: np.ndarray
    files: list[str]
    land_use: np.ndarray
    capacity: np.ndarray
    level: np.ndarray
    parcels: np.ndarray
    depth: np.ndarray
    height: np.ndarray
    bbox: np.ndarray            # (M, 4) xmin, ymin, xmax, ymax
    zmin: np.ndarray
    zmax: np.ndarray
    pos: np.ndarray             # (M, 3)
    time_built: np.ndarray
    bulldoze_samples: list[dict]
    enumeration: str | None = None
    failed: int = 0
    export_seconds: float | None = None
    _cache: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_export(cls, export: dict) -> "TownBuildings | None":
        t = (export or {}).get("townBuildings")
        if not isinstance(t, dict) or not t.get("ids"):
            return None
        M = len(t["ids"])

        def arr(key, default, dtype, k=1):
            v = t.get(key)
            if not isinstance(v, list) or len(v) != k * M:
                return np.full(M * k, default, dtype=dtype)
            return np.asarray([default if x is None or isinstance(x, (str, bool)) else x for x in v], dtype=dtype)

        b6 = arr("bbox", 0.0, np.float64, 6).reshape(M, 6)
        pos = arr("pos", 0.0, np.float64, 3).reshape(M, 3)
        known = (b6[:, 3] > b6[:, 0]) | (b6[:, 4] > b6[:, 1])
        bbox = np.where(known[:, None], b6[:, [0, 1, 3, 4]],
                        np.column_stack([pos[:, 0] - UNKNOWN_HALF_SIZE, pos[:, 1] - UNKNOWN_HALF_SIZE,
                                         pos[:, 0] + UNKNOWN_HALF_SIZE, pos[:, 1] + UNKNOWN_HALF_SIZE]))
        land_use = arr("landUse", -1, np.int64)
        luv = t.get("landUseValues") or {}
        if not luv:
            # raw engine values without the enum table: the .d.tl documents 1/2/3
            land_use = np.where((land_use >= 1) & (land_use <= 3), land_use - 1, -1)
        samples = t.get("bulldozeSamples") or []
        if isinstance(samples, dict):
            samples = list(samples.values())
        return cls(
            ids=np.asarray(t["ids"], dtype=np.int64), town=arr("town", -1, np.int64),
            construction=arr("construction", -1, np.int64), file=arr("file", -1, np.int64),
            files=[str(f) for f in (t.get("files") or [])],
            land_use=land_use.astype(np.int8), capacity=arr("capacity", -1, np.float64),
            level=arr("level", -1, np.int64), parcels=arr("parcels", -1, np.int64),
            depth=arr("depth", -1, np.int64), height=arr("height", -1.0, np.float64),
            bbox=bbox, zmin=np.where(known, b6[:, 2], pos[:, 2]), zmax=np.where(known, b6[:, 5], pos[:, 2]),
            pos=pos, time_built=arr("timeBuilt", -1, np.float64),
            bulldoze_samples=[s for s in samples if isinstance(s, dict)],
            enumeration=t.get("enumeration"), failed=int(t.get("failed") or 0), export_seconds=t.get("seconds"))

    # ------------------------------------------------------------------ basics
    def __len__(self) -> int:
        return len(self.ids)

    def file_name(self, i: int) -> str:
        f = int(self.file[i])
        return self.files[f] if 0 <= f < len(self.files) else "?"

    def summary(self) -> dict:
        lu = {LAND_USES[k]: int((self.land_use == k).sum()) for k in range(3)}
        costs = [s.get("costs") for s in self.bulldoze_samples if isinstance(s.get("costs"), (int, float))]
        return {"count": len(self), "failed": self.failed, "enumeration": self.enumeration, "by_land_use": lu,
                "towns": int(len(np.unique(self.town[self.town >= 0]))),
                "capacity_total": float(self.capacity[self.capacity > 0].sum()),
                "bulldoze_samples": len(self.bulldoze_samples), "bulldoze_costs": costs,
                "export_seconds": self.export_seconds}

    # ------------------------------------------------------------------ costs
    def bulldoze_cost(self) -> np.ndarray:
        """Estimated removal cost per building (see module doc)."""
        if "cost" in self._cache:
            return self._cache["cost"]
        by_class: dict[tuple, list[float]] = {}
        by_lu: dict[int, list[float]] = {}
        allc: list[float] = []
        for s in self.bulldoze_samples:
            c = s.get("costs")
            if not isinstance(c, (int, float)) or c <= 0:
                c = s.get("bulldozeCost")
            if not isinstance(c, (int, float)) or c <= 0 or s.get("error"):
                continue
            key = (s.get("landUse"), s.get("level"), s.get("parcels"))
            by_class.setdefault(key, []).append(float(c))
            by_lu.setdefault(s.get("landUse"), []).append(float(c))
            allc.append(float(c))
        default = float(np.median(allc)) if allc else FALLBACK_BULLDOZE_COST
        out = np.full(len(self), default)
        if allc:
            for i in range(len(self)):
                key = (int(self.land_use[i]), int(self.level[i]), int(self.parcels[i]))
                if key in by_class:
                    out[i] = float(np.mean(by_class[key]))
                elif key[0] in by_lu:
                    # scale by parcels within the land use (bigger buildings cost more)
                    lu_s = [s for s in self.bulldoze_samples if s.get("landUse") == key[0]
                            and isinstance(s.get("costs"), (int, float)) and s["costs"] > 0
                            and isinstance(s.get("parcels"), (int, float)) and s["parcels"] > 0]
                    if lu_s and key[2] > 0:
                        per_parcel = np.mean([s["costs"] / s["parcels"] for s in lu_s])
                        out[i] = per_parcel * key[2]
                    else:
                        out[i] = float(np.mean(by_lu[key[0]]))
        self._cache["cost"] = out
        return out

    def demolition_cost(self, idx) -> float:
        return float(self.bulldoze_cost()[np.asarray(idx, dtype=np.int64)].sum())

    # ------------------------------------------------------------------ geometry
    def in_corridor(self, polyline, width: float, return_distance: bool = False):
        """Buildings whose footprint (AABB) is within width/2 of the polyline [(x, y), ...].

        Returns sorted unique building indices (and their distance to the polyline, m)."""
        pts = np.asarray([(p[0], p[1]) for p in polyline], dtype=np.float64).reshape(-1, 2)
        M = len(self)
        if M == 0 or len(pts) == 0:
            e = np.zeros(0, np.int64)
            return (e, np.zeros(0)) if return_distance else e
        if len(pts) == 1:
            pts = np.vstack([pts, pts])
        h = width / 2.0
        best = np.full(M, np.inf)
        bx0, by0, bx1, by1 = self.bbox.T
        for (ax, ay), (cx, cy) in zip(pts[:-1], pts[1:]):
            cand = np.nonzero((bx0 <= max(ax, cx) + h) & (bx1 >= min(ax, cx) - h)
                              & (by0 <= max(ay, cy) + h) & (by1 >= min(ay, cy) - h))[0]
            if len(cand) == 0:
                continue
            d = _seg_rect_dist(ax, ay, cx, cy, bx0[cand], by0[cand], bx1[cand], by1[cand])
            best[cand] = np.minimum(best[cand], d)
        idx = np.nonzero(best <= h)[0]
        return (idx, best[idx]) if return_distance else idx


def _point_rect_dist(px, py, x0, y0, x1, y1):
    dx = np.maximum(np.maximum(x0 - px, 0.0), px - x1)
    dy = np.maximum(np.maximum(y0 - py, 0.0), py - y1)
    return np.hypot(dx, dy)


def _point_seg_dist(px, py, ax, ay, cx, cy):
    vx, vy = cx - ax, cy - ay
    L2 = vx * vx + vy * vy
    if L2 <= 0:
        return np.hypot(px - ax, py - ay)
    t = np.clip(((px - ax) * vx + (py - ay) * vy) / L2, 0.0, 1.0)
    return np.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _seg_rect_dist(ax, ay, cx, cy, x0, y0, x1, y1) -> np.ndarray:
    """Distance from segment (a, c) to axis-aligned rectangles (vectorized over rectangles)."""
    # intersection test (Liang-Barsky): any t in [0, 1] inside the rectangle
    dx, dy = cx - ax, cy - ay
    t0 = np.zeros(len(x0))
    t1 = np.ones(len(x0))
    hit = np.ones(len(x0), bool)
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            hit &= q >= 0
            continue
        r = q / p
        if p < 0:
            t0 = np.maximum(t0, r)
        else:
            t1 = np.minimum(t1, r)
    hit &= t0 <= t1
    d = np.minimum(_point_rect_dist(ax, ay, x0, y0, x1, y1), _point_rect_dist(cx, cy, x0, y0, x1, y1))
    for qx, qy in ((x0, y0), (x0, y1), (x1, y0), (x1, y1)):
        d = np.minimum(d, _point_seg_dist(qx, qy, ax, ay, cx, cy))
    return np.where(hit, 0.0, d)


# --------------------------------------------------------------------------- per-game cache

_BY_GAME: dict[int, tuple[object, TownBuildings | None]] = {}


def for_game(game) -> TownBuildings | None:
    """Town buildings of a model.Game (cached; None if the export has no v7 section)."""
    raw = getattr(game, "raw", None)
    hit = _BY_GAME.get(id(game))
    if hit is not None and hit[0] is raw:
        return hit[1]
    tb = TownBuildings.from_export(raw) if isinstance(raw, dict) else None
    if len(_BY_GAME) > 4:
        _BY_GAME.pop(next(iter(_BY_GAME)))
    _BY_GAME[id(game)] = (raw, tb)
    return tb


def buildings_in_corridor(game_or_tb, polyline, width: float, return_distance: bool = False):
    """Indices of town buildings within width/2 of the polyline (empty if no v7 export)."""
    tb = game_or_tb if isinstance(game_or_tb, TownBuildings) else for_game(game_or_tb)
    if tb is None:
        e = np.zeros(0, np.int64)
        return (e, np.zeros(0)) if return_distance else e
    return tb.in_corridor(polyline, width, return_distance)


# --------------------------------------------------------------------------- industry footprints (export v8)

DEFAULT_INDUSTRY_HALF = 90.0   # half size of the square assumed around an industry without v8 footprint, m


@dataclass
class IndustryFootprints:
    """Axis-aligned rectangles occupied by industries (their subconstructions, else the whole
    construction / industry entity, plus nearby fields). ``exact`` False: export < v8, every industry
    is a square of +-DEFAULT_INDUSTRY_HALF around its position."""
    rects: np.ndarray              # (K, 4) xmin, ymin, xmax, ymax
    owner: np.ndarray              # (K,) industry id (-1 for fields)
    kind: list[str]                # "part" | "con" | "ind" | "field" | "square"
    exact: bool

    @classmethod
    def from_export(cls, export: dict) -> "IndustryFootprints":
        rects, owner, kind = [], [], []
        exact = False
        fields = set()
        for ind in (export or {}).get("industries") or []:
            fp = ind.get("footprint") if isinstance(ind.get("footprint"), dict) else None
            iid = int(ind.get("id") or -1)
            got = False
            if fp:
                parts = fp.get("parts")
                parts = list(parts.values()) if isinstance(parts, dict) else parts
                if isinstance(parts, list) and len(parts) >= 4:
                    for k in range(0, len(parts) - 3, 4):
                        rects.append(parts[k:k + 4]); owner.append(iid); kind.append("part")
                    got = True
                for key in ("con", "ind"):
                    b = fp.get(key)
                    b = list(b.values()) if isinstance(b, dict) else b
                    if isinstance(b, list) and len(b) == 4 and not (key == "con" and got):
                        rects.append(b); owner.append(iid); kind.append(key)
                        got = True
                fl = fp.get("fields")
                fl = list(fl.values()) if isinstance(fl, dict) else fl
                if isinstance(fl, list):
                    for k in range(0, len(fl) - 3, 4):
                        t = tuple(round(v, 1) for v in fl[k:k + 4])
                        if t not in fields:
                            fields.add(t)
                            rects.append(list(t)); owner.append(-1); kind.append("field")
                exact = exact or got
            if not got:
                pos = ind.get("pos") or {}
                if pos.get("x") is not None:
                    h = DEFAULT_INDUSTRY_HALF
                    rects.append([pos["x"] - h, pos["y"] - h, pos["x"] + h, pos["y"] + h])
                    owner.append(iid); kind.append("square")
        R = np.asarray(rects, dtype=np.float64).reshape(-1, 4)
        if len(R):
            R = np.column_stack([np.minimum(R[:, 0], R[:, 2]), np.minimum(R[:, 1], R[:, 3]),
                                 np.maximum(R[:, 0], R[:, 2]), np.maximum(R[:, 1], R[:, 3])])
        return cls(R, np.asarray(owner, dtype=np.int64), kind, exact)

    def __len__(self) -> int:
        return len(self.rects)

    def signed_distance(self, x: float, y: float) -> np.ndarray:
        """Per rectangle: distance from (x, y) to it (negative = inside, depth to the nearest edge)."""
        R = self.rects
        if len(R) == 0:
            return np.zeros(0)
        dx = np.maximum(np.maximum(R[:, 0] - x, 0.0), x - R[:, 2])
        dy = np.maximum(np.maximum(R[:, 1] - y, 0.0), y - R[:, 3])
        out = np.hypot(dx, dy)
        inside = (dx == 0) & (dy == 0)
        if inside.any():
            depth = np.minimum.reduce([x - R[:, 0], R[:, 2] - x, y - R[:, 1], R[:, 3] - y])
            out = np.where(inside, -depth, out)
        return out

    def clearance(self, x: float, y: float) -> float:
        d = self.signed_distance(x, y)
        return float(d.min()) if len(d) else math.inf

    def clear(self, x: float, y: float, radius: float, margin: float = 5.0) -> bool:
        """A disc of ``radius`` at (x, y) keeps ``margin`` metres away from every industry footprint."""
        return self.clearance(x, y) >= radius + margin

    def nearest(self, x: float, y: float) -> tuple[float, int, str]:
        d = self.signed_distance(x, y)
        if not len(d):
            return math.inf, -1, ""
        k = int(np.argmin(d))
        return float(d[k]), int(self.owner[k]), self.kind[k]


def industry_footprints(game) -> IndustryFootprints:
    """IndustryFootprints of a model.Game (cached on the game object)."""
    fp = getattr(game, "_industry_footprints", None)
    if fp is None:
        fp = IndustryFootprints.from_export(getattr(game, "raw", None) or {})
        try:
            game._industry_footprints = fp
        except Exception:  # noqa: BLE001
            pass
    return fp


if __name__ == "__main__":
    import json

    from .routing import load_game

    g = load_game()
    tb = for_game(g)
    if tb is None:
        print(f"export v{g.raw.get('version')}: no town buildings (needs export v7 - restart the game and export)")
    else:
        print(json.dumps(tb.summary(), ensure_ascii=False, indent=1))
        t = g.towns[0]
        idx = tb.in_corridor([(t.x - 1000, t.y), (t.x + 1000, t.y)], 20.0)
        print(f"{t.name}: 2 km x 20 m corridor through the centre hits {len(idx)} buildings, "
              f"demolition ~{tb.demolition_cost(idx):,.0f}")
