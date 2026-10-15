"""Local web map for the TF3 planner.

Run:  .venv/Scripts/python -X utf8 -m tf3planner.web.server  [--port 8010] [--host 127.0.0.1]

Endpoints
  GET /                 single page UI (static/index.html)
  GET /static/...       JS / CSS
  GET /api/game         towns, industries, cargos, bounds, year, loans, ways, bridges
  GET /api/terrain.png  hillshaded terrain (cached in data/terrain.png)
  GET /api/plan         data/plan.json if present, else data/plan_example.json
  GET /api/routes       data/routes.json if present, else []
  GET /api/network      existing streets / tracks (export v6 network) as polylines for the map
  GET /control.html     control center (tf3planner/control/api.py, /api/control/*)
"""
from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from tf3planner import i18n
from tf3planner.model import DATA, Game
from tf3planner.web import terrain as terrain_mod
from tf3planner.web.localized import JSONResponse, localized_file

WEB = Path(__file__).resolve().parent
STATIC = WEB / "static"
EXPORT = DATA / "export.json"
RECIPES = DATA / "industry_recipes.json"
TERRAIN_PNG = DATA / "terrain.png"
PLAN = DATA / "plan.json"
PLAN_EXAMPLE = DATA / "plan_example.json"
ROUTES = DATA / "routes.json"

_lock = threading.Lock()
_cache: dict = {"mtime": None, "game": None, "payload": None}


# --------------------------------------------------------------------------- game loading

def _normalize_export(export: dict) -> dict:
    """Work around Lua->JSON quirks: a Lua table keyed by 1..n becomes a JSON list.
    perCargo is keyed by cargo id, so ids 1..n turn into a list (index 0 = cargo id 1)."""
    for ind in export.get("industries", []):
        pc = ind.get("perCargo")
        if isinstance(pc, list):
            ind["perCargo"] = {str(i + 1): v for i, v in enumerate(pc) if v}
    return export


def _load_game() -> Game:
    try:
        return Game.load(EXPORT, RECIPES)
    except Exception:
        export = _normalize_export(json.loads(EXPORT.read_text(encoding="utf-8")))
        recipes = json.loads(RECIPES.read_text(encoding="utf-8"))
        return Game(export, recipes)


def get_game() -> Game:
    mtime = EXPORT.stat().st_mtime
    with _lock:
        if _cache["game"] is None or _cache["mtime"] != mtime:
            _cache["game"] = _load_game()
            _cache["mtime"] = mtime
            _cache["payload"] = None
            # names from the save (towns, industries, cargo, vehicles) are never translated
            i18n.register_names(i18n.names_from_export(_cache["game"].raw))
        return _cache["game"]


def protect_save_names() -> None:
    """Make sure the proper names of the current save are known to the translator."""
    try:
        get_game()
    except (OSError, ValueError, KeyError):
        pass


# --------------------------------------------------------------------------- payload

def _type_name(ind) -> str:
    """Localized industry type, taken from the generated name 'Town — Type[ — Suffix]'."""
    parts = [p.strip() for p in ind.name.split("—")]
    return parts[1] if len(parts) > 1 else ind.type


def _cargo_ref(g: Game, key: str) -> dict:
    c = g.cargo_by_key.get(key)
    return {"key": key, "name": c.name if c else key}


def game_payload() -> dict:
    g = get_game()
    with _lock:
        if _cache["payload"] is not None:
            return _cache["payload"]
    t = g.terrain
    bbox = ((g.raw.get("meta") or {}).get("bbox")) or {}
    towns = []
    for tw in g.towns:
        towns.append({
            "id": tw.id, "name": tw.name, "x": tw.x, "y": tw.y, "z": round(tw.z, 2),
            "capacities": tw.capacities,
            "total": int(sum(v or 0 for v in tw.capacities.values())),
            "needs": {lu: [_cargo_ref(g, k) for k in keys] for lu, keys in tw.needs.items()},
        })
    industries = []
    for ind in g.industries:
        industries.append({
            "id": ind.id, "name": ind.name, "type": ind.type, "type_name": _type_name(ind),
            "x": ind.x, "y": ind.y, "z": round(ind.z, 2),
            "level": ind.level, "max_level": ind.max_level, "on_water": ind.on_water,
            "inputs": [_cargo_ref(g, k) for k in ind.inputs],
            "outputs": [_cargo_ref(g, k) for k in ind.outputs],
            "production": ind.per_cargo,
        })
    payload = {
        "year": g.year,
        "money": g.money,
        "bounds": {
            "minX": (bbox.get("min") or {}).get("x", t.origin_x),
            "minY": (bbox.get("min") or {}).get("y", t.origin_y),
            "maxX": (bbox.get("max") or {}).get("x", t.origin_x + (t.nx - 1) * t.step),
            "maxY": (bbox.get("max") or {}).get("y", t.origin_y + (t.ny - 1) * t.step),
        },
        "terrain": {"image_bounds": terrain_mod.image_bounds(t), "step": t.step, "nx": t.nx, "ny": t.ny,
                    "water_level": t.water_level},
        "cargos": [{"id": c.id, "key": c.key, "name": c.name, "is_passenger": c.is_passenger}
                   for c in sorted(g.cargos.values(), key=lambda c: c.id)],
        "towns": towns,
        "industries": industries,
        "loans": [{"kind": l.kind, "amount": l.amount, "months": l.months, "rate": l.rate,
                   "interest_total": l.interest_total, "monthly_payment": l.monthly_payment,
                   "effective_annual_rate": l.effective_annual_rate} for l in g.loans],
        "ways": [{"key": w.key, "name": w.name, "kind": w.kind, "cost_per_m": w.cost_per_m,
                  "maintenance_per_m": w.maintenance_per_m, "speed_kmh": round(w.speed * 3.6),
                  "year_from": w.year_from, "year_to": w.year_to, "electrified": w.electrified,
                  "available": w.available(g.year)} for w in g.ways],
        "bridges": [{"key": b.key, "name": b.name, "tunnel": b.tunnel, "cost_per_m": b.cost_per_m,
                     "maintenance_per_m": b.maintenance_per_m, "speed_kmh": round(b.speed_limit * 3.6),
                     "year_from": b.year_from, "year_to": b.year_to,
                     "available": b.available(g.year)} for b in g.bridges],
    }
    with _lock:
        _cache["payload"] = payload
    return payload


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- app

app = FastAPI(title="TF3 Planner map", default_response_class=JSONResponse)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def _language(request: Request, call_next):
    """Answer in the language the page asked for (X-Lang header, ?lang=), else the saved setting."""
    from tf3planner.control import settings
    i18n.set_lang(request.headers.get("x-lang") or request.query_params.get("lang")
                  or settings.load().get("lang"))
    return await call_next(request)

# control center (one-click launcher UI): /control.html + /api/control/*  — see tf3planner/control/api.py
from tf3planner.control import api as control_api  # noqa: E402
control_api.register(app)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/game")
def api_game():
    return JSONResponse(game_payload())


@app.get("/api/terrain.png")
def api_terrain():
    g = get_game()
    src_mtime = max(EXPORT.stat().st_mtime, Path(terrain_mod.__file__).stat().st_mtime)
    with _lock:
        terrain_mod.ensure_png(g.terrain, TERRAIN_PNG, src_mtime)
    return FileResponse(TERRAIN_PNG, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.get("/api/plan")
def api_plan():
    path = PLAN if PLAN.exists() else PLAN_EXAMPLE
    if not path.exists():
        return JSONResponse({"steps": [], "cashflow": [], "summary": {}, "source": None})
    protect_save_names()
    try:
        plan = localized_file(path)
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"{path.name}: {e}"}, status_code=500)
    if isinstance(plan, dict):
        plan = {**plan, "source": plan.get("source") or path.name}
    return JSONResponse(plan, headers={"Cache-Control": "no-cache"}, translated=True)


@app.get("/api/routes")
def api_routes():
    if not ROUTES.exists():
        return JSONResponse([])
    try:
        protect_save_names()
        return JSONResponse(localized_file(ROUTES), headers={"Cache-Control": "no-cache"}, translated=True)
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"{ROUTES.name}: {e}"}, status_code=500)


@app.get("/api/dossiers")
def api_dossiers():
    """Detailed per-line breakdowns written by `python -m tf3planner.dossier` (data/dossiers.json)."""
    p = ROUTES.parent / "dossiers.json"
    try:
        protect_save_names()
        return JSONResponse(localized_file(p) if p.exists() else {}, headers={"Cache-Control": "no-cache"},
                            translated=True)
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"{p.name}: {e}"}, status_code=500)


@app.get("/api/chords")
def api_chords(q: str = ""):
    """Read-only terrain probe for the map's "build-similar" guide: why does the planned line turn here?
    q = "x1,y1,x2,y2;x1,y1,x2,y2;..." (world metres, up to 64 chords). For every straight chord:
    {water_frac, max_h, min_h, mean_h} sampled every ~20 m (null for a malformed chord)."""
    import math
    t = get_game().terrain
    out = []
    for part in q.split(";")[:64]:
        try:
            x1, y1, x2, y2 = (float(v) for v in part.split(","))
        except ValueError:
            out.append(None)
            continue
        n = max(2, min(500, int(math.hypot(x2 - x1, y2 - y1) / 20) + 1))
        hs, wet = [], 0
        for i in range(n):
            k = i / (n - 1)
            h = t.height(x1 + (x2 - x1) * k, y1 + (y2 - y1) * k)
            hs.append(h)
            wet += h < t.water_level
        out.append({"water_frac": round(wet / n, 3), "max_h": round(max(hs), 1), "min_h": round(min(hs), 1),
                    "mean_h": round(sum(hs) / n, 1)})
    return JSONResponse(out)


# --------------------------------------------------------------------------- existing roads / tracks

# Drawing classes of the existing network (export v6 `network` graph), see network_payload().
NET_CLASSES = ("country", "town", "highway", "player_road", "track_player", "track_other", "tram", "other")
NET_STRUCTS = ("normal", "bridge", "tunnel")
NET_SOURCE_ENV = "TF3_NETWORK_SOURCE"   # dev/test only: read the network from this export-like JSON instead
_net_cache: dict = {"key": None, "body": None, "gzip": None}
_net_lock = threading.Lock()


def _net_edge_class(net) -> "list[int]":
    """Class index (NET_CLASSES) per edge: kind road/rail/tram/other x owner, highways by template name."""
    from tf3planner import network as N
    hw = [("/street/highway/" in t.name.lower()) for t in net.templates]
    out = []
    for k, o, ti in zip(net.kind.tolist(), net.owner.tolist(), net.template.tolist()):
        if k == N.ROAD:
            c = 3 if o == N.PLAYER else 2 if (0 <= ti < len(hw) and hw[ti]) else 1 if o == N.TOWN else 0
        elif k == N.RAIL:
            c = 4 if o == N.PLAYER else 5
        else:
            c = 6 if k == N.TRAM else 7
        out.append(c)
    return out


def _net_edge_interiors(net, tol: float = 0.75) -> "list[list[tuple[float, float]] | None]":
    """Interior points (p0 -> p1 order, without the end points) approximating each edge's cubic Hermite
    curve; None for straight edges. Segment count from the curve's mid deviation |T0 - T1| / 8 so the
    chord error stays below ~tol metres (2..8 segments). Same tangent sanity rule as network.edge_samples."""
    M = net.n_edges
    out: list = [None] * M
    if net.tangents is None or M == 0:
        return out
    import numpy as np
    p0 = net.xyz[net.edges[:, 0], :2]
    p1 = net.xyz[net.edges[:, 1], :2]
    T0 = net.tangents[:, 0, :2]
    T1 = net.tangents[:, 1, :2]
    chord = np.linalg.norm(p1 - p0, axis=1)
    n0, n1 = np.linalg.norm(T0, axis=1), np.linalg.norm(T1, axis=1)
    sane = (chord > 1.0) & (n0 > 0.3 * chord) & (n0 < 3 * chord) & (n1 > 0.3 * chord) & (n1 < 3 * chord)
    dev = np.linalg.norm(T0 - T1, axis=1) / 8.0
    curved = np.nonzero(sane & (dev > tol))[0]
    nseg = np.clip(np.ceil(np.sqrt(dev[curved] / tol)), 2, 8).astype(int)
    for j, n in zip(curved.tolist(), nseg.tolist()):
        ax, ay = p0[j]; bx, by = p1[j]; t0x, t0y = T0[j]; t1x, t1y = T1[j]
        pts = []
        for s in range(1, n):
            t = s / n
            t2, t3 = t * t, t * t * t
            h00, h10, h01, h11 = 2 * t3 - 3 * t2 + 1, t3 - 2 * t2 + t, -2 * t3 + 3 * t2, t3 - t2
            pts.append((h00 * ax + h10 * t0x + h01 * bx + h11 * t1x, h00 * ay + h10 * t0y + h01 * by + h11 * t1y))
        out[j] = pts
    return out


def _rdp(pts: list, eps: float) -> list:
    """Douglas-Peucker on a list of (x, y) (iterative)."""
    n = len(pts)
    if n < 3:
        return pts
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    e2 = eps * eps
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]; bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if L2 > 0:
                cr = (px - ax) * dy - (py - ay) * dx
                d2 = cr * cr / L2
            else:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            if d2 > best:
                best, bi = d2, i
        if bi >= 0 and best > e2:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(pts, keep) if k]


def _net_polylines(net, edge_idx: list, interiors: list) -> "list[list[tuple[float, float]]]":
    """Merge the given edges into maximal chains (through nodes of degree 2 within the group)."""
    inc: dict[int, list[int]] = {}
    E = net.edges
    for j in edge_idx:
        a, b = int(E[j, 0]), int(E[j, 1])
        inc.setdefault(a, []).append(j)
        inc.setdefault(b, []).append(j)
    used: set[int] = set()
    xy = net.xyz

    def walk(start_node: int, j: int) -> list:
        pts = [(float(xy[start_node, 0]), float(xy[start_node, 1]))]
        node = start_node
        while True:
            used.add(j)
            a, b = int(E[j, 0]), int(E[j, 1])
            fwd = a == node
            nxt = b if fwd else a
            mid = interiors[j]
            if mid:
                pts.extend(mid if fwd else mid[::-1])
            pts.append((float(xy[nxt, 0]), float(xy[nxt, 1])))
            node = nxt
            if node == start_node:
                break
            cand = inc.get(node, ())
            if len(cand) != 2:
                break
            j2 = cand[0] if cand[1] == j else cand[1]
            if j2 in used or j2 == j:
                break
            j = j2
        return pts

    lines = []
    for node, js in inc.items():            # chains start at ends / junctions
        if len(js) == 2:
            continue
        for j in js:
            if j not in used:
                lines.append(walk(node, j))
    for j in edge_idx:                      # leftovers: closed loops of degree-2 nodes
        if j not in used:
            lines.append(walk(int(E[j, 0]), j))
    return lines


def _flat_ints(pts: list) -> list[int]:
    """Round to 1 m, drop repeated points, flatten [x0, y0, x1, y1, ...]."""
    out: list[int] = []
    px = py = None
    for x, y in pts:
        ix, iy = int(round(x)), int(round(y))
        if ix == px and iy == py:
            continue
        out.append(ix); out.append(iy)
        px, py = ix, iy
    return out


def network_payload(net, eps: float = 1.0) -> dict:
    """Compact drawing payload: polylines per class and structure, coords rounded to 1 m."""
    import numpy as np
    cls = _net_edge_class(net)
    interiors = _net_edge_interiors(net)
    groups: dict[tuple[int, int], list[int]] = {}
    for j, (c, s, (a, b)) in enumerate(zip(cls, net.struct.tolist(), net.edges.tolist())):
        if a == b and not interiors[j]:
            continue
        groups.setdefault((c, int(s)), []).append(j)
    classes: dict = {}
    km: dict = {}
    n_pts = n_lines = 0
    length = net.length
    for (c, s), idx in sorted(groups.items()):
        cname, sname = NET_CLASSES[c], NET_STRUCTS[s]
        lines = []
        for pl in _net_polylines(net, idx, interiors):
            flat = _flat_ints(_rdp(pl, eps))
            if len(flat) >= 4:
                lines.append(flat)
                n_pts += len(flat) // 2
        n_lines += len(lines)
        classes.setdefault(cname, {})[sname] = lines
        km[cname] = round(km.get(cname, 0.0) + float(np.sum(length[idx])) / 1000, 2)
    xs, ys = net.xyz[:, 0], net.xyz[:, 1]
    return {
        "available": True,
        "hash": net.hash(),
        "edges": net.n_edges, "nodes": net.n_nodes, "failed_edges": net.failed_edges,
        "lines": n_lines, "points": n_pts,
        "bounds": ({"minX": float(xs.min()), "minY": float(ys.min()), "maxX": float(xs.max()), "maxY": float(ys.max())}
                   if len(xs) else None),
        "km": km,
        "classes": classes,
    }


def _network_unavailable(export: dict | None, why: str | None = None) -> dict:
    """{"available": false, "reason": ...} with the most useful explanation."""
    if why is None:
        exp = export or {}
        ver = exp.get("version")
        errs = [e for e in (exp.get("errors") or []) if isinstance(e, dict)
                and str(e.get("section") or "").lower().startswith("network")]
        if isinstance(ver, (int, float)) and ver < 6:
            why = f"экспорт версии {ver}: дорожной сети в нём нет (нужна версия ≥ 6)"
        elif errs:
            first = str(errs[0].get("error") or "").strip().splitlines()
            why = "выгрузка дорожной сети в игре завершилась ошибкой: " + (first[0] if first else "?")
        else:
            why = "в экспорте нет дорожной сети (раздел network без графа)"
    return {"available": False, "reason": why}


def _network_source() -> tuple[Path, float]:
    import os
    alt = os.environ.get(NET_SOURCE_ENV)
    p = Path(alt) if alt else EXPORT
    return p, p.stat().st_mtime


@app.get("/api/network")
def api_network(request: Request):
    """Existing streets and tracks from the game export (read-only), for the map layer.
    Cached per export.json mtime; gzip-compressed when the client accepts it."""
    import gzip
    from tf3planner.network import TransportNetwork
    try:
        path, mtime = _network_source()
    except OSError:
        return JSONResponse(_network_unavailable(None, "нет data/export.json — нажмите экспорт в игре"))
    key = (str(path), mtime)
    with _net_lock:
        if _net_cache["key"] != key:
            try:
                export = get_game().raw if path == EXPORT else json.loads(path.read_text(encoding="utf-8"))
                net = TransportNetwork.from_export(export)
                payload = network_payload(net) if net is not None and net.n_edges else _network_unavailable(export)
            except Exception as e:  # noqa: BLE001 - the map must keep working without the layer
                payload = _network_unavailable(None, f"не удалось прочитать дорожную сеть: {type(e).__name__}: {e}")
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            _net_cache.update(key=key, body=body, gzip=gzip.compress(body, 6))
        body, gz = _net_cache["body"], _net_cache["gzip"]
    headers = {"Cache-Control": "no-cache"}
    if "gzip" in (request.headers.get("accept-encoding") or ""):
        headers["Content-Encoding"] = "gzip"
        return Response(gz, media_type="application/json", headers=headers)
    return Response(body, media_type="application/json", headers=headers)


def start_translation_warm_up(period_s: float = 10.0) -> None:
    """Keep English copies of plan / routes / dossiers ready while English is the chosen language,
    so the map does not wait for the translation after every new plan."""
    import time
    from tf3planner.control import settings
    from tf3planner.web.localized import localized_file

    def loop():
        while True:
            try:
                if settings.load().get("lang") == "en":
                    i18n.set_lang("en")
                    protect_save_names()
                    for p in (PLAN, ROUTES, ROUTES.parent / "dossiers.json"):
                        if p.exists():
                            localized_file(p)
            except Exception:  # noqa: BLE001 - best effort, the request path translates anyway
                pass
            time.sleep(period_s)
    threading.Thread(target=loop, name="i18n-warm-up", daemon=True).start()


def main():
    import uvicorn
    ap = argparse.ArgumentParser(description="TF3 planner web map")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="info"))
    control_api.on_server_start(server, args.port)    # clean shutdown from the UI + auto mode watcher
    start_translation_warm_up()
    server.run()


if __name__ == "__main__":
    main()
