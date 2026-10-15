"""Site selection for the buildings of a route dossier (x, y + a short Russian reason).

Rules (players' practice + game mechanics, docs/mechanics.md §3, §5, §11):
- Depots / maintenance buildings: OUTSIDE built-up areas (noise and pollution hurt the town rating,
  building inside a town demolishes houses), next to the line's route, inside the maintenance
  radius of the LOADING station where the vehicles dwell (road depot 400 m, road maintenance 600 m,
  rail 800/800 m, ship 1000/1400 m), on flat dry land (ship depots: on water near the harbor).
- Cargo station at an industry: ADJACENT to the industry (never on its buildings or fields:
  industry footprints from export v8, tf3planner.buildings.IndustryFootprints; older exports assume
  a square of +-90 m), on the side facing the route, with the catchment reaching the industry.
- No building (station, depot, warehouse) is placed on any industry footprint (dry_flat checks it).
- Cargo station unloading for a town: at the town EDGE on the route side, the catchment reaching
  into the business part: centre -> route entry, at (town radius - catchment / 2) from the centre.
- Harbor: the land shore cell nearest to the ship route end (offshore industries have built-in
  ship terminals and get no harbor). Land station at a harbor hub: next to the harbor on land, so a
  warehouse fits into both catchments.
- Warehouse: inside the catchments of BOTH transfer stations, outside residential areas, flat.
- Passenger stops: inside towns (they need residents in the catchment); only moved off water.

Built-up area: with export v7 town buildings (tf3planner.townmask) a point is built-up when it lies
within BUILDING_MARGIN_M of a building plot; else with an export v6 street network (tf3planner.network)
within BUILT_BUFFER_M of a town-owned street; otherwise a disc of the town radius
10 m x sqrt(total land-use capacity) (passengers.town_infos) plus TOWN_MARGIN_M.
Flatness and water from Game.terrain (32 m height grid).

Demolition is a COST, not a ban (docs/demolition.md): every candidate site of a station / depot /
warehouse is priced by the town buildings its footprint (disc FOOTPRINT_M) would remove
(townmask.TownMask.footprint: money ~25 k$ x building capacity + reputation penalty x
RoutingParams.demolition_rating_usd) and that price competes with the other criteria (distance to the
route, slope, catchment) in money-like units (SCORE_USD $ per score point). Sites outside towns usually
win; a site that still demolishes reports it in Site.demolition_* and in its reason. Passenger stops
stand on streets and demolish nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

BUILT_BUFFER_M = 45.0         # street network mask: distance to the nearest town street sample
BUILDING_MARGIN_M = 12.0      # building mask: distance to the nearest plot (half a small depot)
SCORE_USD = 100.0             # $ of demolition per score point (1 point ~ 1 m of access road / route distance)
# footprint radius of a construction (disc), m: what it demolishes when placed in a town
FOOTPRINT_M = {"depot_road": 16.0, "depot_rail": 30.0, "depot_water": 0.0, "maint_road": 18.0,
               "maint_rail": 30.0, "station_road": 22.0, "station_rail": 45.0, "station_water": 30.0,
               "warehouse": 25.0, "hub": 22.0}
TOWN_MARGIN_M = 60.0          # disc approximation: houses stand a bit beyond the radius
MAX_GRADE = 0.06              # comfortable building ground
ROUTE_LOOKAHEAD_M = 300.0     # route direction at a line end: the route point this far along


@dataclass
class Site:
    x: float
    y: float
    reason: str
    ok: bool = True           # False: no site satisfies every rule (best effort)
    demolition_cost: float = 0.0        # $ of town buildings removed by the footprint (to add to the build cost)
    demolition_buildings: float = 0.0
    demolition_rating: float = 0.0      # town reputation penalty (sum over towns)

    def xy(self) -> list[float]:
        return [round(self.x), round(self.y)]


def _pct(x: float) -> str:
    return f"{x * 100:.0f} %"


def route_point(poly, from_end: bool = True, s: float = ROUTE_LOOKAHEAD_M) -> tuple[float, float] | None:
    """Point `s` metres along a polyline from its start (or its end)."""
    if not poly or len(poly) < 2:
        return None
    pts = list(poly) if from_end else list(poly)[::-1]
    acc = 0.0
    for a, b in zip(pts, pts[1:]):
        d = math.dist(a, b)
        if acc + d >= s and d > 0:
            t = (s - acc) / d
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        acc += d
    return tuple(pts[-1])


class Siting:
    def __init__(self, game):
        from . import network
        from .passengers import town_infos
        self.game = game
        self.t = game.terrain
        self.towns = town_infos(game)
        self.by_id = {t.id: t for t in self.towns}
        self.tc = np.array([[t.x, t.y] for t in self.towns]) if self.towns else np.zeros((0, 2))
        self.tr = np.array([t.radius for t in self.towns]) if self.towns else np.zeros(0)
        self.street_tree = None
        try:
            net = network.for_game(game)
        except Exception:                       # pragma: no cover
            net = None
        if net is not None:
            try:
                from scipy.spatial import cKDTree
                m = net.mask("road", owners="town")
                _, xs, ys = net.edge_samples(m, spacing=20.0)
                if len(xs):
                    self.street_tree = cKDTree(np.c_[xs, ys])
            except Exception:                   # pragma: no cover
                self.street_tree = None
        self.tm = None
        try:
            from . import townmask
            self.tm = townmask.for_game(game)
        except Exception:                       # pragma: no cover
            self.tm = None
        from .routing import RoutingParams
        self.rating_usd = RoutingParams().demolition_rating_usd
        self.plot_tree = None
        if self.tm is not None and self.tm.exact:
            from scipy.spatial import cKDTree
            self.plot_tree = cKDTree(self.tm.bxy)
            self.plot_rmax = float(self.tm.br.max())
        from .buildings import industry_footprints
        self.ifp = industry_footprints(game)
        self.mask_source = ("town buildings (export v7)" if self.plot_tree is not None else
                            "town streets (export v6)" if self.street_tree is not None else "town discs")

    # ------------------------------------------------------------------ terrain
    def water(self, x: float, y: float) -> bool:
        return self.t.is_water(x, y)

    def grade(self, x: float, y: float, r: float = 32.0) -> float:
        h0 = self.t.height(x, y)
        return max(abs(self.t.height(x + dx, y + dy) - h0) / r for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r)))

    def dry_flat(self, x: float, y: float, r: float = 24.0) -> bool:
        """Free land around the point: the building footprint must touch neither water nor an industry."""
        return (all(not self.water(x + dx, y + dy) for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)))
                and self.ifp.clear(x, y, r))

    def industry_gap(self, industry_id, x: float, y: float) -> float:
        """Distance from (x, y) to the footprint of one industry (inf if unknown)."""
        m = self.ifp.owner == int(industry_id) if industry_id is not None else None
        if m is None or not m.any():
            return math.inf
        return float(self.ifp.signed_distance(x, y)[m].min())

    # ------------------------------------------------------------------ towns
    def town(self, town_id) -> object | None:
        return self.by_id.get(town_id)

    def nearest_town(self, x: float, y: float):
        if not len(self.tc):
            return None, math.inf
        d = np.hypot(self.tc[:, 0] - x, self.tc[:, 1] - y)
        k = int(np.argmin(d - self.tr))
        return self.towns[k], float(d[k])

    def built_depth(self, x: float, y: float) -> float:
        """> 0: metres inside the built-up area (0 outside)."""
        if self.plot_tree is not None:
            tm = self.tm
            idx = self.plot_tree.query_ball_point([x, y], self.plot_rmax + BUILDING_MARGIN_M)
            if not idx:
                return 0.0
            idx = np.asarray(idx, np.int64)
            d = np.hypot(tm.bxy[idx, 0] - x, tm.bxy[idx, 1] - y) - tm.br[idx]
            return max(0.0, float(BUILDING_MARGIN_M - d.min()))
        if self.street_tree is not None:
            d, _ = self.street_tree.query([x, y])
            return max(0.0, BUILT_BUFFER_M - float(d))
        if not len(self.tc):
            return 0.0
        d = np.hypot(self.tc[:, 0] - x, self.tc[:, 1] - y) - self.tr - TOWN_MARGIN_M
        return max(0.0, float(-d.min()))

    def demolition(self, x: float, y: float, what: str) -> dict:
        """Town buildings removed by a construction of type ``what`` (FOOTPRINT_M key) at (x, y):
        {cost, buildings, rating, towns, score} (score = money-like penalty / SCORE_USD)."""
        r = FOOTPRINT_M.get(what, 20.0)
        if self.tm is None or r <= 0:
            return {"cost": 0.0, "buildings": 0.0, "rating": 0.0, "towns": {}, "score": 0.0}
        f = self.tm.footprint(x, y, r)
        towns = {self.tm.town_name(k): v for k, v in f["towns"].items()}
        return {"cost": f["cost"], "buildings": f["count"], "rating": f["penalty"], "towns": towns,
                "score": (f["cost"] + self.rating_usd * f["penalty"]) / SCORE_USD}

    @staticmethod
    def demolition_text(dm: dict) -> str:
        if dm["buildings"] < 0.05:
            return ""
        tw = ", ".join(f"г. {k} −{v * 100:.0f} %" for k, v in sorted(dm["towns"].items(), key=lambda kv: -kv[1])) or "—"
        money = f"{dm['cost']:,.0f}".replace(",", " ")
        return f"; СНОС ~{dm['buildings']:.0f} зд. ≈ {money} $ (добавить к стоимости), репутация: {tw}"

    def _with_demolition(self, site: "Site", dm: dict) -> "Site":
        site.demolition_cost = float(dm["cost"])
        site.demolition_buildings = float(dm["buildings"])
        site.demolition_rating = float(dm["rating"])
        site.reason += self.demolition_text(dm)
        return site

    # ------------------------------------------------------------------ helpers
    def _toward(self, cx: float, cy: float, target, dist: float, ang_off: float = 0.0) -> tuple[float, float]:
        if target is None:
            ang = 0.0
        else:
            ang = math.atan2(target[1] - cy, target[0] - cx)
        ang += ang_off
        return cx + dist * math.cos(ang), cy + dist * math.sin(ang)

    def _search(self, cx, cy, target, dists, offs, ok, what: str | None = None,
                rank_pts: float = 200.0) -> tuple[float, float, float] | None:
        """Without ``what``: first point (in order of increasing angular offset) satisfying ok(x, y).
        With it: the point minimizing rank x rank_pts + demolition score of a ``what`` footprint, i.e. the
        preferred (earlier) positions win unless they demolish town buildings."""
        best = None
        k = 0
        for off in offs:
            for sgn in ((1,) if off == 0 else (1, -1)):
                for d in dists:
                    x, y = self._toward(cx, cy, target, d, sgn * off)
                    if ok(x, y):
                        if what is None:
                            return x, y, sgn * off
                        sc = k * rank_pts + self.demolition(x, y, what)["score"]
                        if best is None or sc < best[0]:
                            best = (sc, x, y, sgn * off)
                        k += 1
        return None if best is None else best[1:]

    # ------------------------------------------------------------------ stations
    def industry_station(self, end: dict, route_pt, catchment: float, mode: str = "road") -> Site:
        """Next to the industry on the route side: the nearest free spot (no water, no industry
        buildings/fields under the station footprint, slope <= 10 %) whose catchment still reaches well
        into the industry (<= 0.6 x catchment from its footprint)."""
        cx, cy = end["x"], end["y"]
        d0 = min(0.55 * catchment, 100.0)
        what = f"station_{mode}" if f"station_{mode}" in FOOTPRINT_M else "station_road"
        r = FOOTPRINT_M[what]
        iid = end.get("id")
        own = self.industry_gap(iid, cx, cy) < math.inf

        def ok(x, y):
            if not (self.dry_flat(x, y, r) and self.grade(x, y) <= 0.10):
                return False
            if own:
                return self.industry_gap(iid, x, y) <= 0.6 * catchment
            return math.dist((x, y), (cx, cy)) <= 0.8 * catchment

        dists = tuple(float(d) for d in range(20, int(max(catchment * 1.8, 200)) + 1, 10))
        hit = self._search(cx, cy, route_pt, dists, [math.radians(a) for a in (0, 20, 40, 60, 90, 120, 150, 180)],
                           ok, what=what)
        if hit is not None:
            x, y, off = hit
            d = math.dist((x, y), (cx, cy))
            gap = self.industry_gap(iid, x, y)
            side = "со стороны трассы" if abs(off) < 0.4 else "сбоку (со стороны трассы вода/крутой склон/застройка)"
            near = (f"вплотную к территории предприятия ({max(gap - r, 0):.0f} м от края до станции), "
                    if gap < math.inf else "")
            src = "" if self.ifp.exact else " (контур предприятия приблизительный: экспорт < v8)"
            return self._with_demolition(
                Site(x, y, f"{near}{d:.0f} м от центра предприятия (охват {catchment:.0f} м), {side}{src}"),
                self.demolition(x, y, what))
        hit = self._search(cx, cy, route_pt, (d0, 0.4 * catchment, 0.7 * catchment),
                           [math.radians(a) for a in (0, 20, 40, 60, 90, 120, 150, 180)],
                           lambda x, y: self.dry_flat(x, y) and self.grade(x, y) <= 0.10, what=what)
        if hit is None:
            x, y = self._toward(cx, cy, route_pt, d0)
            return self._with_demolition(
                Site(x, y, f"в охвате предприятия ({d0:.0f} м от центра) со стороны трассы; рельеф неудобный — "
                           f"выровнять площадку", ok=False), self.demolition(x, y, what))
        x, y, off = hit
        d = math.dist((x, y), (cx, cy))
        side = "со стороны трассы" if abs(off) < 0.4 else "сбоку (со стороны трассы вода/крутой склон/застройка)"
        return self._with_demolition(
            Site(x, y, f"в охвате предприятия: {d:.0f} м от его центра (охват {catchment:.0f} м), {side}"),
            self.demolition(x, y, what))

    def town_station(self, end: dict, route_pt, catchment: float, mode: str = "road") -> Site:
        """Unloading/loading cargo station of a town: at the town edge on the route side; positions that
        demolish houses compete by price (deeper in town = more of the business part in the catchment,
        but each house costs ~25 k$ x capacity + reputation)."""
        t = self.town(end.get("id"))
        cx, cy = end["x"], end["y"]
        R = t.radius if t is not None else 300.0
        d0 = max(0.0, R - 0.5 * catchment)
        what = f"station_{mode}" if f"station_{mode}" in FOOTPRINT_M else "station_road"
        hit = self._search(cx, cy, route_pt, (d0, d0 + 0.25 * catchment, max(0.0, d0 - 0.25 * catchment),
                                              d0 + 0.5 * catchment),
                           [math.radians(a) for a in (0, 15, 30, 50, 75, 100)],
                           lambda x, y: self.dry_flat(x, y) and self.grade(x, y) <= 0.10, what=what,
                           rank_pts=100.0)
        if hit is None:
            x, y = self._toward(cx, cy, route_pt, d0)
            ok = False
        else:
            x, y, _ = hit
            ok = True
        d = math.dist((x, y), (cx, cy))
        dm = self.demolition(x, y, what)
        tail = ("станция не ломает жилую застройку" if dm["buildings"] < 0.05 else
                "без сноса подходящего места на краю нет")
        return self._with_demolition(
            Site(x, y, f"на краю города со стороны трассы: {d:.0f} м от центра при радиусе города ~{R:.0f} м — "
                       f"охват {catchment:.0f} м заходит в деловую часть, {tail}", ok=ok), dm)

    def shore(self, x: float, y: float, radius: float = 500.0) -> tuple[float, float] | None:
        """Nearest land cell (touching water) to a point."""
        t = self.t
        step = t.step
        ix0, iy0 = int(round((x - t.origin_x) / step)), int(round((y - t.origin_y) / step))
        k = int(radius // step) + 1
        best, bd = None, math.inf
        h = t.h
        for iy in range(max(1, iy0 - k), min(t.ny - 1, iy0 + k + 1)):
            for ix in range(max(1, ix0 - k), min(t.nx - 1, ix0 + k + 1)):
                if h[iy, ix] < t.water_level:
                    continue
                if not (h[iy - 1:iy + 2, ix - 1:ix + 2] < t.water_level).any():
                    continue
                px, py = t.origin_x + ix * step, t.origin_y + iy * step
                d = math.hypot(px - x, py - y)
                if d < bd:
                    best, bd = (px, py), d
        return best

    def harbor(self, end: dict, water_end, catchment: float) -> Site:
        """Harbor on the shore nearest to the ship route end, preferably inside the catchment of the endpoint."""
        wx, wy = water_end if water_end is not None else (end["x"], end["y"])
        cands = []
        for p in filter(None, (self.shore(wx, wy, 400.0), self.shore(end["x"], end["y"], 600.0))):
            cands.append((math.dist(p, (wx, wy)) + 0.7 * math.dist(p, (end["x"], end["y"])), p))
        if not cands:
            return Site(wx, wy, "берег не найден рядом с концом судового хода — ставить у воды вручную", ok=False)
        _, (x, y) = min(cands)
        dend = math.dist((x, y), (end["x"], end["y"]))
        hub = end.get("type") == "hub"
        what = "узла перевалки" if hub else f"«{end['name']}»"
        cover = ("" if hub else f"; {what} {'в охвате' if dend <= catchment else 'ВНЕ охвата — нужен склад или станция-подвоз'} "
                                f"({dend:.0f} м, охват {catchment:.0f} м)")
        return Site(x, y, f"берег, ближайший к судовому ходу ({math.dist((x, y), (wx, wy)):.0f} м до конца хода){cover}")

    def hub_land_station(self, end: dict, route_pt, catchment: float, harbor_catchment: float = 150.0) -> Site:
        """Land station at a transfer hub: next to the harbor (if the hub is at the shore), toward the route."""
        base = self.shore(end["x"], end["y"], 300.0) if self.water(end["x"], end["y"]) or \
            self.shore(end["x"], end["y"], 64.0) else None
        bx, by = base if base else (end["x"], end["y"])
        gap = min(catchment, harbor_catchment) * 0.8           # warehouse between them fits both circles
        hit = self._search(bx, by, route_pt, (gap, 0.6 * gap, 1.2 * gap),
                           [math.radians(a) for a in (0, 25, 50, 80, 110)],
                           lambda x, y: self.dry_flat(x, y) and self.grade(x, y) <= 0.10, what="hub")
        x, y = (hit[0], hit[1]) if hit else self._toward(bx, by, route_pt, gap)
        what = "от порта" if base else "от точки узла"
        return self._with_demolition(
            Site(x, y, f"узел перевалки: {math.dist((x, y), (bx, by)):.0f} м {what} в сторону трассы — склад между "
                       f"ними попадёт в охват обеих станций", ok=hit is not None), self.demolition(x, y, "hub"))

    def pax_stop(self, x: float, y: float, town_xy) -> Site:
        if not self.water(x, y):
            return Site(x, y, "в застройке города (остановке нужны жители в охвате)")
        tx, ty = town_xy
        for f in (0.8, 0.6, 0.4, 0.2, 0.0):
            px, py = tx + (x - tx) * f, ty + (y - ty) * f
            if not self.water(px, py):
                return Site(px, py, "в застройке города (сдвинута с воды к центру)")
        return Site(x, y, "в застройке города", ok=False)

    # ------------------------------------------------------------------ depots
    def depot(self, anchor: tuple[float, float], poly, radius: float, mode: str, what: str = "депо",
              avoid: list[tuple[float, float]] | None = None, anchor_ru: str = "от станции погрузки") -> Site:
        ax, ay = anchor
        avoid = avoid or []
        fp = ("maint_" if "ТО" in what else "depot_") + ("rail" if mode == "rail" else "road")
        P = np.asarray(poly, dtype=float) if poly is not None and len(poly) >= 2 else None
        cands = []
        if mode == "water":
            for rr in (0.15, 0.3, 0.5, 0.75, 0.9):
                for k in range(16):
                    a = 2 * math.pi * k / 16
                    cands.append((ax + rr * radius * math.cos(a), ay + rr * radius * math.sin(a)))
            best = None
            for x, y in cands:
                if not self.water(x, y) or any(math.dist((x, y), p) < 80 for p in avoid):
                    continue
                # ship depot: on water, open enough, near the harbor
                if not all(self.water(x + dx, y + dy) for dx, dy in ((40, 0), (-40, 0), (0, 40), (0, -40))):
                    continue
                sc = math.dist((x, y), (ax, ay))
                if best is None or sc < best[0]:
                    best = (sc, x, y)
            if best is None:
                return Site(ax, ay, f"{what}: на воде у порта (свободной воды рядом не найдено)", ok=False)
            _, x, y = best
            return Site(x, y, f"{what} на воде у порта: {math.dist((x, y), (ax, ay)):.0f} м от причала "
                              f"(радиус обслуживания {radius:.0f} м), не мешает подходу к причалу")
        if P is not None:
            seg = np.diff(P, axis=0)
            cum = np.concatenate([[0.0], np.cumsum(np.hypot(seg[:, 0], seg[:, 1]))])
            k0 = int(np.argmin(np.hypot(P[:, 0] - ax, P[:, 1] - ay)))
            s0 = cum[k0]
            for s in np.arange(max(0.0, s0 - radius), min(cum[-1], s0 + radius) + 1, 40.0):
                i = int(np.clip(np.searchsorted(cum, s) - 1, 0, len(seg) - 1))
                L = max(math.hypot(*seg[i]), 1e-6)
                t = (s - cum[i]) / L
                px, py = P[i] + seg[i] * t
                nx, ny = -seg[i][1] / L, seg[i][0] / L
                for lat in (35.0, -35.0, 70.0, -70.0):
                    cands.append((px + nx * lat, py + ny * lat))
        for rr in (0.3, 0.55, 0.8, 0.95):
            for k in range(16):
                a = 2 * math.pi * k / 16
                cands.append((ax + rr * radius * math.cos(a), ay + rr * radius * math.sin(a)))
        best = None
        for x, y in cands:
            da = math.dist((x, y), (ax, ay))
            if da > 0.97 * radius or da < 40.0 or not self.dry_flat(x, y):
                continue
            if any(math.dist((x, y), p) < 60 for p in avoid):
                continue
            g = self.grade(x, y)
            depth = self.built_depth(x, y)
            dm = self.demolition(x, y, fp)
            dr = float(np.min(np.hypot(P[:, 0] - x, P[:, 1] - y))) if P is not None else 0.0
            # demolition is priced (dm score); being next to houses (noise) costs 300 points more
            sc = ((300.0 + 2 * depth if depth > 0 else 0.0) + dm["score"] + 3000 * max(0.0, g - 0.02)
                  + 1.0 * dr + 0.15 * da)
            if best is None or sc < best[0]:
                best = (sc, x, y, g, depth, dr, da, dm)
        if best is None:
            return Site(ax, ay, f"{what}: рядом со станцией (подходящей площадки в радиусе {radius:.0f} м не найдено)",
                        ok=False)
        _, x, y, g, depth, dr, da, dm = best
        t, _ = self.nearest_town(x, y)
        where = (f"вне застройки{' г. ' + t.name if t is not None and math.dist((x, y), (t.x, t.y)) < t.radius + 800 else ''}"
                 if depth <= 0 else
                 f"в застройке г. {t.name if t else ''} (место вне застройки в радиусе {radius:.0f} м выходит дороже)")
        return self._with_demolition(
            Site(x, y, f"{what} {where}, {dr:.0f} м от трассы, {da:.0f} м {anchor_ru} "
                       f"(радиус обслуживания {radius:.0f} м), уклон рельефа ~{_pct(g)}"), dm)

    # ------------------------------------------------------------------ warehouses
    def warehouse(self, stations: list[tuple[float, float, float]]) -> Site:
        """Point inside every (x, y, catchment) circle, outside built-up areas, flat and dry."""
        if not stations:
            return Site(0.0, 0.0, "нет станций", ok=False)
        cx = float(np.mean([s[0] for s in stations]))
        cy = float(np.mean([s[1] for s in stations]))
        span = max(s[2] for s in stations)
        best = None
        for dx in np.arange(-span, span + 1, 16.0):
            for dy in np.arange(-span, span + 1, 16.0):
                x, y = cx + dx, cy + dy
                margins = [s[2] - math.dist((x, y), (s[0], s[1])) for s in stations]
                if min(margins) < 25.0 or not self.dry_flat(x, y, 20.0):
                    continue
                g = self.grade(x, y)
                depth = self.built_depth(x, y)
                dm = (self.demolition(x, y, "warehouse") if depth > 0 else
                      {"score": 0.0, "buildings": 0.0, "cost": 0.0, "rating": 0.0, "towns": {}})
                sc = ((300.0 + 2 * depth if depth > 0 else 0.0) + dm["score"] + 2000 * max(0.0, g - 0.02)
                      - 2.0 * min(margins))
                if best is None or sc < best[0]:
                    best = (sc, x, y, min(margins), depth, g, dm)
        if best is None:
            d = max(math.dist(a[:2], b[:2]) for a in stations for b in stations)
            return Site(cx, cy, f"охваты станций почти не пересекаются (станции в {d:.0f} м друг от друга) — сдвинуть "
                                f"станции ближе, склад между ними", ok=False)
        _, x, y, margin, depth, g, dm = best
        return self._with_demolition(
            Site(x, y, f"в охвате всех {len(stations)} станций узла (запас {margin:.0f} м до края охвата), "
                       f"{'вне жилой застройки' if depth <= 0 else 'в застройке — вне её выходит дороже'}, "
                       f"уклон ~{_pct(g)}"), dm)


def for_game(game) -> Siting:
    s = getattr(game, "_siting", None)
    if s is None:
        s = game._siting = Siting(game)
    return s
