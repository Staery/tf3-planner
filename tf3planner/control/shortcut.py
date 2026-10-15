"""Create the desktop shortcut "TF3 Planner" (and its icon).

  python -m tf3planner.control.shortcut
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .paths import NO_WINDOW, ROOT

ICON = Path(__file__).resolve().parent / "tf3planner.ico"
VBS = ROOT / "TF3 Planner.vbs"
NAME = "TF3 Planner"


def make_icon(path: Path = ICON) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, S - 8, S - 8), radius=48, fill=(26, 26, 25, 255), outline=(57, 135, 229, 255),
                        width=10)
    # a route: two stations joined by a rail line with sleepers
    pts = [(56, 188), (110, 120), (150, 150), (200, 72)]
    d.line(pts, fill=(230, 103, 103, 255), width=16, joint="curve")
    for (x, y), col in zip((pts[0], pts[-1]), ((237, 161, 0, 255), (12, 163, 12, 255))):
        d.ellipse((x - 22, y - 22, x + 22, y + 22), fill=col, outline=(255, 255, 255, 255), width=6)
    try:
        font = ImageFont.truetype("segoeuib.ttf", 64)
    except OSError:
        font = ImageFont.load_default()
    d.text((100, 60), "TF3", font=font, fill=(255, 255, 255, 255), anchor="mm")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


def desktop_dir() -> Path:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                             capture_output=True, timeout=15, creationflags=NO_WINDOW).stdout
        p = Path(out.decode("utf-8", "ignore").strip() or out.decode("cp866", "ignore").strip())
        if p.is_dir():
            return p
    except Exception:
        pass
    return Path(os.path.expanduser("~")) / "Desktop"


def create(desktop: Path | None = None) -> Path:
    icon = ICON if ICON.exists() else make_icon()
    lnk = (desktop or desktop_dir()) / f"{NAME}.lnk"
    wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
    env = dict(os.environ, TF3P_LNK=str(lnk), TF3P_TARGET=str(wscript), TF3P_ARGS=f'"{VBS}"',
               TF3P_WD=str(ROOT), TF3P_ICON=f"{icon},0")
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:TF3P_LNK);"
          "$s.TargetPath=$env:TF3P_TARGET;$s.Arguments=$env:TF3P_ARGS;$s.WorkingDirectory=$env:TF3P_WD;"
          "$s.IconLocation=$env:TF3P_ICON;$s.Description='TF3 Planner control center';$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], env=env,
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
    if r.returncode != 0 or not lnk.exists():
        raise RuntimeError(r.stderr.decode("cp866", "ignore").strip() or "PowerShell не создал ярлык")
    return lnk


if __name__ == "__main__":
    try:
        p = create()
        msg = f"Ярлык создан: {p}"
        code = 0
    except Exception as e:
        msg, code = f"Не удалось создать ярлык: {e}", 1
    print(msg)
    if "pythonw" in Path(sys.executable).name.lower():
        from .launcher import message
        message(msg, error=bool(code))
    sys.exit(code)
