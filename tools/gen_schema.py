"""Generate mod/.../tf3_planner/schema.lua from the game's Teal definitions (*.d.tl).

TF3 engine objects are userdata that do not support pairs(), so the exporter needs
field names per type. The schema maps fully qualified record names to
{field = typeCode}; typeCode is "P" (primitive), "R:Full.Name" (record),
"L:<code>" (list), "M:<code>" (map values) or "?" (unknown).
`short` maps an unqualified record name to its full name when unambiguous —
tostring(userdata) yields "ShortName: 0x...", which the exporter uses to pick a record.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in (None, ""):            # run as a script: make `tf3planner` importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tf3planner import config  # noqa: E402

# game install: $TF3P_GAME, settings.json "game_dir" or Steam libraries (tf3planner/config.py)
GAME = config.game_dir()
OUT = config.CODE_ROOT / "mod" / "tf3_planner_1" / "content" / "tf3_planner" / "schema.lua"

PRIMITIVES = {"integer", "number", "string", "boolean", "any", "table", "nil", "ResName", "CargoTypeId", "NodeId"}

RE_RECORD = re.compile(r"^\s*(?:global\s+)?(record|interface)\s+([A-Za-z_]\w*)(?:\s*<[^>]*>)?(?:\s+is\s+(.+?))?\s*$")
RE_ENUM = re.compile(r"^\s*(?:global\s+)?enum\s+([A-Za-z_]\w*)\s*$")
RE_FIELD = re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*(.+?)\s*$")
RE_END = re.compile(r"^\s*end\s*$")


def strip_comment(line: str) -> str:
    i = line.find("--")
    return line if i < 0 else line[:i]


def parse(files):
    records: dict[str, dict[str, str]] = {}
    parents: dict[str, list[str]] = {}
    scopes: dict[str, list[str]] = {}  # record -> enclosing scope chain for resolution
    for f in files:
        stack: list[tuple[str, str]] = []  # (kind, name)
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = strip_comment(raw).rstrip()
            if not line.strip():
                continue
            m = RE_RECORD.match(line)
            if m:
                kind, name, parent = m.groups()
                path = ".".join(n for k, n in stack if k == "record") + ("." if any(k == "record" for k, _ in stack) else "") + name
                stack.append(("record", name))
                records.setdefault(path, {})
                scopes[path] = [path.rsplit(".", i)[0] for i in range(path.count(".") + 1)]
                if parent:
                    parents.setdefault(path, []).extend(p.strip() for p in parent.split(","))
                continue
            if RE_ENUM.match(line):
                stack.append(("enum", ""))
                continue
            if RE_END.match(line):
                if stack:
                    stack.pop()
                continue
            if stack and stack[-1][0] == "record":
                fm = RE_FIELD.match(line)
                if fm and "function" not in fm.group(2):
                    path = ".".join(n for k, n in stack if k == "record")
                    records[path][fm.group(1)] = fm.group(2)
    return records, parents, scopes


def split_top(s: str, sep: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch in "{<(":
            depth += 1
        elif ch in "}>)":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts]


def resolve(name: str, scope_chain: list[str], records) -> str | None:
    name = re.sub(r"<.*>", "", name).strip()
    for sc in scope_chain:
        cand = f"{sc}.{name}"
        if cand in records:
            return cand
    return name if name in records else None


def code(t: str, scope_chain, records) -> str:
    t = t.strip()
    if t.startswith("{") and t.endswith("}"):
        inner = t[1:-1].strip()
        kv = split_top(inner, ":")
        if len(kv) == 2:
            return "M:" + code(kv[1], scope_chain, records)
        items = split_top(inner, ",")
        if len(items) > 1:
            return "P"  # tuple -> generic walk
        return "L:" + code(inner, scope_chain, records)
    if t in PRIMITIVES:
        return "P"
    r = resolve(t, scope_chain, records)
    return f"R:{r}" if r else "?"


def main(game: Path | None = None, out_path: Path | None = None) -> int:
    game = Path(game) if game else (GAME or config.require_game_dir())
    out_path = Path(out_path) if out_path else OUT
    files = sorted((game / "api" / "tealdef").rglob("*.d.tl")) + sorted((game / "base" / "tealdef").rglob("*.d.tl"))
    if not files:
        raise FileNotFoundError(f"в {game} нет определений *.d.tl (api/tealdef, base/tealdef)")
    records, parents, scopes = parse(files)
    out: dict[str, dict[str, str]] = {}
    for rec, fields in records.items():
        chain = scopes.get(rec, [])
        out[rec] = {k: code(v, chain, records) for k, v in fields.items() if not k.endswith("_native")}
    # inheritance: copy parent fields
    for rec, ps in parents.items():
        for p in ps:
            pr = resolve(p, scopes.get(rec, []), records)
            if pr:
                for k, v in out.get(pr, {}).items():
                    out[rec].setdefault(k, v)
    # Model.metadata is a MetadataMap userdata whose keys are the lower-cased names of the
    # ModelMetadata sub-records (transportVehicle, cost, maintenance, availability, ...)
    out["MetadataMap"] = {
        rec.split(".")[1][0].lower() + rec.split(".")[1][1:]: f"R:{rec}"
        for rec in out if rec.startswith("ModelMetadata.") and rec.count(".") == 1
    }
    # enum-like records (every field is the record itself, e.g. RoadType.TRACK : RoadType):
    # drop them and mark references with "E" so the dumper does not recurse into them
    enums = {rec for rec, f in out.items() if f and all(v == f"R:{rec}" for v in f.values())}
    for rec in enums:
        out[rec] = {}
    for rec, f in out.items():
        for k, v in f.items():
            for e in enums:
                if v.endswith(f"R:{e}"):
                    f[k] = v[: -len(f"R:{e}")] + "E"
                    break
    short: dict[str, str | None] = {}
    for rec in out:
        s = rec.rsplit(".", 1)[-1]
        short[s] = rec if s not in short else None

    def lua_str(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["-- generated by tools/gen_schema.py from the game's *.d.tl — do not edit", "local R = {}"]
    for rec in sorted(out):
        if not out[rec]:
            continue
        body = ", ".join(f"[{lua_str(k)}]={lua_str(v)}" for k, v in sorted(out[rec].items()))
        lines.append(f"R[{lua_str(rec)}] = {{{body}}}")
    lines.append("local S = {}")
    for s, rec in sorted(short.items()):
        if rec and out.get(rec):
            lines.append(f"S[{lua_str(s)}] = {lua_str(rec)}")
    lines.append("return { records = R, short = S }")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len([r for r in out if out[r]])} records -> {out_path}")
    for probe in ["CargoType", "StreetTemplate", "BridgeType", "MultipleUnit", "BaseConfig",
                  "Engine.Component.StockList", "Engine.Component.StockList.Rule", "LaneConfig",
                  "ConstructionDesc", "Vec2f", "Vec3f", "ScriptRef"]:
        print(probe, out.get(probe) if probe in out else ("short->" + str(short.get(probe))))
    return len([r for r in out if out[r]])


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Generate the mod's schema.lua from the game's *.d.tl")
    ap.add_argument("--game", type=Path, default=None, help="Transport Fever 3 install folder (default: auto)")
    ap.add_argument("--out", type=Path, default=None, help=f"output file (default {OUT})")
    a = ap.parse_args()
    try:
        main(a.game, a.out)
    except OSError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        sys.exit(2)
