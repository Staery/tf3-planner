"""Concrete transport buildings and their costs, from the exported catalogue (export v4,
`buildings` section) with fall-backs from docs/mechanics.md.

Prices in the export are final (difficulty x1.5 already applied by the game). Modular stations
are priced as the sum of their modules:
- rail cargo station: main building + per 40 m segment (cargo platform + station track)
- road cargo station: one entrance_exit module + 2 cargo platform modules per terminal (bay).
  Verified in base/content/stations/street.zip modular_street_station: createTemplateFn puts
  `cargo_platform.module` at rows j = 0 and 1 of every terminal column plus one
  `entrance_exit.module`; terminateConstructionHook makes one terminal group per column from all
  its modules (10 m each); the menu price is 6 x (2 platforms + 1 entrance) = 66 000 at scale 1
  -> 99 000 on VeryHard for 1 universal terminal (was 63 000 here: entrance + exit + 1 platform).
- harbor: [min, max] attribute of the metacon (min = one pier)
- warehouse: one storage module (500 units); specialized ones load 2x faster
Depots serve a limited number of vehicles at once (mechanics.md §5, depots/*.script.lua
`maintenancePool`): road depot 12, road maintenance building 16, rail 6 / 12, ship 8 / 12.
`maintenance_counts` + `maint_building` + `depot_price` are the single source for the plan
(economics, tours, multimodal, passengers, capacity, capacity_generic) and the dossier.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from .model import Game

SEGMENT_M = 40.0
ROAD_PLATFORMS_PER_TERMINAL = 2   # cargo/passenger platform modules per road terminal (template rows 0 and 1)
DEPOT_CAPACITY = {"road": 12, "rail": 6, "water": 8, "tram": 12}          # maintenance pool
MAINT_CAPACITY = {"road": 16, "rail": 12, "water": 12, "tram": 16}
# script prices x1.5 VeryHard (depots/road/road_maint_station.script.lua result.cost = 60000,
# rail/water_maint_station 240000). The export's menu attribute of road_maint_station.con says
# 180 000 (the era-C template value, 60000 * yearCoefficient), so it must NOT be used.
MAINT_BUILDING = {"road": (90_000, 15_000), "tram": (90_000, 15_000), "rail": (360_000, 60_000), "water": (360_000, 60_000)}
MAINT_FACTOR = 1 / 6          # upkeep per year ~ price / 6 when not exported
TRAM_DEPOT_SCRIPT = (360_000.0, 60_000.0)   # scale 1; tram depots have no maintenance building (pool 12 each)

# fall-backs (final prices, 1900) if the catalogue lacks an item
FALLBACK = {
    "rail_building": 27_000, "rail_platform": 45_000, "rail_platform_special": 90_000, "rail_track": 45_000,
    "road_entrance": 9_000, "road_platform": 45_000, "road_platform_special": 90_000,
    "harbor_small": 144_000, "road_depot": 270_000, "rail_depot": 900_000, "water_depot": 900_000,
    "warehouse": 225_000, "warehouse_special": 450_000,
}

SPECIAL_CLASSES = ("BULK", "LIQUID", "FLATBED", "GOODS")


@dataclass
class Building:
    kind: str
    name: str
    cost: float
    maintenance: float
    notes: str = ""


def _attr(entry: dict, key: str) -> float | None:
    a = ((entry.get("description") or {}).get("attributes") or {}).get(key)
    if isinstance(a, list) and a and isinstance(a[0], (int, float)) and a[0] > 0:
        return float(a[0])
    return None


def _available(entry: dict, year: int) -> bool:
    av = entry.get("availability") or {}
    yf, yt = av.get("yearFrom", 0) or 0, av.get("yearTo", 0) or 0
    return (yf <= year or yf <= 0) and (yt <= 0 or year < yt) and yf < 100000


class Catalogue:
    def __init__(self, game: Game):
        b = game.raw.get("buildings") or {}
        self.modules = b.get("modules") or []
        self.cons = b.get("constructions") or []
        self.metas = b.get("metaConstructions") or []

    @lru_cache(maxsize=None)
    def _module(self, year: int, *needles: str) -> dict | None:
        best = None
        for m in self.modules:
            n = m["name"]
            if all(s in n for s in needles) and _available(m, year) and _attr(m, "cost") is not None:
                if best is None or _attr(m, "cost") < _attr(best, "cost"):
                    best = m
        return best

    def price(self, year: int, fallback_key: str, *needles: str) -> tuple[float, float]:
        m = self._module(year, *needles)
        if m:
            c = _attr(m, "cost")
            return c, _attr(m, "maintenanceCost") or c * MAINT_FACTOR
        c = FALLBACK[fallback_key]
        return c, c * MAINT_FACTOR

    @lru_cache(maxsize=None)
    def construction(self, year: int, *needles: str) -> tuple[float, float] | None:
        for e in self.cons + self.metas:
            if all(s in e["name"] for s in needles) and _available(e, year) and _attr(e, "cost") is not None:
                c = _attr(e, "cost")
                return c, _attr(e, "maintenanceCost") or c * MAINT_FACTOR
        return None


def _special(cargo_classes: list[str]) -> str | None:
    for c in cargo_classes:
        if c in SPECIAL_CLASSES:
            return c.lower()
    return None


def station(cat: Catalogue, mode: str, year: int, cargo_classes, train_length_m: float = 80.0,
            terminals: int = 1) -> Building:
    return _station(cat, mode, year, tuple(cargo_classes), math.ceil(train_length_m / SEGMENT_M), terminals)


@lru_cache(maxsize=4096)
def _station(cat: Catalogue, mode: str, year: int, cargo_classes: tuple, segs: int, terminals: int) -> Building:
    """Cheapest adequate cargo station for one line end."""
    spec = _special(cargo_classes)
    if mode == "rail":
        segs = max(1, segs)
        b, bm = cat.price(year, "rail_building", "modular_station/main_building_1_cargo")
        # universal platform is cheaper; specialized doubles loading speed (worth it on busy lines only)
        p, pm = cat.price(year, "rail_platform", "modular_station/platform_cargo_era", ".module")
        t, tm = cat.price(year, "rail_track", "/infrastructure/track/simple/simple.street_template")
        cost = b + terminals * segs * (p + t)
        return Building("rail_station", f"Ж/д грузовая станция: {terminals}×{segs * 40:.0f} м", cost,
                        bm + terminals * segs * (pm + tm))
    if mode in ("road", "tram"):
        e, em = road_entrance(cat, year)
        p, pm = cat.price(year, "road_platform", "modular_street_station/cargo_platform.module")
        k = terminals * ROAD_PLATFORMS_PER_TERMINAL
        return Building("road_station", f"Грузовая автостанция: {terminals} терминал(а)", e + k * p, em + k * pm)
    if mode == "water":
        c = cat.construction(year, "harbor_modular_era", "small")
        cost, maint = c if c else (FALLBACK["harbor_small"], FALLBACK["harbor_small"] * MAINT_FACTOR)
        return Building("harbor", "Малый грузовой порт", cost, maint)
    return Building(mode, "?", 0.0, 0.0)


def road_entrance(cat: Catalogue, year: int) -> tuple[float, float]:
    """One combined entrance/exit module of a modular road station (the template has exactly one)."""
    m = cat._module(year, "modular_street_station/entrance_exit.module")
    if m:
        c = _attr(m, "cost")
        return c, _attr(m, "maintenanceCost") or c * MAINT_FACTOR
    return cat.price(year, "road_entrance", "modular_street_station/entrance.module")


def road_pax_station(cat: Catalogue, year: int, terminals: int = 1) -> tuple[float, float]:
    """Modular bus/tram passenger station: entrance_exit + 2 passenger platforms per terminal
    (menu: 126 000 / 21 000 at scale 1 for one terminal -> 189 000 VeryHard)."""
    e, em = road_entrance(cat, year)
    p, pm = cat.price(year, "road_platform_special", "modular_street_station/passenger_platform.module")
    k = terminals * ROAD_PLATFORMS_PER_TERMINAL
    return e + k * p, em + k * pm


def depot_price(cat: Catalogue, mode: str, year: int) -> tuple[float, float]:
    """(cost, upkeep/yr) of one depot. The export's depot attributes are script values at scale 1
    (road_depot.script: maintenanceCost 30000, cost x6), so x1.5 VeryHard is applied here."""
    if mode == "tram":
        # depots/road/tram_depot/tram_depot.script.lua: maintenanceCost 60000, cost x6 (the export's
        # menu attribute 180 000 is not the price); pool 12
        c = TRAM_DEPOT_SCRIPT
    elif mode == "road":
        c = cat.construction(year, "road_depot.con") or (FALLBACK["road_depot"] / 1.5, 0)
    elif mode == "rail":
        c = cat.construction(year, "rail_depot.con") or (FALLBACK["rail_depot"] / 1.5, 0)
    else:
        c = cat.construction(year, "water_depot.con") or (FALLBACK["water_depot"] / 1.5, 0)
    return c[0] * 1.5, (c[1] or c[0] * MAINT_FACTOR) * 1.5


def maint_building(cat: Catalogue | None, mode: str, year: int) -> tuple[float, float]:
    """(cost, upkeep/yr) of one maintenance building (script price x1.5, see MAINT_BUILDING)."""
    return MAINT_BUILDING.get(mode, MAINT_BUILDING["road"])


def maintenance_counts(mode: str, n_vehicles: int) -> tuple[int, int]:
    """(depots, maintenance buildings) that maintain n_vehicles at one place: one depot (needed to
    buy vehicles; its pool covers the first DEPOT_CAPACITY vehicles) plus maintenance buildings
    for the rest. A maintenance building has the larger pool and costs less than a depot in every
    mode, so this is also the cheapest layout (capacity.maintenance_plan agrees)."""
    n = max(0, int(n_vehicles))
    if mode == "tram":                 # passengers._depot / dossier: one tram depot per 12 trams
        return max(1, math.ceil(n / DEPOT_CAPACITY["tram"])), 0
    extra = max(0, n - DEPOT_CAPACITY.get(mode, 12))
    return 1, (math.ceil(extra / MAINT_CAPACITY.get(mode, 16)) if extra else 0)


def depot_name(mode: str, n_maint: int = 0, n_depots: int = 1) -> str:
    """'Дорожное депо + 2 здание(я) техобслуживания' (the text the plan and the dossier show)."""
    name = {"road": "Дорожное депо", "tram": "Трамвайное депо", "rail": "Ж/д депо", "water": "Судовое депо"}.get(mode, "Депо")
    if n_depots > 1:
        name = f"{n_depots}× {name}"
    if n_maint:
        name += f" + {n_maint} здание(я) техобслуживания"
    return name


@lru_cache(maxsize=4096)
def depot(cat: Catalogue, mode: str, year: int, n_vehicles: int) -> Building:
    """One depot (needed to buy vehicles) at the loading end, plus maintenance buildings for the
    vehicles beyond the depot's maintenance pool. Vehicles are maintained by proximity, so all
    buildings sit next to the station where vehicles dwell longest (loading end)."""
    cost, maint = depot_price(cat, mode, year)
    n_dep, n_maint = maintenance_counts(mode, n_vehicles)
    mc, mm = maint_building(cat, mode, year)
    return Building("depot", depot_name(mode, n_maint, n_dep), n_dep * cost + n_maint * mc, n_dep * maint + n_maint * mm,
                    "ставить у погрузочной станции: обслуживание идёт по радиусу, пока техника ждёт груз")


def warehouse(cat: Catalogue, year: int, cargo_classes: list[str]) -> Building:
    spec = _special(cargo_classes)
    if spec:
        c, m = cat.price(year, "warehouse_special", f"/warehouses/wh_{spec}.module")
        return Building("warehouse", f"Склад ({spec}), 500 ед.", c, m, "грузит в 2 раза быстрее")
    c, m = cat.price(year, "warehouse", "/warehouses/wh_universal.module")
    return Building("warehouse", "Универсальный склад, 500 ед.", c, m)


if __name__ == "__main__":
    g = Game.load()
    cat = Catalogue(g)
    for y in (1900, 1950, 1990):
        print(y)
        for mode in ("road", "rail", "water"):
            s = station(cat, mode, y, ["UNIVERSAL", "BULK"], 120)
            d = depot(cat, mode, y, 10)
            print(f"  {mode:5} {s.name:40} {s.cost:>9,.0f} (+{s.maintenance:,.0f}/y) | {d.name:18} {d.cost:>9,.0f}")
        w = warehouse(cat, y, ["UNIVERSAL", "BULK"])
        print(f"  {w.name:46} {w.cost:>9,.0f} (+{w.maintenance:,.0f}/y)")
