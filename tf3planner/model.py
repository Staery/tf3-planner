"""Game model built from the mod export: terrain, towns, industries, cargo, infrastructure,
vehicles, loans and the economic formulas taken from the game's scripts.

Formulas (verified against game sources, see comments):
- cargo income per unit  = factor * difficultyFactor * (|d3| + 8 * max(dz, 0))   economy/cargo_income.script.lua
  factor(cargo, constantFn) = initial + (final - initial) * 0.25                    economy/cargotype.script.lua
  factor(passengers)        = 62.5e-3 * 35 * 1.5 * 1.1, times (0.5 + vehicle priceFactor)
  The distance is straight-line origin -> destination, not the route length.
- costs: the game applies the difficulty cost scales to street/bridge/tunnel/vehicle resources
  at load time (base/post_run_util.tl applyCostScale), so exported costs are final — never rescale.
- loans: flat interest amount*rate spread evenly over the months; early repayment pays only
  the remaining principal (interest repayment is commented out in loan.script.tl).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import CODE_ROOT as ROOT, data_dir as _data_dir   # path discovery: tf3planner/config.py

DATA = _data_dir()          # <project>/data, %LOCALAPPDATA%\TF3Planner\data (built app) or $TF3P_DATA

PASSENGER_FACTOR = 62.5e-3 * 35 * 1.5 * 1.1
CLIMB_WEIGHT = 8.0
LAND_USES = ("residential", "commercial", "industrial")


# --------------------------------------------------------------------------- terrain

@dataclass
class Terrain:
    origin_x: float
    origin_y: float
    step: float
    h: np.ndarray  # [iy, ix]
    water_level: float

    @property
    def nx(self) -> int:
        return self.h.shape[1]

    @property
    def ny(self) -> int:
        return self.h.shape[0]

    def height(self, x: float, y: float) -> float:
        """Bilinear height at world position."""
        fx = (x - self.origin_x) / self.step
        fy = (y - self.origin_y) / self.step
        ix = int(np.clip(math.floor(fx), 0, self.nx - 2))
        iy = int(np.clip(math.floor(fy), 0, self.ny - 2))
        tx, ty = fx - ix, fy - iy
        h = self.h
        return float((h[iy, ix] * (1 - tx) + h[iy, ix + 1] * tx) * (1 - ty)
                     + (h[iy + 1, ix] * (1 - tx) + h[iy + 1, ix + 1] * tx) * ty)

    def is_water(self, x: float, y: float) -> bool:
        return self.height(x, y) < self.water_level


# --------------------------------------------------------------------------- entities

@dataclass
class Cargo:
    id: int
    key: str            # resource stem, e.g. "logs"
    name: str           # localized
    classes: list[str]
    factor: float       # income per unit per metre
    time_to_deliver_s: float
    is_passenger: bool


@dataclass
class Town:
    id: int
    name: str
    x: float
    y: float
    z: float
    capacities: dict[str, int]                 # initial land use capacities
    needs: dict[str, list[str]]                # land use -> cargo keys the town asks for


@dataclass
class Industry:
    id: int
    name: str
    type: str          # e.g. "farm"
    file: str
    x: float
    y: float
    z: float
    level: int
    max_level: int
    on_water: bool
    inputs: list[str]
    outputs: list[str]
    recipe: dict | None
    per_cargo: dict[str, dict] = field(default_factory=dict)  # cargo key -> production stats from the game


@dataclass
class Way:
    """Track or road template."""
    key: str
    name: str
    kind: str          # "rail" | "road"
    cost_per_m: float          # final (game pre-applies difficulty scale)
    maintenance_per_m: float   # final, per year
    speed: float               # m/s
    year_from: int
    year_to: int
    max_slope: float
    min_radius: float
    electrified: bool

    def available(self, year: int) -> bool:
        return self.year_from <= year and (self.year_to == 0 or year < self.year_to)


@dataclass
class Bridge:
    key: str
    name: str
    tunnel: bool
    cost_per_m: float
    maintenance_per_m: float
    speed_limit: float
    year_from: int
    year_to: int
    carriers: tuple = ()        # game Carrier enum values: ROAD = 0, RAIL = 1 (empty: unknown)

    def available(self, year: int) -> bool:
        return self.year_from >= 0 and self.year_from <= year and (self.year_to <= 0 or year < self.year_to)


@dataclass
class LoanOffer:
    kind: str
    amount: float
    months: int
    rate: float

    @property
    def interest_total(self) -> float:
        return self.amount * self.rate

    @property
    def monthly_payment(self) -> float:
        return (self.amount + self.interest_total) / self.months

    @property
    def effective_annual_rate(self) -> float:
        """Interest per year relative to principal, assuming it is held to term or repaid early."""
        return self.rate * 12 / self.months


# --------------------------------------------------------------------------- game

class Game:
    def __init__(self, export: dict, recipes: dict):
        self.raw = export
        meta = export["meta"]
        self.year: int = meta["year"]
        self.money: float = meta.get("money", 0)
        self.year_ms: float = meta.get("yearDurationMs", 1461000)
        bc = export.get("baseConfig") or {}
        costs = bc.get("costs") or {}
        self.infra_cost_scale = costs.get("infrastructureCostScale", 1.0)
        self.infra_maint_scale = costs.get("infrastructureMaintenanceScale", 1.0)
        self.vehicle_cost_scale = costs.get("vehiclePurchaseCostScale", 1.0)
        self.vehicle_maint_scale = costs.get("vehicleMaintenanceScale", 1.0)
        econ = bc.get("economy") or {}
        self.difficulty_income = (((econ.get("cargoIncomeScript") or {}).get("params") or {})
                                  .get("difficultyFactor", 1.0))
        self.pass_difficulty_income = (((econ.get("passengerIncomeScript") or {}).get("params") or {})
                                       .get("difficultyFactor", 1.0))
        self.industry_dev = econ.get("industryDevelopment") or {}
        self.town_dev = econ.get("townDevelopment") or {}
        self.catenary_year = bc.get("trackCatenaryYearFrom", 1921)

        hm = export["heightmap"]
        self.terrain = Terrain(hm["origin"]["x"], hm["origin"]["y"], hm["step"],
                               np.array(hm["h"], dtype=np.float32).reshape(hm["ny"], hm["nx"]),
                               hm.get("waterLevel") or 0.0)

        self.cargos = self._cargos(export["cargoTypes"])
        self.cargo_by_key = {c.key: c for c in self.cargos.values()}
        self.towns = self._towns(export["towns"])
        self.industries = self._industries(export["industries"], recipes)
        self.ways = self._ways(export["streetTemplates"])
        self.bridges = self._bridges(export.get("bridgeTypes", []), False) + \
            self._bridges(export.get("tunnelTypes", []), True)
        self.loans = self._loans(export.get("loans"))

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, export_path: Path = DATA / "export.json", recipes_path: Path = DATA / "industry_recipes.json"):
        return cls(json.loads(export_path.read_text(encoding="utf-8")),
                   json.loads(recipes_path.read_text(encoding="utf-8")))

    def _cargos(self, raw) -> dict[int, Cargo]:
        out = {}
        for c in raw:
            d = c["data"]
            sr = d.get("scriptRef") or {}
            p = sr.get("params") or {}
            if c.get("isPassenger"):
                factor = PASSENGER_FACTOR
            elif "initialPrice" in p and "finalPrice" in p:
                factor = p["initialPrice"] + (p["finalPrice"] - p["initialPrice"]) * 0.25
            else:
                factor = 6.0
            out[c["id"]] = Cargo(c["id"], c["name"].rsplit("/", 1)[-1].removesuffix(".cargo"), d.get("name", ""),
                                 list(d.get("cargoClasses") or []), factor,
                                 d.get("timeToDeliverInSeconds", 0), bool(c.get("isPassenger")))
        return out

    def _z(self, pos: dict) -> float:
        return pos.get("z") if pos.get("z") is not None else self.terrain.height(pos["x"], pos["y"])

    def _towns(self, raw) -> list[Town]:
        towns = []
        for t in raw:
            pos = t.get("pos") or t.get("bboxCenter")
            caps = (t.get("town") or {}).get("initialLandUseCapacities") or t.get("initialLandUseCapacities") or [0, 0, 0]
            # town.cargoNeeds is per district (residential, commercial, industrial); the older
            # landUse2CargoNeeds list skips an empty residential entry and is shifted by one
            from .towns import town_needs
            needs = town_needs(self, t)
            if not any(needs.values()):
                needs_raw = t.get("landUse2CargoNeeds") or []
                for i, lu in enumerate(LAND_USES):
                    entry = needs_raw[i] if isinstance(needs_raw, list) and i < len(needs_raw) else []
                    needs[lu] = [n[0] for n in entry] if isinstance(entry, list) else []
            towns.append(Town(t["id"], t["name"], pos["x"], pos["y"], self.terrain.height(pos["x"], pos["y"]),
                              dict(zip(LAND_USES, caps)), needs))
        return towns

    def _industries(self, raw, recipes) -> list[Industry]:
        out = []
        for i in raw:
            pos = i["pos"]
            fn = i.get("fileName", "")
            rec = recipes.get(fn)
            ind = i.get("industry") or {}
            io = i.get("inputsOutputs") or [[], []]
            ins = [self.cargos[c].key for c in (io[0] if isinstance(io[0], list) else []) if c in self.cargos]
            outs = [self.cargos[c].key for c in (io[1] if len(io) > 1 and isinstance(io[1], list) else []) if c in self.cargos]
            if rec and not outs:
                outs = sorted({k for r in rec["rules"] for k in r["output"]})
            if rec and not ins:
                ins = [s["cargo"] for s in rec["stocks"] if s["type"] == "INPUT_STOCK"
                       and any(not r["booster"] and any(a.get(s["cargo"]) for a in r["input"]) for r in rec["rules"])]
            per_cargo = {}
            pc = i.get("perCargo")
            for cid, stats in (pc.items() if isinstance(pc, dict) else []):
                c = self.cargos.get(int(cid))
                if c:
                    per_cargo[c.key] = stats
            out.append(Industry(i["id"], i.get("name", ""), fn.rsplit("/", 1)[-1].removesuffix(".con"), fn,
                                pos["x"], pos["y"], self.terrain.height(pos["x"], pos["y"]),
                                ind.get("level", 1), ind.get("maxLevel", 1), bool(i.get("onWater")),
                                ins, outs, rec, per_cargo))
        return out

    def _ways(self, raw) -> list[Way]:
        ways = []
        for s in raw:
            name = s["name"]
            if not s.get("visible", True):
                continue
            d = s["data"]
            av = d.get("availability") or {}
            if av.get("yearFrom", 0) < 0 or av.get("yearFrom", 0) >= 100000:
                continue  # placeholders / industry-only
            is_rail = "/track/" in name
            is_road = "/street/country/" in name or "/street/highway/" in name or "/street/town/" in name
            if not (is_rail or is_road) or "entrance" in name or "/ind_" in name:
                continue
            lanes = d.get("laneConfigs") or []
            speed = max([l.get("speed", 0) for l in lanes] or [0])
            ways.append(Way(name.rsplit("/", 1)[-1].removesuffix(".street_template"),
                            (d.get("description") or {}).get("name", ""),
                            "rail" if is_rail else "road",
                            d.get("cost", 0),
                            max(0, d.get("maintenanceCost", 0)),
                            speed, av.get("yearFrom", 0), av.get("yearTo", 0),
                            d.get("maxSlope", 0.13), d.get("minCurveRadius", 40),
                            "catenary" in name))
        return ways

    def _bridges(self, raw, tunnel: bool) -> list[Bridge]:
        out = []
        for b in raw:
            d = b["data"]
            av = d.get("availability") or {}
            if "placeholder" in b["name"]:
                continue
            out.append(Bridge(b["name"].rsplit("/", 1)[-1], (d.get("description") or {}).get("name", ""), tunnel,
                              d.get("cost", 0),
                              d.get("maintenanceCost", 0),
                              d.get("speedLimit", 0), av.get("yearFrom", 0), av.get("yearTo", 0),
                              tuple(int(c) for c in (list(d.get("carriers").values()) if isinstance(d.get("carriers"), dict)
                                                     else (d.get("carriers") or [])))))
        return out

    def _loans(self, raw) -> list[LoanOffer]:
        if not raw:
            return []
        month = raw.get("monthDurationMs") or self.year_ms / 12
        offers = []
        for l in (raw.get("state") or {}).get("availableLoans") or []:
            if "amount" in l:
                offers.append(LoanOffer(l["type"], l["amount"], round(l["duration"] / month), l["percentage"]))
        return offers

    # ---------------------------------------------------------------- economics
    def income_per_unit(self, cargo: Cargo, src: tuple[float, float, float], dst: tuple[float, float, float],
                        vehicle_price_factor: float = 0.5) -> float:
        dx, dy, dz = dst[0] - src[0], dst[1] - src[1], dst[2] - src[2]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz) + CLIMB_WEIGHT * max(dz, 0.0)
        if cargo.is_passenger:
            return cargo.factor * self.pass_difficulty_income * dist * (0.5 + vehicle_price_factor)
        return cargo.factor * self.difficulty_income * dist

    def ways_available(self, kind: str, year: int | None = None) -> list[Way]:
        y = self.year if year is None else year
        return [w for w in self.ways if w.kind == kind and w.available(y)]

    def best_way(self, kind: str, year: int | None = None) -> Way | None:
        ws = self.ways_available(kind, year)
        return max(ws, key=lambda w: (w.speed, -w.cost_per_m)) if ws else None

    def cheapest_way(self, kind: str, year: int | None = None) -> Way | None:
        ws = self.ways_available(kind, year)
        return min(ws, key=lambda w: (w.cost_per_m, -w.speed)) if ws else None


if __name__ == "__main__":
    g = Game.load()
    print(f"year {g.year}, money {g.money}, terrain {g.terrain.nx}x{g.terrain.ny} @ {g.terrain.step} m")
    print(f"scales: infra x{g.infra_cost_scale}, vehicles x{g.vehicle_cost_scale}, income x{g.difficulty_income}")
    print("rail now:", [(w.key, w.cost_per_m, round(w.speed * 3.6)) for w in g.ways_available('rail')])
    print("road now:", [(w.key, w.cost_per_m, round(w.speed * 3.6)) for w in g.ways_available('road')][:6])
    print("bridges now:", [(b.key, b.cost_per_m, round(b.speed_limit * 3.6)) for b in g.bridges if b.available(g.year)])
    for l in g.loans:
        print(f"loan {l.kind}: {l.amount/1e6:.0f}M {l.months}m {l.rate:.0%} -> {l.monthly_payment:,.0f}/month, eff {l.effective_annual_rate:.2%}/yr")
