"""Render the game heightmap into a hillshaded PNG for the web map.

Heightmap layout: h[iy, ix], world x = origin_x + ix * step, world y = origin_y + iy * step,
y points north, so row 0 is the SOUTH edge. Images have row 0 at the top (north), hence flipud.

The image is treated as "pixel = area": native sample i covers [x_i - step/2, x_i + step/2].
`image_bounds()` returns that extent; it stays the same when the image is upscaled.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from tf3planner.model import Terrain

UPSCALE = 2           # render at 16 m/px for a smoother look when zoomed in
Z_EXAGGERATION = 3.0  # the map is fairly flat, exaggerate relief for the hillshade

# land colour ramp: (height m, rgb)
LAND_RAMP = [
    (0.0, (96, 140, 78)),
    (8.0, (88, 132, 70)),
    (30.0, (106, 140, 74)),
    (70.0, (138, 146, 86)),
    (130.0, (150, 128, 90)),
    (220.0, (140, 116, 96)),
    (320.0, (200, 196, 190)),
    (420.0, (245, 245, 245)),
]
# water colour ramp by depth below water level (m, rgb)
WATER_RAMP = [
    (0.0, (92, 150, 196)),
    (5.0, (60, 118, 176)),
    (30.0, (36, 84, 148)),
    (100.0, (22, 52, 108)),
]


def image_bounds(t: Terrain) -> dict:
    half = t.step / 2
    return {
        "minX": t.origin_x - half,
        "minY": t.origin_y - half,
        "maxX": t.origin_x + (t.nx - 1) * t.step + half,
        "maxY": t.origin_y + (t.ny - 1) * t.step + half,
    }


def _ramp(values: np.ndarray, ramp) -> np.ndarray:
    xs = np.array([r[0] for r in ramp], dtype=np.float32)
    out = np.empty(values.shape + (3,), dtype=np.float32)
    for c in range(3):
        out[..., c] = np.interp(values, xs, np.array([r[1][c] for r in ramp], dtype=np.float32))
    return out


def render(t: Terrain, upscale: int = UPSCALE) -> Image.Image:
    h = t.h.astype(np.float32)
    step = t.step
    if upscale > 1:
        im = Image.fromarray(h).resize((t.nx * upscale, t.ny * upscale), Image.BILINEAR)
        h = np.asarray(im, dtype=np.float32)
        step = step / upscale

    water = h < t.water_level
    # hillshade in world orientation (axis0 = north, axis1 = east)
    dzdn, dzde = np.gradient(h * Z_EXAGGERATION, step)
    nx, ny, nz = -dzde, -dzdn, np.ones_like(h)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz)
    light = np.array([-1.0, 1.0, 1.4])  # from north-west, elevated
    light /= np.linalg.norm(light)
    shade = (nx * light[0] + ny * light[1] + nz * light[2]) / norm
    flat = light[2]
    shade = np.clip(0.8 + 0.5 * (shade - flat) / (1 - flat), 0.35, 1.3)

    land = _ramp(np.maximum(h - t.water_level, 0), LAND_RAMP)
    land *= shade[..., None]
    sea = _ramp(np.maximum(t.water_level - h, 0), WATER_RAMP)
    # faint relief under water too
    sea *= (0.85 + 0.15 * np.clip(shade, 0.4, 1.3))[..., None]

    rgb = np.where(water[..., None], sea, land)
    # darken the shoreline a bit: land pixels adjacent to water
    shore = np.zeros_like(water)
    shore[1:, :] |= water[:-1, :]
    shore[:-1, :] |= water[1:, :]
    shore[:, 1:] |= water[:, :-1]
    shore[:, :-1] |= water[:, 1:]
    shore &= ~water
    rgb[shore] = rgb[shore] * 0.72 + np.array([200, 190, 150]) * 0.28

    rgb = np.flipud(np.clip(rgb, 0, 255)).astype(np.uint8)
    return Image.fromarray(np.ascontiguousarray(rgb))


def ensure_png(t: Terrain, path: Path, source_mtime: float | None = None) -> Path:
    """Render to `path` unless a cached file newer than the source already exists."""
    if path.exists() and (source_mtime is None or path.stat().st_mtime >= source_mtime):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.png")
    render(t).save(tmp, optimize=True)
    tmp.replace(path)
    return path
