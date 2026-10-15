"""In-game blueprint overlay: plan.json + dossiers.json -> <userdata>/tf3_planner/plan_overlay.lua.

The mod window «TF3 Planner: чертёж» (mod/tf3_planner_1/content/tf3_planner/blueprint.lua) loads
the file with ``app.loadUserdata("tf3_planner", "plan_overlay")`` and draws the selected
project as ghost zones/markers that disappear as the player builds.

File format (``function data() return {...} end``, Lua 5.2, plain decimals, escaped strings)::

    { version = 1, generated = "...", source = {...},
      index = { {id, line, step, year, month, order, title, mode, cargo, cost, kind}, ... },   -- plan order
      projects = { ["L4:s7"] = {
          id, line, step, year, month, title, mode, cargo, cost, kind ("build_line"|"upgrade_track"),
          corridor = width m,
          segments = { {t = "new"|"shared"|"existing"|"upgrade", k = "rail"|"road", way = key, approx = bool,
                        p = {x1, y1, x2, y2, ...}}, ... },          -- thinned, world metres
          spans = { {t = "bridge"|"viaduct"|"tunnel", name, len, p = {...}}, ... },
          items = { {k = "station"|"stop"|"harbor"|"depot"|"maint"|"warehouse", name, text,
                     x, y, z, h (heading rad), len, wid, poly = {x1, y1, ..., x4, y4},
                     match = {plain substrings of the construction fileName}, exclude = {...}}, ... },
          waypoints = { {n, x, y, z, label, stop = bool}, ... },
          demolition = { {p = {...}, text, cost}, ... },
          totals = {new_m, existing_m, shared_m, upgrade_m} } } }

Thinning: Douglas-Peucker (tolerance ``THIN_TOL`` m) plus a minimum vertex spacing of
``MIN_SPACING`` m (<= 2 points per 50 m); the mod resamples into ~40 m ghost chunks itself.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from pathlib import Path

from .model import DATA

THIN_TOL = 1.5
MIN_SPACING = 25.0
WATER_SAMPLE_M = 4.0         # bridge detection: sampling step along the drawn route
BRIDGE_PAD_M = 6.0           # bridge abutments: extend a water crossing onto land on both sides
TURN_MIN_DEG = 35.0          # numbered waypoint at turns sharper than this
KIND_RU = {"station": "станция", "stop": "остановка", "harbor": "порт", "depot": "депо",
           "maint": "здание техобслуживания", "warehouse": "склад"}
CORRIDOR = {"rail": 24.0, "road": 20.0}
# letters drawn in the world next to each footprint (the API has no world text): mod blueprint.lua GLYPHS
LABEL = {"station": "С", "stop": "О", "harbor": "П", "depot": "Д", "maint": "Т", "warehouse": "СК"}

# footprint (length along the route, width) and construction fileName patterns per item kind
RAIL_DEPOT = {"len": 80.0, "wid": 22.0, "match": ["/depots/rail/"], "exclude": ["maint"]}
ROAD_DEPOT = {"len": 32.0, "wid": 26.0, "match": ["/depots/road/"], "exclude": ["maint"]}
WATER_DEPOT = {"len": 50.0, "wid": 30.0, "match": ["/depots/water/"], "exclude": ["maint"]}
MAINT = {"len": 32.0, "wid": 26.0, "match": ["maint"], "exclude": []}
WAREHOUSE = {"len": 40.0, "wid": 40.0, "match": ["/warehouses/", "warehouse"], "exclude": []}

LUA_KEYWORDS = {"and", "break", "do", "else", "elseif", "end", "false", "for", "function", "goto", "if", "in",
                "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# --------------------------------------------------------------------------- Lua writer

def _num(v: float) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if not math.isfinite(v):
        return "0"
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def _str(s: str) -> str:
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 32 or o == 127:
            out.append(f"\\{o:03d}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def to_lua(v, indent: int = 0) -> str:
    """Lua literal for JSON-like data (dict keys: identifiers or ["string"]; lists 1..n)."""
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _num(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, (list, tuple)):
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            return "{" + ",".join(_num(x) for x in v) + "}"
        pad = " " * (indent + 1)
        return "{\n" + ",\n".join(pad + to_lua(x, indent + 1) for x in v) + "}"
    if isinstance(v, dict):
        pad = " " * (indent + 1)
        parts = []
        for k, x in v.items():
            if x is None:
                continue
            k = str(k)
            key = k if (_IDENT.match(k) and k not in LUA_KEYWORDS) else "[" + _str(k) + "]"
            parts.append(f"{pad}{key} = {to_lua(x, indent + 1)}")
        return "{\n" + ",\n".join(parts) + "}"
    return _str(str(v))


def lua_file(data: dict) -> str:
    return "function data()\nreturn " + to_lua(data) + "\nend\n"


# --------------------------------------------------------------------------- geometry

def _dp(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    if len(pts) < 3:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if L2 <= 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
                d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def thin(poly, tol: float = THIN_TOL, min_spacing: float = MIN_SPACING) -> list[tuple[float, float]]:
    """Douglas-Peucker + minimum vertex spacing (end points kept)."""
    pts = [(float(p[0]), float(p[1])) for p in poly or []]
    out = []
    for p in pts:   # drop exact duplicates
        if not out or (abs(p[0] - out[-1][0]) > 1e-6 or abs(p[1] - out[-1][1]) > 1e-6):
            out.append(p)
    if len(out) < 3:
        return out
    out = _dp(out, tol)
    res = [out[0]]
    for p in out[1:-1]:
        if math.hypot(p[0] - res[-1][0], p[1] - res[-1][1]) >= min_spacing:
            res.append(p)
    if len(res) > 1 and math.hypot(out[-1][0] - res[-1][0], out[-1][1] - res[-1][1]) < min_spacing * 0.5:
        res[-1] = out[-1]
    else:
        res.append(out[-1])
    return res


def flat(pts) -> list[float]:
    return [round(c, 1) for p in pts for c in (p[0], p[1])]


def cumdist(pts) -> list[float]:
    c = [0.0]
    for a, b in zip(pts[:-1], pts[1:]):
        c.append(c[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return c


def sub_polyline(pts, d0: float, d1: float) -> list[tuple[float, float]]:
    """Part of a polyline between distances d0..d1 along it."""
    if len(pts) < 2:
        return list(pts)
    c = cumdist(pts)
    d0, d1 = max(0.0, min(d0, c[-1])), max(0.0, min(d1, c[-1]))
    if d1 < d0:
        d0, d1 = d1, d0

    def at(d):
        for i in range(1, len(c)):
            if c[i] >= d:
                seg = c[i] - c[i - 1]
                t = 0.0 if seg <= 0 else (d - c[i - 1]) / seg
                a, b = pts[i - 1], pts[i]
                return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), i
        return pts[-1], len(pts) - 1

    p0, i0 = at(d0)
    p1, i1 = at(d1)
    return [p0] + [pts[i] for i in range(i0, i1) if c[i] > d0 and c[i] < d1] + [p1]


def nearest_heading(x: float, y: float, polys) -> tuple[float, float]:
    """(heading rad of the nearest polyline piece, distance)."""
    best, h = math.inf, 0.0
    for pts in polys:
        for a, b in zip(pts[:-1], pts[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            if L2 <= 0:
                continue
            t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / L2))
            d = math.hypot(x - a[0] - t * dx, y - a[1] - t * dy)
            if d < best:
                best, h = d, math.atan2(dy, dx)
    return h, best


def rect(x: float, y: float, h: float, length: float, width: float) -> list[float]:
    ux, uy = math.cos(h), math.sin(h)
    vx, vy = -uy, ux
    a, b = length / 2, width / 2
    corners = [(x - ux * a - vx * b, y - uy * a - vy * b), (x + ux * a - vx * b, y + uy * a - vy * b),
               (x + ux * a + vx * b, y + uy * a + vy * b), (x - ux * a + vx * b, y - uy * a + vy * b)]
    return flat(corners)


# --------------------------------------------------------------------------- building the overlay

class _Ctx:
    def __init__(self, game=None):
        self.game = game
        self.way_kind = {}
        if game is not None:
            self.way_kind = {w.key: w.kind for w in game.ways}

    def z(self, x: float, y: float) -> float:
        if self.game is None:
            return 0.0
        try:
            return round(float(self.game.terrain.height(x, y)), 1)
        except Exception:  # noqa: BLE001
            return 0.0

    def seg_kind(self, way: str | None, fallback: str) -> str:
        if way and way in self.way_kind:
            return self.way_kind[way]
        if way:
            if way.startswith(("simple", "standard", "high_speed")) or "track" in way:
                return "rail"
            if way.startswith(("country", "town", "highway", "ind_country")):
                return "road"
        return fallback if fallback in ("rail", "road", "water") else "road"


ACCESS_GAP = {"road": 4.0, "rail": 8.0}   # building front -> road/track centre line, m


def _nearest_on(polys, x: float, y: float):
    """(px, py, heading, distance, side) of the nearest point on the polylines; side = +1 when
    (x, y) lies left of the travel direction, -1 right, 0 on the line."""
    best = None
    for pts in polys:
        for a, b in zip(pts[:-1], pts[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            if L2 <= 0:
                continue
            t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / L2))
            px, py = a[0] + t * dx, a[1] + t * dy
            d = math.hypot(x - px, y - py)
            if best is None or d < best[3]:
                cross = dx * (y - a[1]) - dy * (x - a[0])
                best = (px, py, math.atan2(dy, dx), d, (1 if cross > 0 else -1) if d > 2.0 else 0)
    return best


DUP_M = 40.0        # a "shared" piece within this distance of the project's own new road ...
DUP_SHARE = 0.7     # ... over this share of its length is the loop's way back over it


def _near_share(pts, others, tol: float, step: float = 20.0) -> float:
    """Share of the polyline's length (sampled every ``step`` m) within ``tol`` of any of ``others``."""
    c = cumdist(pts)
    L = c[-1]
    if L <= 0:
        return 0.0
    n = max(2, int(L // step) + 1)
    near = 0
    for k in range(n):
        x, y = sub_polyline(pts, L * k / (n - 1), L * k / (n - 1))[0]
        if any(project_on(o, x, y)[1] <= tol for o in others):
            near += 1
    return near / n


def _overlaps(cx: float, cy: float, h: float, L: float, W: float, placed, margin: float = 4.0) -> bool:
    """Oriented-rectangle overlap test (separating axes) against the already placed footprints."""
    def corners(x, y, hh, ll, ww):
        return list(zip(*[iter(rect(x, y, hh, ll + margin, ww + margin))] * 2))

    A = corners(cx, cy, h, L, W)
    for (x, y, hh, ll, ww) in placed:
        if math.hypot(x - cx, y - cy) > (math.hypot(L, W) + math.hypot(ll, ww)) / 2 + margin:
            continue
        B = corners(x, y, hh, ll, ww)
        sep = False
        for P in (A, B):
            for i in range(4):
                ex, ey = P[(i + 1) % 4][0] - P[i][0], P[(i + 1) % 4][1] - P[i][1]
                nx, ny = -ey, ex
                pa = [nx * q[0] + ny * q[1] for q in A]
                pb = [nx * q[0] + ny * q[1] for q in B]
                if max(pa) < min(pb) or max(pb) < min(pa):
                    sep = True
                    break
            if sep:
                break
        if not sep:
            return True
    return False


def beside_road(ctx: "_Ctx", x: float, y: float, polys, L: float, W: float, kind: str = "road",
                toward: tuple[float, float] | None = None, placed=None) -> dict | None:
    """Put a building of L x W next to the route instead of on it: long side parallel to the road,
    front edge ``ACCESS_GAP`` from the centre line, on the side where (x, y) already is (or towards
    ``toward``, e.g. the industry or town served), clear of industries and of footprints already
    placed for this project. Returns x, y, h plus the access stub (front middle -> road point)."""
    near = _nearest_on(polys, x, y) if polys else None
    if near is None:
        return None
    px, py, h, d, side = near
    gap = ACCESS_GAP.get(kind, 4.0)
    if d >= W / 2 + gap - 1.0 and not (placed and _overlaps(x, y, h, L, W, placed)):
        # already off the road (and free): keep the planner's place, orient it and
        if d > 250:                     # draw the access stub from its road-side edge to the road
            return {"x": x, "y": y, "h": h, "skip_access": True}
        ex, ey = (px - x) / d, (py - y) / d
        return {"x": x, "y": y, "h": h, "fx": x + ex * W / 2, "fy": y + ey * W / 2, "ax": px, "ay": py,
                "keep": True}
    if side == 0 and toward is not None:
        n2 = _nearest_on(polys, toward[0], toward[1])
        side = (n2[4] if n2 else 0) or 0
    sides = [side or 1, -(side or 1)]
    fp = None
    if ctx.game is not None:
        try:
            from .buildings import industry_footprints
            fp = industry_footprints(ctx.game)
            if not len(fp):
                fp = None
        except Exception:  # noqa: BLE001
            fp = None
    ux, uy = math.cos(h), math.sin(h)
    vx, vy = -uy, ux                       # left normal of the travel direction
    for shift in (0.0, L + 6, -(L + 6), 2 * (L + 6), -2 * (L + 6)):
        for sd in sides:
            ax, ay = px + ux * shift, py + uy * shift
            off = gap + W / 2
            cx, cy = ax + vx * sd * off, ay + vy * sd * off
            if fp is not None and not fp.clear(cx, cy, math.hypot(L, W) / 2 * 0.8, margin=2.0):
                continue
            if placed and _overlaps(cx, cy, h, L, W, placed):
                continue
            return {"x": cx, "y": cy, "h": h, "fx": ax + vx * sd * gap, "fy": ay + vy * sd * gap,
                    "ax": ax, "ay": ay}
    return None


def _place(ctx: "_Ctx", it: dict, polys, kind: str, toward=None, placed=None) -> dict:
    """Move an item off the drawn route (see beside_road) and record its access stub."""
    pl = beside_road(ctx, it["x"], it["y"], polys, it["len"], it["wid"], kind, toward, placed)
    if pl and pl.get("skip_access"):
        it["h"] = round(pl["h"], 4)
        it["poly"] = rect(it["x"], it["y"], it["h"], it["len"], it["wid"])
    elif pl:
        it.update({"x": round(pl["x"], 1), "y": round(pl["y"], 1), "h": round(pl["h"], 4),
                   "access": [round(pl["fx"], 1), round(pl["fy"], 1), round(pl["ax"], 1), round(pl["ay"], 1)]})
        it["z"] = ctx.z(it["x"], it["y"])
        it["poly"] = rect(it["x"], it["y"], it["h"], it["len"], it["wid"])
    if placed is not None:
        placed.append((it["x"], it["y"], it["h"], it["len"], it["wid"]))
    return it


def _where(it: dict) -> str:
    """How to find the item on the map: its letter + size, and the access stub when there is one."""
    size = f"≈{it['len']:.0f}×{it['wid']:.0f} м"
    acc = ""
    if it.get("access"):
        a = it["access"]
        L = math.hypot(a[2] - a[0], a[3] - a[1])
        acc = (f", подъезд ~{L:.0f} м до трассы — белая полоса (построить)" if L > 15
               else ", въезд с дороги — белая полоса")
    return f" [{size}{acc}]"


# construction file names (matched by suffix against the game's constructionRep in the mod) and the
# construction menu each lives in, for the mission panel's «Открыть в меню» / button highlights
MENU_OF = {"rail": "rail", "road": "road", "water": "water"}


def _res_of(it: dict, mode: str) -> tuple[str | None, str]:
    m = " ".join(it.get("match") or [])
    k = it["k"]
    if k == "warehouse":
        return "warehouse.con", "warehouse"
    if k == "maint":
        kk = "rail" if mode == "rail" else ("water" if mode == "water" else "road")
        return f"{kk}_maint_station.con", MENU_OF[kk]
    if k == "depot":
        if "/depots/rail/" in m:
            return "rail_depot.con", "rail"
        if "/depots/water/" in m:
            return "water_depot.con", "water"
        return "road_depot.con", "road"
    if k == "harbor":
        return "harbor_modular.con", "water"
    if "/stations/rail/" in m:
        return "modular_station.con", "rail"
    if k == "stop":
        mm = re.search(r"\(([\w.]+\.con)\)", it.get("name") or "")
        return (mm.group(1) if mm else "small_old.con"), "road"
    return "modular_terminal.con", "road"


# Modular street terminal (::/stations/street/modular_street_station/modular_terminal.con), geometry from
# the game's own updateFn run offline with the params of a terminal the player built (2026-10-05):
# the entrance slot is at local (0, -15) for 1, 2 and 4 platforms, its street stub runs 15 m further
# along -Y and ends in the snap node at local (0, -30); the station body spans y -15..25 and x by the
# platform count. Construction params: {tramTrack, platforms, specialization, year, seed, modules}.
TERM_X = {1: (-10.0, 20.0), 2: (-20.0, 20.0), 3: (-35.0, 35.0)}
TERM_Y = (-15.0, 25.0)
TERM_SNAP = 30.0
TERM_GAP = 0.0          # snap node -> road centre line, m: the entrance ends ON the road (in its node)
SHIFT_USD_PER_M = 1500.0  # placement: $ per metre moved along the route from the planned spot (vs demolition)
SIDE_USD = 20000.0        # placement: the other side of the road than planned
CATCHMENT_M = 175.0       # street terminal catchment radius (planner facilities)
REAL_ROAD_SNAP_M = 40.0   # a real (exported) road this close replaces the route's drawn line as anchor
HOUSE_REPUTATION_USD = 150000.0  # placement: each demolished house also costs ~5 % town reputation


def terminal_platforms(terms: int) -> int:
    return 1 if terms <= 1 else (2 if terms == 2 else 3)


class _Obstacles:
    """What a building must not stand on: town buildings (export v7 discs) and existing roads/tracks
    (network edge samples). Lazily built per game."""
    _by_game: dict = {}

    def __init__(self, game):
        import numpy as np
        from scipy.spatial import cKDTree
        self.b_xy = self.b_r = self.b_tree = self.b_cost = None
        self.e_xy = self.e_tree = None
        self.a_xy = self.a_tree = None
        try:
            from . import townmask
            tm = townmask.for_game(game)
            if tm is not None and getattr(tm, "exact", False) and len(tm.bxy):
                self.b_xy, self.b_r = np.asarray(tm.bxy), np.asarray(tm.br)
                self.b_cost = np.asarray(tm.bcost, dtype=float)
                self.b_tree = cKDTree(self.b_xy)
        except Exception:  # noqa: BLE001
            pass
        try:
            from . import network
            net = network.for_game(game)
            if net is not None:
                e, x, y = net.edge_samples(None, spacing=4.0)
                if len(x):
                    self.e_xy = np.column_stack([x, y])
                    self.e_tree = cKDTree(self.e_xy)
                    # industry access roads belong to the industry construction: a station cannot be
                    # joined to them by script (no node can be added there) -> not an anchor
                    ind = {k for k in set(e.tolist()) if str(net.template_key(k)).startswith("ind_")}
                    keep = np.array([k not in ind for k in e.tolist()])
                    self.a_xy = self.e_xy[keep]
                    self.a_tree = cKDTree(self.a_xy) if len(self.a_xy) else None
        except Exception:  # noqa: BLE001
            pass

    def real_road(self, x, y, max_d):
        """(x, y, heading) of the nearest existing road/track sample within max_d, else None."""
        tree, xy = getattr(self, "a_tree", None), getattr(self, "a_xy", None)
        if tree is None:
            return None
        d, k = tree.query([x, y])
        if d > max_d:
            return None
        px, py = float(xy[k, 0]), float(xy[k, 1])
        best = None
        for j in (k - 2, k - 1, k + 1, k + 2):           # neighbours on the same edge (4 m sampling)
            if 0 <= j < len(xy):
                qx, qy = float(xy[j, 0]), float(xy[j, 1])
                dd = math.hypot(qx - px, qy - py)
                if 1.0 < dd < 12.0:
                    best = (qx - px, qy - py) if j > k else (px - qx, py - qy)
                    break
        if best is None:
            return None
        return px, py, math.atan2(best[1], best[0])

    @classmethod
    def for_game(cls, game):
        if game is None:
            return None
        o = cls._by_game.get(id(game))
        if o is None:
            o = cls._by_game[id(game)] = cls(game)
        return o

    def hits(self, tx, ty, c0, c1, x0, x1, y0, y1, stub=None, road_half=5.0):
        """(buildings, road samples) inside the local rectangle [x0,x1]x[y0,y1] of the frame
        (origin t, axes c0, c1); stub = (x0, x1, y0, y1) checked against roads only up to its last 6 m."""
        import numpy as np
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        mx, my = tx + c0[0] * cx + c1[0] * cy, ty + c0[1] * cx + c1[1] * cy
        R = math.hypot(x1 - x0, y1 - y0) / 2
        nb, cost = 0, 0.0
        if self.b_tree is not None:
            idx = self.b_tree.query_ball_point([mx, my], R + float(self.b_r.max()))
            for i in idx:
                dx, dy = self.b_xy[i, 0] - tx, self.b_xy[i, 1] - ty
                lx, ly = dx * c0[0] + dy * c0[1], dx * c1[0] + dy * c1[1]
                r = float(self.b_r[i])
                if x0 - r <= lx <= x1 + r and y0 - r <= ly <= y1 + r:
                    nb += 1
                    cost += float(self.b_cost[i]) if self.b_cost is not None else 25000.0
        ne = 0
        if self.e_tree is not None:
            boxes = [(x0, x1, y0, y1)]
            if stub:
                boxes.append(stub)
            for bx0, bx1, by0, by1 in boxes:
                bcx, bcy = (bx0 + bx1) / 2, (by0 + by1) / 2
                bmx, bmy = tx + c0[0] * bcx + c1[0] * bcy, ty + c0[1] * bcx + c1[1] * bcy
                idx = self.e_tree.query_ball_point([bmx, bmy], math.hypot(bx1 - bx0, by1 - by0) / 2 + road_half)
                for i in idx:
                    dx, dy = self.e_xy[i, 0] - tx, self.e_xy[i, 1] - ty
                    lx, ly = dx * c0[0] + dy * c0[1], dx * c1[0] + dy * c1[1]
                    if bx0 - road_half <= lx <= bx1 + road_half and by0 - road_half <= ly <= by1 + road_half:
                        ne += 1
        self.last_cost = cost
        return nb, ne


BUILT_MATCH_M = 150.0     # a player terminal this close to the planned one is that station


def adopt_built_terminal(ctx: "_Ctx", it: dict, placed=None) -> bool:
    """The player already has a modular street terminal near this planned one (export v9
    playerConstructions): the plan keeps it where it really is instead of placing a new one."""
    raw = getattr(ctx.game, "raw", None) if ctx.game is not None else None
    cons = (raw or {}).get("playerConstructions") or []
    if isinstance(cons, dict):
        cons = list(cons.values())
    used = ctx.__dict__.setdefault("_adopted", set())
    best = None
    for c in cons:
        if not str(c.get("fileName") or "").endswith("modular_terminal.con") or c.get("id") in used:
            continue
        t = c.get("transf") or []
        if isinstance(t, dict):
            t = [t[k] for k in sorted(t, key=lambda k: int(str(k).lstrip("#")))]
        if len(t) < 16:
            continue
        d = math.hypot(t[12] - it["x"], t[13] - it["y"])
        if d <= BUILT_MATCH_M and (best is None or d < best[0]):
            best = (d, c, t)
    if best is None:
        return False
    _, c, t = best
    used.add(c.get("id"))
    c0x, c0y, nx, ny, tx, ty = t[0], t[1], t[4], t[5], t[12], t[13]
    P = int((c.get("params") or {}).get("platforms") or it.get("platforms") or 1)
    x0, x1 = TERM_X.get(P, TERM_X[1])
    y0, y1 = TERM_Y
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    poly = []
    for qx, qy in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        poly += [round(tx + c0x * qx + nx * qy, 1), round(ty + c0y * qx + ny * qy, 1)]
    mx, my = tx + c0x * cx + nx * cy, ty + c0y * cx + ny * cy
    # its entrance ends at the snap node (local (0, -30)); connect that to the nearest existing road
    sx, sy = tx - nx * 30, ty - ny * 30
    ex, ey = sx, sy
    obst = _Obstacles.for_game(ctx.game)
    if obst is not None and obst.e_tree is not None:
        dd, k = obst.e_tree.query([sx, sy])
        if dd < 400:
            ex, ey = float(obst.e_xy[k, 0]), float(obst.e_xy[k, 1])
    it.update({"x": round(mx, 1), "y": round(my, 1), "h": round(math.atan2(c0y, c0x), 4), "len": x1 - x0,
               "wid": y1 - y0, "poly": poly, "built_id": c.get("id"),
               "access": [round(tx - nx * 15, 1), round(ty - ny * 15, 1), round(ex, 1), round(ey, 1)]})
    it["z"] = ctx.z(it["x"], it["y"])
    if placed is not None:
        placed.append((mx, my, it["h"], x1 - x0, y1 - y0))
    return True


WATER_MARGIN_M = 0.5     # ground this close above the water level counts as water (shore)


def _wet(ctx: "_Ctx", tx, ty, c0, c1, x0, x1, y0, y1, step: float = 4.0) -> bool:
    """Any point of the local rectangle (every `step` m) at or below the water level (+ margin)."""
    terrain = ctx.game.terrain if ctx.game is not None else None
    if terrain is None:
        return False
    import numpy as np
    from scipy import ndimage
    xs = np.arange(x0, x1 + 0.01, step)
    ys = np.arange(y0, y1 + 0.01, step)
    LX, LY = np.meshgrid(xs, ys)
    wx = tx + c0[0] * LX + c1[0] * LY
    wy = ty + c0[1] * LX + c1[1] * LY
    fy = (wy - terrain.origin_y) / terrain.step
    fx = (wx - terrain.origin_x) / terrain.step
    h = ndimage.map_coordinates(terrain.h, [fy.ravel(), fx.ravel()], order=1, mode="nearest")
    return bool((h <= terrain.water_level + WATER_MARGIN_M).any())


# Road depot / road maintenance geometry from the game's updateFn run offline (research_depots.md,
# 2026-10-05): street side is local -Y; the depot's street stub ends in its snap node at (0, -35);
# the maintenance building has no street edge (service radius), its front is at y -15.
GEOM = {
    "road_depot": {"x": (-17.73, 38.45), "y": (-20.12, 19.92), "snap": 35.0, "front": -20.26,
                   "stub": (-4.0, 4.0, -29.0, -20.0), "res": "road_depot.con", "kind": "construction",
                   "params": {"seed": 0}},
    "road_maint": {"x": (-12.5, 13.8), "y": (-15.0, 11.05), "snap": 21.0, "front": -15.0, "stub": None,
                   "res": "road_maint_station.con", "kind": "construction", "params": {"seed": 0}},
}


def place_street_terminal(ctx: "_Ctx", it: dict, polys, toward=None, placed=None, geom=None) -> bool:
    """Exact placement of a modular street terminal (or a `geom` building) next to the route: its
    entrance snap node on the road centre line, entrance facing the road, clear of industries and other
    buildings. Sets the footprint, the access stub and it["build"] (what the mod's «Построить» sends)."""
    near = _nearest_on(polys, it["x"], it["y"]) if polys else None
    if near is None:
        return False
    px, py, h, d, side = near
    if side == 0 and toward is not None:
        n2 = _nearest_on(polys, toward[0], toward[1])
        side = (n2[4] if n2 else 0) or 0
    sides = [side or 1, -(side or 1)]
    # the route piece the station belongs to: candidates move ALONG it (its own bends), never past its
    # ends (a straight tangent would leave the route, e.g. at the end of a line inside a town)
    route, s0, best_d = None, 0.0, math.inf
    for pts in polys:
        if len(pts) < 2:
            continue
        sp, dd = project_on(pts, it["x"], it["y"])
        if dd < best_d:
            route, s0, best_d = pts, sp, dd
    route_len = cumdist(route)[-1] if route else 0.0
    P = it.get("platforms") or 1
    if geom is None:
        x0, x1 = TERM_X[P]
        y0, y1 = TERM_Y
        snap, front, stub = TERM_SNAP, -15.0, (-4.0, 4.0, -24.0, -15.0)
    else:
        x0, x1 = geom["x"]
        y0, y1 = geom["y"]
        snap, front, stub = geom["snap"], geom["front"], geom["stub"]
    fp = None
    if ctx.game is not None:
        try:
            from .buildings import industry_footprints
            fp = industry_footprints(ctx.game)
            if not len(fp):
                fp = None
        except Exception:  # noqa: BLE001
            fp = None
    ux, uy = math.cos(h), math.sin(h)
    lx, ly = -uy, ux
    L, W = x1 - x0, y1 - y0
    obst = _Obstacles.for_game(ctx.game)
    # catchment: the station must not end up farther from what it serves (`toward`: the town or industry)
    # than the planned spot or its catchment radius
    reach = None
    if toward is not None:
        reach = max(math.hypot(it["x"] - toward[0], it["y"] - toward[1]), CATCHMENT_M) + 20.0
    # every candidate along the route (±180 m, both sides) that is clear of industries, roads and the
    # other buildings of the plan; houses may be demolished (paid): the cheapest of demolition +
    # SHIFT_USD_PER_M per metre moved from the planned spot (+ the far side) wins
    best = None
    for k in range(0, 31):                       # 0, ±12, ±24 … ±180 m along the route
        shift = 12.0 * ((k + 1) // 2) * (1 if k % 2 else -1) if k else 0.0
        if route is None or not (0.0 <= s0 + shift <= route_len):
            continue
        (ax, ay), _ = sub_polyline(route, s0 + shift, s0 + shift)[0], None
        # local direction of the route there
        a2 = sub_polyline(route, max(0.0, s0 + shift - 6.0), max(0.0, s0 + shift - 6.0))[0]
        b2 = sub_polyline(route, min(route_len, s0 + shift + 6.0), min(route_len, s0 + shift + 6.0))[0]
        hl = math.atan2(b2[1] - a2[1], b2[0] - a2[0]) if (b2[0] - a2[0]) ** 2 + (b2[1] - a2[1]) ** 2 > 1e-6 else h
        # where the route runs on an existing way, its drawn line is the router's approximation: anchor
        # the entrance to the REAL road geometry (export network) when one is this close
        # also a NEW segment once built (fresh export): the station attaches to the road as it really is
        real = obst.real_road(ax, ay, REAL_ROAD_SNAP_M) if obst is not None else None
        if real is not None:
            dh = abs((real[2] - hl + math.pi / 2) % math.pi - math.pi / 2)   # undirected heading difference
            if dh < math.radians(30):
                ax, ay, hl = real
        lux, luy = math.cos(hl), math.sin(hl)
        llx, lly = -luy, lux
        for si, sd in enumerate(sides):
            nx, ny = llx * sd, lly * sd                      # road -> station side
            sx, sy = ax + nx * TERM_GAP, ay + ny * TERM_GAP  # snap node
            tx, ty = sx + nx * snap, sy + ny * snap            # construction origin
            c0x, c0y = ny, -nx                               # local X (col 1 of the transf)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            mx, my = tx + c0x * cx + nx * cy, ty + c0y * cx + ny * cy
            hh = math.atan2(c0y, c0x)
            if fp is not None and not fp.clear(mx, my, math.hypot(L, W) / 2 * 0.8, margin=2.0):
                continue
            if placed and _overlaps(mx, my, hh, L, W, placed):
                continue
            if reach is not None and math.hypot(mx - toward[0], my - toward[1]) > reach:
                continue
            if _wet(ctx, tx, ty, (c0x, c0y), (nx, ny), x0 - 3, x1 + 3, -snap - 2.0, y1 + 3):
                continue                                     # body or entrance stub over water
            nb, dem = 0, 0.0
            if obst is not None:
                # body clear of every existing road/track (incl. its own route); the entrance stub
                # (local y -30..-15) may only touch a road in its last metres
                nb, ne = obst.hits(tx, ty, (c0x, c0y), (nx, ny), x0, x1, y0, y1, stub=stub)
                if ne:
                    continue
                dem = obst.last_cost
            score = dem + HOUSE_REPUTATION_USD * nb + SHIFT_USD_PER_M * abs(shift) + (SIDE_USD if si else 0.0)
            if best is None or score < best[0]:
                best = (score, nb, dem, ax, ay, nx, ny, tx, ty, c0x, c0y, mx, my, hh)
    if best is None:
        return False
    _, nb, dem, ax, ay, nx, ny, tx, ty, c0x, c0y, mx, my, hh = best
    fx, fy = tx + nx * front, ty + ny * front                # body front (entrance slot)
    poly = []
    for qx, qy in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        poly += [round(tx + c0x * qx + nx * qy, 1), round(ty + c0y * qx + ny * qy, 1)]
    it.update({"x": round(mx, 1), "y": round(my, 1), "h": round(hh, 4), "len": L, "wid": W, "poly": poly,
               "access": [round(fx, 1), round(fy, 1), round(ax, 1), round(ay, 1)]})
    it["z"] = ctx.z(it["x"], it["y"])
    if geom is None:
        it["build"] = {"kind": "street_terminal", "res": "modular_terminal.con", "platforms": P,
                       "specialization": 1, "tramTrack": 1, "cargo": it.get("cargo_station", True),
                       "t": [round(tx, 2), round(ty, 2)], "n": [round(nx, 5), round(ny, 5)],
                       "snap": TERM_SNAP, "gap": TERM_GAP}
    else:
        it["build"] = {"kind": geom["kind"], "res": geom["res"], "params": dict(geom["params"]),
                       "t": [round(tx, 2), round(ty, 2)], "n": [round(nx, 5), round(ny, 5)],
                       "snap": snap, "gap": TERM_GAP, "count": int(it.get("count") or 1)}
    if nb:
        it["build"]["demolish"] = nb
        it["build"]["demolish_usd"] = round(dem)
        it["demolish"] = nb
    if placed is not None:
        placed.append((mx, my, hh, L, W))
    return True


def _spread_copies(it: dict, n: int, polys) -> list[dict]:
    """n copies of a building item, the first at its place, the others at even distances along the
    longest route piece (anchors for place_street_terminal), named «(k/n)»."""
    route = max((p for p in polys if len(p) >= 2), key=lambda p: cumdist(p)[-1], default=None)
    out = []
    for k in range(n):
        c = dict(it)
        c["count"] = 1
        c["text"] = it["text"].replace(f" ×{n}", "") + f" ({k + 1}/{n})"
        if k and route is not None:
            L = cumdist(route)[-1]
            (x, y), _ = sub_polyline(route, L * k / n, L * k / n)[0], None
            c["x"], c["y"] = round(x, 1), round(y, 1)
            c["poly"] = rect(c["x"], c["y"], c.get("h", 0.0), c["len"], c["wid"])
        out.append(c)
    return out


def _station_item(ctx: _Ctx, s: dict, polys) -> dict | None:
    kind = s.get("kind")
    if kind == "builtin_terminal" or s.get("builtin"):
        return None
    x, y = float(s.get("x") or 0), float(s.get("y") or 0)
    h, _ = nearest_heading(x, y, polys)
    terms = int(s.get("terminals") or 1)
    if kind == "rail_station":
        k, L, W, match = "station", float(s.get("platform_m") or 120) + 20, 12.0 + 8.0 * terms, ["/stations/rail/"]
    elif kind == "road_station":
        P = terminal_platforms(terms)
        k, L, W, match = "station", TERM_X[P][1] - TERM_X[P][0], TERM_Y[1] - TERM_Y[0], ["/stations/street/", "street_station", "terminal"]
    elif kind in ("bus_stop", "cargo_stop"):
        k, L, W, match = "stop", 16.0, 8.0, ["/stations/street/", "stops"]
    elif kind == "harbor":
        k, L, W, match = "harbor", 60.0, 40.0, ["/stations/water/", "harbor"]
    else:
        k, L, W, match = "station", 40.0, 25.0, ["/stations/"]
    name = s.get("building") or kind or "станция"
    where = f" у «{s['endpoint']}»" if s.get("endpoint") else ""
    role = f" — {s['role']}" if s.get("role") else ""
    terms_ru = f", терминалов: {terms}" if kind in ("road_station", "rail_station") and terms > 1 else ""
    it = {"k": k, "name": name, "text": f"{name}{where}{role}{terms_ru}",
          "x": round(x, 1), "y": round(y, 1), "z": ctx.z(x, y), "h": round(h, 4), "len": L, "wid": W,
          "poly": rect(x, y, h, L, W), "match": match,
          # a street stop (pseudo name from the mod's STATION scan) must not count as a terminal
          "exclude": ["maint", "/depots/"] + (["_street_terminal"] if kind == "road_station" else [])}
    if kind == "road_station":
        it["platforms"] = terminal_platforms(terms)
    return it


def _depot_item(ctx: _Ctx, dp: dict, mode: str, polys) -> dict | None:
    if dp.get("free") or dp.get("x") is None:
        return None
    what = str(dp.get("what") or "")
    if "техобслуж" in what or "maint" in what.lower():
        spec, k = MAINT, "maint"
    elif mode == "rail" or "Ж/д" in what:
        spec, k = RAIL_DEPOT, "depot"
    elif mode == "water" or "Судов" in what:
        spec, k = WATER_DEPOT, "depot"
    else:
        spec, k = ROAD_DEPOT, "depot"
    x, y = float(dp["x"]), float(dp["y"])
    h, _ = nearest_heading(x, y, polys)
    n = int(dp.get("count") or 1)
    anchor = f" — {dp['anchor']}" if dp.get("anchor") else ""
    return {"k": k, "name": what, "text": what + (f" ×{n}" if n > 1 else "") + anchor,
            "x": round(x, 1), "y": round(y, 1), "z": ctx.z(x, y), "h": round(h, 4), "len": spec["len"],
            "wid": spec["wid"], "poly": rect(x, y, h, spec["len"], spec["wid"]), "match": spec["match"],
            "exclude": spec["exclude"], "count": n,
            "geom": ("road_depot" if spec is ROAD_DEPOT else
                     "road_maint" if (spec is MAINT and mode not in ("rail", "water")) else None)}


# --------------------------------------------------------------------------- bridges / tunnels on the drawn route

def water_runs(pts, terrain, step: float = WATER_SAMPLE_M, min_len: float = 6.0, pad: float = BRIDGE_PAD_M):
    """(d0, d1) distance ranges along the polyline where it crosses water (terrain below the water
    level), padded by ``pad`` metres onto land on both sides (bridge abutments)."""
    if terrain is None or len(pts) < 2:
        return []
    import numpy as np
    from scipy import ndimage
    c = cumdist(pts)
    L = c[-1]
    if L <= 0:
        return []
    n = int(L // step) + 2
    ds = np.linspace(0.0, L, n)
    P = np.asarray(pts, dtype=float)
    xs = np.interp(ds, c, P[:, 0])
    ys = np.interp(ds, c, P[:, 1])
    fy = (ys - terrain.origin_y) / terrain.step
    fx = (xs - terrain.origin_x) / terrain.step
    h = ndimage.map_coordinates(terrain.h, [fy, fx], order=1, mode="nearest")
    wet = h < terrain.water_level
    runs, i = [], 0
    while i < n:
        if not wet[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and wet[j + 1]:
            j += 1
        d0, d1 = ds[max(i - 1, 0)], ds[min(j + 1, n - 1)]   # last dry samples around the run
        if d1 - d0 >= min_len:
            runs.append((max(0.0, d0 - pad), min(L, d1 + pad)))
        i = j + 1
    merged = []
    for r in runs:                                       # islands shorter than 2 x pad -> one bridge
        if merged and r[0] <= merged[-1][1] + 2 * pad:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(r)
    return merged


def project_on(pts, x: float, y: float) -> tuple[float, float]:
    """(distance along the polyline of the nearest point, distance to it)."""
    c = cumdist(pts)
    best, s = math.inf, 0.0
    for k, (a, b) in enumerate(zip(pts[:-1], pts[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / L2))
        d = math.hypot(x - a[0] - t * dx, y - a[1] - t * dy)
        if d < best:
            best, s = d, c[k] + t * math.sqrt(L2)
    return s, best


def turn_points(pts, min_angle: float = TURN_MIN_DEG, min_gap: float = 150.0, limit: int = 8):
    """Indices of polyline vertices where the route turns by >= min_angle (merged within min_gap)."""
    c = cumdist(pts)
    cand = []
    for i in range(1, len(pts) - 1):
        a, b, d = pts[i - 1], pts[i], pts[i + 1]
        h1 = math.atan2(b[1] - a[1], b[0] - a[0])
        h2 = math.atan2(d[1] - b[1], d[0] - b[0])
        turn = abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi)
        if math.degrees(turn) >= min_angle:
            cand.append((math.degrees(turn), i))
    out = []
    for ang, i in sorted(cand, reverse=True):
        if all(abs(c[i] - c[j]) >= min_gap for j in out) and c[i] >= min_gap and c[-1] - c[i] >= min_gap:
            out.append(i)
        if len(out) >= limit:
            break
    return sorted(out)


def _km(m: float) -> str:
    return f"{m / 1000:.1f}".replace(".", ",") + " км" if m >= 1000 else f"{m:.0f} м"


def _money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " $"


WAY_RU = {"road": "дорогу", "rail": "ж/д путь"}


def build_project(step: dict, dossier: dict | None, ctx: _Ctx, order: int, loans: list | None = None) -> dict:
    mode = (dossier or {}).get("mode") or step.get("kind") or "road"
    legs = (dossier or {}).get("legs") or []
    pid = f"{step.get('line') or step['id']}:{step['id']}"
    terrain = ctx.game.terrain if ctx.game is not None else None
    way_names = {}
    if ctx.game is not None:
        way_names = {w.key: w.name for w in ctx.game.ways}
    segs = []
    totals = {"new_m": 0.0, "existing_m": 0.0, "shared_m": 0.0, "upgrade_m": 0.0}
    if step.get("action") == "upgrade_track":
        raw = [{"type": "upgrade", "polyline": step.get("geometry") or [], "way": None}]
    elif step.get("segments"):
        raw = step["segments"]
    else:
        raw = [{"type": "new", "polyline": step.get("geometry") or [], "way": None, "approx": True}]
    seg_pts = []
    for sg in raw:
        pts = thin(sg.get("polyline") or [])
        if len(pts) < 2:
            continue
        leg = sg.get("leg")
        leg_mode = legs[leg]["mode"] if isinstance(leg, int) and 0 <= leg < len(legs) else (step.get("kind") or mode)
        k = ctx.seg_kind(sg.get("way"), leg_mode)
        if k == "water":
            continue
        t = sg.get("type") or "new"
        L = cumdist(pts)[-1]
        segs.append({"t": t, "k": k, "way": sg.get("way"), "approx": bool(sg.get("approx")) or None, "p": flat(pts),
                     "len": round(L)})
        totals[f"{t}_m"] = totals.get(f"{t}_m", 0.0) + L
        seg_pts.append(pts)

    # ---------------- a loop's way back over its own new road: same road, drawn once
    news = [pts for sg, pts in zip(segs, seg_pts) if sg["t"] == "new"]
    returned_m = 0.0
    if news:
        keep_s, keep_p = [], []
        for sg, pts in zip(segs, seg_pts):
            if sg["t"] == "shared" and _near_share(pts, news, DUP_M) >= DUP_SHARE:
                returned_m += sg["len"]
                totals["shared_m"] = totals.get("shared_m", 0.0) - sg["len"]
                continue
            keep_s.append(sg)
            keep_p.append(pts)
        segs, seg_pts = keep_s, keep_p

    # ---------------- bridges / tunnels: on the drawn polylines
    spans = []
    structs = [st for leg in legs for st in ((leg.get("route") or {}).get("structures") or [])
               if st.get("type") in ("bridge", "viaduct", "tunnel")]
    bridge_info = next((st for st in structs if st.get("type") == "bridge"), None)
    for si, (sg, pts) in enumerate(zip(segs, seg_pts), start=1):
        if sg["t"] != "new":          # "shared" = the same track again (loop return): drawn once
            continue
        if terrain is not None:
            for d0, d1 in water_runs(pts, terrain):
                sp = sub_polyline(pts, d0, d1)
                spans.append({"t": "bridge", "seg": si, "d0": round(d0), "d1": round(d1), "len": round(d1 - d0),
                              "name": (bridge_info or {}).get("name") or "мост", "p": flat(sp), "water": True,
                              "key": (bridge_info or {}).get("key")})
    for st in structs:
        if st.get("x") is None or (terrain is not None and st["type"] == "bridge"):
            continue          # water bridges come from the heightmap above
        best = None
        for si, (sg, pts) in enumerate(zip(segs, seg_pts), start=1):
            if sg["t"] != "new":
                continue
            s, d = project_on(pts, float(st["x"]), float(st["y"]))
            if best is None or d < best[0]:
                best = (d, si, s, pts)
        if best is None or best[0] > 300:
            continue
        _, si, s, pts = best
        half = float(st.get("length_m") or 0) / 2
        if half <= 0:
            continue
        d0, d1 = max(0.0, s - half), min(cumdist(pts)[-1], s + half)
        if any(sp["seg"] == si and sp["d0"] < d1 and d0 < sp["d1"] for sp in spans):
            continue
        spans.append({"t": st["type"], "seg": si, "d0": round(d0), "d1": round(d1), "len": round(d1 - d0),
                      "name": st.get("name") or st.get("type_ru"), "p": flat(sub_polyline(pts, d0, d1)),
                      "key": st.get("key")})
    spans.sort(key=lambda sp: (sp["seg"], sp["d0"]))

    # ---------------- items
    items, demol = [], []
    seen_items = set()
    polys = seg_pts or [[(float(p[0]), float(p[1])) for p in ((legs[0].get("route") or {}).get("polyline") or [[0, 0], [1, 1]])]] \
        if legs else seg_pts
    stops0 = ((dossier or {}).get("tour") or {}).get("stops") or step.get("stops") or \
        [e for e in (step.get("from"), step.get("to")) if e]
    anchors = [(float(s["x"]), float(s["y"])) for s in stops0 if s.get("x") is not None]
    # rail stations sit on the track and street stops on the street; everything else goes beside
    def toward(it):
        if not anchors:
            return None
        return min(anchors, key=lambda a: math.hypot(a[0] - it["x"], a[1] - it["y"]))

    def side_kind(it):
        m = " ".join(it.get("match") or [])
        if it["k"] in ("stop", "harbor") or "/stations/rail/" in m or "/depots/water/" in m:
            return None
        return "rail" if "/depots/rail/" in m else "road"

    for leg in legs:
        route = leg.get("route") or {}
        for s in leg.get("stations") or []:
            it = _station_item(ctx, s, polys)
            if it and (it["k"], round(it["x"]), round(it["y"])) not in seen_items:
                seen_items.add((it["k"], round(it["x"]), round(it["y"])))
                items.append(it)
        for dp in leg.get("depots") or []:
            it = _depot_item(ctx, dp, leg.get("mode") or mode, polys)
            if it and (it["k"], round(it["x"]), round(it["y"])) not in seen_items:
                seen_items.add((it["k"], round(it["x"]), round(it["y"])))
                n_it = int(it.get("count") or 1)
                if n_it > 1 and it["k"] == "maint":
                    # «×N» maintenance buildings: N separate steps, spread along the route (each serves a
                    # radius, so they cover the whole line); the step is done only when each one stands
                    for i in _spread_copies(it, n_it, polys):
                        items.append(i)
                else:
                    items.append(it)
        dem = route.get("demolition") or {}
        for part in dem.get("parts") or []:
            pp = thin(part.get("polyline") or [], 0.5, 5.0)
            if len(pp) >= 2:
                demol.append({"p": flat(pp), "cost": round(float(part.get("cost") or 0)), "x": part.get("x"),
                              "y": part.get("y"), "buildings": part.get("buildings"),
                              "text": f"снос ~{float(part.get('buildings') or 0):.1f} зд. (по оценке), ≈ {_money(float(part.get('cost') or 0))}".replace(".", ",", 1)})
    for w in (dossier or {}).get("warehouses") or []:
        if w.get("x") is None:
            continue
        x, y = float(w["x"]), float(w["y"])
        h, _ = nearest_heading(x, y, seg_pts) if seg_pts else (0.0, 0.0)
        items.append({"k": "warehouse", "name": w.get("name") or "Склад",
                      "text": (w.get("name") or "Склад") + (f" — {w.get('hub')}" if w.get("hub") else ""),
                      "x": round(x, 1), "y": round(y, 1), "z": ctx.z(x, y), "h": round(h, 4),
                      "len": WAREHOUSE["len"], "wid": WAREHOUSE["wid"], "poly": rect(x, y, h, WAREHOUSE["len"], WAREHOUSE["wid"]),
                      "match": WAREHOUSE["match"], "exclude": WAREHOUSE["exclude"]})
    placed = [(it["x"], it["y"], it["h"], it["len"], it["wid"]) for it in items if side_kind(it) is None]
    # route pieces that run on existing ways (their drawn line is approximate: placement snaps to the real road)
    ctx.existing_polys = [pts for sg, pts in zip(segs, seg_pts) if sg["t"] in ("existing", "shared")]
    for it in items:
        sk = side_kind(it)
        if sk is not None and polys:
            it["cargo_station"] = step.get("project_kind") != "passenger"
            if it.get("platforms") and (adopt_built_terminal(ctx, it, placed)
                                        or place_street_terminal(ctx, it, polys, toward(it), placed)):
                continue
            g = GEOM.get(it.get("geom") or "")
            if g is not None and place_street_terminal(ctx, it, polys, toward(it), placed, geom=g):
                it["len"], it["wid"] = g["x"][1] - g["x"][0], g["y"][1] - g["y"][0]
                continue
            _place(ctx, it, polys, sk, toward(it), placed)
    for it in items:
        it["kind_ru"] = KIND_RU.get(it["k"], it["k"])
        it["label"] = LABEL.get(it["k"], "")
        it["res"], it["menu"] = _res_of(it, mode)

    # ---------------- waypoints: segment ends (junctions), bridge/tunnel ends, sharp turns
    stops = ((dossier or {}).get("tour") or {}).get("stops") or step.get("stops") or \
        [e for e in (step.get("from"), step.get("to")) if e]
    stop_pts = [(float(s["x"]), float(s["y"]), str(s.get("name") or "")) for s in stops if s.get("x") is not None]

    def place_name(x, y):
        best = None
        for it in items:
            d = math.hypot(it["x"] - x, it["y"] - y)
            if d < 250 and (best is None or d < best[0]):
                best = (d, f"{it['kind_ru']} «{it['name']}»" if it["k"] != "station" else f"станция ({it['text'].split(' — ')[0]})")
        for sx, sy, nm in stop_pts:
            d = math.hypot(sx - x, sy - y)
            if d < 500 and (best is None or d < best[0]):
                best = (d, f"«{nm}»")
        return best[1] if best else None

    waypoints = []

    def add_wp(x, y, label, seg=None, kind="turn"):
        for w in waypoints:
            if math.hypot(w["x"] - x, w["y"] - y) < 60:
                return w["n"]
        n = len(waypoints) + 1
        waypoints.append({"n": n, "x": round(x, 1), "y": round(y, 1), "z": ctx.z(x, y), "label": label,
                          "seg": seg, "kind": kind})
        return n

    seg_wps = {}
    for si, (sg, pts) in enumerate(zip(segs, seg_pts), start=1):
        if sg["t"] not in ("new", "upgrade"):
            continue
        wl = []
        a = pts[0]
        prev = segs[si - 2]["t"] if si >= 2 else None
        start_ru = ("примыкание к существующей дороге" if prev in ("existing", "shared") else "начало участка")
        wl.append(add_wp(a[0], a[1], f"{start_ru} ({a[0]:.0f}, {a[1]:.0f})" + (f", {place_name(*a)}" if place_name(*a) else ""),
                         si, "junction"))
        for i in turn_points(pts):
            p = pts[i]
            wl.append(add_wp(p[0], p[1], f"поворот ({p[0]:.0f}, {p[1]:.0f})", si, "turn"))
        for sp in spans:
            if sp["seg"] == si:
                sp_pts = list(zip(sp["p"][0::2], sp["p"][1::2]))
                what = {"bridge": "мост", "viaduct": "эстакада", "tunnel": "тоннель"}[sp["t"]]
                sp["wp"] = [add_wp(sp_pts[0][0], sp_pts[0][1], f"{what}: начало ({sp_pts[0][0]:.0f}, {sp_pts[0][1]:.0f})", si, sp["t"]),
                            add_wp(sp_pts[-1][0], sp_pts[-1][1], f"{what}: конец ({sp_pts[-1][0]:.0f}, {sp_pts[-1][1]:.0f})", si, sp["t"])]
                wl += sp["wp"]
        b = pts[-1]
        nxt = segs[si]["t"] if si < len(segs) else None
        end_ru = "примыкание к существующей дороге" if nxt in ("existing", "shared") else "конец участка"
        wl.append(add_wp(b[0], b[1], f"{end_ru} ({b[0]:.0f}, {b[1]:.0f})" + (f", {place_name(*b)}" if place_name(*b) else ""),
                         si, "junction"))
        seg_wps[si] = sorted(set(wl), key=lambda n: project_on(pts, waypoints[n - 1]["x"], waypoints[n - 1]["y"])[0])

    # ---------------- checklist (step by step)
    checklist = []

    def add(kind, text, x=None, y=None, r=300.0, ref=None, wps=None, ui=None):
        e = {"n": len(checklist) + 1, "kind": kind, "text": text, "ref": ref, "wp": wps or None, "ui": ui}
        if x is not None:
            e.update({"x": round(float(x), 1), "y": round(float(y), 1), "z": ctx.z(float(x), float(y)), "r": round(r)})
        checklist.append(e)
        return e["n"]

    # loans the plan takes right before this project (expected number of open loans after each)
    for ln in loans or []:
        add("loan", ln["text"], ref={"loans": ln["open_after"]},
            ui={"menu": "finances", "amount": ln.get("amount")})
    # the road network first, then the buildings joined to it (the user's order, 2026-10-05)
    for dm in demol:
        if dm.get("x") is not None:
            dm["step"] = add("demolition", f"Снос под трассу: {dm['text']}", dm["x"], dm["y"], 300,
                             ui={"menu": "bulldozer"})
    for si, (sg, pts) in enumerate(zip(segs, seg_pts), start=1):
        if sg["t"] not in ("new", "upgrade"):
            continue
        wname = way_names.get(sg.get("way") or "", sg.get("way") or "")
        a, b = place_name(*pts[0]), place_name(*pts[-1])
        ab = f" от {a}" if a else ""
        ab += f" до {b}" if b and b != a else ""
        wp = seg_wps.get(si) or []
        wp_ru = f"; точки {wp[0]}–{wp[-1]}" if len(wp) > 1 else ""
        mid = sub_polyline(pts, sg["len"] / 2, sg["len"] / 2)[0]
        if sg["t"] == "upgrade":
            text = f"Модернизация пути ({_km(sg['len'])}): {step.get('title') or ''}"
        else:
            text = (f"Построить {'новый ' if sg['k'] == 'rail' else 'новую '}{WAY_RU.get(sg['k'], 'путь')} "
                    f"{_km(sg['len'])}{f' ({wname})' if wname else ''}{ab}{wp_ru}"
                    + (" — трасса приблизительная" if sg.get("approx") else ""))
        sg["step"] = add("segment", text, mid[0], mid[1], min(max(sg["len"] * 0.7, 400), 8000), {"seg": si}, wp,
                         ui={"menu": "tracks" if sg["k"] == "rail" else "roads", "way": sg.get("way")})
    for sp in spans:
        pts = list(zip(sp["p"][0::2], sp["p"][1::2]))
        mid = sub_polyline(pts, sp["len"] / 2, sp["len"] / 2)[0]
        what = {"bridge": "Мост", "viaduct": "Эстакада", "tunnel": "Тоннель"}[sp["t"]]
        over = " через воду" if sp.get("water") else ""
        wp = sp.get("wp") or []
        sp["step"] = add("span", f"{what} {sp['len']} м{over} ({sp.get('name') or ''})"
                                 + (f": от точки {wp[0]} до точки {wp[1]}" if len(wp) == 2 else ""),
                         mid[0], mid[1], max(sp["len"] * 2, 250), {"seg": sp["seg"], "d0": sp["d0"], "d1": sp["d1"]}, wp,
                         ui={"menu": "tracks" if segs[sp["seg"] - 1]["k"] == "rail" else "roads"})
    shared = sum(sg["len"] for sg in segs if sg["t"] == "shared")
    existing = sum(sg["len"] for sg in segs if sg["t"] == "existing")
    if shared or existing or returned_m:
        parts = []
        if existing:
            parts.append(f"по существующим дорогам {_km(existing)} (серый пунктир)")
        if shared:
            parts.append(f"по участкам, построенным раньше для других линий плана, {_km(shared)} (тусклые синие)")
        if returned_m:
            parts.append(f"обратный путь кольца по этой же новой дороге {_km(returned_m)} (не рисуется второй раз)")
        add("info", "Строить не нужно: " + "; ".join(parts))
    for ii, it in enumerate(items, start=1):
        if it["k"] in ("station", "stop", "harbor"):
            it["step"] = add("item", f"{it['kind_ru'].capitalize()}: {it['text']}{_where(it)}", it["x"], it["y"],
                             max(it["len"], it["wid"]) * 2.5, {"item": ii},
                             ui={"menu": it["menu"], "res": it["res"],
                                 "filter": "passenger" if (step.get("project_kind") == "passenger" or it["k"] == "stop")
                                 else "cargo"})
    for ii, it in enumerate(items, start=1):
        if it["k"] not in ("station", "stop", "harbor"):
            it["step"] = add("item", f"{it['kind_ru'].capitalize()}: {it['text']}{_where(it)}", it["x"], it["y"],
                             max(it["len"], it["wid"]) * 3, {"item": ii}, ui={"menu": it["menu"], "res": it["res"]})
    veh = step.get("vehicles") or []
    if veh:
        add("vehicles", "Купить технику: " + ", ".join(f"{v.get('count', 1)}× {v.get('name') or v.get('model')}" for v in veh),
            ref={"vehicles": sum(int(v.get("count") or 1) for v in veh),
                 "models": sorted({str(v.get("model") or "") for v in veh if v.get("model")})},
            ui={"menu": "vehicles"})
    if stop_pts:
        tour = ((dossier or {}).get("tour") or {}).get("stops") or []
        acts = {str(s.get("name")): s.get("action_ru") for s in tour}
        stops_ru = " → ".join(f"{k}. {nm}" + (f" ({acts[nm]})" if acts.get(nm) else "")
                             for k, (_, _, nm) in enumerate(stop_pts, start=1))
        add("line", f"Создать линию: {stops_ru}", ref={"line": True}, ui={"menu": "lines"})

    # ---------------- framing for the camera
    xs = [x for pts in seg_pts for x, _ in pts] + [it["x"] for it in items]
    ys = [y for pts in seg_pts for _, y in pts] + [it["y"] for it in items]
    bbox = [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))] if xs else None

    seg_kind = next((s["k"] for s in segs if s["t"] == "new"), None) or (mode if mode in ("rail", "road") else "road")
    return {
        "id": pid, "line": step.get("line"), "step": step["id"], "year": step.get("year"), "month": step.get("month"),
        "order": order, "title": step.get("title") or pid, "mode": mode, "cargo": step.get("cargo"),
        "cost": round(float(step.get("cost") or 0)), "kind": step.get("action"),
        "corridor": CORRIDOR.get(seg_kind, 20.0), "bbox": bbox,
        "segments": segs, "spans": spans, "items": items, "waypoints": waypoints, "demolition": demol,
        "checklist": checklist,
        "totals": {k2: round(v) for k2, v in totals.items()},
    }


def build_overlay(plan: dict, dossiers: dict, game=None, year: int | None = None,
                  project: str | None = None) -> dict:
    ctx = _Ctx(game)
    by_step = {d.get("step_id"): d for d in (dossiers or {}).values() if isinstance(d, dict)}
    index, projects = [], {}
    order = 0
    open_loans = 0
    pending: list[dict] = []
    for st in plan.get("steps") or []:
        if st.get("action") == "take_loan":
            open_loans += 1
            ln = st.get("loan") or {}
            pending.append({"text": f"Возьмите кредит {_money(float(ln.get('amount') or 0))}: "
                                    f"{ln.get('months', '?')} мес., {float(ln.get('rate') or 0) * 100:.0f} % "
                                    f"(Финансы → Займы)", "open_after": open_loans, "amount": ln.get("amount")})
            continue
        if st.get("action") == "repay_loan":
            open_loans = max(0, open_loans - 1)
            continue
        if st.get("action") not in ("build_line", "upgrade_track"):
            continue
        order += 1
        if year is not None and st.get("year") != year:
            continue
        pid = f"{st.get('line') or st['id']}:{st['id']}"
        if project is not None and project not in (pid, st.get("line"), st["id"]):
            continue
        d = by_step.get(st["id"]) or (dossiers or {}).get(st.get("line")) if st.get("action") == "build_line" else None
        pr = build_project(st, d, ctx, order, pending)
        pending = []
        projects[pid] = pr
        index.append({k: pr[k] for k in ("id", "line", "step", "year", "month", "order", "title", "mode", "cargo", "cost", "kind")})
    return {"version": 1, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {"plan": plan.get("generated"), "projects": len(index)},
            "index": index, "projects": projects}


def default_out() -> Path:
    from .loader import USERDATA
    return USERDATA / "tf3_planner" / "plan_overlay.lua"


def blueprint_lang() -> str:
    """Language of the plan texts for the in-game blueprint (control-center setting)."""
    try:
        from .control import settings
        return settings.load().get("lang") or "ru"
    except Exception:  # noqa: BLE001
        return "ru"


def write_overlay(out: Path | None = None, year: int | None = None, project: str | None = None,
                  plan_path: Path = DATA / "plan.json", dossiers_path: Path = DATA / "dossiers.json",
                  with_terrain: bool = True) -> tuple[Path, dict]:
    """Hook for the planner / control centre: regenerate the overlay file. Returns (path, overlay)."""
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    dossiers = json.loads(Path(dossiers_path).read_text(encoding="utf-8")) if Path(dossiers_path).exists() else {}
    game = None
    if with_terrain:
        try:
            from .routing import load_game
            game = load_game()
        except Exception:  # noqa: BLE001
            game = None
    ov = build_overlay(plan, dossiers, game, year, project)
    lang = blueprint_lang()
    if lang == "en":            # plan texts in the blueprint follow the control-center language
        from . import i18n
        if game is not None:
            i18n.register_names(i18n.names_from_export(game.raw))
        ov = i18n.localize(ov, "en")
    ov["lang"] = lang
    out = Path(out) if out else default_out()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(lua_file(ov), encoding="utf-8")
    tmp.replace(out)
    return out, ov


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write the in-game blueprint overlay (plan_overlay.lua)")
    ap.add_argument("--year", type=int, default=None, help="only projects of this plan year")
    ap.add_argument("--project", default=None, help="only this project (line id, step id or 'L4:s7')")
    ap.add_argument("--out", default=None, help="output file (default <userdata>/tf3_planner/plan_overlay.lua)")
    a = ap.parse_args(argv)
    t0 = time.perf_counter()
    out, ov = write_overlay(a.out, a.year, a.project)
    size = out.stat().st_size
    print(f"{out}: {len(ov['index'])} projects, {size / 1024:.0f} KB, {time.perf_counter() - t0:.1f} s")


if __name__ == "__main__":
    main()
