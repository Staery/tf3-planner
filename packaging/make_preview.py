"""Draw the Mod Hub preview image mod/tf3_planner_1/_metadata/0.png (1280x720, no game assets).

  .venv/Scripts/python -X utf8 packaging/make_preview.py

The game's Mod Hub reads gallery images _metadata/0.png (0.jpg), 1.png, ... (falls back to
image_00.tga); the first one is the logo on mod.io. Everything here is drawn procedurally.
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "mod" / "tf3_planner_1" / "_metadata" / "0.png"
W, H = 1280, 720

BG = (24, 27, 31)
GRID = (38, 43, 50)
BLUE = (57, 135, 229)
RED = (230, 103, 103)
ORANGE = (237, 161, 0)
GREEN = (12, 163, 12)
WHITE = (240, 240, 240)
GREY = (160, 168, 178)


def font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> Path:
    rnd = random.Random(3493540)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # terrain-like contour blobs
    terrain = Image.new("L", (W, H), 0)
    td = ImageDraw.Draw(terrain)
    for _ in range(26):
        x, y, r = rnd.randint(380, W), rnd.randint(0, H), rnd.randint(60, 190)
        td.ellipse((x - r, y - r, x + r, y + r), fill=rnd.randint(20, 55))
    terrain = terrain.filter(ImageFilter.GaussianBlur(40))
    img.paste(Image.new("RGB", (W, H), (44, 62, 52)), (0, 0), terrain)
    d = ImageDraw.Draw(img)
    for x in range(0, W, 64):
        d.line((x, 0, x, H), fill=GRID, width=1)
    for y in range(0, H, 64):
        d.line((0, y, W, y), fill=GRID, width=1)

    # towns (grey squares) and industries (coloured squares)
    def m(x, y):                         # layout space -> right part of the image (left is the panel)
        return (round(620 + (x - 470) * 0.83), y)

    towns = [m(*p) for p in [(560, 170), (1010, 140), (1120, 470), (720, 560), (880, 330)]]
    inds = [(*m(x, y), c) for x, y, c in
            [(470, 420, ORANGE), (640, 300, GREEN), (1180, 260, ORANGE), (980, 620, GREEN), (820, 180, RED)]]
    # routes: rail (red, sleepers), road (blue)
    rail = [m(*p) for p in [(470, 420), (560, 380), (640, 300), (760, 300), (880, 330), (1010, 300), (1180, 260)]]
    road = [[m(*p) for p in seg] for seg in ([(560, 170), (640, 300)], [(880, 330), (1120, 470), (980, 620)],
                                             [(720, 560), (880, 330)], [(820, 180), (1010, 140)])]
    for seg in road:
        d.line(seg, fill=BLUE, width=9, joint="curve")
    d.line(rail, fill=RED, width=12, joint="curve")
    for (x1, y1), (x2, y2) in zip(rail, rail[1:]):
        n = int(math.hypot(x2 - x1, y2 - y1) // 18)
        for k in range(1, n):
            t = k / n
            x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            ang = math.atan2(y2 - y1, x2 - x1) + math.pi / 2
            dx, dy = math.cos(ang) * 11, math.sin(ang) * 11
            d.line((x - dx, y - dy, x + dx, y + dy), fill=(255, 210, 210), width=3)
    for x, y in towns:
        for _ in range(9):
            ox, oy, s = rnd.randint(-38, 38), rnd.randint(-30, 30), rnd.randint(8, 16)
            d.rectangle((x + ox - s, y + oy - s, x + ox + s, y + oy + s), fill=(120, 126, 136), outline=BG)
    for x, y, col in inds:
        d.rounded_rectangle((x - 26, y - 26, x + 26, y + 26), radius=8, fill=col, outline=WHITE, width=4)
    for x, y in (rail[0], rail[-1]):
        d.ellipse((x - 14, y - 14, x + 14, y + 14), fill=WHITE, outline=RED, width=5)

    # left panel with the title
    panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    pd.rectangle((0, 0, 560, H), fill=(18, 20, 23, 236))
    img.paste(panel, (0, 0), panel)
    d = ImageDraw.Draw(img)
    f_title = font(["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"], 92)
    f_sub = font(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"], 34)
    f_small = font(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"], 26)
    d.text((56, 120), "TF3", font=f_title, fill=BLUE)
    d.text((56, 215), "Planner", font=f_title, fill=WHITE)
    d.line((60, 340, 460, 340), fill=BLUE, width=4)
    lines = ["Экспорт карты в один клик", "Оптимизатор логистики", "Чертёж плана прямо в игре"]
    for i, t in enumerate(lines):
        y = 372 + i * 54
        d.ellipse((60, y + 12, 76, y + 28), fill=[ORANGE, GREEN, RED][i])
        d.text((92, y), t, font=f_sub, fill=WHITE)
    d.text((60, 640), "Script Mod  ·  Transport Fever 3", font=f_small, fill=GREY)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, optimize=True)
    print(f"{OUT} ({OUT.stat().st_size // 1024} KB)")
    return OUT


if __name__ == "__main__":
    main()
