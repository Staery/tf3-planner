"""Compute rail/road route matrices for the current year, print the cheapest links and render
data/routes_preview.png (terrain + MST links) for a visual sanity check.

usage: python tools/routes_cli.py [--year Y] [--cell 64] [--no-cache] [--bridge-ref water|ground]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner.model import DATA  # noqa: E402
from tf3planner.routing import (NetworkMatrix, RoutingParams, get_planner, land_components,  # noqa: E402
                                load_game, network_matrix, water_network)


def fmt_row(nm: NetworkMatrix, i: int, j: int, extra: str = "") -> str:
    a, b = nm.nodes[i], nm.nodes[j]
    return (f"  {a.name[:38]:38s} -> {b.name[:38]:38s} build {nm.build[i, j]:>11,.0f}  "
            f"{nm.length[i, j] / 1000:5.2f} km  x{nm.detour[i, j]:.2f}  "
            f"bridge {nm.water_bridge_m[i, j] + nm.viaduct_m[i, j]:5.0f} m  tunnel {nm.tunnel_m[i, j]:4.0f} m {extra}")


def top_links(nm: NetworkMatrix, game, k: int = 10) -> None:
    ind_by_id = {i.id: i for i in game.industries}
    sym = nm.symmetric("build")
    nodes = nm.nodes
    ti = [(sym[a.index, b.index], a.index, b.index) for a in nodes if a.kind == "town"
          for b in nodes if b.kind == "industry" and not b.water_only]
    print(f" cheapest town-industry links ({nm.kind}):")
    for _, i, j in sorted(ti)[:k]:
        print(fmt_row(nm, i, j, f"[{nodes[j].type}]"))
    ii = []
    for a in nodes:
        for b in nodes:
            if a.kind == b.kind == "industry" and a.index != b.index:
                ca = set(ind_by_id[a.id].outputs) & set(ind_by_id[b.id].inputs)
                if ca:
                    ii.append((nm.build[a.index, b.index], a.index, b.index, ",".join(sorted(ca))))
    print(f" cheapest industry->industry supply links ({nm.kind}; water-only sources reach the shore only):")
    for _, i, j, cargo in sorted(ii)[:k]:
        flag = " (shore)" if nodes[i].water_only else ""
        print(fmt_row(nm, i, j, f"[{cargo}]{flag}"))


# --------------------------------------------------------------------------- preview

def terrain_rgb(game) -> np.ndarray:
    h = game.terrain.h.astype(np.float64)
    wl = game.terrain.water_level
    gy, gx = np.gradient(h, game.terrain.step)
    shade = np.clip(0.75 + 1.6 * (-gx - gy) / np.sqrt(2), 0.35, 1.25)   # light from the south-west in map coords
    t = np.clip((h - wl) / max(1.0, h.max() - wl), 0, 1)[..., None]
    low, mid, high = np.array([96, 150, 80]), np.array([150, 140, 95]), np.array([235, 235, 230])
    land = np.where(t < 0.4, low + (mid - low) * (t / 0.4), mid + (high - mid) * ((t - 0.4) / 0.6))
    land = land * shade[..., None]
    depth = np.clip((wl - h) / 100.0, 0, 1)[..., None]
    water = np.array([110, 165, 220]) * (1 - depth) + np.array([30, 70, 150]) * depth
    rgb = np.where((h < wl)[..., None], water, land)
    return np.clip(rgb[::-1], 0, 255).astype(np.uint8)     # flip: north (max y) at the top


def render_preview(game, mats: dict[str, NetworkMatrix], out: Path, scale: float = 1.5) -> None:
    tr = game.terrain
    base = Image.fromarray(terrain_rgb(game)).resize((int(tr.nx * scale), int(tr.ny * scale)), Image.BILINEAR)
    H = base.height
    img = Image.new("RGB", (base.width, H + 28), (255, 255, 255))   # legend strip below the map
    img.paste(base, (0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    def px(x, y):
        return ((x - tr.origin_x) / tr.step * scale, H - 1 - (y - tr.origin_y) / tr.step * scale)

    styles = {"road": ((255, 210, 40, 220), 2), "rail": ((200, 20, 20, 255), 3)}
    for kind in ("road", "rail"):
        nm = mats.get(kind)
        if nm is None:
            continue
        color, width = styles[kind]
        for i, j in nm.mst_pairs("build"):
            r = nm.route(i, j)
            if r is None or len(r.polyline) < 2:
                continue
            d.line([px(*p) for p in r.polyline], fill=color, width=width, joint="curve")
            for s, e, _ in r.bridges:
                d.line([px(*s), px(*e)], fill=(0, 230, 255, 255), width=width + 2)
            for s, e, _ in r.tunnels:
                d.line([px(*s), px(*e)], fill=(255, 0, 255, 255), width=width + 2)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
        small = ImageFont.truetype("arial.ttf", 10)
    except OSError:
        font = small = ImageFont.load_default()
    nm = next(iter(mats.values()))
    for nd in nm.nodes:
        x, y = px(nd.x, nd.y)
        if nd.kind == "town":
            d.ellipse([x - 6, y - 6, x + 6, y + 6], fill=(240, 240, 240), outline=(0, 0, 0), width=2)
            d.text((x + 8, y - 8), nd.name, fill=(0, 0, 0), font=font, stroke_width=2, stroke_fill=(255, 255, 255))
        else:
            c = (40, 90, 255) if nd.water_only else (255, 140, 0)
            d.rectangle([x - 4, y - 4, x + 4, y + 4], fill=c, outline=(0, 0, 0))
            d.text((x + 6, y + 2), nd.type, fill=(30, 30, 30), font=small)
    legend = [("rail MST (build cost)", styles["rail"][0]), ("road MST", styles["road"][0]),
              ("bridge", (0, 230, 255, 255)), ("tunnel", (255, 0, 255, 255)),
              ("industry", (255, 140, 0, 255)), ("water industry", (40, 90, 255, 255))]
    x = 10
    for label, c in legend:
        d.rectangle([x, H + 8, x + 20, H + 20], fill=c)
        d.text((x + 26, H + 7), label, fill=(0, 0, 0), font=font)
        x += 40 + int(d.textlength(label, font=font))
    img.save(out)


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--cell", type=float, default=64.0)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--bridge-ref", choices=["water", "ground"], default="water")
    ap.add_argument("--maint-years", type=float, default=10.0)
    ap.add_argument("--out", type=Path, default=DATA / "routes_preview.png")
    args = ap.parse_args()

    g = load_game()
    year = args.year or g.year
    params = RoutingParams(cell=args.cell, bridge_height_ref=args.bridge_ref, maintenance_years=args.maint_years)
    mats: dict[str, NetworkMatrix] = {}
    for kind in ("rail", "road"):
        t0 = time.perf_counter()
        pl = get_planner(g, kind, year, params)
        t1 = time.perf_counter()
        nm = network_matrix(g, kind, year, params, cache=not args.no_cache)
        t2 = time.perf_counter()
        mats[kind] = nm
        print(f"== {kind} {year}: way {pl.way.key} ({pl.way.cost_per_m:.0f}/m, {pl.way.speed * 3.6:.0f} km/h), "
              f"bridge {pl.bridge.key if pl.bridge else '-'}, tunnel {pl.tunnel.key if pl.tunnel else '-'}; "
              f"grid {pl.nx}x{pl.ny} built in {t1 - t0:.1f} s, matrix {len(nm.nodes)}x{len(nm.nodes)} "
              f"in {t2 - t1:.1f} s (computed in {nm.compute_seconds:.1f} s)")
        top_links(nm, g)
        mst = nm.mst_pairs("build")
        tot = sum(nm.symmetric("build")[i, j] for i, j in mst)
        print(f" MST over all land nodes: {len(mst)} links, {tot:,.0f} total build "
              f"(independent links, no segment sharing)\n")

    comps = land_components(g, "rail", year, params)
    print(f"== land connectivity without bridges: {len(comps)} groups")
    for k, ns in sorted(comps.items(), key=lambda kv: -len(kv[1])):
        names = [n.name for n in ns if n.kind == "town"]
        print(f"  group {k}: {len(ns)} nodes, towns: {', '.join(names) or '-'}")

    wn = water_network(g, year)
    print(f"\n== water bodies (ship speed {wn.ship_speed * 3.6:.0f} km/h); bodies >= 0.05 km2 with ports:")
    for b in wn.bodies:
        if b.nodes:
            print(f"  #{b.label}: {b.area_km2:6.2f} km2, max depth {b.max_depth:.0f} m, "
                  f"edge {'yes' if b.touches_map_edge else 'no '}: {', '.join(b.nodes)}")
    ind = {i.id: i for i in g.industries}
    print(" ship links between producers and consumers:")
    for i in wn.ports:
        for j in wn.ports:
            a, b = wn.nodes[i], wn.nodes[j]
            if a.kind == b.kind == "industry" and np.isfinite(wn.length[i, j]) and i != j:
                c = set(ind[a.id].outputs) & set(ind[b.id].inputs)
                if c:
                    print(f"  {a.name} -> {b.name}: {wn.length[i, j] / 1000:.1f} km, "
                          f"{wn.time[i, j] / 60:.0f} min [{','.join(sorted(c))}]")

    render_preview(g, mats, args.out)
    print(f"\npreview written to {args.out}")


if __name__ == "__main__":
    main()
