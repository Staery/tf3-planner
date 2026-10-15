"""Vehicle catalogue normalized from the export (base game + mods).

Carrier enum (inferred from model folders): 0 ROAD, 1 RAIL, 2 TRAM, 3 AIR, 4 WATER.
Road transportModes: [3] = bus, [4] = truck. Rail units with engine power are locomotives
(or self-propelled units when they also carry cargo), without power they are wagons.
Prices/running costs in the export are final: the game pre-applies the difficulty scales
(base/post_run_util.tl applyCostScale).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Cargo, Game

CARRIERS = {0: "road", 1: "rail", 2: "tram", 3: "air", 4: "water"}
DAYS_PER_YEAR = 365.25


@dataclass
class Vehicle:
    id: int
    file: str
    name: str
    carrier: str
    role: str                  # bus, truck, tram, loco, railcar, wagon, ship, plane, heli
    year_from: int
    year_to: int               # 0 = never retired
    price: float               # final in-game price
    running_cost: float        # per year, final
    lifespan_years: float
    top_speed: float           # m/s
    power_kw: float
    tractive_effort_kn: float
    weight_empty_t: float
    weight_max_payload_t: float
    load_speed: float
    comfort: float
    price_factor: float        # passenger ticket factor (0.5 default)
    capacity: dict[str, int] = field(default_factory=dict)   # cargo key -> max units
    multiple_unit: str | None = None
    mod: str | None = None

    def available(self, year: int) -> bool:
        return self.year_from <= year and (self.year_to == 0 or year < self.year_to)

    def carries(self, cargo_key: str) -> int:
        return self.capacity.get(cargo_key, 0)


def _accepts(cts: dict, cargo: Cargo) -> bool:
    if not isinstance(cts, dict):
        return False
    inc_cls = set(cts.get("cargoClassesIncluded") or [])
    inc_typ = {t.rsplit("/", 1)[-1].removesuffix(".cargo") for t in (cts.get("cargoTypesIncluded") or [])}
    exc_cls = set(cts.get("cargoClassesExcluded") or [])
    exc_typ = {t.rsplit("/", 1)[-1].removesuffix(".cargo") for t in (cts.get("cargoTypesExcluded") or [])}
    if cargo.key in exc_typ or exc_cls & set(cargo.classes):
        return False
    return cargo.key in inc_typ or bool(inc_cls & set(cargo.classes))


def _as_list(x) -> list:
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return [x[k] for k in sorted(x, key=lambda k: int(k) if str(k).isdigit() else 0)]
    return []


def _capacities(tv: dict, cargos: list[Cargo]) -> dict[str, int]:
    cap: dict[str, int] = {}
    for comp in _as_list(tv.get("compartments")):
        best: dict[str, int] = {}
        for lc in _as_list(comp.get("loadConfigs")):
            ce = lc.get("cargoEntry") or {}
            n = ce.get("capacity", 0) or 0
            for c in cargos:
                if _accepts(ce.get("cargoTypeSet") or {}, c):
                    best[c.key] = max(best.get(c.key, 0), n)
        for k, n in best.items():
            cap[k] = cap.get(k, 0) + n
    return cap


def load_vehicles(game: Game) -> list[Vehicle]:
    cargos = list(game.cargos.values())
    mu_of: dict[str, str] = {}
    for mu in game.raw.get("multipleUnits", []):
        for v in _as_list((mu.get("data") or {}).get("vehicles")):
            mu_of[v.get("name", "")] = mu["name"]
    out = []
    for raw in game.raw["vehicles"]:
        md = raw.get("metadata")
        name = raw["name"]
        if not isinstance(md, dict) or "placeholders/" in name or not raw.get("visible", True):
            continue
        tv = md.get("transportVehicle") or {}
        av = md.get("availability") or {}
        cost = md.get("cost") or {}
        mt = md.get("maintenance") or {}
        lv = md.get("landVehicle") or {}
        wv = md.get("waterVehicle") or {}
        aiv = md.get("airVehicle") or {}
        body = lv or wv or aiv
        engines = _as_list(lv.get("engines"))
        # land engines are in kW; waterVehicle.availPower is in W
        power = sum(e.get("power", 0) for e in engines) or (wv.get("availPower", 0) or 0) / 1000.0
        te = sum(e.get("tractiveEffort", 0) for e in engines)
        carrier = CARRIERS.get(tv.get("carrier"), "?")
        cap = _capacities(tv, cargos)
        modes = _as_list(tv.get("transportModes"))
        if carrier == "road":
            role = "bus" if 3 in modes else "truck"
        elif carrier == "rail":
            role = ("railcar" if cap else "loco") if power > 0 else "wagon"
        elif carrier == "tram":
            role = "tram"
        elif carrier == "water":
            role = "ship"
        elif carrier == "air":
            role = "heli" if aiv.get("isHelicopter") else "plane"
        else:
            role = "?"
        out.append(Vehicle(
            id=raw["id"], file=name, name=(md.get("description") or {}).get("name", name),
            carrier=carrier, role=role,
            year_from=av.get("yearFrom", 0), year_to=av.get("yearTo", 0),
            price=cost.get("price", 0) or 0,
            running_cost=mt.get("runningCosts", 0) or 0,
            lifespan_years=(mt.get("lifespan", 0) or 0) / DAYS_PER_YEAR,
            top_speed=body.get("topSpeed", 0) or 0,
            power_kw=power, tractive_effort_kn=te,
            weight_empty_t=(body.get("weightEmpty", 0) or 0) / 1000,
            weight_max_payload_t=(body.get("weightMaxPayload", 0) or 0) / 1000,
            load_speed=tv.get("loadSpeed", 1) or 1,
            comfort=tv.get("comfortFactor", 0.5) or 0.5,
            price_factor=tv.get("priceFactor", 0.5) if tv.get("priceFactor") is not None else 0.5,
            capacity=cap,
            multiple_unit=mu_of.get(name),
            mod=None if name.startswith("::") else name.split("::")[0],
        ))
    return out


def available(vehicles: list[Vehicle], year: int, carrier: str | None = None, role: str | None = None) -> list[Vehicle]:
    return [v for v in vehicles if v.available(year) and (carrier is None or v.carrier == carrier)
            and (role is None or v.role == role)]


if __name__ == "__main__":
    g = Game.load()
    vs = load_vehicles(g)
    print(f"{len(vs)} vehicles; available in {g.year}:")
    for v in sorted(available(vs, g.year), key=lambda v: (v.carrier, v.role, v.price)):
        cap = ", ".join(f"{k}:{n}" for k, n in sorted(v.capacity.items())[:6])
        if len(v.capacity) > 6:
            cap += f" (+{len(v.capacity) - 6})"
        print(f"  {v.carrier:5} {v.role:7} {v.name[:34]:34} {v.year_from}-{v.year_to or '':4} "
              f"price {v.price:>10,.0f} run {v.running_cost:>9,.0f}/y life {v.lifespan_years:4.0f}y "
              f"{v.top_speed * 3.6:4.0f}km/h {v.power_kw:5.0f}kW | {cap}")
