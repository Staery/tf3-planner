"""Route dossiers: a complete, checkable breakdown of every line the plan builds.

For one plan line (a ``build_line`` step of plan.json, a planner.Candidate, an
economics.LinkEval or a candidates.ProjectCandidate) the dossier lists where everything goes,
what it costs and how the vehicles actually run:

- route: length, straight distance, detour, grades, elevation, bridges/tunnels (where, how long,
  type, cost, speed limit), track/road type and price, build cost breakdown, upkeep;
- stations at both ends (building, platform length / terminals, location, catchment, cost),
  depot + maintenance buildings (pool sizes, radii, placed at the loading end);
- vehicles and the consist (loco + wagons, length, mass loaded/empty, kW/t, tractive effort),
  performance on flat / 3.75 % / 7.5 % (like the in-game vehicle shop), steepest workable grade;
- run timing from dynamics.py (acceleration, braking, grades, curves, bridge limits), both on the
  recommended ruling grade and on a terrain-following track, loading/unloading, cycle, trips/year;
- capacity vs volume, load factor, terminals, single-track passing loops (capacity.py);
- money: income per trip / per year (chord rule), running, upkeep, profit, payback, NPV;
  delivery-time check; step-by-step build instructions in Russian.

Project kinds (``project_kind`` of the plan step, see ``_step_kind``):
- cargo       one producer -> one consumer line;
- tour        multi-stop cargo line from tours.py (milk run / circuit, ``stops`` + per-segment ``legs``):
              one consist runs the whole cycle; every stop with its load/unload per cargo;
- multimodal  chain feeder -> warehouse hub -> trunk (-> warehouse -> final) from multimodal.py:
              one dossier per leg, ports (none at offshore industries: built-in ship terminals),
              the warehouse(s) at the transfer hub(s) sized >= 2x the largest consist calling there;
- passenger   town loops (small stops spread over the ring), intercity bus/train lines and bundles
              of both (passengers.py), passenger buildings and vehicles, headway, tickets;
- workers     town stop -> industry stop, with the 10-workers-in-2-years production boost rule;
- industry_build  resource prospecting (efficiency.py): rank permit, expected industry, first line.

CLI:  python -m tf3planner.dossier [--line L7|s31] [--plan data/plan.json] [--out data/dossiers.json]
Output keys are line ids; a repeated line id (older plans) gets "<line>@<step id>"; every dossier
carries ``step_id`` (the web map indexes dossiers by it).

Conventions: money per game year, game seconds (1 year = game.year_ms / 1000 s, 1 day = 4 s),
prices final (difficulty already applied). Texts for the user are Russian.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from . import dynamics as D
from . import facilities
from . import siting as SIT
from .model import DATA, Game

try:                                   # optional layers owned by other modules
    from . import capacity as CAP
except Exception:                      # pragma: no cover
    CAP = None

MODE_RU = {"rail": "Ж/д", "road": "Автодорога", "water": "Водный путь", "tram": "Трамвай", "air": "Авиа",
           "multi": "Мультимодально"}
ROLE_RU = {"line": "Линия", "feeder": "Подвоз", "trunk": "Магистраль", "final": "Развоз",
           "loop": "Городское кольцо", "intercity": "Междугородняя линия", "workers": "Рабочие",
           "first_line": "Первая линия", "tour": "Маршрут"}
KIND_RU = {"cargo": "Грузовая линия", "tour": "Многоостановочный грузовой маршрут",
           "multimodal": "Мультимодальная цепочка через склад", "passenger": "Пассажирская линия",
           "workers": "Линия для рабочих", "industry_build": "Разведка ресурса"}
CATCHMENT_M = {"rail": 150.0, "road": 175.0, "tram": 175.0, "water": 150.0, "air": 250.0}
SMALL_STOP_CATCHMENT = 160.0           # small street stop (mechanics.md 11.2)
# Unloading into a town needs no modular terminal: the game's street stops (small_old/mid/new.con)
# take PASSENGERS and UNIVERSAL cargo (export: cargoClassesIncluded), stand on an existing street
# (no plot, no demolition) and cost a fraction. Used for unload-only town ends up to this volume.
CARGO_STOP_MAX_PER_YEAR = 600.0
CARGO_STOP_SEARCH_M = 350.0            # look for the best-covering street point this far from the route end
RAIL_PAX_CATCHMENT = 150.0
# mode -> (depot pool, depot radius, maintenance building pool, radius)  (docs/mechanics.md §5)
MAINT = {"rail": (6, 800.0, 12, 800.0), "road": (12, 400.0, 16, 600.0), "tram": (12, 400.0, 16, 600.0),
         "water": (8, 1000.0, 12, 1400.0)}
BED_WIDTH = {"rail": 6.0, "road": 8.0, "tram": 8.0}
DAY_S = 4.0                            # millisPerDay 4000
NPV_YEARS, DISCOUNT = 15, 0.05
KEEP_SPEED_FRACTION = 0.4              # ruling grade: loaded consist keeps >= 40 % of top speed
MIN_RULING_GRADE = 0.005
PLANNER_SPEED_FACTOR = 0.85            # economics.evaluate_link
STOP_DELAYS_S = 4.0                    # arrival + departure delay per stop
MANOEUVRE_S = {"road": 6.0, "tram": 6.0, "rail": 20.0, "water": 40.0, "air": 60.0}
CHART_POINTS = 240
LOAD_FACTOR_DEFAULT = 0.0625
TOWN_STOP_SPACING_M = 350.0
STREET_MAX_GRADE = 0.08                # buses/trams on existing streets: simulated grade cap            # intercity bus: extra stops in a town this far apart along the route
RIDE_DETOUR_TOWN = 1.5                 # passengers.PassengerParams.ride_detour_town
MIN_WORKERS = 10                       # distinct residents arriving by line within 2 years (mechanics.md 8)
CLASS_RU = {"liquid": "жидкие грузы", "bulk": "сыпучие грузы", "flatbed": "негабаритные грузы", "goods": "товары"}
LOAD_RU = {"full": "«полная загрузка» (FULL_LOAD_ANY)", "if_available": "«загружать, если есть» (LOAD_IF_AVAILABLE)"}


# --------------------------------------------------------------------------- formatting

def _m(x: float) -> str:
    """Money / big number with thin-space thousands."""
    if x is None or not np.isfinite(x):
        return "—"
    return f"{x:,.0f}".replace(",", " ")


def _km(m: float) -> str:
    return f"{m / 1000:.1f}".replace(".", ",") + " км"


def _pct(x: float, d: int = 1) -> str:
    return f"{x * 100:.{d}f}".replace(".", ",") + " %"


def _kmh(v: float) -> str:
    return f"{v * 3.6:.0f} км/ч"


def _s(t: float) -> str:
    """Seconds with game days."""
    if t is None or not np.isfinite(t):
        return "—"
    return f"{t:,.0f}".replace(",", " ") + f" с (~{t / DAY_S:,.0f} дн.)".replace(",", " ")


def _clean(o):
    """JSON-safe: numpy scalars -> python, inf/nan -> None, tuples -> lists."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return round(f, 4) if math.isfinite(f) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    return o


def _decimate(pts: list, n: int = CHART_POINTS) -> list:
    if len(pts) <= n:
        return pts
    idx = np.linspace(0, len(pts) - 1, n).round().astype(int)
    return [pts[i] for i in idx]


# --------------------------------------------------------------------------- text parsing (plan details)

_NUM = r"(\d[\d   ]*(?:[.,]\d+)?)"


def _to_num(s: str) -> float:
    return float(re.sub(r"[\s  ]", "", s).replace(",", "."))


def _re_num(pattern: str, text: str | None, default=None):
    m = re.search(pattern, text or "")
    if not m:
        return default
    try:
        return _to_num(m.group(1))
    except ValueError:
        return default


def _strip_title(t: str | None) -> str:
    return re.sub(r"^(?:Охват:\s*)+", "", t or "")


def _short(name: str) -> str:
    return name.replace(" — ", ": ")


# --------------------------------------------------------------------------- context

class Context:
    """Game data, routing matrices, vehicle catalogue; reuses a Planner's objects when given."""

    def __init__(self, game: Game | None = None, planner=None):
        from .routing import load_game, network_nodes
        from .vehicles import load_vehicles
        self.planner = planner
        self.game = game or getattr(planner, "game", None) or load_game()
        g = self.game
        self.vehicles = getattr(planner, "vehicles", None) or load_vehicles(g)
        self.nodes = getattr(planner, "nodes", None) or network_nodes(g)
        self.by_key = {n.key: n for n in self.nodes}
        self.by_name = {}
        for n in self.nodes:
            self.by_name.setdefault(n.name, n)
            self.by_name.setdefault(_short(n.name), n)
        self._mat = dict(getattr(planner, "mat", None) or {})
        self._water = getattr(planner, "water", None)
        cat = getattr(g, "_catalogue", None)
        if cat is None:
            cat = g._catalogue = facilities.Catalogue(g)
        self.cat = cat
        self.year_s = g.year_ms / 1000.0
        self.frictions = {}
        for raw in g.raw.get("vehicles", []):
            lv = (raw.get("metadata") or {}).get("landVehicle") if isinstance(raw.get("metadata"), dict) else None
            if isinstance(lv, dict) and lv.get("friction") is not None:
                self.frictions[raw["id"]] = float(lv["friction"])
        self.by_id = {v.id: v for v in self.vehicles}
        self.by_file: dict[str, list] = {}
        for v in self.vehicles:
            self.by_file.setdefault(v.file.rsplit("/", 1)[-1], []).append(v)
        self.cargo_by_name = {c.name.lower(): c for c in g.cargo_by_key.values()}
        self.siting = SIT.for_game(g)

    # ---- routing
    def mat(self, mode: str):
        if mode not in self._mat:
            from .routing import network_matrix
            self._mat[mode] = network_matrix(self.game, mode, self.game.year)
        return self._mat[mode]

    def water(self):
        if self._water is None:
            from .routing import water_network
            self._water = water_network(self.game, self.game.year, port_radius=1500.0)
        return self._water

    def route_planner(self, mode: str):
        from .routing import get_planner
        m = self.mat(mode)
        return get_planner(self.game, m.kind, m.year, m.params)

    # ---- entities
    def node(self, ref):
        """Node from a Node, node index, endpoint dict {type, id}, Endpoint or key string."""
        if ref is None:
            return None
        if hasattr(ref, "index") and hasattr(ref, "key"):
            return ref
        if isinstance(ref, (bool, np.bool_)):
            return None
        if isinstance(ref, (int, np.integer)):
            return self.nodes[int(ref)] if 0 <= int(ref) < len(self.nodes) else None
        if isinstance(ref, str):
            return self.by_key.get(ref)
        if isinstance(ref, dict):
            t, i = ref.get("type") or ref.get("kind"), ref.get("id")
            if ref.get("key") is not None and not isinstance(ref.get("key"), str):
                n = self.node(ref["key"])
                if n is not None and ref.get("name") and n.name != ref["name"]:
                    n = self.by_name.get(ref["name"], n)       # node lists of another export: trust the name
                return n
            if isinstance(ref.get("key"), str) and ref["key"] in self.by_key:
                return self.by_key[ref["key"]]
            if t in ("town", "industry") and i is not None:
                return self.by_key.get(f"{t}:{i}")
            return None
        kind, i = getattr(ref, "kind", None), getattr(ref, "id", None)
        if kind in ("town", "industry"):
            return self.by_key.get(f"{kind}:{i}")
        return None

    def vehicle(self, model: str | None = None, name: str | None = None, vid: int | None = None):
        if vid is not None and vid in self.by_id:
            return self.by_id[vid]
        cands = self.by_file.get((model or "").rsplit("/", 1)[-1], [])
        if name:
            exact = [v for v in cands if v.name == name]
            if exact:
                return exact[0]
        if cands:
            return cands[0]
        if name:
            for v in self.vehicles:
                if v.name == name:
                    return v
        return None

    def nearest_town(self, x: float, y: float) -> tuple[str, float]:
        best = min(self.game.towns, key=lambda t: (t.x - x) ** 2 + (t.y - y) ** 2, default=None)
        return (best.name, math.dist((best.x, best.y), (x, y))) if best else ("", 0.0)

    def place(self, x: float, y: float) -> str:
        n, d = self.nearest_town(x, y)
        return f"у г. {n}" if d < 1500 else f"{_km(d)} от г. {n}" if n else ""

    def cargo_key(self, name: str) -> str | None:
        c = self.cargo_by_name.get((name or "").strip().lower())
        return c.key if c else None


def _endpoint_dict(ctx: Context, node=None, raw: dict | None = None) -> dict:
    raw = raw or {}
    if node is not None:
        return {"name": node.name, "kind": node.kind, "id": node.id, "node": node.index, "key": node.key,
                "x": float(node.x), "y": float(node.y), "z": float(node.z), "type": getattr(node, "type", node.kind),
                "water_only": bool(getattr(node, "water_only", False))}
    x, y = float(raw.get("x", 0.0)), float(raw.get("y", 0.0))
    key = raw.get("key")
    return {"name": raw.get("name", "точка"), "kind": raw.get("type", "point"), "id": raw.get("id"), "node": None,
            "key": key if isinstance(key, str) else None,
            "x": x, "y": y, "z": float(raw.get("z", ctx.game.terrain.height(x, y))),
            "type": "hub" if isinstance(key, str) and key.startswith("hub:") else "point", "water_only": False}


# --------------------------------------------------------------------------- input normalization

class Leg(SimpleNamespace):
    """One line to analyse. mode, cargo, src, dst (endpoint dicts), units (per consist), n_consists,
    volume, role, route_free_by, station_free_by (pair), plan (dict), geometry, stops, loop and the
    optional fields of LEG_DEFAULTS (pax kind, built-in terminals, tour data, parsed plan info)."""


LEG_DEFAULTS = dict(
    mode="road", cargo=None, src=None, dst=None, units=(), n_consists=1, volume=None, role="line",
    route_free_by=None, station_free_by=(None, None), plan=None, geometry=None, stops=0, loop=False,
    income_override=None,
    pax=None,                 # None (cargo) | "loop" | "intercity" | "workers"
    title=None,               # plan title of this part (passenger bundles, multimodal legs)
    builtin=(False, False),   # end has a built-in ship terminal (offshore industry): no harbor
    depot_end=0,              # which end gets the depot (feeders: the hub)
    prefer_geometry=False,    # use the plan geometry as route (multimodal ship legs)
    stops_per_town=1,
    route_cost_override=None,  # (cost, maintenance) e.g. tram lanes of a town loop
    info=None,                # parsed facts from the plan text
    streets=None,             # passengers on existing streets (passengers.StreetLine.pax_json + segments)
    tour=None,                # tour data (tours.py lines)
    estimate=False,           # source location is an estimate (prospecting)
)


def _mkleg(**kw) -> Leg:
    d = {k: (dict(v) if isinstance(v, dict) else v) for k, v in LEG_DEFAULTS.items()}
    d.update(kw)
    d["plan"] = d["plan"] or {}
    d["info"] = d["info"] or {}
    d["units"] = list(d["units"] or [])
    return Leg(**d)


def _consist_from_totals(totals: list[tuple], n: int | None, mode: str, details: str = "") -> tuple[list, int]:
    """Per-consist units from total vehicle counts and the number of consists."""
    if not totals:
        return [], 0
    if n is None and details:
        m = re.search(r"Состав:\s*(\d+)\s*[×x]", details)
        n = int(m.group(1)) if m else None
    if n is None:
        if mode == "rail":
            n = sum(c for v, c in totals if v.role in ("loco", "railcar")) or 1
        else:
            n = sum(c for _, c in totals)
    n = max(1, int(n))
    return [(v, max(1, round(c / n))) for v, c in totals], n


def _parse_volume(details: str) -> float | None:
    m = re.match(r"\s*([\d\s ]+?)\s*ед\./год", details or "")
    return float(m.group(1).replace(" ", "").replace(" ", "")) if m else None


def _totals(ctx: Context, vehicles: list[dict] | None) -> list[tuple]:
    out = []
    for v in vehicles or []:
        veh = ctx.vehicle(v.get("model") or v.get("file"), v.get("name"), v.get("vehicle_id"))
        if veh is not None:
            out.append((veh, int(v.get("count", 1))))
    return out


def _step_kind(step: dict) -> str:
    """cargo | tour | multimodal | passenger | workers | industry_build."""
    pk = step.get("project_kind")
    legs = step.get("legs") or []
    first = legs[0] if legs and isinstance(legs[0], dict) else {}
    if pk == "industry_build":
        return "industry_build"
    if pk == "multimodal" or "role" in first:
        return "multimodal"
    if pk == "workers":
        return "workers"
    if pk == "passenger" or step.get("cargo") == "passengers":
        return "passenger"
    title = _strip_title(step.get("title"))
    if (len(step.get("stops") or []) > 2 or isinstance(first.get("from"), (int, np.integer))
            or title.startswith(("Кольцевой маршрут", "Сборный маршрут", "Сборный рейс"))):
        return "tour" if legs else "cargo"
    return "cargo"


# ---- infrastructure built by earlier plan steps (shared routes, stations)

def _key_of(ctx: Context, ref) -> str | None:
    if isinstance(ref, str) and ref.startswith("hub:"):
        return ref
    if isinstance(ref, dict) and isinstance(ref.get("key"), str) and ref["key"].startswith("hub:"):
        return ref["key"]
    n = ctx.node(ref)
    return n.key if n is not None else None


def _infra_of_step(ctx: Context, s: dict) -> tuple[list, list]:
    """(route pairs [(key_a, key_b, mode)], stations [(key, mode, passenger?)]) built by a step."""
    kind = _step_kind(s)
    pairs, sts = [], []
    if kind == "industry_build":
        return pairs, sts
    if kind in ("multimodal", "tour"):
        for l in s.get("legs") or []:
            if kind == "multimodal":
                a, b, m = _key_of(ctx, l.get("from")), _key_of(ctx, l.get("to")), l.get("mode")
            else:
                a, b, m = _key_of(ctx, l.get("from")), _key_of(ctx, l.get("to")), s.get("kind")
            if a and b and m != "water":
                pairs.append((a, b, m))
            for k in (a, b):
                if k:
                    sts.append((k, m, False))
        return pairs, sts
    if kind in ("passenger", "workers"):
        parts = s.get("legs") if s.get("legs") else [{"mode": s.get("kind"), "nodes": None}]
        for p in parts:
            m = p.get("mode") or s.get("kind")
            if p.get("nodes") is not None:
                keys = [ctx.nodes[i].key for i in p["nodes"] if isinstance(i, int) and 0 <= i < len(ctx.nodes)]
            else:
                keys = [k for k in (_key_of(ctx, s.get("from")), _key_of(ctx, s.get("to"))) if k]
            for k in keys:
                sts.append((k, m, True))
            if len(keys) == 2 and m in ("rail", "road"):
                pairs.append((keys[0], keys[1], m))
        return pairs, sts
    a, b, m = _key_of(ctx, s.get("from")), _key_of(ctx, s.get("to")), s.get("kind")
    if a and b and m != "water":
        pairs.append((a, b, m))
    for k in (a, b):
        if k:
            sts.append((k, m, False))
    return pairs, sts


def _prior(ctx: Context, steps: list[dict] | None, step) -> dict:
    """Routes and stations built by build_line steps before `step` -> line id that built them."""
    P = {"pairs": {}, "stations": {}}
    sid = step.get("id") if isinstance(step, dict) else None
    for s in steps or []:
        if s is step or (sid is not None and s.get("id") == sid):
            break
        if s.get("action") != "build_line":
            continue
        lid = str(s.get("line") or s.get("id"))
        try:
            pairs, sts = _infra_of_step(ctx, s)
        except Exception:                            # pragma: no cover - malformed old step
            continue
        for a, b, m in pairs:
            P["pairs"].setdefault((frozenset((a, b)), m), lid)
        for k, m, px in sts:
            P["stations"].setdefault((k, m, px), lid)
    return P


def _pair_free(prior: dict, a: dict, b: dict, mode: str) -> str | None:
    if mode == "water" or not a.get("key") or not b.get("key"):
        return None
    return prior["pairs"].get((frozenset((a["key"], b["key"])), mode))


def _station_free(prior: dict, end: dict, mode: str, pax: bool = False) -> str | None:
    return prior["stations"].get((end.get("key"), mode, pax)) if end.get("key") else None


def _plan_of(step: dict) -> dict:
    return {"cost": step.get("cost"), "income_per_year": step.get("income_per_year"),
            "running_cost_per_year": step.get("running_cost_per_year"), "roi_years": step.get("roi_years"),
            "volume": _parse_volume(step.get("details", "")), "length_m": step.get("length_m")}


# ---- cargo (single link)

def _legs_cargo(ctx: Context, step: dict, prior: dict) -> list[Leg]:
    mode = step.get("kind") or "road"
    a, b = ctx.node(step.get("from")), ctx.node(step.get("to"))
    units, n = _consist_from_totals(_totals(ctx, step.get("vehicles")), None, mode, step.get("details", ""))
    src, dst = _endpoint_dict(ctx, a, step.get("from")), _endpoint_dict(ctx, b, step.get("to"))
    plan = _plan_of(step)
    builtin = (mode == "water" and src["water_only"], mode == "water" and dst["water_only"])
    return [_mkleg(mode=mode, cargo=step.get("cargo"), src=src, dst=dst, units=units, n_consists=n,
                   volume=plan["volume"], route_free_by=_pair_free(prior, src, dst, mode),
                   station_free_by=(_station_free(prior, src, mode), _station_free(prior, dst, mode)),
                   plan=plan, geometry=step.get("geometry"), builtin=builtin)]


# ---- multimodal chains

def _legs_multimodal(ctx: Context, step: dict, prior: dict) -> list[Leg]:
    legs: list[Leg] = []
    used: dict[tuple, str] = {}
    for k, l in enumerate(step.get("legs") or []):
        mode = l.get("mode", "road")
        fa, fb = l.get("from") or {}, l.get("to") or {}
        a = None if isinstance(fa.get("key"), str) and fa["key"].startswith("hub:") else ctx.node(fa)
        b = None if isinstance(fb.get("key"), str) and fb["key"].startswith("hub:") else ctx.node(fb)
        src, dst = _endpoint_dict(ctx, a, fa), _endpoint_dict(ctx, b, fb)
        if src["key"] is None and isinstance(fa.get("key"), str):
            src["key"] = fa["key"]
        if dst["key"] is None and isinstance(fb.get("key"), str):
            dst["key"] = fb["key"]
        units, n = _consist_from_totals(_totals(ctx, l.get("vehicles")), l.get("n_consists"), mode)
        role = l.get("role", "line")
        how = {"water": "судном", "road": "грузовиками", "rail": "по ж/д"}.get(mode, mode)
        a_txt = "склад узла" if src.get("type") == "hub" else src["name"]
        b_txt = "склад узла" if dst.get("type") == "hub" else dst["name"]
        label = f"{ROLE_RU.get(role, role)} {how}: {a_txt} → {b_txt}"
        st_free = []
        for end in (src, dst):
            ek = (end.get("key"), mode)
            st_free.append(used.get(ek) or _station_free(prior, end, mode))
        for end in (src, dst):
            used.setdefault((end.get("key"), mode), f"плечо «{label}»")
        free = None
        if mode != "water":
            free = _pair_free(prior, src, dst, mode) or ("ранее" if l.get("route_build") == 0 else None)
        plan = {"income_per_year": l.get("income"), "running_cost_per_year": l.get("running"),
                "cost": (l.get("vehicle_capex") or 0) + (l.get("route_build") or 0), "volume": l.get("volume"),
                "length_m": l.get("length_m"), "trip_time_s": l.get("trip_time_s"),
                "station_from": l.get("station_from"), "station_to": l.get("station_to")}
        legs.append(_mkleg(
            mode=mode, cargo=step.get("cargo"), src=src, dst=dst, units=units, n_consists=n, volume=l.get("volume"),
            role=role, route_free_by=free, station_free_by=tuple(st_free), plan=plan, geometry=l.get("geometry"),
            income_override=l.get("income"), title=label,
            builtin=(mode == "water" and src["water_only"], mode == "water" and dst["water_only"]),
            depot_end=1 if role == "feeder" else 0, prefer_geometry=(mode == "water")))
    return legs


# ---- passengers and workers

def _pax_parts(step: dict) -> list[dict]:
    legs = step.get("legs") or []
    if legs and isinstance(legs[0], dict) and "nodes" in legs[0]:
        return [dict(p) for p in legs]
    return [{"title": step.get("title"), "mode": step.get("kind"), "nodes": None, "volume": None,
             "income": step.get("income_per_year"), "running": None, "vehicles": step.get("vehicles"),
             "geometry": step.get("geometry"), "details": step.get("details"), "infra_capex": None,
             "vehicle_capex": None}]


def _scale_ring(geom: list | None, length_m: float | None) -> list | None:
    """Scale a ring polyline around its centroid so its perimeter equals the planned loop length."""
    if not geom or len(geom) < 3 or not length_m:
        return geom
    P = np.asarray(geom, dtype=float)
    per = float(D.polyline_cum([tuple(p) for p in P])[-1])
    if per <= 0:
        return geom
    c = P[:-1].mean(axis=0) if np.allclose(P[0], P[-1]) else P.mean(axis=0)
    return ((P - c) * (length_m / per) + c).round(1).tolist()


def _pax_info(det: str) -> dict:
    return {
        "passengers": _re_num(rf"Пассажиров {_NUM}/год", det),
        "demand": _re_num(rf"спрос {_NUM}", det),
        "capacity": _re_num(rf"провозная способность {_NUM}", det),
        "headway_s": _re_num(rf"интервал {_NUM} с", det),
        "cycle_s": _re_num(rf"круг {_NUM} с", det),
        "ticket": _re_num(rf"Билет {_NUM} за", det),
        "ticket_income": _re_num(rf"билеты {_NUM}/год", det),
        "stops": _re_num(rf"{_NUM} остановок", det),
        "loop_km": _re_num(rf"кольцо ~{_NUM} км", det),
        "stop_price": _re_num(rf"малые, {_NUM} каждая", det),
        "avg_trip_m": _re_num(rf"средняя поездка {_NUM} м", det),
        "coverage": re.search(r"Охват город\S*\s+([\d%/ ]+)", det or "").group(1).strip()
        if re.search(r"Охват город\S*\s+([\d%/ ]+)", det or "") else None,
        "tram_track": _re_num(rf"Трамвайные пути {_NUM}", det),
        "stops_per_town": _re_num(rf"{_NUM} малые остановки в каждом городе", det) or 1,
        "rail_station": (re.search(r"Пассажирский вокзал: [^.]+", det or "").group(0)
                         if re.search(r"Пассажирский вокзал: [^.]+", det or "") else None),
        "road_exists": "(дорога уже есть)" in (det or ""),
        "road_to_build": "(дорогу нужно построить)" in (det or ""),
        "workers_per_year": _re_num(rf"Ожидается {_NUM} поездок на работу", det),
        "distinct_workers_2y": _re_num(rf"разных рабочих за 2 года ~{_NUM}", det),
        "boost_factor": _re_num(rf"Бонус производства ×{_NUM}", det),
        "boost_value": _re_num(rf"\(\+{_NUM}/год прибыли\)", det),
        "boosted": "Бонус производства ×" in (det or ""),
        "feeders": (re.search(r"с подвозом: ([^)]+)\)", det or "").group(1)
                    if re.search(r"с подвозом: ([^)]+)\)", det or "") else None),
    }


def _pax_streets(ctx: Context, part: dict, step: dict, single: bool, pk: str, ns: list, info: dict) -> dict | None:
    """Stops / path of a passenger part on the existing streets (passengers.py street planner): the
    plan's own data (``pax`` + ``segments`` of the leg or the single-part step), else rebuilt the way
    passengers.py builds it (same greedy stops, street paths, TSP order) from the export v6 network."""
    pax = part.get("pax") or (step.get("pax") if single else None)
    segs = part.get("segments") or (step.get("segments") if single else None)
    tot = part.get("segment_totals") or (step.get("segment_totals") if single else None)
    if pax and pax.get("stops"):
        out = dict(pax)
        if segs:
            out["segments"], out["segment_totals"] = segs, tot
        out["source"] = "plan"
        return out
    try:
        from . import passengers as PX
        sp = PX.street_planner(ctx.game)
        if sp is None:
            return None
        towns = sp.towns
        pp = PX.PassengerParams()
        line = None
        if pk == "loop":
            t = towns[ns[0].index] if ns[0].index < len(towns) else None
            n = int(info.get("stops") or 0)
            ls = sp.loop(t, n, pp.stop_catchment_m, pp.max_intra_stops) if t is not None and n >= 2 else []
            line = ls[0] if ls else None
            depot = sp.depot_for(line.stops, (t.x, t.y)) if line is not None else None
        elif len(ns) >= 2 and ns[0].index < len(towns):
            a = towns[ns[0].index]
            ga = sp.main_stop(a, pp.stop_catchment_m)
            j = ns[-1].index
            if j < len(towns):
                gb = sp.main_stop(towns[j], pp.stop_catchment_m)
                sb = sp.stop_dict(gb, towns[j], no=2) if gb is not None else None
            else:
                gb = sp.snap(ns[-1].x, ns[-1].y, max_dist=PX.INDUSTRY_STOP_M)
                sb = sp.stop_dict(gb, None, no=2) if gb is not None else None
            if ga is not None and sb is not None:
                line = sp.street_path_line(ga, gb, [sp.stop_dict(ga, a, no=1), sb])
            depot = sp.depot_for(line.stops[:1], (a.x, a.y)) if line is not None else None
            k = int(info.get("stops_per_town") or 1)
            if line is not None and k > 1 and j < len(towns):
                st = ([line.stops[0]] + PX.extra_town_stops(sp, line.geometry, a, True, k)
                      + PX.extra_town_stops(sp, line.geometry, towns[j], False, k)[::-1] + [line.stops[-1]])
                out = line.pax_json(depot)
                out.update(stops=[dict(x, no=q + 1) for q, x in enumerate(st)], segments=line.segments,
                           segment_totals=line.totals, geometry=line.geometry, source="rebuilt")
                return out
        if line is None:
            return None
        out = line.pax_json(depot)
        out.update(segments=line.segments, segment_totals=line.totals, geometry=line.geometry, source="rebuilt")
        return out
    except Exception as e:                       # pragma: no cover - defensive
        print(f"dossier: street layout failed: {e}", file=sys.stderr)
        return None


def _legs_pax(ctx: Context, step: dict, prior: dict, kind: str) -> list[Leg]:
    out: list[Leg] = []
    parts = _pax_parts(step)
    used: dict[tuple, str] = {}                  # (town key, mode) -> part that builds a stop there
    for p in parts:
        mode = p.get("mode") or step.get("kind") or "road"
        det = p.get("details") or ""
        if p.get("nodes") is not None:
            ns = [ctx.node(int(i)) for i in p["nodes"]]
        else:
            ns = [ctx.node(step.get("from")), ctx.node(step.get("to"))]
            if ns[0] is not None and ns[1] is not None and ns[0].key == ns[1].key:
                ns = ns[:1]
        ns = [n for n in ns if n is not None]
        if not ns:
            continue
        units, n = _consist_from_totals(_totals(ctx, p.get("vehicles")), None, mode)
        info = _pax_info(det)
        pk = "workers" if kind == "workers" else ("loop" if len(ns) == 1 else "intercity")
        a, b = ns[0], ns[-1]
        src, dst = _endpoint_dict(ctx, a), _endpoint_dict(ctx, b)
        volume = info["passengers"] or p.get("volume")
        geometry = p.get("geometry")
        stops = 0
        route_free = None
        cost_override = None
        streets = _pax_streets(ctx, p, step, len(parts) == 1, pk, ns, info) if mode in ("road", "tram") else None
        if streets and streets.get("geometry") and (streets.get("source") == "rebuilt" or not geometry):
            geometry = streets["geometry"]
        if pk == "loop":
            stops = len(streets["stops"]) if streets else int(info["stops"] or 4)
            if not streets:
                geometry = _scale_ring(geometry, (info["loop_km"] or 0) * 1000.0 or None)
            new_c = float((streets or {}).get("new_cost") or 0.0)
            if mode == "tram":
                track = info["tram_track"] or 0.0
                cost_override = (track + new_c, track / 6.0)
            elif streets and not streets.get("streets_only", True):
                cost_override = (new_c, new_c / 6.0)
            else:
                route_free = ("существующие улицы города — новых дорог не строим, дома не сносим"
                              if streets else "улицы города (строить не нужно)")
        elif mode == "road":
            if streets and not (streets.get("segment_totals") or {}).get("new_m"):
                route_free = "только существующие дороги — новых дорог не строим"
            elif pk == "workers":
                route_free = None if info["road_to_build"] else "дорога уже есть"
            else:
                route_free = "дорога уже есть (города связаны)" if info["road_exists"] else None
            route_free = route_free or _pair_free(prior, src, dst, mode)
        else:
            route_free = _pair_free(prior, src, dst, mode)
        income = p.get("income")
        if pk == "workers" and info["ticket_income"] is not None:
            income = info["ticket_income"]
        plan = {"cost": (p.get("infra_capex") or 0) + (p.get("vehicle_capex") or 0) if p.get("infra_capex") is not None
                else step.get("cost"),
                "income_per_year": p.get("income"), "running_cost_per_year": p.get("running")
                if p.get("running") is not None else step.get("running_cost_per_year"),
                "volume": volume, "length_m": step.get("length_m") if p.get("nodes") is None else None}
        out.append(_mkleg(
            mode=mode, cargo="passengers", src=src, dst=dst, units=units, n_consists=n, volume=volume,
            role={"loop": "loop", "intercity": "intercity", "workers": "workers"}[pk],
            route_free_by=route_free, plan=plan, geometry=geometry, stops=stops, loop=(pk == "loop"),
            station_free_by=(used.get((src["key"], mode)) or _station_free(prior, src, mode, True),
                             used.get((dst["key"], mode)) or _station_free(prior, dst, mode, True)),
            income_override=income, pax=pk, title=p.get("title"), info=info,
            stops_per_town=int(info["stops_per_town"] or 1) if pk == "intercity" else 1,
            route_cost_override=cost_override, streets=streets))
        for e in (src, dst):
            used.setdefault((e["key"], mode), f"остановка «{p.get('title') or step.get('title')}»")
    return out


# ---- tours (milk runs, circuits)

def _tour_flows(ctx: Context, details: str) -> list[dict]:
    """'Потоки: A → B: глина 70; C → D: кирпичи 70' -> [{from, to, cargo, volume}] (nodes, cargo key)."""
    m = re.search(r"Потоки:\s*(.*?)\.\s+(?:Каждая|Состав|$)", details or "")
    if not m:
        return []
    out = []
    for item in m.group(1).split("; "):
        if " → " not in item:
            continue
        left, right = item.split(" → ", 1)
        if ": " not in right:
            continue
        dst_name, rest = right.rsplit(": ", 1)
        mm = re.match(rf"(.+?)\s+{_NUM}\s*$", rest)
        if not mm:
            continue
        a, b = ctx.by_name.get(left.strip()), ctx.by_name.get(dst_name.strip())
        ck = ctx.cargo_key(mm.group(1))
        if a is None or b is None or ck is None:
            continue
        out.append({"from": a, "to": b, "cargo": ck, "volume": _to_num(mm.group(2))})
    return out


def _legs_tour(ctx: Context, step: dict, prior: dict) -> list[Leg]:
    mode = step.get("kind") or "road"
    segs = []
    for l in step.get("legs") or []:
        a, b = ctx.node(l.get("from")), ctx.node(l.get("to"))
        if a is None or b is None:
            continue
        segs.append({"a": a, "b": b, "length_m": float(l.get("length_m") or 0.0), "load": float(l.get("load") or 0.0)})
    if not segs:
        return _legs_cargo(ctx, step, prior)
    flows = _tour_flows(ctx, step.get("details", ""))
    units, n = _consist_from_totals(_totals(ctx, step.get("vehicles")), None, mode, step.get("details", ""))
    title = step.get("title") or ""
    topology = "shuttle" if title.rstrip().endswith("⇄") or "челнок" in (step.get("details") or "") else "loop"
    label = re.match(r"^([^(:]+)", _strip_title(title))
    plan = _plan_of(step)
    peak = max((s["load"] for s in segs), default=0.0)
    first = _endpoint_dict(ctx, segs[0]["a"])
    tour = {"segments": segs, "flows": flows, "topology": topology,
            "label": (label.group(1).strip() if label else "Маршрут"), "peak_load": peak,
            "prior_pairs": prior["pairs"], "prior_stations": prior["stations"]}
    return [_mkleg(mode=mode, cargo=step.get("cargo"), src=first, dst=first, units=units, n_consists=n,
                   volume=peak or plan["volume"], role="tour", plan=plan, geometry=step.get("geometry"),
                   loop=False, tour=tour, title=title)]


# ---- prospecting

def _prospect_info(step: dict) -> dict:
    det = step.get("details") or ""
    res = re.search(r"Разведка ресурса «([^»]+)» у города ([^(]+?) \(ранг (\d+)", det)
    exp = re.search(r"Ожидается (.+?) ~" + _NUM + r" ед\./год", det)
    first = re.search(r"Первая линия: (\S+) → (.+?) \((\w+)\), " + _NUM + r" ед\./год, доход " + _NUM, det)
    site = (step.get("geometry") or [[None, None]])[0]
    return {
        "resource": res.group(1) if res else None, "town": res.group(2).strip() if res else None,
        "rank": int(res.group(3)) if res else None,
        "success": _re_num(r"успех ~(\d+)%", det), "months": _re_num(rf"~{_NUM} мес", det),
        "expected": exp.group(1).strip() if exp else None, "expected_volume": _to_num(exp.group(2)) if exp else None,
        "start_level": _re_num(r"старт ~уровень (\d+)", det), "avg_level": _re_num(r"в среднем до (\d+)", det),
        "first_line": None if not first else {"cargo": first.group(1), "to": first.group(2), "mode": first.group(3),
                                              "volume": _to_num(first.group(4)), "income": _to_num(first.group(5))},
        "site": {"x": site[0], "y": site[1]} if site and site[0] is not None else None,
        "estimate_note": "Место — оценка (центр свободной зоны у города); уточнить после появления предприятия.",
    }


def _legs_prospect(ctx: Context, step: dict, prior: dict) -> tuple[list[Leg], dict]:
    info = _prospect_info(step)
    mode = step.get("kind") or "road"
    b = ctx.node(step.get("to"))
    site = info["site"] or {"x": (step.get("from") or {}).get("x", 0.0), "y": (step.get("from") or {}).get("y", 0.0)}
    what = info["expected"] or "предприятие"
    src = _endpoint_dict(ctx, None, {"name": f"будущее предприятие: {what} у г. {info['town'] or '?'} (место оценочное)",
                                     "x": site["x"], "y": site["y"], "type": "planned"})
    dst = _endpoint_dict(ctx, b, step.get("to"))
    units, n = _consist_from_totals(_totals(ctx, step.get("vehicles")), None, mode)
    vol = (info["first_line"] or {}).get("volume") or info["expected_volume"]
    plan = {"cost": step.get("cost"), "income_per_year": step.get("income_per_year"),
            "running_cost_per_year": step.get("running_cost_per_year"), "volume": vol}
    leg = _mkleg(mode=mode, cargo=step.get("cargo"), src=src, dst=dst, units=units, n_consists=n, volume=vol,
                 role="first_line", plan=plan, station_free_by=(None, _station_free(prior, dst, mode)),
                 builtin=(mode == "water" and step.get("cargo") in ("fish", "crude_oil"), False),
                 estimate=True, title="Первая линия от будущего предприятия")
    return [leg], info


# ---- ProjectCandidate / LinkEval inputs (planner objects)

def _legs_from_linkeval(ctx: Context, ev, p=None, d=None) -> list[Leg]:
    a = ctx.node(p) if p is not None else ctx.node(ev.src)
    b = ctx.node(d) if d is not None else ctx.node(ev.dst)
    units = list(ev.consist.units) if ev.consist else []
    plan = {"cost": ev.capex, "income_per_year": ev.income, "running_cost_per_year": ev.running + ev.maintenance,
            "roi_years": ev.payback_years if math.isfinite(ev.payback_years) else None, "volume": ev.volume,
            "length_m": ev.route_m, "trip_time_s": ev.trip_time_s}
    route_free_by = None
    if ev.mode in ("rail", "road") and getattr(ev, "route_build", 0.0) == 0 and a is not None and b is not None:
        try:
            if float(ctx.mat(ev.mode).build[a.index, b.index]) > 0:
                route_free_by = "ранее"
        except Exception:
            pass
    src, dst = _endpoint_dict(ctx, a, vars(ev.src)), _endpoint_dict(ctx, b, vars(ev.dst))
    return [_mkleg(mode=ev.mode, cargo=ev.cargo, src=src, dst=dst, units=units, n_consists=ev.n_consists,
                   volume=ev.volume, route_free_by=route_free_by, plan=plan,
                   builtin=(ev.mode == "water" and src["water_only"], ev.mode == "water" and dst["water_only"]))]


def _project_as_step(c) -> dict:
    """ProjectCandidate -> plan-step-like dict, so all project kinds share one code path."""
    vehicles = [{"model": v.file.rsplit("/", 1)[-1], "name": v.name, "count": v.count, "vehicle_id": v.vehicle_id}
                for v in c.vehicles]
    step = {"id": None, "line": (c.meta or {}).get("line_id"), "action": "build_line", "kind": c.mode,
            "cargo": c.cargo, "title": c.title, "details": c.details, "geometry": c.geometry,
            "project_kind": c.kind, "vehicles": vehicles, "cost": c.capex, "income_per_year": c.income,
            "running_cost_per_year": c.running + c.maintenance, "length_m": (c.meta or {}).get("route_m")}
    return step


def _legs_from_project(ctx: Context, c, prior: dict) -> list[Leg]:
    step = _project_as_step(c)
    if c.kind == "multimodal" or (c.legs and isinstance(c.legs[0], dict) and "role" in c.legs[0]):
        step["legs"] = c.legs
        return _legs_multimodal(ctx, step, prior)
    if c.kind in ("passenger", "workers"):
        if c.legs:
            step["legs"] = c.legs
        if len(c.nodes) >= 2:
            step["from"], step["to"] = ctx.nodes[c.nodes[0]].key, ctx.nodes[c.nodes[1]].key
        elif c.nodes:
            step["from"] = step["to"] = ctx.nodes[c.nodes[0]].key
        legs = _legs_pax(ctx, step, prior, c.kind)
        if c.kind == "passenger" and len(c.nodes) == 1 and legs and (c.meta or {}).get("stops"):
            legs[0].stops = int(c.meta["stops"])
        return legs
    if (c.meta or {}).get("generator") == "tours" and c.legs:
        step["legs"] = c.legs
        step["stops"] = [ctx.nodes[i].key for i in c.nodes]
        legs = _legs_tour(ctx, step, prior)
        if legs and legs[0].tour and c.meta.get("flows"):
            legs[0].tour["flows"] = [{"from": ctx.nodes[f["from"]], "to": ctx.nodes[f["to"]], "cargo": f["cargo"],
                                      "volume": f["volume"]} for f in c.meta["flows"]]
            legs[0].tour["topology"] = c.meta.get("topology", legs[0].tour["topology"])
        return legs
    totals = [(ctx.vehicle(v.file, v.name, v.vehicle_id), v.count) for v in c.vehicles]
    totals = [(v, k) for v, k in totals if v is not None]
    units, n = _consist_from_totals(totals, None, c.mode)
    plan = {"cost": c.capex, "income_per_year": c.income, "running_cost_per_year": c.running + c.maintenance,
            "roi_years": c.payback_years if math.isfinite(c.payback_years) else None, "volume": c.volume,
            "length_m": (c.meta or {}).get("route_m")}
    a, b = ctx.node(c.nodes[0]), ctx.node(c.nodes[-1])
    src, dst = _endpoint_dict(ctx, a), _endpoint_dict(ctx, b)
    return [_mkleg(mode=c.mode, cargo=c.cargo, src=src, dst=dst, units=units, n_consists=n, volume=c.volume,
                   plan=plan, geometry=c.geometry,
                   builtin=(c.mode == "water" and src["water_only"], c.mode == "water" and dst["water_only"]))]


def _normalize(ctx: Context, obj, plan_steps: list[dict] | None) -> tuple[dict, list[Leg], dict]:
    """-> (header, legs, extra) for a plan step dict, planner.Candidate, LinkEval or ProjectCandidate."""
    extra: dict = {}
    if isinstance(obj, dict):
        kind = _step_kind(obj)
        hdr = {"line": obj.get("line") or obj.get("id"), "step_id": obj.get("id"), "year": obj.get("year"),
               "month": obj.get("month"), "title": obj.get("title"), "kind": kind, "kind_ru": KIND_RU[kind],
               "project_kind": obj.get("project_kind"), "mode": obj.get("kind"), "cargo": obj.get("cargo"),
               # planner decides universal vs specialized terminal modules (capacity.apply_capacity)
               "platform": ("specialized" if "Платформы: специализированные" in (obj.get("details") or "")
                            else "universal" if "Платформы: универсальные" in (obj.get("details") or "") else None)}
        prior = _prior(ctx, plan_steps, obj)
        if kind == "multimodal":
            legs = _legs_multimodal(ctx, obj, prior)
        elif kind in ("passenger", "workers"):
            legs = _legs_pax(ctx, obj, prior, kind)
        elif kind == "tour":
            legs = _legs_tour(ctx, obj, prior)
        elif kind == "industry_build":
            legs, extra["prospect"] = _legs_prospect(ctx, obj, prior)
        else:
            legs = _legs_cargo(ctx, obj, prior)
        extra["details"] = obj.get("details") or ""
        return hdr, legs, extra
    if hasattr(obj, "ev") and hasattr(obj, "p") and hasattr(obj, "d"):          # planner.Candidate
        ev = obj.ev
        hdr = {"line": None, "step_id": None, "year": ev.year, "month": None, "kind": "cargo",
               "kind_ru": KIND_RU["cargo"], "mode": ev.mode, "cargo": ev.cargo,
               "title": f"{MODE_RU.get(ev.mode, ev.mode)}: {ev.cargo} {ev.src.name} → {ev.dst.name}"}
        return hdr, _legs_from_linkeval(ctx, ev, obj.p, obj.d), extra
    if hasattr(obj, "consist") and hasattr(obj, "src") and hasattr(obj, "route_m"):  # LinkEval
        hdr = {"line": None, "step_id": None, "year": obj.year, "month": None, "kind": "cargo",
               "kind_ru": KIND_RU["cargo"], "mode": obj.mode, "cargo": obj.cargo,
               "title": f"{MODE_RU.get(obj.mode, obj.mode)}: {obj.cargo} {obj.src.name} → {obj.dst.name}"}
        return hdr, _legs_from_linkeval(ctx, obj), extra
    if hasattr(obj, "nodes") and hasattr(obj, "vehicles") and hasattr(obj, "kind"):   # ProjectCandidate
        kind = obj.kind if obj.kind in KIND_RU else "cargo"
        if (obj.meta or {}).get("generator") == "tours" and obj.legs:
            kind = "tour"
        hdr = {"line": (obj.meta or {}).get("line_id"), "step_id": None, "year": (obj.meta or {}).get("year"),
               "month": None, "kind": kind, "kind_ru": KIND_RU[kind], "project_kind": obj.kind, "mode": obj.mode,
               "cargo": obj.cargo, "title": obj.title}
        prior = {"pairs": {}, "stations": {}}
        if kind == "industry_build":
            legs, extra["prospect"] = _legs_prospect(ctx, _project_as_step(obj) | {
                "to": ctx.nodes[obj.nodes[-1]].key if obj.nodes else None}, prior)
        else:
            legs = _legs_from_project(ctx, obj, prior)
        extra["details"] = obj.details or ""
        if isinstance(obj.meta, dict) and obj.meta.get("warehouses"):
            extra["warehouses_meta"] = obj.meta["warehouses"]
        return hdr, legs, extra
    raise TypeError(f"build_dossier: unsupported object {type(obj).__name__}")


# --------------------------------------------------------------------------- route

def _orient(pts: list, src: dict) -> list:
    """Polyline running from the source end (plan geometries of hub legs may be reversed)."""
    if len(pts) >= 2 and math.dist(pts[0], (src["x"], src["y"])) > math.dist(pts[-1], (src["x"], src["y"])):
        return pts[::-1]
    return pts


def _synthetic_route(ctx: Context, leg: Leg, mode: str):
    """Route-like object from a geometry or the straight line (no routing data)."""
    pts = leg.geometry or [[leg.src["x"], leg.src["y"]], [leg.dst["x"], leg.dst["y"]]]
    pts = [(float(p[0]), float(p[1])) for p in pts]
    if not leg.loop:
        pts = _orient(pts, leg.src)
    cum = D.polyline_cum(pts)
    L = float(cum[-1]) if len(cum) else 0.0
    if L <= 0 and leg.plan.get("length_m"):
        L = float(leg.plan["length_m"])
        pts = [pts[0], pts[-1]]
        cum = np.array([0.0, L])
    t = ctx.game.terrain
    if mode == "water":
        prof = [(0.0, float(t.water_level)), (L, float(t.water_level))]
    else:
        ss = np.arange(0.0, L + 1e-9, 50.0) if L > 0 else np.array([0.0])
        if L > 0 and ss[-1] < L - 1e-6:
            ss = np.append(ss, L)
        xs = np.interp(ss, cum, [p[0] for p in pts]) if len(pts) > 1 else np.full(len(ss), pts[0][0])
        ys = np.interp(ss, cum, [p[1] for p in pts]) if len(pts) > 1 else np.full(len(ss), pts[0][1])
        prof = [(float(s), max(float(t.height(x, y)), float(t.water_level))) for s, x, y in zip(ss, xs, ys)]
    straight = math.dist(pts[0], pts[-1]) if pts else 0.0
    return SimpleNamespace(kind=mode, way_key=None, polyline=pts, length_m=L, build_cost=0.0,
                           maintenance_per_year=0.0, max_grade=0.0, max_terrain_grade=0.0, bridges=[], tunnels=[],
                           water_bridge_m=0.0, viaduct_m=0.0, tunnel_m=0.0, earthworks_m3=0.0, profile=prof,
                           straight_distance=straight, detour_factor=L / straight if straight > 0 else 1.0,
                           synthetic=True)


def _route_for(ctx: Context, leg: Leg):
    """(route, route planner or None, source text)."""
    mode = "road" if leg.mode == "tram" else leg.mode
    i, j = leg.src.get("node"), leg.dst.get("node")
    if leg.loop:
        rp = None
        if mode in ("rail", "road"):
            try:
                rp = ctx.route_planner(mode)
            except Exception:
                rp = None
        if leg.streets:
            return (_synthetic_route(ctx, leg, leg.mode), rp,
                    f"по существующим улицам (сеть export v6), {leg.streets.get('length_m', 0)} м за круг: "
                    f"{leg.streets.get('route_text', '')}")
        return _synthetic_route(ctx, leg, leg.mode), rp, "кольцо по городу (схема плана, длина по плану)"
    if leg.streets and leg.pax in ("intercity", "workers") and leg.geometry and len(leg.geometry) >= 2:
        rp = None
        st = leg.streets.get("stops") or []
        if leg.streets.get("variant") == "route" and len(st) >= 2:
            try:                                    # the same routing.py route the plan priced (bridges etc.)
                rp = ctx.route_planner(mode)
                r = rp.route_xy((st[0]["x"], st[0]["y"]), (st[-1]["x"], st[-1]["y"]))
                if r is not None:
                    return r, rp, "от остановки до остановки: существующие дороги + новые участки (routing.py)"
            except Exception:
                rp = None
        return (_synthetic_route(ctx, leg, leg.mode), rp,
                "от остановки до остановки только по существующим дорогам (сеть export v6)")
    if leg.prefer_geometry and leg.geometry and len(leg.geometry) >= 2:
        return _synthetic_route(ctx, leg, leg.mode), None, ("судовой ход по воде (трасса цепочки, multimodal.py)"
                                                            if mode == "water" else "трасса цепочки (multimodal.py)")
    try:
        if mode in ("rail", "road") and i is not None and j is not None:
            r = ctx.mat(mode).route(i, j)
            if r is not None:
                return r, ctx.route_planner(mode), "трассировка по рельефу (routing.py)"
        if mode in ("rail", "road"):
            rp = ctx.route_planner(mode)
            r = rp.route_xy((leg.src["x"], leg.src["y"]), (leg.dst["x"], leg.dst["y"]))
            if r is not None:
                return r, rp, "трассировка по рельефу (routing.py)"
        if mode == "water" and i is not None and j is not None:
            r = ctx.water().route(i, j)
            if r is not None:
                return r, None, "судовой ход по воде (routing.water_network)"
    except Exception as e:                          # pragma: no cover - defensive
        print(f"dossier: routing failed for {leg.src['name']} -> {leg.dst['name']}: {e}", file=sys.stderr)
    return _synthetic_route(ctx, leg, leg.mode), None, "по геометрии плана / по прямой (трасса не найдена)"


def _route_section(ctx: Context, leg: Leg, r, rp, src_text: str) -> dict:
    g = ctx.game
    way = getattr(rp, "way", None) if rp else None
    bridge = getattr(rp, "bridge", None) if rp else None
    tunnel = getattr(rp, "tunnel", None) if rp else None
    terrain_cost = getattr(rp, "terrain_cost", 7.3125) if rp else 7.3125
    L = float(r.length_m)
    poly = list(r.polyline)
    cum = D.polyline_cum(poly)
    scale = L / cum[-1] if len(cum) and cum[-1] > 0 else 1.0
    P = np.asarray(poly, dtype=float) if poly else np.zeros((0, 2))

    def at(pt):
        if not len(P):
            return 0.0
        k = int(np.argmin(np.hypot(P[:, 0] - pt[0], P[:, 1] - pt[1])))
        return float(cum[k] * scale)

    structures = []
    br_sum = 0.0
    for a, b, length in getattr(r, "bridges", []) or []:
        xs = np.linspace(a[0], b[0], 7)
        ys = np.linspace(a[1], b[1], 7)
        wet = any(g.terrain.is_water(float(x), float(y)) for x, y in zip(xs, ys))
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        cost = float(length) * (bridge.cost_per_m if bridge else 0.0)
        br_sum += cost
        structures.append({
            "type": "bridge" if wet else "viaduct", "type_ru": "мост через воду" if wet else "виадук (овраг/склон)",
            "name": bridge.name if bridge else "мост", "key": bridge.key if bridge else None,
            "length_m": round(float(length)), "at_m": round(min(at(a), at(b))), "to_m": round(max(at(a), at(b))),
            "x": round(mid[0]), "y": round(mid[1]), "place": ctx.place(*mid),
            "cost": round(cost), "maintenance": round(float(length) * (bridge.maintenance_per_m if bridge else 0.0)),
            "speed_limit_kmh": round(bridge.speed_limit * 3.6) if bridge and bridge.speed_limit > 0 else None})
    tn_sum = 0.0
    for a, b, length in getattr(r, "tunnels", []) or []:
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        cost = float(length) * (tunnel.cost_per_m if tunnel else 0.0)
        tn_sum += cost
        structures.append({
            "type": "tunnel", "type_ru": "тоннель", "name": tunnel.name if tunnel else "тоннель",
            "key": tunnel.key if tunnel else None, "length_m": round(float(length)),
            "at_m": round(min(at(a), at(b))), "to_m": round(max(at(a), at(b))), "x": round(mid[0]), "y": round(mid[1]),
            "place": ctx.place(*mid), "cost": round(cost),
            "maintenance": round(float(length) * (tunnel.maintenance_per_m if tunnel else 0.0)),
            "speed_limit_kmh": None})
    structures.sort(key=lambda s: s["at_m"])
    track = L * (way.cost_per_m if way else 0.0)
    earth = float(getattr(r, "earthworks_m3", 0.0)) * terrain_cost
    build = float(getattr(r, "build_cost", 0.0))
    maint_track = L * (way.maintenance_per_m if way else 0.0)
    maint_total = float(getattr(r, "maintenance_per_year", 0.0))
    if getattr(r, "synthetic", False) and leg.route_cost_override is None:
        build = track + br_sum + tn_sum + earth            # synthetic route: plain way price over its length
        maint_total = maint_track
    if (leg.streets and leg.pax in ("intercity", "workers") and not getattr(r, "synthetic", False)
            and float(leg.streets.get("new_cost") or 0.0) > build):
        build = float(leg.streets["new_cost"])             # demolition priced by passengers.price_new_road
    if leg.route_cost_override is not None:                # e.g. tram lanes on the town streets of a loop
        track, br_sum, tn_sum, earth = float(leg.route_cost_override[0]), 0.0, 0.0, 0.0
        build, maint_track = track, float(leg.route_cost_override[1])
        maint_total = maint_track
    surcharge = build - track - br_sum - tn_sum - earth
    prof = list(r.profile)
    h = np.array([p[1] for p in prof]) if prof else np.zeros(1)
    dh = np.diff(h) if len(h) > 1 else np.zeros(0)
    straight2d = math.dist((leg.src["x"], leg.src["y"]), (leg.dst["x"], leg.dst["y"]))
    if leg.loop:
        straight2d = L / math.pi if L > 0 else 0.0          # ring: its diameter
    seg_json = None
    if getattr(r, "segments", None):
        from .routing import route_segments_json, segment_totals
        seg_json = route_segments_json(r)
    elif leg.streets and leg.streets.get("segments"):          # passengers on existing streets
        from .routing import segment_totals
        seg_json = leg.streets["segments"]
    sec = {
        "segments": seg_json or [],
        "segment_totals": (segment_totals(seg_json) if seg_json else None),
        "source": src_text, "length_m": round(L), "straight_m": round(straight2d),
        "detour": round(L / straight2d, 3) if straight2d > 0 else None,
        "z_from": round(leg.src["z"], 1), "z_to": round(leg.dst["z"], 1),
        "elevation_gain_m": round(float(dh[dh > 0].sum()), 1), "elevation_loss_m": round(float(-dh[dh < 0].sum()), 1),
        "max_terrain_grade": round(float(getattr(r, "max_terrain_grade", 0.0)), 4),
        "max_grade": round(float(getattr(r, "max_grade", 0.0)), 4),
        "way": None if way is None else {
            "key": way.key, "name": way.name, "cost_per_m": way.cost_per_m, "maintenance_per_m": way.maintenance_per_m,
            "speed_kmh": round(way.speed * 3.6), "max_slope": way.max_slope, "min_radius": way.min_radius,
            "electrified": way.electrified},
        "bridge_type": None if bridge is None else {"name": bridge.name, "key": bridge.key, "cost_per_m": bridge.cost_per_m,
                                                    "speed_kmh": round(bridge.speed_limit * 3.6) if bridge.speed_limit else None},
        "tunnel_type": None if tunnel is None else {"name": tunnel.name, "key": tunnel.key, "cost_per_m": tunnel.cost_per_m},
        "structures": structures,
        "bridges_m": round(float(getattr(r, "water_bridge_m", 0.0) + getattr(r, "viaduct_m", 0.0))),
        "tunnels_m": round(float(getattr(r, "tunnel_m", 0.0))),
        "earthworks_m3": round(float(getattr(r, "earthworks_m3", 0.0))),
        "cost": {"track": round(track), "bridges": round(br_sum), "tunnels": round(tn_sum), "earthworks": round(earth),
                 "surcharges": round(surcharge), "total": round(build)},
        "maintenance": {"track": round(maint_track), "structures": round(maint_total - maint_track),
                        "total": round(maint_total)},
        "free": leg.route_free_by is not None, "free_by": leg.route_free_by,
        # length on roads/tracks that already exist in the game (export v6 network): nothing to build there
        "reused_m": round(float(getattr(r, "reused_m", 0.0) or 0.0)),
        "polyline": [[round(x), round(y)] for x, y in _decimate(poly, 120)],
    }
    if leg.mode == "water":
        sec["free"], sec["free_by"] = True, "вода (строить не нужно)"
    if leg.streets:
        ls = leg.streets
        sec["streets"] = {
            "stops": [{k: st.get(k) for k in ("no", "x", "y", "street", "way", "edge_id", "where", "junction_m",
                                               "town", "label")} for st in ls.get("stops") or []],
            "path_ru": ls.get("route_text"), "length_m": ls.get("length_m"), "unique_m": ls.get("unique_m"),
            "coverage": ls.get("coverage"), "streets_only": ls.get("streets_only"), "variant": ls.get("variant"),
            "new_m": ls.get("new_m"), "new_cost": ls.get("new_cost"), "demolition": ls.get("demolition"),
            "summary_ru": ("новых дорог не строим, дома не сносим" if ls.get("streets_only") else
                           f"новые участки {ls.get('new_m', 0)} м за {_m(ls.get('new_cost') or 0)} "
                           f"(в т.ч. снос домов {_m(ls.get('demolition') or 0)})"),
            "source": "план" if ls.get("source") == "plan" else "восстановлено по сети улиц (passengers.py)"}
    if not sec["free"]:
        dem = _route_demolition(ctx, leg, r, rp, sec)
        if dem:
            sec["demolition"] = dem
            sec["cost"]["demolition"] = dem["cost"]            # informational: already inside "total"
    return sec


# --------------------------------------------------------------------------- demolition of town buildings
# docs/demolition.md: routing.py prices the town buildings a NEW way runs through (money is inside the
# route build cost), siting.py the buildings under a station / depot / warehouse footprint (added here to
# the building's price). Town reputation: every removed building costs capacity / district capacity / 0.405;
# level "Poor" (< 2/6) forbids further demolition, "Very poor" (< 1/6) new stations in that town;
# recovery 1/6 per 3 game months.

DEMO_MIN_BLD = 0.05            # fewer (expected) buildings: no demolition
DEMO_PART_GAP_M = 60.0         # affected stretches of a segment closer than this are drawn as one
REP_WARN = 0.5                 # reputation loss in one town worth a warning (planner uses the same)
REP_POOR = 4 / 6               # from full reputation below 2/6 ("Poor"): further demolition forbidden
REP_VERY_POOR = 5 / 6          # below 1/6 ("Very poor"): no new stations in that town
REP_RECOVERY_MONTHS = 18.0     # 1/6 per 3 months -> 100 % in 18 months


def _tm(ctx: Context):
    return getattr(ctx.siting, "tm", None)


def _demo_new() -> dict:
    return {"cost": 0.0, "m": 0.0, "buildings": 0.0, "rating": 0.0, "towns": {}}


def _demo_add(acc: dict, d: dict | None) -> dict:
    if not d:
        return acc
    for k in ("cost", "m", "buildings", "rating"):
        acc[k] += float(d.get(k) or 0.0)
    for t, v in (d.get("towns") or {}).items():
        a = acc["towns"].setdefault(t, {"buildings": 0.0, "rating_loss": 0.0})
        if isinstance(v, dict):
            a["buildings"] += float(v.get("buildings") or 0.0)
            a["rating_loss"] += float(v.get("rating_loss") or 0.0)
        else:                                               # planner form {town: penalty}
            a["rating_loss"] += float(v or 0.0)
    return acc


def _demo_round(d: dict | None) -> dict | None:
    if not d or float(d.get("buildings") or 0.0) < DEMO_MIN_BLD:
        return None
    towns = sorted(d.get("towns", {}).items(), key=lambda kv: -kv[1]["rating_loss"])
    out = {"cost": round(float(d["cost"])), "m": round(float(d.get("m") or 0.0)),
           "buildings": round(float(d["buildings"]), 1), "rating": round(float(d.get("rating") or 0.0), 4),
           "towns": {t: {"buildings": round(v["buildings"], 1), "rating_loss": round(v["rating_loss"], 4)}
                     for t, v in towns}}
    out["text_ru"] = _demo_text(out)
    return out


def _nb(n: float) -> str:
    """Building count: whole numbers, one decimal below 1 (expected values without export v7)."""
    n = float(n or 0.0)
    return f"{n:.0f}" if n >= 0.95 else f"{n:.1f}".replace(".", ",")


def _towns_ru(towns: dict, with_rep: bool = True) -> str:
    parts = []
    for t, v in sorted(towns.items(), key=lambda kv: -kv[1]["rating_loss"]):
        s = f"г. {t}"
        if v.get("buildings"):
            s += f" ~{_nb(v['buildings'])} зд."
        if with_rep and v.get("rating_loss", 0) >= 0.0005:
            s += f", репутация −{v['rating_loss'] * 100:.0f} %"
        parts.append(s)
    return "; ".join(parts)


def _demo_text(d: dict, with_m: bool = True) -> str:
    """"снос зданий: N м, ≈ X $, ~K зд. (г. A ~3 зд., репутация −7 %)"."""
    m = f"{float(d.get('m') or 0):.0f} м, " if with_m and float(d.get("m") or 0) >= 1 else ""
    tw = _towns_ru(d.get("towns") or {})
    return f"снос зданий: {m}≈ {_m(d['cost'])} $, ~{_nb(d['buildings'])} зд." + (f" ({tw})" if tw else "")


def _rep_warning(town: str, loss: float, tag: str = "") -> str | None:
    if loss <= REP_WARN:
        return None
    level = ("уровень «Очень плохо» — игра запретит и снос, и новые станции в этом городе" if loss >= REP_VERY_POOR
             else "уровень «Плохо» — игра запретит дальнейший снос" if loss >= REP_POOR
             else "почти «Плохо» (< 2/6 — снос запрещён, < 1/6 — станции)")
    months = min(loss, 1.0) * REP_RECOVERY_MONTHS
    return (f"{tag}Снос в г. {town}: репутация −{loss * 100:.0f} % (> 50 %) — игра может запретить снос/станции "
            f"({level}, если до стройки репутация полная); восстановление ~{months:.0f} мес. (1/6 за 3 мес.). "
            f"Обойти застройку, строить по частям или переждать.")


def _split_towns(tm, idx, towns_pen: dict, n: float, pen: float) -> dict:
    """{town name: {buildings, rating_loss}} of a townmask corridor/footprint, scaled to the given totals
    (routing / siting numbers are authoritative; the mask call only splits them by town)."""
    names: dict[str, list[float]] = {}
    for k, p in towns_pen.items():
        names.setdefault(tm.town_name(int(k)), [0.0, 0.0])[1] += float(p)
    if len(idx) and getattr(tm, "btown", None) is not None:
        for t in tm.btown[np.asarray(idx, np.int64)].tolist():
            names.setdefault(tm.town_name(int(t)), [0.0, 0.0])[0] += 1.0
    else:                                                   # expected values: buildings in proportion to penalty
        tot = sum(v[1] for v in names.values()) or 1.0
        for v in names.values():
            v[0] = n * v[1] / tot
    nb = sum(v[0] for v in names.values())
    np_ = sum(v[1] for v in names.values())
    kb = n / nb if nb > 0 else 0.0
    kp = pen / np_ if np_ > 0 else 0.0
    return {t: {"buildings": v[0] * kb, "rating_loss": v[1] * kp} for t, v in names.items()}


def _poly_cum(P: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))]) if len(P) > 1 else np.zeros(len(P))


def _segment_demolition(ctx: Context, seg: dict, width: float) -> dict:
    """Demolition of one "new" segment (route_segments_json numbers) split by town, plus the stretches of
    the segment that run through demolished plots: ``parts[{polyline, x, y, length_m, buildings, cost, towns}]``."""
    n = float(seg.get("demolition_buildings") or 0.0)
    cost = float(seg.get("demolition_cost") or 0.0)
    dm = float(seg.get("demolition_m") or 0.0)
    pen = float(seg.get("demolition_rating") or 0.0)
    out = {"cost": cost, "m": dm, "buildings": n, "rating": pen, "towns": {}, "parts": []}
    P = np.asarray(seg.get("polyline") or [], np.float64).reshape(-1, 2)
    tm = _tm(ctx)
    if len(P) < 2:
        return out
    whole = {"polyline": P, "buildings": n, "cost": cost, "towns": None}
    if tm is None:
        mid = P[len(P) // 2]
        out["towns"] = {ctx.nearest_town(*mid)[0]: {"buildings": n, "rating_loss": pen}}
        out["parts"] = [whole]
    else:
        f = tm.corridor(P, width)
        out["towns"] = _split_towns(tm, f["idx"], f["towns"], n, pen)
        if not out["towns"]:
            mid = P[len(P) // 2]
            out["towns"] = {ctx.nearest_town(*mid)[0]: {"buildings": n, "rating_loss": pen}}
        from .townmask import _densify
        D = _densify(P, 4.0)
        cum = _poly_cum(D)
        if f["exact"] and len(f["idx"]):
            hit = np.asarray(f["idx"], np.int64)
            from scipy.spatial import cKDTree
            tr = cKDTree(tm.bxy[hit])
            dd, kk = tr.query(D)
            inside = dd <= width / 2.0 + tm.br[hit][kk]
            _, near = cKDTree(D).query(tm.bxy[hit])              # building -> nearest way point
            w_b = np.ones(len(hit))
            w_c = tm.bcost[hit]
        else:
            inside = tm.cover(D[:, 0], D[:, 1]) >= 0.10
            near = np.arange(len(D))
            w_b = tm.per_m(D[:, 0], D[:, 1], width, "n") * np.gradient(cum) if len(D) > 1 else np.zeros(len(D))
            w_c = tm.per_m(D[:, 0], D[:, 1], width, "cost") * np.gradient(cum) if len(D) > 1 else np.zeros(len(D))
            hit = None
        runs = []
        k = 0
        while k < len(D):
            if not inside[k]:
                k += 1
                continue
            j = k
            while j + 1 < len(D) and inside[j + 1]:
                j += 1
            if runs and cum[k] - cum[runs[-1][1]] < DEMO_PART_GAP_M:
                runs[-1][1] = j
            else:
                runs.append([k, j])
            k = j + 1
        if not runs:
            out["parts"] = [whole]
        else:
            starts = np.array([a for a, _ in runs])
            ends = np.array([b for _, b in runs])
            # every building / sample goes to the nearest run
            def run_of(i):
                d = np.where(i < starts, starts - i, np.where(i > ends, i - ends, 0))
                return int(np.argmin(d))
            rb = np.zeros(len(runs))
            rc = np.zeros(len(runs))
            rt: list[dict] = [dict() for _ in runs]
            for q, i in enumerate(near.tolist()):
                if w_b[q] <= 0 and w_c[q] <= 0:
                    continue
                r_ = run_of(i)
                rb[r_] += w_b[q]
                rc[r_] += w_c[q]
                if hit is not None:
                    t = tm.town_name(int(tm.btown[hit[q]]))
                    rt[r_][t] = rt[r_].get(t, 0.0) + 1.0
            kb = n / rb.sum() if rb.sum() > 0 else 0.0
            kc = cost / rc.sum() if rc.sum() > 0 else 0.0
            for r_, (a, b) in enumerate(runs):
                a2, b2 = max(0, a - 1), min(len(D) - 1, b + 1)
                pts = D[a2:b2 + 1]
                out["parts"].append({"polyline": pts, "buildings": rb[r_] * kb, "cost": rc[r_] * kc,
                                     "length_m": float(cum[b2] - cum[a2]),
                                     "towns": {t: c * kb for t, c in rt[r_].items()} or None})
    for p in out["parts"]:
        pts = np.asarray(p["polyline"])
        c = _poly_cum(pts)
        L = float(c[-1]) if len(c) else 0.0
        p["length_m"] = round(p.get("length_m", L))
        p["x"], p["y"] = (round(float(np.interp(L / 2, c, pts[:, 0]))), round(float(np.interp(L / 2, c, pts[:, 1])))) \
            if L > 0 else (round(float(pts[0, 0])), round(float(pts[0, 1])))
        p["polyline"] = [[round(float(x)), round(float(y))] for x, y in _decimate([tuple(q) for q in pts], 40)]
        if not p.get("towns"):
            share = p["buildings"] / n if n > 0 else 1.0
            p["towns"] = {t: v["buildings"] * share for t, v in out["towns"].items()}
    return out


def _route_demolition(ctx: Context, leg: Leg, r, rp, sec: dict) -> dict | None:
    """Route section "demolition": totals, per town, per segment texts and the affected stretches (map)."""
    mode = "road" if leg.mode == "tram" else leg.mode
    if mode not in ("rail", "road"):
        return None
    width = float(getattr(rp, "dem_width", 0.0) or BED_WIDTH.get(mode, 6.0))
    total = _demo_new()
    parts, lines = [], []
    for k, g in enumerate(sec.get("segments") or []):
        if g.get("type") != "new" or float(g.get("demolition_buildings") or 0.0) < DEMO_MIN_BLD:
            continue
        rep = min(float(g.get("length_m") or 0.0), float(g.get("repeat_m") or 0.0))
        if g.get("length_m") and rep >= 0.98 * float(g["length_m"]):
            continue                                        # the same way again (return leg of a loop)
        sd = _segment_demolition(ctx, g, width)
        rd = _demo_round(sd)
        if not rd:
            continue
        g["demolition_towns"] = rd["towns"]
        g["demolition_ru"] = rd["text_ru"]
        lines.append(f"участок {k + 1} (строить, {_km(float(g.get('length_m') or 0))}): {rd['text_ru']}")
        _demo_add(total, sd)
        for p in sd["parts"]:
            parts.append({**p, "segment": k + 1, "buildings": round(p["buildings"], 1), "cost": round(p["cost"]),
                          "towns": {t: round(v, 1) for t, v in sorted(p["towns"].items(), key=lambda kv: -kv[1])}})
    if total["buildings"] < DEMO_MIN_BLD and float(getattr(r, "demolition_buildings", 0.0) or 0.0) >= DEMO_MIN_BLD \
            and not sec.get("segments"):
        # route without segments (e.g. a direct RoutePlanner route): the whole polyline as one "new" segment
        g = {"type": "new", "polyline": [list(p) for p in r.polyline], "length_m": float(r.length_m),
             "demolition_cost": r.demolition_cost, "demolition_m": r.demolition_m,
             "demolition_buildings": r.demolition_buildings, "demolition_rating": r.demolition_rating}
        sd = _segment_demolition(ctx, g, width)
        _demo_add(total, sd)
        rd = _demo_round(sd)
        if rd:
            lines.append(f"трасса: {rd['text_ru']}")
            parts += [{**p, "segment": None, "buildings": round(p["buildings"], 1), "cost": round(p["cost"]),
                       "towns": {t: round(v, 1) for t, v in p["towns"].items()}} for p in sd["parts"]]
    out = _demo_round(total)
    if not out:
        return None
    rt = getattr(r, "demolition_towns", None)
    if isinstance(rt, dict) and rt and not getattr(r, "synthetic", False):
        out["route_towns"] = {str(t): round(float(v), 4) for t, v in rt.items()}   # routing.Route as reported
    out["segments_ru"] = lines
    out["total_ru"] = (f"в т.ч. снос: ~{_nb(out['buildings'])} зд., ≈ {_m(out['cost'])} $ "
                       f"({out['m']} м трассы через застройку): {_towns_ru(out['towns'])}")
    out["parts"] = parts
    return out


def _site_fp(mode: str, end: dict | None = None) -> str | None:
    """siting.FOOTPRINT_M key of a cargo station at ``end``."""
    if mode == "water":
        return None
    if end is not None and end.get("type") == "hub":
        return "hub"
    k = f"station_{'road' if mode == 'tram' else mode}"
    return k if k in SIT.FOOTPRINT_M else "station_road"


def _site_demolition(ctx: Context, site, what: str | None) -> dict | None:
    """Demolition under a sited building (siting.Site.demolition_*), split by town."""
    if site is None or what is None or float(getattr(site, "demolition_buildings", 0.0) or 0.0) < DEMO_MIN_BLD:
        return None
    n, pen = float(site.demolition_buildings), float(site.demolition_rating)
    d = {"cost": float(site.demolition_cost), "m": 0.0, "buildings": n, "rating": pen, "towns": {}}
    tm = _tm(ctx)
    if tm is not None:
        f = tm.footprint(site.x, site.y, SIT.FOOTPRINT_M.get(what, 20.0))
        d["towns"] = _split_towns(tm, f["idx"], f["towns"], n, pen)
    if not d["towns"]:
        d["towns"] = {ctx.nearest_town(site.x, site.y)[0]: {"buildings": n, "rating_loss": pen}}
    return d


def _site_ins(dm: dict | None) -> str:
    """Instruction tail of a building: what it demolishes."""
    if not dm:
        return " Без сноса городских зданий."
    return (f" СНОС под площадку: ~{_nb(dm['buildings'])} зд. ({_towns_ru(dm['towns'])}) ≈ {_m(dm['cost'])} — "
            f"уже в цене.")


def _leg_demolition(route: dict, stations: list[dict], depots: list[dict]) -> dict | None:
    acc = _demo_new()
    items = []
    if route.get("demolition") and not route.get("free"):
        _demo_add(acc, route["demolition"])
        items.append({"what": "трасса", **{k: route["demolition"][k] for k in ("cost", "m", "buildings", "towns")}})
    for s in list(stations) + list(depots):
        dm = s.get("demolition")
        if not dm or s.get("free_by") or s.get("free"):
            continue
        _demo_add(acc, dm)
        items.append({"what": s.get("building") or s.get("what"), "x": s.get("x"), "y": s.get("y"),
                      **{k: dm[k] for k in ("cost", "buildings", "towns")}})
    out = _demo_round(acc)
    if out:
        out["items"] = items
    return out


# --------------------------------------------------------------------------- consist

def _ruling_grade(spec: D.ConsistSpec, mass_t: float, way_max: float) -> float:
    """Steepest grade on which the loaded consist keeps KEEP_SPEED_FRACTION of its top speed."""
    if spec.mode in ("water", "air"):
        return 0.0
    target = KEEP_SPEED_FRACTION * spec.top_speed
    lo, hi = 0.0, max(way_max, 0.01)
    if spec.balancing_speed(hi, mass_t) >= target:
        return hi
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if spec.balancing_speed(mid, mass_t) >= target:
            lo = mid
        else:
            hi = mid
    return lo


def _max_wagons(loco_units: list, wagon, cargo: str, grade: float, frictions: dict) -> int:
    """Most wagons of `wagon` the locos can haul loaded on `grade` keeping half top speed."""
    best = 0
    for n in range(1, 61):
        units = loco_units + [(wagon, n)]
        spec = D.consist_spec(units, "rail", frictions)
        m = spec.mass_empty_t + D.payload_t(units, cargo)
        if spec.balancing_speed(grade, m) >= KEEP_SPEED_FRACTION * spec.top_speed:
            best = n
        else:
            break
    return best


def _consist_section(ctx: Context, leg: Leg, cargo_key: str, year: int | None) -> tuple[dict, D.ConsistSpec, float, float]:
    units = leg.units
    spec = D.consist_spec(units, leg.mode if leg.mode in ("rail", "road", "water", "air", "tram") else None,
                          ctx.frictions)
    cap = sum(v.carries(cargo_key) * n for v, n in units)
    pay = D.payload_t(units, cargo_key)
    m_e, m_l = spec.mass_empty_t, spec.mass_empty_t + pay
    vehicles = []
    for v, n in units:
        vehicles.append({
            "id": v.id, "model": v.file.rsplit("/", 1)[-1], "name": v.name, "role": v.role,
            "per_consist": n, "count": n * leg.n_consists, "capacity": v.carries(cargo_key),
            "top_speed_kmh": round(v.top_speed * 3.6), "power_kw": round(D._power_kw(v)),
            "tractive_effort_kn": v.tractive_effort_kn, "weight_empty_t": round(v.weight_empty_t, 2),
            "unit_weight_t": round(D.unit_weight_t(v, cargo_key), 3), "load_speed": v.load_speed,
            "price": round(v.price), "running_cost": round(v.running_cost), "lifespan_years": round(v.lifespan_years, 1),
            "year_from": v.year_from, "year_to": v.year_to or None,
            "available": v.available(year) if year else True, "mod": v.mod,
            "friction": ctx.frictions.get(v.id)})
    label = " + ".join(f"{n}× {v.name}" if n > 1 else v.name for v, n in units)
    sec = {
        "label": label, "n_consists": leg.n_consists, "units": sum(n for _, n in units), "vehicles": vehicles,
        "length_m": round(spec.length_m), "capacity": cap, "payload_t": round(pay, 1),
        "mass_empty_t": round(m_e, 1), "mass_loaded_t": round(m_l, 1), "power_kw": round(spec.power_kw),
        "tractive_effort_kn": round(spec.te_kn, 1), "top_speed_kmh": round(spec.top_speed * 3.6),
        "kw_per_t_loaded": round(spec.power_kw / m_l, 2) if m_l else None,
        "kw_per_t_empty": round(spec.power_kw / m_e, 2) if m_e else None,
        "friction": round(spec.friction, 3), "brake_decel": spec.brake,
        "max_start_grade_loaded": round(spec.max_grade(m_l), 4), "max_start_grade_empty": round(spec.max_grade(m_e), 4),
        "performance_loaded": D.performance(spec, pay), "performance_empty": D.performance(spec, 0.0),
    }
    # mixed top speeds: the slowest unit limits the consist (game tip: loco and wagons similar)
    tops = sorted({round(v.top_speed * 3.6) for v, _ in units if v.top_speed > 0})
    sec["top_speed_note"] = (f"скорость состава ограничена самым медленным: {tops[0]} км/ч (у остальных до {tops[-1]})"
                             if len(tops) > 1 else "")
    locos = [(v, n) for v, n in units if D._power_kw(v) > 0]
    wagons = [(v, n) for v, n in units if D._power_kw(v) <= 0 and v.carries(cargo_key)]
    if leg.mode == "rail" and locos and len(wagons) == 1:
        w = wagons[0][0]
        sec["max_wagons"] = {f"{g:.3f}": _max_wagons(locos, w, cargo_key, g, ctx.frictions)
                             for g in (0.0, 0.01, 0.02, 0.03)}
    return sec, spec, pay, cap


# --------------------------------------------------------------------------- loading

def _load_time(ctx: Context, leg: Leg, cargo_key: str, cap: float) -> dict:
    carriers = [(v, n) for v, n in leg.units if v.carries(cargo_key)]
    if leg.pax:
        # passengers board every vehicle in parallel: cap / loadSpeed per vehicle (passengers.py board_s)
        board = max((v.carries(cargo_key) / max(v.load_speed, 0.1) for v, _ in carriers), default=0.0)
        if leg.pax == "loop":
            per = 0.25 * board * max(leg.stops, 1)            # ~half a load changes at every stop
            model = (f"посадка/высадка {board:.0f} с на полную машину; на кольце меняется ~половина салона на "
                     f"каждой из {max(leg.stops, 1)} остановок (passengers.py)")
        else:
            per = board
            model = f"посадка/высадка {board:.0f} с на полную машину на каждом конце (passengers.py)"
        return {"load_s": float(per), "unload_s": float(per), "load_speed": min((v.load_speed for v, _ in carriers),
                                                                               default=1.0),
                "cargo_factor": 1.0, "parallel_units": sum(n for _, n in carriers), "planner_load_s": float(per),
                "model": model}
    ls = min((v.load_speed for v, _ in carriers), default=1.0) or 1.0
    n_par = sum(n for _, n in carriers) if leg.mode in ("rail", "tram") else 1
    f = LOAD_FACTOR_DEFAULT
    if CAP is not None:
        try:
            f = CAP.cargo_load_factor(ctx.game, cargo_key)
        except Exception:
            pass
    if CAP is not None:
        t = CAP.loading_time_s(cap, ls, f, False, False, n_units=max(1, n_par))
    else:
        t = (cap / max(1, n_par)) / max(ls * f, 1e-6)
    simple = cap / max(ls, 0.1)
    return {"load_s": float(t), "unload_s": float(t), "load_speed": ls, "cargo_factor": f, "parallel_units": n_par,
            "planner_load_s": float(simple),
            "model": (f"cap/(loadSpeed×{f:g}) на {'каждый вагон параллельно' if n_par > 1 else 'машину'} "
                      f"(capacity.py, НЕ замерено в игре; модель планировщика: cap/loadSpeed = {simple:.0f} с)")}


# --------------------------------------------------------------------------- buildings

def _cargo_stop(ctx: Context, year: int, mode: str, end: dict, classes, loads: dict, unloads: dict,
                near_pt, role: str, free_by=None):
    """A street stop for unloading into a town (see CARGO_STOP_MAX_PER_YEAR), on the existing town
    street nearest to the route point inside the town; None when it does not apply."""
    if mode != "road" or end.get("kind") != "town" or loads or not unloads:
        return None
    if sum(float(q) for q in unloads.values()) > CARGO_STOP_MAX_PER_YEAR:
        return None
    # the unloaded cargo must be carriable as UNIVERSAL (the stop takes PASSENGERS + UNIVERSAL)
    for ck in unloads:
        cg = ctx.game.cargo_by_key.get(ck) if hasattr(ctx.game, "cargo_by_key") else None
        if cg is not None and "UNIVERSAL" not in (cg.classes or []):
            return None
    try:
        from . import network as _network
        net = _network.for_game(ctx.game)
    except Exception:  # noqa: BLE001
        net = None
    if net is None or near_pt is None:
        return None
    hit = net.snap_point(float(near_pt[0]), float(near_pt[1]), "road")
    if hit is None or hit[2] > 250.0:
        return None
    S = net._samples("road")
    xy = S["xy"][hit[1]]
    # deliveries go to the town buildings in the stop's catchment: among the streets near the route's
    # town end, take the point with the most buildings within the catchment (none count over water)
    try:
        from . import townmask as _tm
        tm = _tm.for_game(ctx.game)
        if tm is not None and getattr(tm, "exact", False) and len(tm.bxy) and S["tree"] is not None:
            from scipy.spatial import cKDTree
            btree = getattr(tm, "_stop_tree", None)
            if btree is None:
                btree = cKDTree(tm.bxy)
                tm._stop_tree = btree
            cand = S["tree"].query_ball_point([float(xy[0]), float(xy[1])], CARGO_STOP_SEARCH_M)
            best = None
            for g_ in cand[::3]:
                q = S["xy"][g_]
                n_b = len(btree.query_ball_point([float(q[0]), float(q[1])], SMALL_STOP_CATCHMENT))
                score = n_b - math.dist((q[0], q[1]), (xy[0], xy[1])) / 100.0
                if best is None or score > best[0]:
                    best = (score, q)
            if best is not None:
                xy = best[1]
    except Exception:  # noqa: BLE001
        pass
    if math.dist((xy[0], xy[1]), (end["x"], end["y"])) > SMALL_STOP_CATCHMENT + 400.0:
        return None
    name, c, m = _small_stop(ctx, year)
    return _station_dict(role, end, f"Дорожная остановка ({name}) — разгрузка на улице", "cargo_stop",
                         [float(xy[0]), float(xy[1])], SMALL_STOP_CATCHMENT, c, m, free_by,
                         site_reason="на существующей улице у трассы (остановка принимает универсальный груз)",
                         load={k: round(q, 1) for k, q in loads.items()},
                         unload={k: round(q, 1) for k, q in unloads.items()})


def _small_stop(ctx: Context, year: int) -> tuple[str, float, float]:
    for name in ("small_old.con", "small_mid.con", "small_new.con"):
        c = ctx.cat.construction(year, "small_stops/" + name)
        if c:
            return name, c[0], c[1]
    return "small_old.con", 45_000.0, 7_500.0


def _poly_point(poly: list, cum: np.ndarray, s: float) -> list[float]:
    if not poly:
        return [0.0, 0.0]
    if len(poly) == 1 or cum[-1] <= 0:
        return [poly[0][0], poly[0][1]]
    s = min(max(s, 0.0), float(cum[-1]))
    return [float(np.interp(s, cum, [p[0] for p in poly])), float(np.interp(s, cum, [p[1] for p in poly]))]


def _station_dict(role: str, end: dict, building: str, kind: str, pt, catch: float, cost: float, maint: float,
                  free_by: str | None, terminals: int = 1, segs: int | None = None, **extra) -> dict:
    d = {"role": role, "endpoint": end["name"], "endpoint_key": end.get("key"), "building": building, "kind": kind,
         "platform_m": segs * facilities.SEGMENT_M if segs else None, "segments": segs, "terminals": terminals,
         "x": round(pt[0]), "y": round(pt[1]),
         "dist_to_endpoint_m": round(math.dist((pt[0], pt[1]), (end["x"], end["y"]))),
         "catchment_m": catch, "cost": 0 if free_by else round(cost), "maintenance": 0 if free_by else round(maint),
         "free_by": free_by, "full_cost": round(cost), "full_maintenance": round(maint)}
    d.update(extra)
    return d


def _stations(ctx: Context, leg: Leg, year: int, spec, terminals: int, r, cargo) -> list[dict]:
    g = ctx.game
    poly = [tuple(p) for p in r.polyline]
    cum = D.polyline_cum(poly) if poly else np.zeros(1)
    L = float(cum[-1]) if len(cum) else 0.0
    out = []
    free = leg.station_free_by or (None, None)
    if leg.pax == "loop" and leg.streets and leg.streets.get("stops"):
        name, c, m = _small_stop(ctx, year)
        tram = leg.mode == "tram"
        bname = f"Малая остановка ({name})" + (" на улице с трамвайными путями" if tram else "")
        sts = leg.streets["stops"]
        n = len(sts)
        for k, st in enumerate(sts):
            out.append(_station_dict(f"остановка {k + 1}/{n} кольца", leg.src, bname, "tram_stop" if tram else "bus_stop",
                                     [st["x"], st["y"]], SMALL_STOP_CATCHMENT, c, m, free[0] if k == 0 else None,
                                     stop_no=k + 1, spacing_m=round(L / n), street=st.get("street"),
                                     edge_id=st.get("edge_id"),
                                     site_reason=f"на улице: {st.get('label')} (точка на оси улицы, в стороне от "
                                                 f"перекрёстка — остановка встраивается в существующую улицу)"))
        return out
    if leg.pax == "loop":
        name, c, m = _small_stop(ctx, year)
        tram = leg.mode == "tram"
        bname = f"Малая остановка ({name})" + (" на улице с трамвайными путями" if tram else "")
        n = max(1, int(leg.stops or 1))
        for k in range(n):
            pt = _poly_point(poly, cum, L * k / n)
            site = ctx.siting.pax_stop(pt[0], pt[1], (leg.src["x"], leg.src["y"]))
            out.append(_station_dict(f"остановка {k + 1}/{n} кольца", leg.src, bname, "tram_stop" if tram else "bus_stop",
                                     site.xy(), SMALL_STOP_CATCHMENT, c, m, free[0] if k == 0 else None,
                                     stop_no=k + 1, spacing_m=round(L / n),
                                     site_reason=site.reason + f"; кольцо по городу, остановки через ~{L / n:.0f} м"))
        return out
    if leg.pax in ("intercity", "workers"):
        if leg.mode == "rail":
            from .passengers import rail_pax_station
            cost, maint, name = rail_pax_station(ctx.cat, year, spec.length_m, terminals)
            segs = max(2, math.ceil(spec.length_m / facilities.SEGMENT_M))
            for k, end in enumerate((leg.src, leg.dst)):
                pt = list(poly[0] if k == 0 else poly[-1]) if poly else [end["x"], end["y"]]
                site = ctx.siting.pax_stop(pt[0], pt[1], (end["x"], end["y"]))
                pt = site.xy()
                out.append(_station_dict(f"вокзал в г. {end['name']}", end,
                                         f"{name} (главное здание + платформы для пассажиров)", "rail_pax_station",
                                         pt, RAIL_PAX_CATCHMENT, cost, maint, free[k], terminals, segs))
            return out
        name, c, m = _small_stop(ctx, year)
        bname = f"Малая остановка ({name})"
        sts = (leg.streets or {}).get("stops") or []
        if len(sts) >= 2:                   # stops on the streets chosen by passengers.py
            n_here = {0: 0, 1: 0}
            for q, st in enumerate(sts):
                k = 0 if (q == 0 or (st.get("town") == leg.src["name"] and q < len(sts) - 1)) else 1
                end = leg.src if k == 0 else leg.dst
                n_here[k] += 1
                where = (f"у предприятия «{end['name']}»" if end.get("kind") == "industry" else f"в г. {end['name']}")
                out.append(_station_dict(f"остановка {where}" + (f" ({n_here[k]})" if n_here[k] > 1 else ""), end,
                                         bname, "bus_stop", [st["x"], st["y"]], SMALL_STOP_CATCHMENT, c, m,
                                         free[k] if n_here[k] == 1 else None, stop_no=n_here[k], town_end=k,
                                         street=st.get("street"), edge_id=st.get("edge_id"),
                                         site_reason=f"на улице: {st.get('label')}"))
            return out
        k_town = max(1, int(leg.stops_per_town or 1))
        for k, end in enumerate((leg.src, leg.dst)):
            n_here = 1 if (leg.pax == "workers" and end.get("kind") == "industry") else k_town
            sp = min(TOWN_STOP_SPACING_M, L / (2 * n_here + 1)) if L > 0 else 0.0
            for j in range(n_here):
                s = j * sp if k == 0 else L - j * sp
                pt = _poly_point(poly, cum, s)
                site = ctx.siting.pax_stop(pt[0], pt[1], (end["x"], end["y"]))
                pt = site.xy()
                where = (f"у предприятия «{end['name']}»" if end.get("kind") == "industry"
                         else f"в г. {end['name']}")
                role = f"остановка {where}" + (f" ({j + 1}/{n_here})" if n_here > 1 else "")
                reason = (f"у предприятия, в {SMALL_STOP_CATCHMENT:.0f} м от его зданий (рабочие идут только с остановки)"
                          if end.get("kind") == "industry" else
                          site.reason + (" — у центра" if j == 0 else f" — {j * sp:.0f} м от центра по маршруту"))
                out.append(_station_dict(role, end, bname, "bus_stop", pt, SMALL_STOP_CATCHMENT, c, m,
                                         free[k] if j == 0 else None, stop_no=j + 1, town_end=k, site_reason=reason))
        return out
    # cargo
    classes = cargo.classes if cargo else ["UNIVERSAL"]
    st = facilities.station(ctx.cat, "road" if leg.mode == "tram" else leg.mode, year, classes, spec.length_m, terminals)
    segs = max(1, math.ceil(spec.length_m / facilities.SEGMENT_M)) if leg.mode == "rail" else None
    catch = CATCHMENT_M.get(leg.mode, 150.0)
    for k, (end, role) in enumerate(((leg.src, "погрузка"), (leg.dst, "выгрузка"))):
        if leg.mode == "water" and leg.builtin[k]:
            out.append(_station_dict(role, end, "встроенный причал предприятия (порт строить не нужно)",
                                     "builtin_terminal", [end["x"], end["y"]], 0.0, 0.0, 0.0,
                                     "встроенный причал", 1, None, builtin=True,
                                     site_reason="морское предприятие: встроенный причал для судов"))
            continue
        if k == 1 and leg.volume and cargo is not None:
            near = poly[-1] if poly else None
            stop = _cargo_stop(ctx, year, leg.mode, end, classes, {}, {cargo.key: float(leg.volume)}, near, role,
                               free[k])
            if stop is not None:
                out.append(stop)
                continue
        site = _site_station(ctx, leg.mode, end, poly, k == 0, catch)
        dm = _site_demolition(ctx, site, _site_fp(leg.mode, end))
        out.append(_station_dict(role, end, st.name, st.kind, site.xy(), catch, st.cost + (dm["cost"] if dm else 0.0),
                                 st.maintenance, free[k], terminals, segs, site_reason=site.reason, site_ok=site.ok,
                                 demolition=None if free[k] else _demo_round(dm)))
    return out


def _site_station(ctx: Context, mode: str, end: dict, poly: list, at_start: bool, catch: float,
                  route_pt=None):
    """Where to put a cargo station / harbor at a line end (siting.py rules) -> siting.Site."""
    S = ctx.siting
    if route_pt is None:
        route_pt = SIT.route_point(poly, at_start) if poly else None
    if mode == "water":
        water_end = (poly[0] if at_start else poly[-1]) if poly else None
        return S.harbor(end, water_end, catch)
    if end.get("type") == "hub":
        return S.hub_land_station(end, route_pt, catch)
    if end.get("kind") == "industry":
        return S.industry_station(end, route_pt, catch, mode)
    if end.get("kind") == "town":
        return S.town_station(end, route_pt, catch, "road" if mode == "tram" else mode)
    pt = (poly[0] if at_start else poly[-1]) if poly else (end["x"], end["y"])
    return SIT.Site(pt[0], pt[1], "начало линии у оценочного места будущего предприятия", ok=False)


def _depots(ctx: Context, leg: Leg, year: int, n_veh: int, stations: list[dict], poly=None) -> list[dict]:
    cat = ctx.cat
    mode = leg.mode
    real = [s for s in stations if not s.get("builtin")]
    depot_free = any(s["free_by"] for s in real)
    idx = min(leg.depot_end, len(stations) - 1) if stations else 0
    anchor = stations[idx] if stations else {"x": leg.src["x"], "y": leg.src["y"], "endpoint": leg.src["name"]}
    if anchor.get("builtin") and real:
        anchor = real[0]
    where_end = "у первой остановки" if leg.pax else ("у станции на узле" if leg.depot_end == 1 else
                                                     "рядом с погрузочной станцией")
    if mode == "water":
        where_end = "на воде рядом с портом" + (" узла" if leg.depot_end == 1 else "")
    _, drad_, _, mrad_ = MAINT.get(mode, MAINT["road"])
    S = ctx.siting
    a_ru = "от первой остановки" if leg.pax else "от станции погрузки"
    dsite = S.depot((anchor["x"], anchor["y"]), poly, drad_, mode, "депо", anchor_ru=a_ru)
    pd = (leg.streets or {}).get("depot") if leg.pax else None
    if pd:
        dsite = SIT.Site(float(pd["x"]), float(pd["y"]), pd.get("reason") or "вне застройки у дороги")
    msite = S.depot((anchor["x"], anchor["y"]), poly, mrad_, mode, "здание ТО", avoid=[(dsite.x, dsite.y)],
                    anchor_ru=a_ru)
    if mode == "tram":
        from .passengers import _depot
        cost, maint, name = _depot(cat, ctx.game, "tram", year, n_veh)
        k = max(1, math.ceil(n_veh / facilities.DEPOT_CAPACITY["tram"]))
        dpool, drad, _, _ = MAINT["tram"]
        dx, dy = dsite.xy()
        tdm = None if depot_free else _site_demolition(ctx, dsite, "depot_road")
        return [{"what": "Трамвайное депо", "count": k, "pool": dpool, "radius_m": drad,
                 "where": f"({dx}, {dy}): {dsite.reason}; подключить к трамвайным путям кольца",
                 "x": dx, "y": dy, "cost": 0 if depot_free else round(cost + (tdm["cost"] if tdm else 0.0)),
                 "site_reason": dsite.reason,
                 "maintenance": 0 if depot_free else round(maint), "free": depot_free, "demolition": _demo_round(tdm)}]
    dmode = "road" if mode == "tram" else mode
    fpk = "rail" if mode == "rail" else "road"
    ddm = None if (depot_free or mode == "water") else _site_demolition(ctx, dsite, f"depot_{fpk}")
    dp_total = facilities.depot(cat, dmode, year, n_veh)
    dp_one = facilities.depot(cat, dmode, year, 1)
    dpool, drad, mpool, mrad = MAINT.get(mode, MAINT["road"])
    extra = max(0, n_veh - facilities.DEPOT_CAPACITY.get(mode, 12))
    n_maint = math.ceil(extra / facilities.MAINT_CAPACITY.get(mode, 16)) if extra else 0
    dx, dy = dsite.xy()
    depots = [{"what": dp_one.name, "count": 1, "pool": dpool, "radius_m": drad,
               "where": f"({dx}, {dy}): {dsite.reason}", "site_reason": dsite.reason,
               "anchor": f"{where_end} «{anchor.get('endpoint', '')}» ({anchor['x']}, {anchor['y']})",
               "x": dx, "y": dy,
               "cost": 0 if depot_free else round(dp_one.cost + (ddm["cost"] if ddm else 0.0)),
               "maintenance": 0 if depot_free else round(dp_one.maintenance),
               "free": depot_free, "demolition": _demo_round(ddm)}]
    if n_maint:
        mc = (dp_total.cost - dp_one.cost) / n_maint
        mm = (dp_total.maintenance - dp_one.maintenance) / n_maint
        mx, my = msite.xy()
        mdm = None if mode == "water" else _site_demolition(ctx, msite, f"maint_{fpk}")
        depots.append({"what": "Здание техобслуживания", "count": n_maint, "pool": mpool, "radius_m": mrad,
                       "where": f"({mx}, {my}): {msite.reason}" + (" — несколько зданий рядом друг с другом"
                                                                 if n_maint > 1 else ""),
                       "site_reason": msite.reason, "x": mx, "y": my,
                       "cost": round(mc * n_maint + (mdm["cost"] if mdm else 0.0)), "maintenance": round(mm * n_maint),
                       "free": False, "demolition": _demo_round(mdm)})
    return depots


# --------------------------------------------------------------------------- per leg

def _merge_runs(runs: list, offsets: list[float]):
    """One RunResult from consecutive runs (tour segments: every stop starts and ends at standstill)."""
    if len(runs) == 1:
        return runs[0]
    add = lambda f: float(sum(getattr(r, f) for r in runs))
    t = add("time_s")
    dist = add("distance_m")
    trace, stall = [], []
    for r, off in zip(runs, offsets):
        trace += [(p[0] + off, *p[1:]) for p in r.trace]
        stall += [x + off for x in r.stall_at_m]
    bns = [(r.bottleneck, off) for r, off in zip(runs, offsets) if r.bottleneck]
    bn = None
    if bns:
        b, off = max(bns, key=lambda x: x[0].get("lost_s", 0) or 0)
        bn = dict(b)
        for k in ("from_m", "to_m"):
            if bn.get(k) is not None:
                bn[k] = bn[k] + off
    climbs = [r.min_climb_speed for r in runs if r.min_climb_speed is not None]
    return D.RunResult(
        mode=runs[0].mode, distance_m=dist, time_s=t, avg_speed=dist / t if t > 0 else 0.0,
        max_speed=max(r.max_speed for r in runs), top_speed=runs[0].top_speed, ideal_time_s=add("ideal_time_s"),
        limits_time_s=add("limits_time_s"), time_lost_s=add("time_lost_s"), lost_limits_s=add("lost_limits_s"),
        lost_accel_s=add("lost_accel_s"), lost_brake_s=add("lost_brake_s"), lost_grade_s=add("lost_grade_s"),
        accel_time_s=add("accel_time_s"), brake_time_s=add("brake_time_s"), energy_mj=add("energy_mj"),
        mass_t=max(r.mass_t for r in runs), payload_t=max(r.payload_t for r in runs),
        stalled=any(r.stalled for r in runs), stall_at_m=stall, min_climb_speed=min(climbs) if climbs else None,
        bottleneck=bn, trace=trace)


def _leg_dossier(ctx: Context, leg: Leg, year: int, warnings: list[str]) -> dict:
    if leg.tour:
        return _tour_leg_dossier(ctx, leg, year, warnings)
    g = ctx.game
    cargo = g.cargo_by_key.get(leg.cargo) if leg.cargo else None
    cargo_key = leg.cargo or "passengers"
    r, rp, src_text = _route_for(ctx, leg)
    route = _route_section(ctx, leg, r, rp, src_text)
    way = getattr(rp, "way", None) if rp else None
    bridge = getattr(rp, "bridge", None) if rp else None
    base = {"role": leg.role, "role_ru": ROLE_RU.get(leg.role, leg.role), "mode": leg.mode,
            "mode_ru": MODE_RU.get(leg.mode, leg.mode), "title": leg.title, "from": leg.src, "to": leg.dst,
            "loop": leg.loop, "pax": leg.pax, "estimate": leg.estimate}
    if not leg.units:
        warnings.append("Состав не распознан (модели техники не найдены в каталоге) — расчёт движения пропущен.")
        return {**base, "route": route, "error": "no consist"}
    consist, spec, pay, cap = _consist_section(ctx, leg, cargo_key, year)
    L = float(r.length_m)

    # ---- profiles and speed limits
    way_max = way.max_slope if way else 0.13
    ruling = _ruling_grade(spec, spec.mass_empty_t + pay, way_max)
    # buses/trams use the existing streets and roads: no ruling grade to design
    street = bool(leg.pax and leg.mode in ("road", "tram"))
    design_grade = (None if (leg.mode in ("water", "air") or street)
                    else max(MIN_RULING_GRADE, min(way_max, ruling)))
    # existing streets/roads: the terrain under them is smoothed by the game; cap like a built road
    sim_grade = min(way_max, STREET_MAX_GRADE) if street else design_grade
    coeffs = D.speed_coeffs(g, way.key) if way else None
    zones = D.route_speed_limits(r, way, bridge, coeffs) if (way and leg.mode not in ("water", "air")) else []
    stops = []
    if leg.loop and leg.stops > 1:
        stops = [L * k / leg.stops for k in range(1, leg.stops)]
    elif leg.pax == "intercity" and leg.stops_per_town > 1 and L > 0:
        k_town = leg.stops_per_town
        sp = min(TOWN_STOP_SPACING_M, L / (2 * k_town + 1))
        stops = sorted([j * sp for j in range(1, k_town)] + [L - j * sp for j in range(1, k_town)])
    load = _load_time(ctx, leg, cargo_key, cap)
    stops_n = (1 if leg.loop else 2) + len(stops)
    dwell = stops_n * STOP_DELAYS_S + (1 if leg.loop else 2) * MANOEUVRE_S.get(leg.mode, 10.0)
    volume = float(leg.volume) if leg.volume else None
    dirs = 1.0                                  # loaded directions / turnover per cycle
    if leg.pax in ("intercity", "workers"):
        dirs = 2.0
    elif leg.pax == "loop":
        avg = (leg.info or {}).get("avg_trip_m")
        ride = avg * RIDE_DETOUR_TOWN if avg else L / max(leg.stops, 1)
        dirs = max(1.0, min(float(max(leg.stops, 1)), L / max(ride, 1.0)))
    if leg.loop:
        pay_run = pay if not leg.pax else 0.5 * pay
        lr = D.simulate_run(leg.units, pay_run, r.profile, zones, stops, mode=spec.mode, max_grade=sim_grade,
                            spec=spec, round_trip=False)
        er = D.simulate_run(leg.units, 0.0, r.profile, zones, stops, mode=spec.mode, max_grade=sim_grade,
                            spec=spec, round_trip=False)
        rt = lr.time_s + load["load_s"] + load["unload_s"] + dwell
        trips = ctx.year_s / rt if rt > 0 else 0.0
        per = cap * trips
        need = max(1, math.ceil(volume / (per * dirs))) if (volume and per > 0) else None
        cyc = SimpleNamespace(loaded=lr, empty=er, round_trip_s=rt, trips_per_year=trips, per_consist_year=per,
                              vehicles_needed=need)
    else:
        cyc = D.cycle(leg.units, r, (load["load_s"], load["unload_s"]), dwell, cargo_key=cargo_key,
                      volume=(volume / dirs if volume else None), year_s=ctx.year_s, speed_limits=zones,
                      max_grade=sim_grade, mode=spec.mode, frictions=ctx.frictions, stops=stops)
    terrain_run = None
    if (not leg.loop and not leg.pax and leg.mode in ("rail", "road", "tram") and r.profile and len(r.profile) > 2):
        terrain_run = D.simulate_run(leg.units, pay, r.profile, zones, stops, mode=spec.mode, max_grade=way_max,
                                     spec=spec, round_trip=True)
    # design vs terrain-following track: earthworks to hold the ruling grade
    earth = None
    if design_grade is not None and not leg.loop and r.profile and len(r.profile) > 2:
        s1, h_des = D.track_profile(r.profile, 10.0, design_grade)
        _, h_ter = D.track_profile(r.profile, 10.0, way_max)
        diff = h_des - h_ter
        bw = BED_WIDTH.get(leg.mode, 6.0)
        vol = float(np.sum(np.abs(diff) * (bw + np.abs(diff))) * 10.0)
        g_des = np.abs(np.gradient(h_des, 10.0))
        earth = {"ruling_grade": round(design_grade, 4), "max_fill_m": round(max(0.0, float(diff.max())), 1),
                 "max_cut_m": round(max(0.0, float(-diff.min())), 1), "volume_m3": round(vol),
                 "cost_if_paid": round(vol * (getattr(rp, "terrain_cost", 7.3125) if rp else 7.3125)),
                 "max_grade_design": round(float(g_des.max()), 4)}

    # ---- ruling-grade variants: time vs earthworks trade-off
    variants = []
    if design_grade is not None and not leg.loop and not leg.pax and r.profile and len(r.profile) > 2:
        _, h_ter10 = D.track_profile(r.profile, 10.0, way_max)
        grades = sorted({round(design_grade, 4), *[x for x in (0.01, 0.02, 0.03, 0.05, 0.08) if x < way_max],
                         round(way_max, 4)})
        for gv in grades:
            if abs(gv - design_grade) < 1e-4:
                run = cyc.loaded
                run.empty = cyc.empty
            elif terrain_run is not None and abs(gv - way_max) < 1e-4:
                run = terrain_run
            else:
                run = D.simulate_run(leg.units, pay, r.profile, zones, stops, mode=spec.mode, max_grade=gv,
                                     spec=spec, round_trip=True, ds=10.0)
            _, h_v = D.track_profile(r.profile, 10.0, gv)
            dv = h_v - h_ter10
            variants.append({"grade": gv, "loaded_s": round(run.time_s, 1), "empty_s": round(run.empty.time_s, 1),
                             "round_trip_s": round(run.time_s + run.empty.time_s + load["load_s"] + load["unload_s"] + dwell, 1),
                             "stalled": run.stalled or run.empty.stalled,
                             "max_fill_m": round(max(0.0, float(dv.max())), 1), "max_cut_m": round(max(0.0, float(-dv.min())), 1),
                             "recommended": abs(gv - design_grade) < 1e-4})

    # ---- simple planner-style timing for comparison
    one_way_simple = L / max(spec.top_speed * PLANNER_SPEED_FACTOR, 0.1)
    rt_simple = (1 if leg.loop else 2) * one_way_simple + 2 * load["planner_load_s"] + stops_n * STOP_DELAYS_S

    # ---- capacity
    n = max(1, leg.n_consists)
    per_consist = cyc.per_consist_year * dirs
    fleet = n * per_consist
    moved = min(volume, fleet) if volume else fleet
    load_factor = moved / fleet if fleet > 0 else 0.0
    headway = cyc.round_trip_s / n
    wait_for_cargo = 0.0
    if not leg.pax and volume and fleet > volume and volume > 0:
        # with full load the consists wait at the loading end until the production fills them
        wait_for_cargo = max(0.0, n * cap * ctx.year_s / volume - cyc.round_trip_s)
    dwell_end = load["load_s"] + STOP_DELAYS_S + MANOEUVRE_S.get(leg.mode, 10.0)
    terminals = 1
    if CAP is not None:
        try:
            terminals = CAP.terminals_needed(CAP.terminal_occupancy(n, cyc.round_trip_s, dwell_end))
        except Exception:
            terminals = 1
    else:
        terminals = max(1, math.ceil(n * dwell_end / cyc.round_trip_s / 0.7))
    rail_layout = None
    if leg.mode == "rail" and CAP is not None and way is not None and n > 1 and not leg.loop:
        try:
            v_eff = L / max(cyc.loaded.time_s, 1.0)
            rp_ = CAP.rail_line_plan(n * cyc.trips_per_year, L, v_eff, spec.length_m, way.cost_per_m,
                                     way.maintenance_per_m, n_trains=n, year_s=ctx.year_s)
            rail_layout = {"kind": rp_.kind, "loops": rp_.loops, "capex": round(rp_.capex),
                           "maintenance": round(rp_.maintenance), "capacity_trains_year": round(rp_.capacity, 1),
                           "utilization": round(rp_.utilization, 3), "speed_factor": round(rp_.speed_factor, 3),
                           "platoon": rp_.platoon, "reason": rp_.reason,
                           "spacing_m": round(L / (rp_.loops + 1)) if rp_.loops else None}
        except Exception as e:                       # pragma: no cover
            rail_layout = {"kind": "?", "reason": str(e)}

    # ---- buildings
    stations = _stations(ctx, leg, year, spec, terminals, r, cargo)
    depots = _depots(ctx, leg, year, n, stations, list(r.polyline))

    # ---- money
    src_xyz = (leg.src["x"], leg.src["y"], leg.src["z"])
    dst_xyz = (leg.dst["x"], leg.dst["y"], leg.dst["z"])
    override = leg.income_override is not None and (leg.pax or leg.role != "line" or leg.loop)
    boost = 0.0
    if leg.pax == "workers" and (leg.info or {}).get("boosted"):
        boost = float(leg.info.get("boost_value") or 0.0)
    if override:
        income = float(leg.income_override) * (min(1.0, fleet / volume) if (leg.pax and volume and fleet > 0) else 1.0)
        ipu = income / moved if moved else 0.0
    elif cargo and not leg.loop:
        ipu = g.income_per_unit(cargo, src_xyz, dst_xyz)
        income = moved * ipu
    else:
        ipu, income = 0.0, 0.0
    chord = math.dist(src_xyz, dst_xyz)
    veh_capex = sum(v.price * k for v, k in leg.units) * n
    running = sum(v.running_cost * k for v, k in leg.units) * n
    route_capex = 0.0 if route["free"] else route["cost"]["total"]
    route_maint = 0.0 if route["free"] else route["maintenance"]["total"]
    loops_capex = (rail_layout or {}).get("capex", 0) or 0
    loops_maint = (rail_layout or {}).get("maintenance", 0) or 0
    st_capex = sum(s["cost"] for s in stations)
    st_maint = sum(s["maintenance"] for s in stations)
    dp_capex = sum(d["cost"] for d in depots)
    dp_maint = sum(d["maintenance"] for d in depots)
    infra = route_capex + st_capex + dp_capex
    maint = route_maint + st_maint + dp_maint
    profit = income + boost - running - maint
    capex = infra + veh_capex
    annuity = (1 - (1 + DISCOUNT) ** -NPV_YEARS) / DISCOUNT
    trips_total = n * cyc.trips_per_year
    money = {
        "income_per_unit": round(ipu, 2), "chord_m": round(chord), "income_per_unit_km_route": round(ipu / (L / 1000), 2) if L else None,
        "income_per_trip": round(income / trips_total) if (leg.pax and trips_total) else round(cap * ipu),
        "moved_per_year": round(moved, 1), "income_per_year": round(income + boost),
        "ticket_income_per_year": round(income) if leg.pax else None, "boost_income_per_year": round(boost) if boost else None,
        "capex": {"route": round(route_capex), "stations": round(st_capex), "depots": round(dp_capex),
                  "vehicles": round(veh_capex), "total": round(capex), "passing_loops_extra": round(loops_capex)},
        "per_year": {"running": round(running), "route_maintenance": round(route_maint),
                     "stations_maintenance": round(st_maint), "depots_maintenance": round(dp_maint),
                     "total_costs": round(running + maint), "passing_loops_maintenance_extra": round(loops_maint)},
        "profit_per_year": round(profit), "profit_per_trip": round(profit / trips_total) if trips_total else None,
        "payback_years": round(capex / profit, 2) if profit > 0 else None,
        "npv": round(profit * annuity - capex), "npv_years": NPV_YEARS, "discount": DISCOUNT,
        "vehicle_depreciation_per_year": round(veh_capex / max(min((v.lifespan_years for v, _ in leg.units), default=30), 1)),
        "plan": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in (leg.plan or {}).items()},
    }
    if leg.plan.get("income_per_year"):
        money["plan_vs_detail"] = {
            "income_ratio": round((income + boost) / leg.plan["income_per_year"], 3) if leg.plan["income_per_year"] else None,
            "cost_ratio": round(capex / leg.plan["cost"], 3) if leg.plan.get("cost") else None}

    # ---- delivery deadline
    ttd = float(cargo.time_to_deliver_s) if (cargo and cargo.time_to_deliver_s and not leg.pax) else None
    transit = load["load_s"] + cyc.loaded.time_s + load["unload_s"] + 0.5 * headway
    delivery = None
    if ttd:
        delivery = {"time_to_deliver_s": ttd, "transit_s": round(transit, 1),
                    "ok": transit <= ttd, "margin_s": round(ttd - transit, 1),
                    "to_town": leg.dst.get("kind") == "town",
                    "note": ("груз к городу: опоздание портит рейтинг доставки города" if leg.dst.get("kind") == "town"
                             else "опоздание не снижает оплату (только рейтинг ближайшего города)")}

    # ---- timing
    lr, er = cyc.loaded, cyc.empty
    timing = {
        "loaded": lr.summary(), "empty": er.summary(), "one_way": bool(leg.loop),
        "load_s": round(load["load_s"], 1), "unload_s": round(load["unload_s"], 1), "loading_model": load["model"],
        "stops": stops_n, "stop_delays_s": stops_n * STOP_DELAYS_S,
        "manoeuvre_s": (1 if leg.loop else 2) * MANOEUVRE_S.get(leg.mode, 10.0), "dwell_s": round(dwell, 1),
        "round_trip_s": round(cyc.round_trip_s, 1), "round_trip_days": round(cyc.round_trip_s / DAY_S, 1),
        "trips_per_year": round(cyc.trips_per_year, 3), "headway_s": round(headway, 1),
        "wait_for_cargo_s": round(wait_for_cargo, 1),
        "planner_round_trip_s": round(rt_simple, 1),
        "planner_diff": round(cyc.round_trip_s / rt_simple - 1, 3) if rt_simple > 0 else None,
        "ruling_grade": None if design_grade is None else round(design_grade, 4),
        "ruling_grade_reason": (f"самый крутой уклон, на котором гружёный состав держит ≥{KEEP_SPEED_FRACTION:.0%} "
                                f"максимальной скорости" if design_grade is not None else None),
        "terrain_variant": None if terrain_run is None else {
            "max_grade": round(way_max, 3), "loaded_s": round(terrain_run.time_s, 1),
            "empty_s": round(terrain_run.empty.time_s, 1), "stalled": terrain_run.stalled or terrain_run.empty.stalled,
            "stall_at_m_loaded": [round(x) for x in terrain_run.stall_at_m[:10]],
            "stall_at_m_empty": [round(L - x) for x in terrain_run.empty.stall_at_m[:10]]},
        "earthworks": earth, "grade_variants": variants,
    }
    capacity = {"per_trip": cap, "per_consist_year": round(per_consist, 1), "fleet_year": round(fleet, 1),
                "volume": volume, "moved": round(moved, 1), "load_factor": round(load_factor, 3),
                "consists_needed": cyc.vehicles_needed, "consists_planned": n, "terminals": terminals,
                "terminal_dwell_s": round(dwell_end, 1), "rail_layout": rail_layout,
                "directions": round(dirs, 2)}

    # ---- passenger facts
    pax = None
    if leg.pax:
        info = leg.info or {}
        pax = {"type": leg.pax, "type_ru": {"loop": "кольцо по городу", "intercity": "между городами",
                                             "workers": "рабочие на предприятие"}[leg.pax],
               "stops": len(stations), "stops_per_town": leg.stops_per_town if leg.pax == "intercity" else None,
               "headway_s": round(headway, 1), "headway_plan_s": info.get("headway_s"),
               "vehicles_per_hour_game": None,
               "passengers_per_year": round(moved, 1), "demand_per_year": info.get("demand"),
               "capacity_per_year": round(fleet, 1), "capacity_plan": info.get("capacity"),
               "ticket": round(ipu), "ticket_plan": info.get("ticket"), "ticket_income_per_year": round(income),
               "coverage": info.get("coverage"), "avg_trip_m": info.get("avg_trip_m"), "feeders": info.get("feeders"),
               "turnover": round(dirs, 2) if leg.pax == "loop" else None}
        if leg.pax == "workers":
            d2 = info.get("distinct_workers_2y")
            pax.update({"workers_per_year": info.get("workers_per_year"), "distinct_workers_2y": d2,
                        "min_workers": MIN_WORKERS, "boost": bool(info.get("boosted")),
                        "boost_factor": info.get("boost_factor"), "boost_value": info.get("boost_value"),
                        "rule": ("Бонус производства ×(1,5 + случайное 0…1) включается, если за последние 2 года "
                                 f"на предприятие по линии приехали ≥{MIN_WORKERS} РАЗНЫХ жителей "
                                 "(у предприятия 20 рабочих мест; пешком и на машине туда не ходят).")})

    # ---- warnings
    tag = f"[{leg.title or ROLE_RU.get(leg.role, leg.role) + ' ' + leg.src['name'] + ' → ' + leg.dst['name']}] " \
        if leg.role != "line" else ""
    if volume and fleet < 0.97 * volume:
        warnings.append(f"{tag}Провозная способность {fleet:.0f} {'пасс.' if leg.pax else 'ед.'}/год меньше объёма "
                        f"{volume:.0f}: нужно {cyc.vehicles_needed} состав(ов) вместо {n}.")
    if volume and load_factor < 0.6 and not leg.pax:
        warnings.append(f"{tag}Загрузка парка {load_factor:.0%}: составов больше, чем груза "
                        f"(хватит {cyc.vehicles_needed}); лишние будут стоять под погрузкой.")
    if lr.stalled or er.stalled:
        if street:
            warnings.append(f"{tag}{consist['label']} на подъёмах (до {_pct(sim_grade or 0, 0)}) почти встаёт — "
                            f"выбрать машину мощнее или маршрут через более пологие улицы.")
        else:
            warnings.append(f"{tag}Даже на рекомендуемом уклоне {_pct(design_grade or way_max)} состав местами ползёт — "
                            f"нужен второй локомотив или меньше вагонов.")
    if terrain_run is not None and (terrain_run.stalled or terrain_run.empty.stalled):
        at = terrain_run.stall_at_m[:4]
        warnings.append(f"{tag}Если тянуть путь по рельефу (уклоны до {_pct(way_max, 0)}), гружёный состав "
                        f"встанет/поползёт на км {', '.join(f'{x / 1000:.1f}' for x in at) or '—'}: "
                        f"держите уклон ≤ {_pct(design_grade or 0)}.")
    if earth and max(earth["max_fill_m"], earth["max_cut_m"]) > 25:
        warnings.append(f"{tag}Для уклона ≤ {_pct(earth['ruling_grade'])} нужны насыпи/выемки до "
                        f"{max(earth['max_fill_m'], earth['max_cut_m']):.0f} м — вероятно понадобятся виадук или тоннель "
                        f"(или обход по более пологой местности).")
    if delivery and not delivery["ok"] and delivery["to_town"]:
        warnings.append(f"{tag}Доставка {transit:.0f} с > срока {ttd:.0f} с: город будет недоволен.")
    if consist.get("top_speed_note"):
        warnings.append(f"{tag}{consist['top_speed_note']}.")
    for v in consist["vehicles"]:
        if not v["available"]:
            warnings.append(f"{tag}{v['name']} недоступен в {year} г. ({v['year_from']}–{v['year_to'] or '…'}).")
    segs = stations[0].get("segments") if stations else None
    if leg.mode == "rail" and segs and spec.length_m > segs * facilities.SEGMENT_M:
        warnings.append(f"{tag}Поезд {spec.length_m:.0f} м длиннее платформы — грузится в 4 раза медленнее.")
    if leg.pax == "workers" and pax and pax.get("distinct_workers_2y") is not None \
            and pax["distinct_workers_2y"] < MIN_WORKERS:
        warnings.append(f"{tag}Разных рабочих за 2 года ~{pax['distinct_workers_2y']:.0f} < {MIN_WORKERS}: бонуса "
                        f"производства не будет, линия окупается только билетами.")
    if leg.estimate:
        warnings.append(f"{tag}Начало линии — оценочное место будущего предприятия: пересчитать после его появления.")
    for s_ in stations:
        if leg.pax == "loop" or s_.get("builtin") or s_["free_by"]:
            continue
        if s_["dist_to_endpoint_m"] > s_["catchment_m"] and not (leg.pax and s_.get("stop_no", 1) > 1):
            end = leg.src if s_["endpoint"] == leg.src["name"] else leg.dst
            if end.get("type") == "hub" or end.get("kind") in ("planned", "town"):
                continue                             # hub: warehouse in range; town: placed at its edge on purpose
            warnings.append(f"{tag}Станция ({s_['role']}) у «{s_['endpoint']}» в {s_['dist_to_endpoint_m']} м от центра — "
                            f"больше охвата {s_['catchment_m']:.0f} м"
                            + (": придвинуть ближе или поставить склад." if end.get("kind") == "industry"
                               else " (для города охват покрывает только ближний район)."))
    if rail_layout and rail_layout.get("kind") == "infeasible":
        warnings.append(f"{tag}Однопутка не пропустит столько поездов: {rail_layout.get('reason')}.")

    # ---- charts
    s_des, h_des = (D.track_profile(r.profile, 10.0, design_grade) if design_grade is not None
                    else D.track_profile(r.profile, 10.0, None, 0))
    back = (lambda s: s) if leg.loop else (lambda s: L - s)
    chart = {
        "length_m": round(L),
        "terrain": [[round(p[0]), round(p[1], 1)] for p in _decimate(list(r.profile))],
        "track": [[round(float(a)), round(float(b), 1)] for a, b in _decimate(list(zip(s_des, h_des)))],
        "loaded": [[round(t[0]), round(t[2] * 3.6, 1), round(t[3] * 3.6, 1)] for t in _decimate(lr.trace)],
        "empty": [[round(back(t[0])), round(t[2] * 3.6, 1), round(t[3] * 3.6, 1)] for t in _decimate(er.trace)],
        "zones": [{"from_m": round(z[0]), "to_m": round(z[1]), "kmh": round(z[2] * 3.6), "reason": z[3]}
                  for z in zones if z[3] != "way"][:80],
        "top_kmh": round(spec.top_speed * 3.6),
        "water_level": float(g.terrain.water_level),
    }
    return {**base, "route": route, "consist": consist, "timing": timing, "capacity": capacity, "stations": stations,
            "depots": depots, "money": money, "delivery": delivery, "chart": chart, "passengers": pax,
            "demolition": _leg_demolition(route, stations, depots)}


# --------------------------------------------------------------------------- tours

def _tour_visits(segs: list[dict], flows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Visits (line stops in order) with what is loaded/unloaded there, and flows with their visit
    indices: a flow A->B is loaded at the visit of A from which B comes first going forward."""
    nodes = [s["a"] for s in segs]
    n = len(nodes)
    visits = [{"n": i + 1, "node": nodes[i], "load": {}, "unload": {}} for i in range(n)]
    placed = []
    for f in flows:
        best = None
        for i in range(n):
            if nodes[i].key != f["from"].key:
                continue
            for k in range(1, n + 1):
                if nodes[(i + k) % n].key == f["to"].key:
                    if best is None or k < best[1]:
                        best = (i, k)
                    break
        if best is None:
            continue
        i, k = best
        j = (i + k) % n
        visits[i]["load"][f["cargo"]] = visits[i]["load"].get(f["cargo"], 0.0) + f["volume"]
        visits[j]["unload"][f["cargo"]] = visits[j]["unload"].get(f["cargo"], 0.0) + f["volume"]
        placed.append({**f, "load_visit": i, "unload_visit": j, "segments": [(i + q) % n for q in range(k)]})
    return visits, placed


def _cargo_list(ctx: Context, d: dict) -> str:
    g = ctx.game
    return ", ".join(f"{g.cargo_by_key[c].name.lower() if c in g.cargo_by_key else c} {v:.0f}" for c, v in d.items())


def _tour_leg_dossier(ctx: Context, leg: Leg, year: int, warnings: list[str]) -> dict:
    g = ctx.game
    T = leg.tour
    mode = leg.mode
    segs = T["segments"]
    visits, flows = _tour_visits(segs, T["flows"])
    main = leg.cargo or (flows[0]["cargo"] if flows else None)
    cargo = g.cargo_by_key.get(main) if main else None
    consist, spec, pay, cap = _consist_section(ctx, leg, main, year)
    peak = T["peak_load"] or leg.volume or 0.0
    # ---- routes per segment (shuttles run the same road both ways: built once)
    routes = []
    seen: dict[tuple, int] = {}
    for k, s in enumerate(segs):
        sl = _mkleg(mode=mode, src=_endpoint_dict(ctx, s["a"]), dst=_endpoint_dict(ctx, s["b"]),
                    plan={"length_m": s["length_m"]})
        r, rp, src_text = _route_for(ctx, sl)
        pk = (frozenset((s["a"].key, s["b"].key)), mode)
        prior_line = T["prior_pairs"].get(pk) if mode != "water" else None
        shared = seen.get(pk)
        sl.route_free_by = (f"линия {prior_line}" if prior_line else
                            f"тот же путь, что участок {shared + 1}" if shared is not None else None)
        sec = _route_section(ctx, sl, r, rp, src_text)
        seen.setdefault(pk, k)
        routes.append((sl, r, rp, sec, prior_line, shared))
    rp0 = next((x[2] for x in routes if x[2] is not None), None)
    way = getattr(rp0, "way", None)
    bridge = getattr(rp0, "bridge", None)
    way_max = way.max_slope if way else 0.13
    ruling = _ruling_grade(spec, spec.mass_empty_t + pay, way_max)
    design_grade = None if mode in ("water", "air") else max(MIN_RULING_GRADE, min(way_max, ruling))
    coeffs = D.speed_coeffs(g, way.key) if way else None
    # ---- runs: every stop starts/ends at standstill, so the cycle = sum of the segment runs
    runs, runs_e, offs = [], [], []
    poly_all, terrain, track, structures, zones_all, seg_out = [], [], [], [], [], []
    on_board = [dict() for _ in segs]
    for f in flows:
        for q in f["segments"]:
            on_board[q][f["cargo"]] = on_board[q].get(f["cargo"], 0.0) + f["volume"]
    off = 0.0
    for k, (sl, r, rp, sec, prior_line, shared) in enumerate(routes):
        s = segs[k]
        zones = D.route_speed_limits(r, way, bridge, coeffs) if (way and mode not in ("water", "air")) else []
        frac = min(1.0, s["load"] / peak) if peak > 0 else 0.0
        run = D.simulate_run(leg.units, pay * frac, r.profile, zones, None, mode=spec.mode, max_grade=design_grade,
                             spec=spec, round_trip=False)
        run_e = D.simulate_run(leg.units, 0.0, r.profile, zones, None, mode=spec.mode, max_grade=design_grade,
                               spec=spec, round_trip=False)
        Lk = float(r.length_m)
        runs.append(run)
        runs_e.append(run_e)
        offs.append(off)
        poly_all += [list(p) for p in r.polyline]
        terrain += [(p[0] + off, p[1]) for p in r.profile]
        sd, hd = (D.track_profile(r.profile, 10.0, design_grade) if design_grade is not None
                  else D.track_profile(r.profile, 10.0, None, 0))
        track += [(float(a) + off, float(b)) for a, b in zip(sd, hd)]
        for st in sec["structures"]:
            structures.append({**st, "at_m": st["at_m"] + round(off), "to_m": st["to_m"] + round(off), "segment": k + 1})
        zones_all += [(z[0] + off, z[1] + off, z[2], z[3]) for z in zones]
        seg_out.append({
            "n": k + 1, "from": s["a"].name, "to": s["b"].name, "length_m": round(Lk), "plan_length_m": round(s["length_m"]),
            "load": round(s["load"], 1), "fill": round(frac, 3), "on_board": {c: round(v, 1) for c, v in on_board[k].items()},
            "on_board_ru": _cargo_list(ctx, on_board[k]) or "порожним",
            "free": sec["free"], "free_by": sec["free_by"], "prior_line": prior_line,
            "shared_with": None if shared is None else shared + 1,
            "cost": 0 if sec["free"] else sec["cost"]["total"], "maintenance": 0 if sec["free"] else sec["maintenance"]["total"],
            "way": (sec.get("way") or {}).get("name"), "time_s": round(run.time_s, 1), "avg_kmh": round(run.avg_speed * 3.6, 1),
            "structures": len(sec["structures"]), "source": sec["source"], "reused_m": sec.get("reused_m", 0),
            "demolition": None if (sec["free"] or shared is not None) else sec.get("demolition")})
        off += Lk
    L = off
    lr = _merge_runs(runs, offs)
    er = _merge_runs(runs_e, offs)
    # ---- timing
    load = _load_time(ctx, leg, main, cap)
    total_vol = sum(f["volume"] for f in flows) or peak
    handled = total_vol / peak if peak > 0 else 1.0          # full consists loaded (and unloaded) per cycle
    load_s, unload_s = load["load_s"] * handled, load["unload_s"] * handled
    dwell = len(segs) * (STOP_DELAYS_S + MANOEUVRE_S.get(mode, 10.0))
    rt = lr.time_s + load_s + unload_s + dwell
    trips = ctx.year_s / rt if rt > 0 else 0.0
    n = max(1, leg.n_consists)
    per_consist = cap * trips
    fleet = n * per_consist
    scale = min(1.0, fleet / peak) if peak > 0 else 1.0
    moved_peak = min(peak, fleet)
    headway = rt / n
    dwell_end = load["load_s"] + STOP_DELAYS_S + MANOEUVRE_S.get(mode, 10.0)
    terminals = 1
    if CAP is not None:
        try:
            terminals = CAP.terminals_needed(CAP.terminal_occupancy(n, rt, dwell_end))
        except Exception:
            terminals = 1
    one_way_simple = L / max(spec.top_speed * PLANNER_SPEED_FACTOR, 0.1)
    rt_simple = one_way_simple + 2 * load["planner_load_s"] * handled + len(segs) * STOP_DELAYS_S
    # ---- stations: one per distinct stop
    classes = sorted({c for f in flows for c in (g.cargo_by_key[f["cargo"]].classes if f["cargo"] in g.cargo_by_key else [])}) \
        or (cargo.classes if cargo else ["UNIVERSAL"])
    st = facilities.station(ctx.cat, mode, year, classes, spec.length_m, terminals)
    rail_segs = max(1, math.ceil(spec.length_m / facilities.SEGMENT_M)) if mode == "rail" else None
    stations, seen_st = [], set()
    for i, v in enumerate(visits):
        nd = v["node"]
        if nd.key in seen_st:
            continue
        seen_st.add(nd.key)
        loads = {c: q for vv in visits if vv["node"].key == nd.key for c, q in vv["load"].items()}
        unloads = {c: q for vv in visits if vv["node"].key == nd.key for c, q in vv["unload"].items()}
        role = "; ".join(x for x in (f"погрузка: {_cargo_list(ctx, loads)}" if loads else "",
                                     f"выгрузка: {_cargo_list(ctx, unloads)}" if unloads else "") if x) or "проезд"
        poly = list(routes[i][1].polyline)
        prev = list(routes[i - 1][1].polyline)              # incoming segment (cyclic)
        p_out = SIT.route_point(poly, True) if poly else None
        p_in = SIT.route_point(prev, False) if prev else None
        rpt = ((p_out[0] + p_in[0]) / 2, (p_out[1] + p_in[1]) / 2) if (p_out and p_in) else (p_out or p_in)
        end = _endpoint_dict(ctx, nd)
        free = T["prior_stations"].get((nd.key, mode, False))
        stop = _cargo_stop(ctx, year, mode, end, classes, loads, unloads, rpt, role, free)
        if stop is not None:
            stations.append(stop)
            continue
        site = _site_station(ctx, mode, end, poly, True, CATCHMENT_M.get(mode, 150.0), route_pt=rpt)
        dm = _site_demolition(ctx, site, _site_fp(mode, end))
        stations.append(_station_dict(role, end, st.name, st.kind, site.xy(), CATCHMENT_M.get(mode, 150.0),
                                      st.cost + (dm["cost"] if dm else 0.0),
                                      st.maintenance, free, terminals, rail_segs, site_reason=site.reason,
                                      demolition=None if free else _demo_round(dm),
                                      load={c: round(q, 1) for c, q in loads.items()},
                                      unload={c: round(q, 1) for c, q in unloads.items()}))
    first_load = next((k for k, s_ in enumerate(stations) if s_["load"]), 0)
    leg.depot_end = first_load
    depots = _depots(ctx, leg, year, n, stations, poly_all)
    # ---- route summary
    free_all = all(s["free"] for s in seg_out)
    costs = {"track": 0, "bridges": 0, "tunnels": 0, "earthworks": 0, "surcharges": 0, "total": 0}
    maint = {"track": 0, "structures": 0, "total": 0}
    for (sl, r, rp, sec, _, _) in routes:
        if sec["free"]:
            continue
        for kk in costs:
            costs[kk] += sec["cost"][kk]
        for kk in maint:
            maint[kk] += sec["maintenance"][kk]
    straight = sum(math.dist((s["a"].x, s["a"].y), (s["b"].x, s["b"].y)) for s in segs)
    hz = np.array([p[1] for p in terrain]) if terrain else np.zeros(1)
    dh = np.diff(hz) if len(hz) > 1 else np.zeros(0)
    w0 = routes[0][3]
    route = {
        "source": f"{len(segs)} участков: " + "; ".join(sorted({x[3]['source'] for x in routes})),
        "length_m": round(L), "straight_m": round(straight), "detour": round(L / straight, 3) if straight > 0 else None,
        "z_from": round(segs[0]["a"].z, 1), "z_to": round(segs[0]["a"].z, 1),
        "elevation_gain_m": round(float(dh[dh > 0].sum()), 1), "elevation_loss_m": round(float(-dh[dh < 0].sum()), 1),
        "max_terrain_grade": max(x[3]["max_terrain_grade"] for x in routes),
        "max_grade": max(x[3]["max_grade"] for x in routes),
        "way": w0.get("way"), "bridge_type": w0.get("bridge_type"), "tunnel_type": w0.get("tunnel_type"),
        "structures": sorted(structures, key=lambda s: s["at_m"]),
        "bridges_m": sum(x[3]["bridges_m"] for x in routes), "tunnels_m": sum(x[3]["tunnels_m"] for x in routes),
        "earthworks_m3": sum(x[3]["earthworks_m3"] for x in routes),
        "cost": costs, "maintenance": maint,
        "free": free_all, "free_by": "весь путь уже построен" if free_all else None,
        "polyline": [[round(x), round(y)] for x, y in _decimate(poly_all, 160)],
        "segments": seg_out,
    }
    dacc, dparts, dlines = _demo_new(), [], []
    for so in seg_out:
        dmo = so.get("demolition")
        if not dmo:
            continue
        _demo_add(dacc, dmo)
        dparts += [{**p_, "segment": so["n"]} for p_ in dmo.get("parts") or []]
        so["demolition"] = {k: v for k, v in dmo.items() if k not in ("parts", "segments_ru")}
        dlines.append(f"участок {so['n']} ({so['from']} → {so['to']}): {dmo['text_ru']}")
    dsum = _demo_round(dacc)
    if dsum:
        dsum.update(segments_ru=dlines, parts=dparts,
                    total_ru=(f"в т.ч. снос: ~{_nb(dsum['buildings'])} зд., ≈ {_m(dsum['cost'])} $ "
                              f"({dsum['m']} м трассы через застройку): {_towns_ru(dsum['towns'])}"))
        route["demolition"] = dsum
        route["cost"]["demolition"] = dsum["cost"]
    # ---- money (each unit paid on the chord from its pickup to its drop-off stop)
    income = 0.0
    flow_out = []
    for f in flows:
        a, b = f["from"], f["to"]
        cg = g.cargo_by_key.get(f["cargo"])
        ipu = g.income_per_unit(cg, (a.x, a.y, a.z), (b.x, b.y, b.z)) if cg else 0.0
        inc = f["volume"] * scale * ipu
        income += inc
        flow_out.append({"from": a.name, "to": b.name, "cargo": f["cargo"],
                         "cargo_ru": cg.name.lower() if cg else f["cargo"], "volume": round(f["volume"], 1),
                         "income_per_unit": round(ipu, 2), "income_per_year": round(inc),
                         "load_stop": f["load_visit"] + 1, "unload_stop": f["unload_visit"] + 1,
                         "chord_m": round(math.dist((a.x, a.y, a.z), (b.x, b.y, b.z)))})
    if not flows and leg.plan.get("income_per_year"):
        income = float(leg.plan["income_per_year"])
    moved = total_vol * scale
    veh_capex = sum(v.price * k for v, k in leg.units) * n
    running = sum(v.running_cost * k for v, k in leg.units) * n
    route_capex, route_maint = costs["total"], maint["total"]
    st_capex = sum(s["cost"] for s in stations)
    st_maint = sum(s["maintenance"] for s in stations)
    dp_capex = sum(d["cost"] for d in depots)
    dp_maint = sum(d["maintenance"] for d in depots)
    capex = route_capex + st_capex + dp_capex + veh_capex
    upkeep = route_maint + st_maint + dp_maint
    profit = income - running - upkeep
    annuity = (1 - (1 + DISCOUNT) ** -NPV_YEARS) / DISCOUNT
    trips_total = n * trips
    money = {
        "income_per_unit": round(income / moved, 2) if moved else 0.0, "chord_m": None,
        "income_per_unit_km_route": None,
        "income_per_trip": round(income / trips_total) if trips_total else None,
        "moved_per_year": round(moved, 1), "income_per_year": round(income),
        "capex": {"route": round(route_capex), "stations": round(st_capex), "depots": round(dp_capex),
                  "vehicles": round(veh_capex), "total": round(capex), "passing_loops_extra": 0},
        "per_year": {"running": round(running), "route_maintenance": round(route_maint),
                     "stations_maintenance": round(st_maint), "depots_maintenance": round(dp_maint),
                     "total_costs": round(running + upkeep), "passing_loops_maintenance_extra": 0},
        "profit_per_year": round(profit), "profit_per_trip": round(profit / trips_total) if trips_total else None,
        "payback_years": round(capex / profit, 2) if profit > 0 else None,
        "npv": round(profit * annuity - capex), "npv_years": NPV_YEARS, "discount": DISCOUNT,
        "vehicle_depreciation_per_year": round(veh_capex / max(min((v.lifespan_years for v, _ in leg.units), default=30), 1)),
        "plan": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in (leg.plan or {}).items()},
        "flows": flow_out,
    }
    if leg.plan.get("income_per_year"):
        money["plan_vs_detail"] = {"income_ratio": round(income / leg.plan["income_per_year"], 3),
                                   "cost_ratio": round(capex / leg.plan["cost"], 3) if leg.plan.get("cost") else None}
    timing = {
        "loaded": lr.summary(), "empty": er.summary(), "one_way": True,
        "load_s": round(load_s, 1), "unload_s": round(unload_s, 1),
        "loading_model": load["model"] + f"; за круг грузится/выгружается {handled:.2f} полного состава "
                                         f"(сумма потоков / пиковая загрузка участка)",
        "stops": len(segs), "stop_delays_s": len(segs) * STOP_DELAYS_S,
        "manoeuvre_s": len(segs) * MANOEUVRE_S.get(mode, 10.0), "dwell_s": round(dwell, 1),
        "round_trip_s": round(rt, 1), "round_trip_days": round(rt / DAY_S, 1),
        "trips_per_year": round(trips, 3), "headway_s": round(headway, 1), "wait_for_cargo_s": 0.0,
        "planner_round_trip_s": round(rt_simple, 1),
        "planner_diff": round(rt / rt_simple - 1, 3) if rt_simple > 0 else None,
        "ruling_grade": None if design_grade is None else round(design_grade, 4),
        "ruling_grade_reason": (f"самый крутой уклон, на котором гружёный состав держит ≥{KEEP_SPEED_FRACTION:.0%} "
                                f"максимальной скорости" if design_grade is not None else None),
        "terrain_variant": None, "earthworks": None, "grade_variants": [],
    }
    capacity = {"per_trip": cap, "per_consist_year": round(per_consist, 1), "fleet_year": round(fleet, 1),
                "volume": round(peak, 1), "moved": round(moved_peak, 1),
                "load_factor": round(moved_peak / fleet, 3) if fleet > 0 else 0.0,
                "consists_needed": max(1, math.ceil(peak / per_consist)) if per_consist > 0 else None,
                "consists_planned": n, "terminals": terminals, "terminal_dwell_s": round(dwell_end, 1),
                "rail_layout": None, "volume_note": "объём = пиковая загрузка участка (ед./год)"}
    tour = {
        "label": T["label"], "topology": T["topology"],
        "topology_ru": "кольцо (в одну сторону)" if T["topology"] == "loop" else "челнок туда-обратно",
        "stops": [{"n": v["n"], "name": v["node"].name, "x": round(v["node"].x), "y": round(v["node"].y),
                   "kind": v["node"].kind,
                   "load": {c: round(q, 1) for c, q in v["load"].items()},
                   "unload": {c: round(q, 1) for c, q in v["unload"].items()},
                   "load_ru": _cargo_list(ctx, v["load"]), "unload_ru": _cargo_list(ctx, v["unload"]),
                   "load_mode": ("if_available" if v["load"] else None),
                   "action_ru": _visit_action(ctx, v)} for v in visits],
        "segments": seg_out, "flows": flow_out, "peak_load": round(peak, 1), "total_volume": round(total_vol, 1),
        "distinct_stops": len(stations),
    }
    tag = f"[{T['label']}] "
    if peak and fleet < 0.97 * peak:
        warnings.append(f"{tag}Провозная способность {fleet:.0f} ед./год меньше пиковой загрузки участка {peak:.0f}: "
                        f"нужно {capacity['consists_needed']} состав(ов) вместо {n}.")
    if not flows:
        warnings.append(f"{tag}Потоки грузов по остановкам не распознаны в тексте плана — погрузка/выгрузка "
                        f"по остановкам не расписана.")
    if lr.stalled:
        warnings.append(f"{tag}На рекомендуемом уклоне {_pct(design_grade or 0)} состав местами ползёт — "
                        f"нужен второй локомотив или меньше вагонов.")
    if consist.get("top_speed_note"):
        warnings.append(f"{tag}{consist['top_speed_note']}.")
    for v in consist["vehicles"]:
        if not v["available"]:
            warnings.append(f"{tag}{v['name']} недоступен в {year} г. ({v['year_from']}–{v['year_to'] or '…'}).")
    for s_ in stations:
        if not s_["free_by"] and s_["dist_to_endpoint_m"] > s_["catchment_m"] and \
                next((v for v in visits if v["node"].name == s_["endpoint"]), {"node": None})["node"].kind == "industry":
            warnings.append(f"{tag}Станция у «{s_['endpoint']}» в {s_['dist_to_endpoint_m']} м от центра — больше "
                            f"охвата {s_['catchment_m']:.0f} м: придвинуть ближе.")
    chart = {
        "length_m": round(L),
        "terrain": [[round(p[0]), round(p[1], 1)] for p in _decimate(terrain)],
        "track": [[round(a), round(b, 1)] for a, b in _decimate(track)],
        "loaded": [[round(t[0]), round(t[2] * 3.6, 1), round(t[3] * 3.6, 1)] for t in _decimate(lr.trace)],
        "empty": [[round(t[0]), round(t[2] * 3.6, 1), round(t[3] * 3.6, 1)] for t in _decimate(er.trace)],
        "zones": [{"from_m": round(z[0]), "to_m": round(z[1]), "kmh": round(z[2] * 3.6), "reason": z[3]}
                  for z in zones_all if z[3] != "way"][:80],
        "top_kmh": round(spec.top_speed * 3.6), "water_level": float(g.terrain.water_level),
        "stops_m": [round(o) for o in offs],
    }
    return {"role": "tour", "role_ru": T["label"], "mode": mode, "mode_ru": MODE_RU.get(mode, mode),
            "title": leg.title, "from": leg.src, "to": leg.dst, "loop": False, "pax": None, "estimate": False,
            "route": route, "consist": consist, "timing": timing, "capacity": capacity, "stations": stations,
            "depots": depots, "money": money, "delivery": None, "chart": chart, "tour": tour, "passengers": None,
            "demolition": _leg_demolition(route, stations, depots)}


def _visit_action(ctx: Context, v: dict) -> str:
    parts = []
    if v["unload"]:
        parts.append(f"выгрузить {_cargo_list(ctx, v['unload'])} ед./год")
    if v["load"]:
        parts.append(f"загрузить {_cargo_list(ctx, v['load'])} ед./год")
    return "; ".join(parts) if parts else "без операций (проезд)"


# --------------------------------------------------------------------------- multimodal warehouses

def _mm_warehouses(ctx: Context, hdr: dict, legs_in: list[Leg], legs_out: list[dict], year: int,
                   details: str) -> list[dict]:
    """Warehouse at every transfer hub: trunk start (and final start). Size: >= 2x the largest consist
    calling there (multimodal.WAREHOUSE_TWO_CONSISTS) and >= cycle + safety stock over one outbound headway."""
    from . import multimodal as MM
    g = ctx.game
    cargo = g.cargo_by_key.get(hdr.get("cargo") or "")
    classes = cargo.classes if (cargo and MM.SPECIALIZED_WAREHOUSE) else ["UNIVERSAL"]
    spec_cls = facilities._special(classes)
    wh = facilities.warehouse(ctx.cat, year, classes)
    plan_mods = [int(x) for x in re.findall(r"500 ед\. ×(\d+)", details or "")]
    hubs = []
    for li in legs_in:
        if li.role in ("trunk", "final") and li.src.get("key") not in [h for h in hubs]:
            hubs.append(li.src.get("key"))
    total = sum(li.volume or 0 for li in legs_in if li.role == "trunk") or max((li.volume or 0 for li in legs_in), default=0)
    rate = total / ctx.year_s
    out = []
    for hi, hk in enumerate(hubs):
        calling = [(li, lo) for li, lo in zip(legs_in, legs_out)
                   if not lo.get("error") and hk in (li.src.get("key"), li.dst.get("key"))]
        if not calling:
            continue
        inbound = [(li, lo) for li, lo in calling if li.dst.get("key") == hk]
        outbound = [(li, lo) for li, lo in calling if li.src.get("key") == hk]
        caps = {id(lo): lo["consist"]["capacity"] for _, lo in calling}
        largest_li, largest_lo = max(calling, key=lambda x: caps[id(x[1])])
        largest = caps[id(largest_lo)]
        batch_in = max((caps[id(lo)] for _, lo in inbound), default=0.0)
        out_cap = max((caps[id(lo)] for _, lo in outbound), default=0.0)
        headway = max((lo["timing"]["headway_s"] for _, lo in outbound), default=0.0)
        buffer = max(out_cap, batch_in) + MM.SAFETY_Z * math.sqrt(max(headway * batch_in * rate, 0.0))
        two = MM.WAREHOUSE_TWO_CONSISTS * largest
        buffer = max(buffer, two)
        modules = max(1, math.ceil(buffer / MM.WAREHOUSE_MODULE))
        end = largest_li.src if largest_li.src.get("key") == hk else largest_li.dst
        sts = []
        for li, lo in calling:
            k = 0 if li.src.get("key") == hk else 1
            st = (lo.get("stations") or [None, None])[k] if len(lo.get("stations") or []) > k else None
            if st is None:
                continue
            sts.append({"leg": lo.get("title") or lo["role_ru"], "role": lo["role_ru"], "mode": li.mode,
                        "building": st["building"], "x": st["x"], "y": st["y"], "catchment_m": st["catchment_m"],
                        "direction": "выгружает в склад" if k == 1 else "грузит со склада",
                        "consist": lo["consist"]["label"], "capacity": lo["consist"]["capacity"]})
        site = (ctx.siting.warehouse([(s_["x"], s_["y"], s_["catchment_m"]) for s_ in sts]) if sts
                else SIT.Site(end["x"], end["y"], "у узла", ok=False))
        wdm = _site_demolition(ctx, site, "warehouse")
        out.append({
            "name": wh.name, "hub": end["name"], "hub_key": hk, "x": round(site.x, 1), "y": round(site.y, 1),
            "demolition": _demo_round(wdm),
            "site_reason": site.reason, "site_ok": site.ok,
            "modules": modules, "capacity_units": int(modules * MM.WAREHOUSE_MODULE), "buffer_units": round(buffer, 1),
            "largest_consist": {"label": largest_lo["consist"]["label"], "capacity": largest,
                                "leg": largest_lo.get("title") or largest_lo["role_ru"]},
            "two_consists_units": round(two, 1),
            "rule": (f"≥ {MM.WAREHOUSE_TWO_CONSISTS:g} × самый большой состав, заходящий на склад "
                     f"({largest:.0f} ед.), и ≥ партия + страховой запас за интервал отправки"),
            "specialized": bool(spec_cls), "class": spec_cls, "class_ru": CLASS_RU.get(spec_cls or "", "все грузы"),
            "module_cost": round(wh.cost), "module_maintenance": round(wh.maintenance),
            "cost": round(modules * wh.cost + (wdm["cost"] if wdm else 0.0)),
            "maintenance": round(modules * wh.maintenance),
            "notes": wh.notes, "stations": sts,
            "plan_modules": plan_mods[hi] if hi < len(plan_mods) else None,
            "min_catchment_m": min((s["catchment_m"] for s in sts), default=150.0),
        })
    return out


# --------------------------------------------------------------------------- instructions

class _Ins:
    def __init__(self):
        self.out: list[str] = []

    def __call__(self, t: str):
        self.out.append(f"{len(self.out) + 1}. {t}")


def _cname(ctx: Context, key: str | None) -> str:
    c = ctx.game.cargo_by_key.get(key or "")
    return c.name.lower() if c else (key or "груз")


def _ins_stations(add, ctx, hdr, lg, pre):
    mode = lg["mode"]
    for s in lg["stations"]:
        if s.get("builtin"):
            add(f"{pre}У «{s['endpoint']}» порт НЕ строить: у морского предприятия есть встроенный причал для судов "
                f"(и вертолётная площадка) — линия заходит прямо к нему.")
            continue
        if s["free_by"]:
            add(f"{pre}Станция ({s['role']}) у «{s['endpoint']}»: уже есть ({s['free_by']}) — "
                f"использовать её{', добавить терминал' if s['terminals'] > 1 else ''}.")
            continue
        plat = hdr.get("platform")
        plat_ru = (" — модули платформ СПЕЦИАЛИЗИРОВАННЫЕ под класс груза (погрузка ×2)" if plat == "specialized"
                   else " — модули платформ УНИВЕРСАЛЬНЫЕ" if plat == "universal" else "")
        if mode == "rail":
            what = (f"грузовую ж/д станцию {s['terminals']}×{s['platform_m']:.0f} м (главное здание 1 + "
                    f"{s['segments']} сегм. платформы по 40 м на путь){plat_ru}")
        elif mode == "water":
            what = "малый грузовой порт (1 причал) на берегу"
        else:
            what = f"грузовую автостанцию на {s['terminals']} терминал(а){plat_ru}"
        end_type = (lg["from"] if s["endpoint"] == lg["from"]["name"] else lg["to"]).get("type")
        hub_note = (" Это пункт перевалки: рядом встанет склад — он должен попасть в охват этой станции."
                    if end_type == "hub" else "")
        add(f"{pre}Поставить {what} под {'погрузку' if s['role'] == 'погрузка' else 'выгрузку'} у «{s['endpoint']}»: "
            f"точка ({s['x']}, {s['y']}), {s['dist_to_endpoint_m']} м от центра; охват {s['catchment_m']:.0f} м — "
            f"{'склад' if end_type == 'hub' else 'предприятие/город'} должно попасть в круг. "
            f"Почему здесь: {s.get('site_reason') or '—'}. "
            f"Цена {_m(s['cost'])}, содержание {_m(s['maintenance'])}/год.{hub_note}"
            + _site_ins(s.get("demolition")))


def _reuse_txt(reused_m, length_m, mode) -> str:
    """", из них X км — по уже существующей дороге (строить только остальное)" when the route follows
    existing roads/tracks of the game (export v6 network) for a noticeable part."""
    if not reused_m or reused_m < 100 or not length_m:
        return ""
    what = "путям" if mode == "rail" else "дороге"
    if reused_m >= 0.98 * length_m:
        return f" — весь путь идёт по уже существующей {what}, строить только примыкания"
    return f", из них {_km(reused_m)} — по уже существующей {what} (строить только остальные {_km(length_m - reused_m)})"


def _ins_route(add, lg, pre):
    mode, route, cons, tim, cap = lg["mode"], lg["route"], lg["consist"], lg["timing"], lg["capacity"]
    if mode in ("rail", "road", "tram"):
        if route["free"]:
            add(f"{pre}Путь {_km(route['length_m'])}: {route['free_by']} — ничего не строить.")
        else:
            w = route.get("way") or {}
            kind = "пути" if mode == "rail" else "дорогу"
            txt = (f"{pre}Проложить {kind} «{w.get('name', '?')}» ({w.get('speed_kmh', '?')} км/ч, "
                   f"{w.get('cost_per_m', 0):.0f}/м) {_km(route['length_m'])} по трассе на карте "
                   f"(по прямой {_km(route['straight_m'])}, объезд ×{(route['detour'] or 1):.2f}); "
                   f"трасса {_m(route['cost']['track'])}" + _reuse_txt(route.get("reused_m"), route["length_m"], mode))
            ruling = tim.get("ruling_grade")
            if ruling is not None:
                txt += f"; держать уклон ≤ {_pct(ruling)}"
                if mode == "rail":
                    txt += f" (дальше {cons['label']} не тянет с нормальной скоростью)"
            dem = route.get("demolition")
            add(txt + ("." if dem else "; без сноса городских зданий."))
            if dem:
                add(f"{pre}  СНОС городских зданий по трассе: ~{_nb(dem['buildings'])} зд. "
                    f"({_towns_ru(dem['towns'])}), {dem['m']} м трассы через застройку, ≈ {_m(dem['cost'])} "
                    f"(уже в цене трассы); места — красная штриховка на карте. Игра покажет снос при прокладке: "
                    f"сверить число домов.")
                for ln in dem.get("segments_ru") or []:
                    add(f"{pre}    {ln[:1].upper() + ln[1:]}.")
            for st in route["structures"]:
                lim = f", ограничение {st['speed_limit_kmh']} км/ч" if st.get("speed_limit_kmh") else ""
                add(f"{pre}  {st['type_ru'].capitalize()} {st['length_m']} м ({st['name']}) на км "
                    f"{st['at_m'] / 1000:.1f}–{st['to_m'] / 1000:.1f} {st['place']} ({st['x']}, {st['y']})"
                    f"{lim}; ≈{_m(st['cost'])}.")
            ew = tim.get("earthworks")
            if ew and ew["volume_m3"] > 1000:
                add(f"{pre}  Насыпи/выемки для уклона ≤ {_pct(ew['ruling_grade'])}: до {ew['max_fill_m']:.0f} м "
                    f"насыпи и {ew['max_cut_m']:.0f} м выемки (~{_m(ew['volume_m3'])} м³; игра делает их "
                    f"при прокладке, отдельная цена не известна).")
        rl = cap.get("rail_layout")
        if rl and rl.get("loops"):
            add(f"{pre}Разъезды: {rl['loops']} шт. примерно каждые {_km(rl['spacing_m'] or 0)} "
                f"(длина ≥ {cons['length_m'] * rl.get('platoon', 1) + 40:.0f} м, по сигналу на каждый путь; "
                f"≈{_m(rl['capex'])}).")
        elif rl and rl.get("kind") == "double":
            add(f"{pre}Второй путь по всей длине (однопутка не пропустит поезда): ≈{_m(rl['capex'])}.")
    else:
        add(f"{pre}Судовой ход {_km(route['length_m'])} строить не нужно; убедиться, что оба причала на одном водоёме.")


def _ins_depots(add, lg, pre):
    for d in lg["depots"]:
        if d.get("free"):
            add(f"{pre}{d['what']}: использовать существующее (на конце уже есть станция этой сети).")
            continue
        add(f"{pre}{d['what']} ×{d['count']} {d['where']} — {'каждое ' if d['count'] > 1 else ''}обслуживает до "
            f"{d['pool']} ед. техники в радиусе {d['radius_m']:.0f} м; цена {_m(d['cost'])}, содержание "
            f"{_m(d['maintenance'])}/год." + _site_ins(d.get("demolition")))


def _ins_vehicles(add, lg, pre, pax: bool = False):
    cons, mode = lg["consist"], lg["mode"]
    n = cons["n_consists"]
    vtxt = " + ".join(f"{v['count']}× {v['name']}" for v in cons["vehicles"])
    price = _m(sum(v['price'] * v['count'] for v in cons['vehicles']))
    unit = "пасс." if pax else "ед."
    if mode == "rail":
        add(f"{pre}Купить {vtxt}: {n} поезд(а) по {cons['label']} — длина {cons['length_m']} м, "
            f"масса {cons['mass_empty_t']:.0f} т порожним / {cons['mass_loaded_t']:.0f} т гружёным, "
            f"{cons['kw_per_t_loaded']} кВт/т; вместимость {cons['capacity']} {unit} Цена {price}.")
    else:
        add(f"{pre}Купить {vtxt} (вместимость {cons['capacity']} {unit} каждая); цена {price}.")


def _ins_check(add, lg, pre, what: str = "провоз"):
    tim, cap, mode = lg["timing"], lg["capacity"], lg["mode"]
    unit = "пасс." if lg.get("pax") else "ед."
    run = (f"рабочий ход {tim['loaded']['time_s']:.0f} с" if tim.get("one_way")
           else f"гружёным {tim['loaded']['time_s']:.0f} с, порожним {tim['empty']['time_s']:.0f} с")
    add(f"{pre}Проверить в игре: круг ≈{_s(tim['round_trip_s'])} ({run}), "
        f"≈{tim['trips_per_year']:.2f} рейса/год на {'поезд' if mode == 'rail' else 'единицу'}, "
        f"{what} ≈{cap['moved']:.0f} {unit}/год при объёме {cap['volume'] or 0:.0f}. Если сильно иначе — "
        f"выгрузить статистику для калибровки.")


def _ins_cargo_line(add, ctx, hdr, lg, pre, warehouses: list[dict]):
    tim, cap, mode = lg["timing"], lg["capacity"], lg["mode"]
    sts = lg["stations"]
    cname = _cname(ctx, hdr.get("cargo"))
    n = lg["consist"]["n_consists"]
    a = sts[0] if sts else {"endpoint": lg["from"]["name"]}
    b = sts[1] if len(sts) > 1 else {"endpoint": lg["to"]["name"]}
    wait = min(600, max(0, round(tim["headway_s"])))
    hub_from = lg["from"].get("type") == "hub" or any(w["hub_key"] == lg["from"].get("key") for w in warehouses)
    hub_to = lg["to"].get("type") == "hub" or any(w["hub_key"] == lg["to"].get("key") for w in warehouses)
    s1 = ("встроенный причал «" + a["endpoint"] + "»") if a.get("builtin") else f"станция у «{a['endpoint']}»"
    s2 = ("встроенный причал «" + b["endpoint"] + "»") if b.get("builtin") else f"станция у «{b['endpoint']}»"
    load_txt = (f"загрузка {cname} со склада {LOAD_RU['full']}, макс. стоянка ≈{wait} с" if hub_from
                else f"загрузка {cname} {LOAD_RU['full']}")
    unload_txt = ("выгрузить весь груз — он уходит на склад в охвате станции" if hub_to
                  else "выгрузка всего груза")
    add(f"{pre}Создать линию: остановка 1 — {s1}, {load_txt}; остановка 2 — {s2}, {unload_txt}.")
    unit_ru = {"rail": "поезд(ов)", "water": "судно(а)"}.get(mode, "машин(ы)")
    add(f"{pre}Настройки линии: {n} {unit_ru} с интервалом ≈{_s(tim['headway_s'])}; на погрузке "
        f"«макс. время стоянки» ≈{wait} с, чтобы состав не стоял сверх интервала"
        f"{' (груза меньше, чем вместимость парка)' if cap['load_factor'] < 0.9 else ''}.")


def _ins_leg_cargo(add, ctx, hdr, lg, pre, warehouses=()):
    _ins_stations(add, ctx, hdr, lg, pre)
    _ins_route(add, lg, pre)
    _ins_depots(add, lg, pre)
    _ins_vehicles(add, lg, pre)
    _ins_cargo_line(add, ctx, hdr, lg, pre, list(warehouses))
    _ins_check(add, lg, pre)


def _ins_warehouse(add, w: dict):
    kind = (f"СПЕЦИАЛИЗИРОВАННЫЙ ({w['class_ru']}: погрузка ×2, срок доставки на складе не идёт)"
            if w["specialized"] else "универсальный (общий запас для всех грузов)")
    sts = "; ".join(f"{s['building']} плеча «{s['leg']}» ({s['direction']}, охват {s['catchment_m']:.0f} м, "
                    f"точка {s['x']}, {s['y']})" for s in w["stations"])
    plan = f" (в плане ×{w['plan_modules']})" if w.get("plan_modules") and w["plan_modules"] != w["modules"] else ""
    add(f"Склад в узле «{w['hub']}», точка ({w['x']:.0f}, {w['y']:.0f}): {kind}, {w['modules']} модул(я/ей) по 500 ед. = "
        f"{w['capacity_units']} ед{plan}. Правило: вместимость ≥ 2 × самый большой состав, заходящий сюда "
        f"({w['largest_consist']['label']}, {w['largest_consist']['capacity']:.0f} ед. → ≥ {w['two_consists_units']:.0f} ед.); "
        f"расчётный буфер {w['buffer_units']:.0f} ед. Цена {_m(w['cost'])}, содержание {_m(w['maintenance'])}/год."
        + _site_ins(w.get("demolition")))
    add(f"  Ставить склад так, чтобы он был в охвате ОБЕИХ станций: {sts}. "
        f"Выбранная точка: {w.get('site_reason') or 'между станциями'}. При постройке игра подсвечивает охваты "
        f"станций, которые смогут работать со складом.")
    if w["specialized"]:
        add("  В окне склада закрепить груз за модулями (специализированный модуль держит один груз за раз).")


def _ins_multimodal(add, ctx, hdr, legs: list[dict], warehouses: list[dict]):
    ok = [l for l in legs if not l.get("error")]
    names = {l.get("title") or l["role_ru"]: l for l in ok}
    for lg in ok:
        _ins_stations(add, ctx, hdr, lg, f"[{lg.get('title') or lg['role_ru']}] ")
    for w in warehouses:
        _ins_warehouse(add, w)
    for title, lg in names.items():
        pre = f"[{title}] "
        _ins_route(add, lg, pre)
        _ins_depots(add, lg, pre)
        _ins_vehicles(add, lg, pre)
        _ins_cargo_line(add, ctx, hdr, lg, pre, warehouses)
        _ins_check(add, lg, pre)
    if warehouses:
        add("Порядок запуска: сначала линии подвоза (склад наполнится), потом магистраль; на станции узла у "
            "магистрали «полная загрузка» берёт груз только со склада — без склада в охвате состав будет ждать вечно.")


def _ins_pax(add, ctx, hdr, lg, pre):
    st, tim, cap, cons = lg["stations"], lg["timing"], lg["capacity"], lg["consist"]
    px = lg.get("passengers") or {}
    mode = lg["mode"]
    n = cons["n_consists"]
    route = lg["route"]
    paid = [s for s in st if not s["free_by"]]
    streets = route.get("streets")
    if lg["pax"] == "loop" and streets:
        town = lg["from"]["name"]
        b = st[0]["building"] if st else "малая остановка"
        add(f"{pre}Поставить {len(st)} шт. «{b}» на существующих улицах г. {town} (каждая "
            f"{_m(st[0]['full_cost']) if st else '—'}, охват {SMALL_STOP_CATCHMENT:.0f} м, вместе охватывают "
            f"~{(streets.get('coverage') or 0) * 100:.0f} % улиц города):"
            + (f" первая уже есть ({st[0]['free_by']})." if st and st[0]["free_by"] else ""))
        for s_ in streets["stops"]:
            add(f"{pre}  остановка {s_['no']}: ({s_['x']:.0f}, {s_['y']:.0f}) — {s_['label']}.")
        add(f"{pre}Маршрут по улицам: {streets.get('path_ru')}.")
        if mode == "tram":
            add(f"{pre}Уложить трамвайные пути по улицам кольца (~{_km(streets.get('unique_m') or route['length_m'])}): "
                f"≈{_m(route['cost']['total'])}, содержание ≈{_m(route['maintenance']['total'])}/год.")
        elif streets.get("streets_only"):
            add(f"{pre}Новых дорог не строим, дома не сносим: кольцо ~{_km(route['length_m'])} целиком по "
                f"существующим улицам.")
        else:
            add(f"{pre}Новые участки улиц {streets.get('new_m')} м за {_m(streets.get('new_cost') or 0)} "
                f"(в т.ч. снос домов {_m(streets.get('demolition') or 0)}) — на карте жирным; остальное по улицам.")
    elif lg["pax"] == "loop":
        town = lg["from"]["name"]
        pts = ", ".join(f"({s['x']}, {s['y']})" for s in st)
        b = st[0]["building"] if st else "малая остановка"
        add(f"{pre}Поставить {len(st)} шт. «{b}» по кругу через г. {town} (каждая {_m(st[0]['full_cost']) if st else '—'}, "
            f"охват {SMALL_STOP_CATCHMENT:.0f} м): примерно {pts} — каждые ~{st[0].get('spacing_m', 0) if st else 0} м, "
            f"в самой плотной застройке, с обеих сторон улицы не нужно (кольцо в одну сторону)."
            + (f" Первая уже есть ({st[0]['free_by']})." if st and st[0]["free_by"] else ""))
        if mode == "tram":
            add(f"{pre}Уложить трамвайные пути по улицам кольца (~{_km(route['length_m'])}): ≈{_m(route['cost']['total'])}, "
                f"содержание ≈{_m(route['maintenance']['total'])}/год.")
        else:
            add(f"{pre}Улицы уже есть — дорогу не строить (кольцо ~{_km(route['length_m'])}).")
    elif mode == "rail":
        for s in st:
            add(f"{pre}{'Вокзал уже есть (' + s['free_by'] + ') — использовать' if s['free_by'] else 'Поставить ' + s['building']} "
                f"{s['role']}: точка ({s['x']}, {s['y']}), охват {s['catchment_m']:.0f} м — ставить у центра города; "
                f"цена {_m(s['cost'])}, содержание {_m(s['maintenance'])}/год.")
        _ins_route(add, lg, pre)
    else:
        for end_k in (0, 1):
            group = [s for s in st if s.get("town_end") == end_k]
            if not group:
                continue
            endn = group[0]["endpoint"]
            ind = (lg["from"] if end_k == 0 else lg["to"]).get("kind") == "industry"
            where = (f"у предприятия «{endn}» (в {SMALL_STOP_CATCHMENT:.0f} м от его зданий — рабочие ходят только "
                     f"с остановки)" if ind else f"в г. {endn}")
            pts = ", ".join(f"({s['x']}, {s['y']})" + (f" {s['street']}" if s.get("street") else "") for s in group)
            have = [s for s in group if s["free_by"]]
            add(f"{pre}{len(group)} × «{group[0]['building']}» {where}: {pts}"
                + (f" — первая у центра, следующие по маршруту через ~{TOWN_STOP_SPACING_M:.0f} м" if len(group) > 1 else "")
                + (f"; уже есть ({have[0]['free_by']})" if have else "")
                + f"; каждая {_m(group[0]['full_cost'])}, охват {SMALL_STOP_CATCHMENT:.0f} м.")
        if streets and streets.get("path_ru") and lg["pax"] in ("intercity", "workers"):
            tot = route.get("segment_totals") or {}
            add(f"{pre}Путь от остановки до остановки: {_km(tot.get('existing_m') or 0)} по существующим дорогам"
                + (f", новые участки {_km(tot.get('new_m') or 0)}" if tot.get("new_m") else " — новых дорог не строим")
                + ".")
        if route["free"]:
            add(f"{pre}Дорога {_km(route['length_m'])}: {route['free_by']} — не строить.")
        else:
            _ins_route(add, lg, pre)
    _ins_depots(add, lg, pre)
    _ins_vehicles(add, lg, pre, pax=True)
    # line
    if lg["pax"] == "loop":
        order = " → ".join(str(k + 1) for k in range(len(st))) + " → 1"
        add(f"{pre}Создать кольцевую линию: остановки {order} (в одну сторону"
            + (", маршрут по улицам как выше — игра сама выберет этот кратчайший путь" if streets else "")
            + "); на всех остановках посадка и высадка, БЕЗ «полной загрузки» и без ожидания.")
    elif lg["pax"] == "workers":
        add(f"{pre}Создать линию: остановка в г. {lg['from']['name']} → остановка у «{lg['to']['name']}» → обратно; "
            f"посадка/высадка на обеих, без «полной загрузки».")
    else:
        a = [s for s in st if s.get("town_end", 0) == 0] or st[:1]
        b = [s for s in st if s.get("town_end", 1) == 1] or st[-1:]
        seq = " → ".join([f"{lg['from']['name']}-{k + 1}" for k in range(len(a))] +
                         [f"{lg['to']['name']}-{k + 1}" for k in range(len(b))][::-1])
        add(f"{pre}Создать линию: {seq} и обратно тем же путём (остановки по порядку); посадка и высадка везде, "
            f"без «полной загрузки».")
    unit = {"rail": "поезд(ов)", "tram": "трамва(я/ев)"}.get(mode, "автобус(ов)")
    add(f"{pre}Настройки: {n} {unit} на линии, интервал ≈{_s(tim['headway_s'])}"
        + (f" (в плане {px['headway_plan_s']:.0f} с)" if px.get("headway_plan_s") else "")
        + f"; круг ≈{tim['round_trip_s']:.0f} с. Ожидается ~{px.get('passengers_per_year', 0):.0f} пасс./год"
        + (f" (спрос {px['demand_per_year']:.0f})" if px.get("demand_per_year") else "")
        + f", билеты ≈{_m(lg['money'].get('ticket_income_per_year') or 0)}/год"
        + (f", ~{_m(px['ticket'])} за пассажира" if px.get("ticket") else "") + ".")
    if lg["pax"] == "workers":
        d2 = px.get("distinct_workers_2y")
        verdict = ("бонус ожидается" if px.get("boost") else
                   f"ожидается ~{d2:.0f} — бонуса не будет, пока рабочих меньше {MIN_WORKERS}" if d2 is not None else
                   "проверить счётчик рабочих в окне предприятия")
        add(f"{pre}Правило рабочих: производство предприятия растёт ×(1,5…2,5), если за последние 2 года на него "
            f"по линии приехали ≥{MIN_WORKERS} РАЗНЫХ жителей ({verdict}). Остановка у предприятия обязательна: "
            f"пешком и на машинах рабочие туда не ходят.")
    _ins_check(add, lg, pre, "пассажиров")


def _ins_tour(add, ctx, hdr, lg):
    T, st, tim, cap, cons = lg["tour"], lg["stations"], lg["timing"], lg["capacity"], lg["consist"]
    mode = lg["mode"]
    pre = ""
    for s in st:
        if s["free_by"]:
            add(f"Станция у «{s['endpoint']}» ({s['role']}): уже есть (линия {s['free_by']}) — использовать её.")
            continue
        what = (f"грузовую ж/д станцию {s['terminals']}×{s['platform_m']:.0f} м" if mode == "rail"
                else "малый грузовой порт" if mode == "water" else f"грузовую автостанцию на {s['terminals']} терминал(а)")
        add(f"Поставить {what} у «{s['endpoint']}» ({s['role']}): точка ({s['x']}, {s['y']}), {s['dist_to_endpoint_m']} м "
            f"от центра, охват {s['catchment_m']:.0f} м — {s.get('site_reason') or ''}. Цена {_m(s['cost'])}, "
            f"содержание {_m(s['maintenance'])}/год." + _site_ins(s.get("demolition")))
    w = lg["route"].get("way") or {}
    for sg in T["segments"]:
        if mode == "water":
            continue
        if sg["shared_with"]:
            txt = f"тот же путь, что участок {sg['shared_with']} (обратный ход) — второй раз не строить"
        elif sg["prior_line"]:
            txt = f"уже построен линией {sg['prior_line']} — не строить"
        elif sg["free"]:
            txt = f"{sg['free_by']} — не строить"
        else:
            dmo = sg.get("demolition")
            txt = (f"проложить {'пути' if mode == 'rail' else 'дорогу'} «{w.get('name', '?')}» ≈{_m(sg['cost'])}"
                   + (f", сооружений {sg['structures']}" if sg["structures"] else "")
                   + _reuse_txt(sg.get("reused_m"), sg["length_m"], mode)
                   + (f"; в т.ч. СНОС ~{_nb(dmo['buildings'])} зд. ({_towns_ru(dmo['towns'])}) ≈ {_m(dmo['cost'])}"
                      if dmo else "; без сноса"))
        add(f"  Участок {sg['n']}: {sg['from']} → {sg['to']}, {_km(sg['length_m'])} — {txt}; везём: {sg['on_board_ru']}.")
    if tim.get("ruling_grade") is not None and mode == "rail":
        add(f"Новые пути вести с уклоном ≤ {_pct(tim['ruling_grade'])} (дальше {cons['label']} не тянет с нормальной скоростью).")
    _ins_depots(add, lg, pre)
    _ins_vehicles(add, lg, pre)
    topo = ("по кругу: после последней остановки — снова первая" if T["topology"] == "loop"
            else "туда-обратно: промежуточные остановки проходятся в обе стороны")
    add(f"Создать ОДНУ линию из {len(T['stops'])} остановок по порядку ({topo}):")
    for v in T["stops"]:
        mode_txt = ""
        if v["load"]:
            mode_txt = f"; режим {LOAD_RU['if_available']}, макс. стоянка ≈{min(600, round(tim['headway_s']))} с"
        add(f"  Остановка {v['n']} — «{v['name']}»: {v['action_ru']}{mode_txt}.")
    add("Не ставить «полную загрузку» на многоостановочной линии: машина зависнет на первой погрузке и остальные "
        "остановки останутся без вывоза; груз сам выгрузится там, где его принимают (у грузов в TF3 нет адреса).")
    unit_ru = {"rail": "поезд(ов)", "water": "судно(а)"}.get(mode, "машин(ы)")
    add(f"Настройки линии: {cons['n_consists']} {unit_ru}, интервал ≈{_s(tim['headway_s'])}; пиковая загрузка участка "
        f"{cap['volume']:.0f} ед./год, вместимость парка {cap['fleet_year']:.0f} ед./год.")
    _ins_check(add, lg, pre)


def _ins_prospect(add, P: dict):
    res, town = P.get("resource") or "ресурс", P.get("town") or "?"
    add(f"Применить ранг компании ≥ {P.get('rank') or '?'}: разрешение «Разведка: {res}» открывается на этом ранге, "
        f"разовое (один раз за игру на ресурс), бесплатное; работает только после ПРИМЕНЕНИЯ ранга.")
    months = f"{P.get('months') or 2.6:.1f}".replace(".", ",")
    add(f"Разрешения → Разведка: выбрать ресурс «{res}» и город «{town}». Успех ~{P.get('success') or 97:.0f} %, "
        f"ожидание ~{months} мес.")
    site = P.get("site") or {}
    lvl = (f"; старт ~уровень {P['start_level']:.0f}, при вывозе ≥75 % растёт на уровень раз в полгода"
           f" (в среднем до {P['avg_level']:.0f})" if P.get("start_level") else "")
    add(f"Ожидается: {P.get('expected') or 'предприятие'} ~{P.get('expected_volume') or 0:.0f} ед./год{lvl}. "
        f"Место — оценка (свободная зона у города, около ({site.get('x', 0):.0f}, {site.get('y', 0):.0f})); "
        f"точное место будет известно только после появления предприятия.")
    fl = P.get("first_line")
    if fl:
        add(f"Первая линия: {res.lower()} → «{fl['to']}» ({MODE_RU.get(fl['mode'], fl['mode']).lower()}), "
            f"~{fl['volume']:.0f} ед./год, доход ~{_m(fl['income'])}/год. Её разбор ниже посчитан от оценочного места — "
            f"после появления предприятия выгрузить данные мода и перестроить план/разбор.")


def _instructions(ctx: Context, hdr: dict, legs: list[dict], warehouses: list[dict], extra: dict | None = None) -> list[str]:
    add = _Ins()
    extra = extra or {}
    kind = hdr.get("kind", "cargo")
    ok = [l for l in legs if not l.get("error")]
    if kind == "multimodal" or (warehouses and kind == "cargo"):
        _ins_multimodal(add, ctx, hdr, legs, warehouses)
        return add.out
    if kind == "industry_build":
        _ins_prospect(add, extra.get("prospect") or {})
        for lg in ok:
            _ins_leg_cargo(add, ctx, hdr, lg, "[Первая линия] ")
        return add.out
    for lg in ok:
        if lg.get("tour"):
            _ins_tour(add, ctx, hdr, lg)
        elif lg.get("pax"):
            pre = f"[{lg.get('title') or lg['role_ru']}] " if len(ok) > 1 else ""
            _ins_pax(add, ctx, hdr, lg, pre)
        else:
            pre = f"[{lg['role_ru']}] " if len(ok) > 1 else ""
            _ins_leg_cargo(add, ctx, hdr, lg, pre)
    return add.out


# --------------------------------------------------------------------------- public API

def _dossier_demolition(step, legs: list[dict], warehouses: list[dict], warnings: list[str]) -> dict | None:
    """Demolition of the whole project (route + stations + depots of every leg + warehouses), per-town
    reputation, the planner's own figure (step['demolition']) and warnings (> 50 % in one town).
    The planner's step['warnings'] are taken over; ours are added only for towns they do not name."""
    acc = _demo_new()
    for lg in legs:
        _demo_add(acc, lg.get("demolition"))
    for w in warehouses:
        _demo_add(acc, w.get("demolition"))
    out = _demo_round(acc)
    plan_d = step.get("demolition") if isinstance(step, dict) else getattr(step, "demolition", None)
    plan_w = step.get("warnings") if isinstance(step, dict) else getattr(step, "warnings", None)
    plan_w = [str(x) for x in plan_w if x] if isinstance(plan_w, (list, tuple)) else []
    for x in plan_w:
        if x not in warnings:
            warnings.append(x)
    if out:
        for t, v in out["towns"].items():
            msg = _rep_warning(t, v["rating_loss"])
            if msg and not any(t in x for x in plan_w):
                warnings.append(msg)
        out["warn_towns"] = [t for t, v in out["towns"].items() if v["rating_loss"] > REP_WARN]
    if isinstance(plan_d, dict) and (plan_d.get("buildings") or plan_d.get("cost")):
        pd = _demo_add(_demo_new(), plan_d)
        plan = {"cost": round(float(plan_d.get("cost") or 0)), "m": round(float(plan_d.get("m") or 0)),
                "buildings": round(float(plan_d.get("buildings") or 0), 1),
                "towns": {t: round(v["rating_loss"], 4) for t, v in sorted(pd["towns"].items(),
                                                                            key=lambda kv: -kv[1]["rating_loss"])}}
        if out is None:
            out = {"cost": plan["cost"], "m": plan["m"], "buildings": plan["buildings"],
                   "rating": round(sum(plan["towns"].values()), 4),
                   "towns": {t: {"buildings": 0.0, "rating_loss": v} for t, v in plan["towns"].items()},
                   "items": [], "source": "plan"}
            out["text_ru"] = _demo_text(out)
            out["warn_towns"] = [t for t, v in plan["towns"].items() if v > REP_WARN]
            for t in out["warn_towns"]:
                msg = _rep_warning(t, plan["towns"][t])
                if msg and not any(t in x for x in plan_w):
                    warnings.append(msg)
        out["plan"] = plan
    if out is not None:
        out["rule_ru"] = ("Снос городских зданий: ≈ 25 тыс. $ × вместимость здания; репутация города падает на "
                          "долю снесённого района / 0,405. Уровень «Плохо» (< 2/6) запрещает снос, «Очень плохо» "
                          "(< 1/6) — новые станции в городе; восстановление 1/6 за 3 месяца (docs/demolition.md).")
    return out


def build_dossier(game: Game | None, planner, step_or_candidate, plan_steps: list[dict] | None = None,
                  ctx: Context | None = None) -> dict:
    """Detailed breakdown of one plan line. `step_or_candidate`: a plan.json step dict
    (build_line), planner.Candidate, economics.LinkEval or candidates.ProjectCandidate.
    `plan_steps` (all steps of the plan) lets the dossier see infrastructure built by earlier
    lines and the later steps of the same line. Returns a JSON-ready dict."""
    ctx = ctx or Context(game, planner)
    hdr, legs, extra = _normalize(ctx, step_or_candidate, plan_steps)
    year = int(hdr.get("year") or ctx.game.year)
    warnings: list[str] = []
    leg_out = [_leg_dossier(ctx, l, year, warnings) for l in legs]
    warehouses: list[dict] = []
    if hdr["kind"] == "multimodal":
        try:
            warehouses = _mm_warehouses(ctx, hdr, legs, leg_out, year, extra.get("details", ""))
        except Exception as e:                       # pragma: no cover - defensive
            warnings.append(f"Склад не рассчитан: {type(e).__name__}: {e}")
        if not warehouses and extra.get("warehouses_meta"):
            warehouses = list(extra["warehouses_meta"])
        for w in warehouses:
            if w.get("plan_modules") and w["plan_modules"] < w.get("modules", 0):
                warnings.append(f"Склад в узле «{w['hub']}»: в плане ×{w['plan_modules']} модул., по правилу "
                                f"«≥ 2 состава» нужно ×{w['modules']} ({w['capacity_units']} ед.).")
    elif extra.get("warehouses_meta"):
        warehouses = list(extra["warehouses_meta"])
    ok = [l for l in leg_out if not l.get("error")]
    demolition = _dossier_demolition(step_or_candidate, ok, warehouses, warnings)
    tot_capex = sum(l["money"]["capex"]["total"] for l in ok) + sum(w.get("cost", 0) for w in warehouses)
    tot_income = sum(l["money"]["income_per_year"] for l in ok)
    tot_costs = sum(l["money"]["per_year"]["total_costs"] for l in ok) + sum(w.get("maintenance", 0) for w in warehouses)
    profit = tot_income - tot_costs
    cargo = ctx.game.cargo_by_key.get(hdr.get("cargo") or "")
    timeline = []
    if plan_steps and hdr.get("line"):
        started = hdr.get("step_id") is None
        for s in plan_steps:
            if not started:
                started = s.get("id") == hdr.get("step_id")
                continue
            if str(s.get("line")) != str(hdr["line"]):
                continue
            if s.get("action") == "build_line":
                break                                # the line id was reused by a later project (older plans)
            timeline.append({"year": s.get("year"), "month": s.get("month"), "action": s.get("action"),
                             "title": s.get("title"), "cost": s.get("cost")})
    assumptions = [
        "Движение: сила тяги min(TE, P/v); уклон 3,27·i·m (как в окне покупки техники); качение 0,1·friction·g·m; "
        "лёгкое сопротивление воздуха; торможение 2,5 м/с² (ж/д), 5 м/с² (авто).",
        "Скорость в кривых v = x·(R+y)^z по speedCoeffs пути; радиусы — по сглаженной трассе планировщика.",
        "Профиль пути: рельеф с уклоном не круче рекомендуемого (насыпи/выемки), затем сглаживание 150 м.",
        "Погрузка: cap/(loadSpeed×0,0625) на вагон/машину, вагоны параллельно — не замерено в игре.",
        "Оплата: по прямой между концами (3D + 8×подъём), объезд не оплачивается; ранг компании не учтён.",
        f"Год = {ctx.year_s:.0f} с игрового времени, день = 4 с.",
    ]
    kind = hdr["kind"]
    if kind == "tour":
        assumptions.append("Маршрут: один состав проходит все участки; на каждой остановке разгон с нуля, поэтому "
                           "круг = сумма участков + погрузка/выгрузка + задержки; на участке загрузка = поток участка / "
                           "пиковый поток. Каждая единица оплачивается по прямой от своей погрузки до выгрузки.")
    if kind == "multimodal":
        assumptions.append("Перевалка только через склад (станции не хранят груз); оплата по хорде источник→потребитель "
                           "делится между плечами (multimodal.py); склад ≥ 2 самых больших состава, заходящих на него.")
    if kind in ("passenger", "workers"):
        assumptions.append("Пассажиры: спрос, охват и доля поездок — из модели passengers.py (не замерено в игре); "
                           "билет 3,609 $/м по прямой × (0,5 + priceFactor); междугородние линии везут в обе стороны, "
                           "кольцо меняет пассажиров на остановках (оборот = длина кольца / средняя поездка).")
    out = {
        **hdr, "mode_ru": MODE_RU.get(hdr.get("mode"), hdr.get("mode")),
        "cargo_info": None if cargo is None else {"key": cargo.key, "name": cargo.name, "classes": cargo.classes,
                                                  "time_to_deliver_s": cargo.time_to_deliver_s},
        "generated": datetime.now().isoformat(timespec="seconds"), "year_s": ctx.year_s, "day_s": DAY_S,
        "legs": leg_out, "warehouses": warehouses,
        "totals": {"capex": round(tot_capex), "income_per_year": round(tot_income), "costs_per_year": round(tot_costs),
                   "profit_per_year": round(profit),
                   "payback_years": round(tot_capex / profit, 2) if profit > 0 else None},
        "demolition": demolition,
        "instructions": _instructions(ctx, hdr, leg_out, warehouses, extra),
        "timeline": timeline, "warnings": warnings, "assumptions": assumptions,
    }
    if extra.get("prospect"):
        out["prospect"] = extra["prospect"]
    tours = [l["tour"] for l in ok if l.get("tour")]
    if tours:
        out["tour"] = tours[0]
    return _clean(out)


# --------------------------------------------------------------------------- CLI

def build_all(plan_path: Path = DATA / "plan.json", line: str | None = None, planner=None,
              game: Game | None = None) -> dict:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    steps = plan.get("steps", [])
    if planner is None and game is None:
        from .planner import Planner
        planner = Planner()
    ctx = Context(game, planner)
    out: dict = {}
    counts: dict[str, list[int]] = {}
    for s in steps:
        if s.get("action") != "build_line":
            continue
        lid = str(s.get("line") or s.get("id"))
        if line and lid != line and str(s.get("id")) != line:
            continue
        key = lid if lid not in out else f"{lid}@{s.get('id')}"
        t0 = time.time()
        kind = _step_kind(s)
        try:
            d = build_dossier(ctx.game, planner, s, steps, ctx=ctx)
            out[key] = d
            ok = bool([l for l in d.get("legs", []) if not l.get("error")]) and bool(d.get("instructions"))
        except Exception as e:                       # keep going: one broken line must not kill the file
            import traceback
            traceback.print_exc()
            out[key] = {"line": lid, "step_id": s.get("id"), "kind": kind, "title": s.get("title"),
                        "error": f"{type(e).__name__}: {e}"}
            ok = False
        c = counts.setdefault(kind, [0, 0])
        c[0] += 1
        c[1] += int(ok)
        print(f"  {key:10} {kind[:10]:10} {s.get('kind', ''):5} {time.time() - t0:5.2f}s  {s.get('title', '')[:70]}")
    out["_meta"] = {"generated": datetime.now().isoformat(timespec="seconds"),
                    "plan_generated": plan.get("generated"), "lines": len([k for k in out if not k.startswith("_")]),
                    "by_kind": {k: {"steps": v[0], "ok": v[1]} for k, v in counts.items()},
                    "keys": "line id; a repeated line id gets '<line>@<step id>'; match by step_id"}
    return out


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="Подробный разбор линий плана -> data/dossiers.json")
    ap.add_argument("--line", help="только эта линия (L7) или шаг (s3)")
    ap.add_argument("--plan", default=str(DATA / "plan.json"))
    ap.add_argument("--out", default=str(DATA / "dossiers.json"))
    ap.add_argument("--print", action="store_true", help="вывести инструкции в консоль")
    args = ap.parse_args(argv)
    t0 = time.time()
    res = build_all(Path(args.plan), args.line)
    out = Path(args.out)
    if args.line and out.exists():                   # merge into the existing file
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
            meta = old.get("_meta")
            old.update(res)
            if meta:
                old["_meta"] = meta
            res = old
        except json.JSONDecodeError:
            pass
    out.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    m = res.get("_meta", {})
    print(f"dossiers -> {out} ({len([k for k in res if not k.startswith('_')])} lines, {time.time() - t0:.1f}s)")
    for k, v in (m.get("by_kind") or {}).items():
        print(f"  {k:15} {v['ok']}/{v['steps']} с разбором")
    if args.print:
        for k, d in res.items():
            if k.startswith("_") or (args.line and k != args.line and d.get("step_id") != args.line):
                continue
            print(f"\n== {k}: {d.get('title')}")
            for w in d.get("warnings", []):
                print("  ! " + w)
            for t in d.get("instructions", []):
                print("  " + t)


if __name__ == "__main__":
    main()
