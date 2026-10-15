"""Blueprint overlay: Python writer (tf3planner.overlay) + the mod side (blueprint.lua and the
window recipe) against a fake engine/GUI API. Run with pytest or directly."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest
from lupa.lua52 import LuaRuntime, lua_type

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner.model import DATA  # noqa: E402
from tf3planner.overlay import build_overlay, lua_file, sub_polyline, thin, to_lua  # noqa: E402
from tf3planner import overlay as ov  # noqa: E402

CONTENT = Path(__file__).resolve().parent.parent / "mod" / "tf3_planner_1" / "content"


# --------------------------------------------------------------------------- synthetic plan

PLAN = {"generated": "test", "steps": [
    {"id": "s1", "year": 1900, "month": 1, "action": "build_line", "kind": "rail", "line": "L1", "cargo": "logs",
     "title": "Ж/д «A» → B \"кавычки\" \\ слэш", "cost": 1000,
     "vehicles": [{"name": "Borsig", "count": 2}, {"name": "Вагон", "count": 8}],
     "segments": [{"type": "new", "polyline": [[0, 0], [250, 0.4], [500, 0], [1000, 0]], "way": "simple", "leg": 0},
                  {"type": "existing", "polyline": [[1000, 0], [1000, 300]], "way": "town_old_small", "leg": 0}]},
    {"id": "s2", "year": 1900, "month": 3, "action": "build_line", "kind": "road", "line": "L2", "title": "Дорога C → D",
     "segments": [{"type": "new", "polyline": [[0, 500], [800, 500]], "way": "country_old_small", "leg": 0}],
     "from": {"x": 0, "y": 500, "name": "C"}, "to": {"x": 800, "y": 500, "name": "D"}},
    {"id": "s3", "year": 1901, "month": 1, "action": "upgrade_track", "kind": "rail", "line": "L1",
     "title": "Разъезды L1", "geometry": [[0, 0], [1000, 0]]},
    {"id": "s4", "year": 1901, "action": "note", "title": "-"},
]}
DOSSIERS = {"L1": {"step_id": "s1", "line": "L1", "mode": "rail", "warehouses": [
    {"name": "Склад (bulk)", "x": 500, "y": -60, "hub": "узел"}],
    "tour": {"stops": [{"n": 1, "name": "A", "x": 0, "y": 0, "action_ru": "загрузить"}, {"n": 2, "name": "B", "x": 1000, "y": 0}]},
    "legs": [{"mode": "rail", "route": {
        "polyline": [[0, 0], [1000, 0]],
        "structures": [{"type": "bridge", "at_m": 400, "to_m": 500, "length_m": 100, "name": "Мост", "x": 450, "y": 30},
                       {"type": "tunnel", "at_m": 700, "to_m": 760, "length_m": 60, "name": "Туннель", "x": 730, "y": 0}],
        "demolition": {"parts": [{"polyline": [[990, 0], [1000, 10]], "cost": 5000, "buildings": 1.0}]}},
        "stations": [{"kind": "rail_station", "platform_m": 160, "terminals": 1, "x": 100, "y": 20,
                      "building": "Ж/д грузовая станция: 1×160 м", "endpoint": "A", "role": "погрузка"},
                     {"kind": "builtin_terminal", "x": 5, "y": 5, "builtin": True}],
        "depots": [{"what": "Ж/д депо", "x": 900, "y": 25, "count": 1},
                   {"what": "Ж/д депо", "x": 1900, "y": 25, "free": True}]}]}}


def overlay():
    return build_overlay(PLAN, DOSSIERS, None)


def test_lua_writer_roundtrip():
    data = {"a": 1, "b": -0.5, "c": 1e-7, "d": 123456789012, "s": 'x"y\\z\n\tü\x01', "list": [1, 2.5, -3],
            "nested": [{"end": True, "k-ey": None, "2": "two"}], "big": 1.5e17}
    lua = LuaRuntime()
    lua.execute(lua_file(data))
    d = lua.globals().data()
    assert d["a"] == 1 and d["b"] == -0.5 and d["c"] == 0 and d["d"] == 123456789012
    assert d["s"] == 'x"y\\z\n\tü\x01'
    assert list(d["list"].values()) == [1, 2.5, -3]
    assert d["nested"][1]["end"] is True and d["nested"][1]["2"] == "two" and d["nested"][1]["k-ey"] is None
    assert "e" not in to_lua(1.5e17) and "e" not in to_lua(1e-7)


def test_thin_and_subpolyline():
    pts = [(i * 5.0, 0.0 if i % 2 else 0.3) for i in range(101)]      # 500 m, 1-m wiggle
    t = thin(pts)
    assert t[0] == pts[0] and t[-1] == pts[-1] and len(t) <= 2 + 500 / 25
    assert all(math.dist(a, b) >= 12.5 for a, b in zip(t[:-1], t[1:]))
    corner = thin([(0, 0), (100, 0), (100, 100)])
    assert (100, 0) in corner
    sp = sub_polyline([(0, 0), (100, 0), (100, 100)], 50, 150)
    assert sp[0] == (50, 0) and sp[-1] == (100, 50) and (100, 0) in sp


def test_water_bridges_from_heightmap():
    """A river under the drawn route: the bridge span starts/ends exactly at the banks (+ abutments)."""
    import numpy as np
    from types import SimpleNamespace
    from tf3planner.model import Terrain
    from tf3planner.overlay import BRIDGE_PAD_M, _Ctx, build_project, water_runs
    h = np.full((40, 60), 10.0, dtype=np.float32)
    h[:, 20:24] = -5.0                                  # river: x = 640..736 (cells 20..23, 32 m)
    tr = Terrain(0.0, 0.0, 32.0, h, 0.0)
    runs = water_runs([(0, 300), (1500, 300)], tr)
    assert len(runs) == 1
    d0, d1 = runs[0]
    assert 600 - BRIDGE_PAD_M <= d0 <= 640 and 736 <= d1 <= 780 + BRIDGE_PAD_M, runs
    game = SimpleNamespace(terrain=tr, ways=[])
    step = {"id": "s9", "line": "L9", "action": "build_line", "kind": "road", "year": 1900,
            "segments": [{"type": "new", "polyline": [[0, 300], [1500, 300]], "way": "country_old_small"}]}
    dossier = {"mode": "road", "legs": [{"mode": "road", "route": {"polyline": [[0, 0], [1500, 0]], "structures": [
        {"type": "bridge", "x": 2000, "y": 0, "length_m": 300, "name": "Мост-эстакада"}]}}]}
    pr = build_project(step, dossier, _Ctx(game), 1)
    assert [(sp["t"], sp["d0"], sp["d1"]) for sp in pr["spans"]] == [("bridge", round(d0), round(d1))]
    assert pr["spans"][0]["name"] == "Мост-эстакада" and pr["spans"][0]["water"] is True
    xs = pr["spans"][0]["p"][0::2]
    assert abs(xs[0] - d0) < 1 and abs(xs[-1] - d1) < 1


def test_build_overlay():
    ov = overlay()
    assert [e["id"] for e in ov["index"]] == ["L1:s1", "L2:s2", "L1:s3"]
    assert [e["order"] for e in ov["index"]] == [1, 2, 3]
    p = ov["projects"]["L1:s1"]
    assert [(s["t"], s["k"]) for s in p["segments"]] == [("new", "rail"), ("existing", "road")]
    assert p["totals"]["new_m"] == 1000 and p["totals"]["existing_m"] == 300
    kinds = [it["k"] for it in p["items"]]
    assert kinds == ["station", "depot", "warehouse"]                 # builtin terminal / free depot skipped
    st = p["items"][0]
    assert st["len"] == 180 and st["match"] == ["/stations/rail/"] and abs(st["h"]) < 0.01
    xs, ys = st["poly"][0::2], st["poly"][1::2]
    assert abs(max(xs) - min(xs) - 180) < 0.2 and abs(max(ys) - min(ys) - 20) < 0.2
    assert p["items"][1]["exclude"] == ["maint"]
    assert [s["t"] for s in p["spans"]] == ["bridge", "tunnel"]
    br = list(zip(p["spans"][0]["p"][0::2], p["spans"][0]["p"][1::2]))
    assert br[0] == (400, 0) and br[-1] == (500, 0)
    assert p["demolition"][0]["cost"] == 5000
    # waypoints: route start/end junctions + bridge/tunnel ends (no 2-km filler points)
    kinds_wp = [w["kind"] for w in p["waypoints"]]
    assert kinds_wp.count("junction") == 2 and kinds_wp.count("bridge") == 2 and kinds_wp.count("tunnel") == 2
    assert any("примыкание к существующей дороге" in w["label"] for w in p["waypoints"])
    cl = p["checklist"]
    # the road network first, then the buildings joined to it
    assert [c["kind"] for c in cl] == ["segment", "span", "span", "info", "item", "item", "item", "vehicles", "line"]
    assert cl[4]["text"].startswith("Станция: Ж/д грузовая станция: 1×160 м у «A»") and cl[4]["ref"] == {"item": 1}
    assert cl[0]["text"].startswith("Построить новый ж/д путь 1,0 км") and cl[0]["ref"] == {"seg": 1}
    assert len(cl[0]["wp"]) == 6
    assert cl[1]["text"].startswith("Мост 100 м") and cl[1]["ref"] == {"seg": 1, "d0": 400, "d1": 500}
    assert "300 м" in cl[3]["text"]
    assert cl[7]["text"] == "Купить технику: 2× Borsig, 8× Вагон"
    assert cl[8]["text"] == "Создать линию: 1. A (загрузить) → 2. B"
    assert p["items"][0]["step"] == 5 and p["items"][1]["step"] == 6 and p["items"][1]["kind_ru"] == "депо"
    assert p["bbox"] == [0, -60, 1000, 300]
    assert ov["projects"]["L1:s3"]["segments"][0]["t"] == "upgrade"
    assert ov["projects"]["L2:s2"]["corridor"] == 20 and p["corridor"] == 24
    one = build_overlay(PLAN, DOSSIERS, None, project="L2")
    assert list(one["projects"]) == ["L2:s2"]
    assert [e["id"] for e in build_overlay(PLAN, DOSSIERS, None, year=1901)["index"]] == ["L1:s3"]


# --------------------------------------------------------------------------- fake engine + GUI

class UD:
    """Engine object stand-in: field access only, no pairs() (like TF3 userdata)."""

    def __init__(self, table, name="userdata"):
        self._d, self._name = dict(table.items()), name

    def __getattr__(self, k):
        if k.startswith("_"):
            raise AttributeError(k)
        return self._d.get(k)

    def __str__(self):
        return f"{self._name}: 0x0"


FAKE = r"""
local function ud(t, name) return make_ud(t, name) end
ZONES, MARKERS, ZONE_CALLS, SENT = {}, {}, 0, 0
GAME_DATE = { 1900, 1 }
CAMERA, CAMERA_CALLS, GUI_SAVE = nil, 0, nil
GAME_TIME = 1000
P = 77
COMPS, OWNED = {}, {}
local function v(x, y, z) return { x = x, y = y, z = z or 0 } end
function add_edge(e, x0, y0, x1, y1, track, owner)
  COMPS[e] = { BASE_EDGE = ud({ position0 = v(x0, y0), position1 = v(x1, y1), tangent0 = v(x1 - x0, y1 - y0),
    tangent1 = v(x1 - x0, y1 - y0), roadType = track and 0 or 1 }, "Engine.Component.BaseEdge"),
    PLAYER_OWNED = ud({ player = owner or P }) }
  table.insert(OWNED, e)
end
function add_con(e, file, x, y, owner)
  COMPS[e] = { CONSTRUCTION = ud({ fileName = file }), PLAYER_OWNED = ud({ player = owner or P }),
    BOUNDING_VOLUME = ud({ bbox = ud({ min = ud(v(x - 5, y - 5)), max = ud(v(x + 5, y + 5, 3)) }) }) }
  table.insert(OWNED, e)
end
function remove_entity(e)
  COMPS[e] = nil
  for i, x in ipairs(OWNED) do if x == e then table.remove(OWNED, i) break end end
end
OVERLAY = nil
USERDATA = {}
app = {
  loadUserdata = function(dir, name)
    assert(dir == "tf3_planner")
    if name == "plan_overlay" then return OVERLAY end
    return USERDATA[name]
  end,
  saveUserdata = function(dir, name, data) assert(dir == "tf3_planner"); USERDATA[name] = data end,
}
api = {
  type = {
    ComponentType = { BASE_EDGE = "BASE_EDGE", CONSTRUCTION = "CONSTRUCTION", PLAYER_OWNED = "PLAYER_OWNED",
      BOUNDING_VOLUME = "BOUNDING_VOLUME", NAME = "NAME", GAME_TIME = "GAME_TIME" },
    Vec2f = { new = function(x, y) return { x = x, y = y } end },
    Vec3f = { new = function(x, y, z) return { x = x, y = y, z = z } end },
    Vec4f = { new = function(r, g, b, a) return { r, g, b, a } end },
    enum = { RoadType = { TRACK = 0, STREET = 1 } },
  },
  engine = {
    getComponent = function(e, ct) local c = COMPS[e]; return c and c[ct] or nil end,
    getEntitiesWithComponent = function(ct)
      if REFUSE_LOOP then error("Cannot loop over this component type") end
      assert(ct == "PLAYER_OWNED", "unexpected loop over " .. tostring(ct))
      local r = {}; for i, e in ipairs(OWNED) do r[i] = e end; return r
    end,
    util = {
      getYear = function() return 1900 end, getPlayer = function() return P end,
      getCalendarDate = function() return { year = GAME_DATE[1], month = GAME_DATE[2], day = 1 } end,
      octree = { findEntitiesInCircle = function(c, r, ct)
        OCTREE_CALLS = (OCTREE_CALLS or 0) + 1
        local out = {}
        for _, e in ipairs(OWNED) do
          local cs = COMPS[e]
          local x, y
          if ct == "BASE_EDGE" and cs.BASE_EDGE then
            local a, b = cs.BASE_EDGE.position0, cs.BASE_EDGE.position1
            -- distance from the circle centre to the edge chord
            local dx, dy = b.x - a.x, b.y - a.y
            local t = math.max(0, math.min(1, ((c.x - a.x) * dx + (c.y - a.y) * dy) / (dx * dx + dy * dy)))
            x, y = a.x + t * dx, a.y + t * dy
          elseif ct == "CONSTRUCTION" and cs.CONSTRUCTION then
            local bb = cs.BOUNDING_VOLUME.bbox
            x, y = (bb.min.x + bb.max.x) / 2, (bb.min.y + bb.max.y) / 2
          end
          if x and (x - c.x) ^ 2 + (y - c.y) ^ 2 <= r * r then out[#out + 1] = e end
        end
        return out
      end },
    },
  },
  gui = {
    camera = { focusPosition = function(pos, dist) CAMERA = { pos.x, pos.y, dist }; CAMERA_CALLS = CAMERA_CALLS + 1 end },
    game = {
      getGuiSaveData = function(mod) if mod ~= "tf3_planner_1" then return GUI_SAVES[mod] end; return GUI_SAVE end,
      setGuiSaveData = function(mod, data) if mod ~= "tf3_planner_1" then GUI_SAVES[mod] = data; return end; GUI_SAVE = data end,
    },
    mission = {
    setZone = function(key, poly, draw, color, prohibit, anim)
      assert(type(key) == "string" and #poly >= 3 and draw == true and prohibit == false)
      ZONES[key] = { n = #poly, color = color }; ZONE_CALLS = ZONE_CALLS + 1
    end,
    setZoneCircle = function(key, c, r, draw, color, prohibit, anim)
      assert(type(key) == "string" and r > 0 and draw == true and prohibit == false)
      ZONES[key] = { circle = r, anim = anim, color = color }; ZONE_CALLS = ZONE_CALLS + 1
    end,
    removeZone = function(key) ZONES[key] = nil end,
    setMarkerAtPosition = function(key, pos, typ, scale) assert(pos.z and typ == "arrow"); MARKERS[key] = scale end,
    removeMarker = function(key) MARKERS[key] = nil end,
  } },
  cmd = { sendCommand = function() SENT = SENT + 1 end },
}

-- fake react / builtin for the window recipe
REACT = { mounts = {}, unmounts = {}, timers = {}, sets = 0 }
GUI_SAVES = {}
local R = {}
R.RegisterRecipe = function(name, fn) return function(params) return fn(params or {}) end end
R.RegisterWrapperRecipe = function(name, base, fn) return function(params) return fn(params or {}) end end
R.RegisterPluginRecipe = function(ext, name, fn) return fn end
R.useState = function(init)
  local s = { v = init }
  function s:old() return self.v end
  function s:set(x) self.v = x; REACT.sets = REACT.sets + 1 end
  return s
end
R.onMount = function(fn) table.insert(REACT.mounts, fn) end
R.onUnmount = function(fn) table.insert(REACT.unmounts, fn) end
R.onStepTimer = function(fn, sec) table.insert(REACT.timers, { fn = fn, sec = sec }) end
FAKE_REACT = R
FAKE_BUILTIN = setmetatable({ type = { Orientation = { Horizontal = 0, Vertical = 1 },
  ScrollBarPolicy = { AlwaysOff = 0, AsNeeded = 1 } } }, {
  __index = function(t, k) return function(params) return { builtin = k, params = params } end end })
WC = { windows = {} }
WC.addWindow = function(recipe, key, params) WC.windows[key] = recipe(params); WC.params = params end
WC.removeWindow = function(recipe, key) WC.windows[key] = nil end
"""


def make_lua():
    lua = LuaRuntime(unpack_returned_tuples=True, attribute_handlers=(lambda o, k: getattr(o, k, None), None))
    lua.globals().make_ud = UD
    lua.execute(FAKE)

    def ug_require(path):
        g = lua.globals()
        if path.startswith("tf3_planner_1::/"):
            return lua.execute((CONTENT / path.split("::/", 1)[1]).read_text(encoding="utf-8"))
        fakes = {"::/gui/main/react.lua": g.FAKE_REACT, "::/gui/main/builtin.lua": g.FAKE_BUILTIN,
                 "::/gui/main/main_mod_button_area.tl": lua.eval("{ MainModButtonAreaExtension = 'ext' }"),
                 "::/gui/main/game_react_globals.tl": lua.eval("{ getDefaultWindowApi = function() return WC end }")}
        if path in fakes:
            return fakes[path]
        raise RuntimeError("module not found: " + path)

    lua.globals().ug_require = ug_require
    lua.execute(lua_file(overlay()).replace("function data()", "function overlay_data()", 1))
    lua.execute("OVERLAY = overlay_data()")
    return lua


def blueprint(lua):
    return lua.execute((CONTENT / "tf3_planner" / "blueprint.lua").read_text(encoding="utf-8"))


def count(t):
    return len(list(t.keys())) if lua_type(t) == "table" else 0


def zone_keys(lua, part=""):
    return [k for k in lua.globals().ZONES.keys() if part in k]


def test_blueprint_render_and_cleanup():
    lua = make_lua()
    bp = blueprint(lua)
    bp.open()
    v = bp.view()
    assert v["id"] == "L1:s1" and v["count"] == 2 and v["year"] == 1900       # year filter: s1, s2
    assert v["progress"].startswith("Трасса построена на 0 %") and "станции 0/1" in v["progress"]
    assert "депо 0/1" in v["progress"] and "склады 0/1" in v["progress"]
    z = zone_keys(lua)
    assert z and all(k.startswith("tf3p:") for k in z)
    assert zone_keys(lua, ":new_rail") and zone_keys(lua, "tf3p:s:") and zone_keys(lua, "tf3p:d:")
    assert not zone_keys(lua, ":existing")                    # existing roads hidden by default
    # mission focus: step 1 is the track: its route points, no building arrows, no letter labels
    assert zone_keys(lua, "tf3p:w:L1:s1:") and not [k for k in lua.globals().MARKERS.keys() if ":i" in k]
    assert not zone_keys(lua, "tf3p:l:L1:s1:i")
    assert zone_keys(lua, ":dim")                             # the buildings stay as faint outlines
    cam = list(lua.globals().CAMERA.values())
    assert cam[0] == 500 and cam[1] == 120 and 1000 <= cam[2] <= 1300     # whole route framed
    bp.selectTask(5)                                          # the station: one arrow, no route points
    assert len([k for k in lua.globals().MARKERS.keys() if ":i" in k]) == 1 and not zone_keys(lua, "tf3p:w:")
    bp.toggle("showExisting")
    assert zone_keys(lua, ":existing")
    bp.toggle("showCorridor")
    assert zone_keys(lua, "tf3p:c:")
    bp.next(1)
    v = bp.view()
    assert v["id"] == "L2:s2"
    assert not zone_keys(lua, "L1:s1")                         # previous project removed
    bp.toggle("showYearAll")
    assert zone_keys(lua, "L1:s1") and zone_keys(lua, "L2:s2")
    bp.toggle("hidden")
    assert count(lua.globals().ZONES) == 0 and count(lua.globals().MARKERS) == 0
    bp.toggle("hidden")
    assert count(lua.globals().ZONES) > 0
    bp.close()
    assert count(lua.globals().ZONES) == 0 and count(lua.globals().MARKERS) == 0
    assert lua.globals().SENT == 0


def test_blueprint_ghost_progress():
    lua = make_lua()
    bp = blueprint(lua)
    bp.open()
    assert bp.tick() is False                                   # nothing changed
    # the player builds the first half of the rail line (two edges, slightly offset), plus a
    # road edge and a non-player track on the second half that must NOT count
    lua.execute("""
      add_edge(1001, 0, 3, 260, 3, true)
      add_edge(1002, 260, 3, 500, -2, true)
      add_edge(1003, 500, 0, 1000, 0, false)
      add_edge(1004, 500, 1, 1000, 1, true, 999)
      add_edge(1005, 520, 40, 1000, 40, true)
    """)
    assert bp.tick() is True
    v = bp.view()
    assert abs(v["pct"] - 50) <= 4 and v["mode"] == "grid", v["progress"]
    assert zone_keys(lua, ":done") and zone_keys(lua, ":new_rail")
    # perpendicular track crossing the line does not count
    lua.execute("add_edge(1006, 750, -200, 750, 200, true)")
    bp.tick()
    assert abs(bp.view()["pct"] - 50) <= 4
    # station (right type, inside footprint), wrong type near the depot, then the depot
    lua.execute("""
      add_con(2001, '::/stations/rail/modular_station/modular_station.con', 110, 25)
      add_con(2002, '::/depots/rail/rail_maint_station.con', 900, 25)
    """)
    bp.tick()
    v = bp.view()
    assert "станции 1/1" in v["progress"] and "депо 0/1" in v["progress"]
    assert not v["done"]
    lua.execute("""
      add_edge(1007, 500, -3, 1000, -3, true)
      add_con(2003, '::/depots/rail/rail_depot.con', 905, 30)
      add_con(2004, '::/warehouses/warehouse.con', 500, -60)
    """)
    bp.tick()
    v = bp.view()
    assert v["pct"] == 100 and v["done"] is True, v["progress"]
    assert not lua.globals().MARKERS[next(iter([k for k in lua.globals().MARKERS.keys() if ":i" in k]), "none")]
    # "показывать построенное" off -> built parts vanish completely
    bp.toggle("showBuilt")
    assert not zone_keys(lua, ":done") and not zone_keys(lua, "new_rail")
    # demolished again -> the ghost comes back
    lua.execute("remove_entity(1007)")
    bp.tick()
    assert abs(bp.view()["pct"] - 50) <= 4 and zone_keys(lua, "new_rail")
    bp.close()
    assert count(lua.globals().ZONES) == 0 and lua.globals().SENT == 0


def test_blueprint_checklist_focus_and_memory():
    lua = make_lua()
    bp = blueprint(lua)
    bp.open()
    v = bp.view()
    cl = [v["checklist"][k] for k in sorted(v["checklist"])]
    assert [c["kind"] for c in cl][:3] == ["segment", "span", "span"]
    assert cl[0]["done"] is False and cl[4]["done"] is False and cl[7]["done"] is False  # vehicles: no line yet
    assert cl[4]["canShow"] and not cl[7]["canShow"]
    assert v["legend"][1].startswith("оранжевая")
    # "Показать" step 2 (bridge): camera there + a pulsing circle that expires
    calls = lua.globals().CAMERA_CALLS
    assert bp.show(2) is True
    assert lua.globals().CAMERA_CALLS == calls + 1 and list(lua.globals().CAMERA.values())[0] == 450
    hl = [k for k in lua.globals().ZONES.keys() if k.startswith("tf3p:hl:")]
    assert hl and lua.globals().ZONES[hl[0]]["anim"] is True and bp.view()["highlight"] == 2
    lua.execute("ORIG_TIME = os.time; os.time = function() return ORIG_TIME() + 100 end")
    assert bp.tick() is True
    assert not [k for k in lua.globals().ZONES.keys() if k.startswith("tf3p:hl:")]
    lua.execute("os.time = ORIG_TIME")
    # build the bridge part only -> its step done, the whole segment not yet
    lua.execute("add_edge(1001, 380, 0, 520, 0, true)")
    bp.tick()
    cl = [bp.view()["checklist"][k] for k in sorted(bp.view()["checklist"])]
    assert cl[1]["done"] is True and cl[0]["done"] is False and 0 < cl[0]["pct"] < 30
    # once something is built the camera goes to the first unbuilt step (the track, step 1)
    bp.next(1)
    bp.next(-1)
    assert list(lua.globals().CAMERA.values())[0] == 500
    # the selection is remembered (GUI save data) and restored after a reload of the game
    bp.next(1)
    assert lua.globals().GUI_SAVE["project"] == "L2:s2"
    bp.close()
    bp2 = blueprint(lua)        # fresh module state, as after loading the savegame
    bp2._reset()
    bp2.open()
    assert bp2.view()["id"] == "L2:s2"
    bp2.close()
    # no GUI save data -> the userdata file; nothing at all -> first project at the game date
    lua.execute("GUI_SAVE = nil")
    bp3 = blueprint(lua)
    bp3._reset()
    bp3.open()
    assert bp3.view()["id"] == "L2:s2"
    bp3.close()
    lua.execute("GUI_SAVE = nil; USERDATA = {}; GAME_DATE = { 1900, 2 }")
    bp4 = blueprint(lua)
    bp4._reset()
    bp4.open()
    assert bp4.view()["id"] == "L2:s2"      # s1 (January) is already past in February 1900
    bp4.close()
    assert lua.globals().SENT == 0


def test_status_poll_reload_and_export():
    lua = make_lua()
    # export.lua stub: the window's «Экспорт и пересчёт» must call the same export
    lua.execute("""
      EXPORTS = 0
      package.loaded = package.loaded or {}
      local orig = ug_require
      ug_require = function(path)
        if path == "tf3_planner_1::/tf3_planner/export.lua" then
          return { run = function() EXPORTS = EXPORTS + 1; return true, "v8 ok" end }
        end
        return orig(path)
      end
    """)
    bp = blueprint(lua)
    bp.open()                                   # no status.lua yet: nothing shown, no error
    v = bp.view()
    assert v["recalc"] is None and v["id"] == "L1:s1"
    lua.execute("""USERDATA.status = { state = "running", kind = "plan", step = 3, steps = 4, label = "Расчёт плана",
      progress = 0.4, year = 1925, ["end"] = 2030, updated = 100, overlay_updated = 5 }""")
    assert bp.tick() is True
    v = bp.view()
    assert v["recalc"] == "Пересчёт плана: шаг 3/4 «Расчёт плана», 1925/2030 (40 %)"
    assert v["recalcBar"] == "[########------------]"
    assert bp.tick() is False                   # unchanged status: no redraw
    # a new overlay file: reloaded automatically, the same project kept (ids changed, title matched)
    lua.execute("""
      local o = OVERLAY
      local p = o.projects["L1:s1"]
      o.projects["L7:s70"] = p; o.projects["L1:s1"] = nil
      p.id, p.line, p.step = "L7:s70", "L7", "s70"
      o.index[1].id, o.index[1].line, o.index[1].step = "L7:s70", "L7", "s70"
      USERDATA.status = { state = "done", text = "до 2030, 297 проектов", updated = 120, overlay_updated = 6 }
    """)
    assert bp.tick() is True
    v = bp.view()
    assert v["id"] == "L7:s70" and v["notice"] == "Чертёж обновлён"
    assert v["recalc"] == "План обновлён: до 2030, 297 проектов"
    assert lua.globals().ZONES and any("L7:s70" in k for k in lua.globals().ZONES.keys())
    # «Экспорт и пересчёт»: export runs, the window waits for the control centre
    assert bp.exportAndRecalc()[0] is True
    assert lua.globals().EXPORTS == 1 and bp.view()["recalc"].startswith("ожидаю пересчёт")
    lua.execute("USERDATA.status = { state = 'running', step = 1, steps = 4, label = 'Загрузка экспорта', progress = -1, updated = 130, overlay_updated = 6 }")
    bp.tick()
    v = bp.view()
    assert v["recalc"] == "Пересчёт плана: шаг 1/4 «Загрузка экспорта»" and v["recalcBar"] is None
    lua.execute("USERDATA.status = { state = 'failed', text = 'нет export.json', updated = 140 }")
    bp.tick()
    assert bp.view()["recalc"] == "Ошибка пересчёта: нет export.json"
    lua.execute("USERDATA.status = 'garbage'")
    bp.tick()
    assert bp.view()["recalc"] is None
    bp.close()
    assert lua.globals().SENT == 0


def test_digits():
    lua = make_lua()
    bp = blueprint(lua)
    polys = bp.digitPolys(18, 0, 0, 20)
    keys = [polys[k]["key"] for k in sorted(polys)]
    assert keys == ["1b", "1c"] + ["2" + c for c in "abcdefg"]
    p = polys[1]["poly"]
    assert len(list(p.keys())) == 4


def test_blueprint_octree_fallback():
    lua = make_lua()
    lua.execute("REFUSE_LOOP = true")
    bp = blueprint(lua)
    bp.open()
    lua.execute("add_edge(1001, 0, 0, 1000, 0, true); add_con(2001, '::/stations/rail/x.con', 100, 20)")
    bp.tick()
    v = bp.view()
    assert v["mode"] == "octree" and v["pct"] == 100 and "станции 1/1" in v["progress"]
    assert lua.globals().OCTREE_CALLS > 0
    bp.close()


def test_blueprint_missing_file_and_reload():
    lua = make_lua()
    lua.execute("SAVED_OVERLAY = OVERLAY; OVERLAY = nil")
    bp = blueprint(lua)
    bp.open()
    v = bp.view()
    assert "plan_overlay" in v["status"] and v["id"] is None
    assert count(lua.globals().ZONES) == 0
    lua.execute("OVERLAY = SAVED_OVERLAY")
    bp.reload()
    v = bp.view()
    assert v["id"] == "L1:s1" and v["status"] is None and count(lua.globals().ZONES) > 0
    bp.setSearch("Дорога")
    assert bp.view()["id"] == "L2:s2"
    bp.setSearch("")
    bp.yearStep(1)
    v = bp.view()
    assert v["year"] == 1901 and v["id"] == "L1:s3" and zone_keys(lua, ":upgrade")
    bp.close()


def test_window_recipe():
    lua = make_lua()
    script = (CONTENT / "gui" / "tf3_planner" / "tf3_planner_button.script.lua").read_text(encoding="utf-8")
    lua.execute(script)
    mods = lua.globals().data()
    g = lua.globals()
    # the plugin: export button + blueprint window button + on/off (no step bar while off)
    root = mods["Tf3PlannerButton"]()
    assert root["builtin"] == "BoxLayout"
    buttons = list(root["params"]["children"].values())
    assert len(buttons) == 3 and buttons[1]["params"]["content"]["builtin"] == "BoxLayout"
    bar_timer = g.REACT.timers[1]
    buttons[1]["params"]["onClick"]()
    win = g.WC.windows["tf3p_blueprint"]
    assert win["builtin"] == "Window" and win["params"]["closable"] is True
    content = win["params"]["content"]
    assert content["builtin"] == "BoxLayout"
    kids = list(content["params"]["children"].values())
    assert all(lua_type(k) == "table" and k["builtin"] for k in kids)
    # lifecycle: window mount turns the blueprint on, the bar timer ticks every 3 s
    assert bar_timer["sec"] == 3.0
    for fn in g.REACT.mounts.values():
        fn()
    assert count(g.ZONES) > 0
    lua.execute("add_edge(1001, 0, 0, 1000, 0, true)")
    for t in g.REACT.timers.values():
        t["fn"]()
    assert g.REACT.sets > 0
    # re-render shows the progress line
    texts = []

    def walk(node):
        if lua_type(node) != "table":
            return
        if node["builtin"] == "TextView":
            texts.append(node["params"]["text"])
        p = node["params"]
        if lua_type(p) == "table":
            for key in ("children", "content", "layout"):
                c = p[key]
                if lua_type(c) == "table":
                    if c["builtin"]:
                        walk(c)
                    else:
                        for ch in c.values():
                            walk(ch)

    walk(mods["Tf3pBlueprintContent"]())
    # mission panel: header with progress, task list, the current task with its actions, settings collapsed
    assert any(t.startswith("Задание 1 из 2") for t in texts), texts
    assert any(t.startswith("Выполнено ") for t in texts), texts
    assert any("5. Станция" in t for t in texts) and "Сейчас:" in texts, texts
    assert "Показать на карте" in texts and "Открыть в меню" in texts, texts
    assert "Настройки и пересчёт" in texts and "Легенда цветов" not in texts
    # closing the window keeps the blueprint; the bar shows the current step with < > and «Показать»
    g.WC.params["onClose"]()
    assert "tf3p_blueprint" not in list(g.WC.windows.keys())
    assert count(g.ZONES) > 0
    bar = list(mods["Tf3PlannerButton"]()["params"]["children"].values())
    labels = []
    for b in bar:
        walk(b)
    labels = [t for t in texts if "шаг" in t]
    assert any(t.startswith("L1:s1") and "шаг 5/" in t and "Станция" in t for t in labels), labels   # track built -> station
    step_btn = next(b for b in bar if b["builtin"] == "Button"
                    and "шаг" in b["params"]["content"]["params"]["children"][1]["params"]["text"])
    calls = g.CAMERA_CALLS
    step_btn["params"]["onClick"]()
    assert g.CAMERA_CALLS == calls + 1 and [k for k in g.ZONES.keys() if k.startswith("tf3p:hl:")]
    # the on/off button turns everything off (highlight included)
    onoff = bar[2]
    assert onoff["params"]["content"]["params"]["children"][1]["params"]["text"] == "[x] на карте"
    onoff["params"]["onClick"]()
    assert count(g.ZONES) == 0 and count(g.MARKERS) == 0
    bar_timer["fn"]()
    assert count(g.ZONES) == 0
    assert g.SENT == 0


def test_all_lua_compiles():
    lua = LuaRuntime()
    chk = lua.eval("function(s, n) local f, e = load(s, n) return e end")
    for f in CONTENT.rglob("*.lua"):
        assert chk(f.read_text(encoding="utf-8"), str(f)) is None, f


def test_real_overlay():
    plan, dossiers = DATA / "plan.json", DATA / "dossiers.json"
    if not plan.exists():
        pytest.skip("no data/plan.json")
    ov = build_overlay(json.loads(plan.read_text(encoding="utf-8")),
                       json.loads(dossiers.read_text(encoding="utf-8")) if dossiers.exists() else {}, None)
    assert ov["index"] and len(ov["projects"]) == len(ov["index"])
    text = lua_file(ov)
    assert len(text) < 8_000_000
    lua = LuaRuntime()
    lua.execute(text)
    d = lua.globals().data()
    assert len(d["index"]) == len(ov["index"])
    # every polyline thinned to <= ~2 points per 50 m
    for pr in ov["projects"].values():
        for s in pr["segments"]:
            pts = list(zip(s["p"][0::2], s["p"][1::2]))
            L = sum(math.dist(a, b) for a, b in zip(pts[:-1], pts[1:]))
            assert len(pts) <= 2 + 2 * L / 50 + 1


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except pytest.skip.Exception as ex:
                print(f"SKIP {name}: {ex}")
            except Exception as ex:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(ex).__name__}: {ex}")
    sys.exit(1 if failed else 0)


def test_label_glyphs():
    lua = make_lua()
    bp = blueprint(lua)
    chars = bp.chars("СК9")
    assert [chars[k] for k in sorted(chars)] == ["С", "К", "9"]
    polys = bp.labelPolys("Д", 9, 0, 0, 28)
    keys = [polys[k]["key"] for k in sorted(polys)]
    assert any(k.startswith("g1:") for k in keys) and any(k.startswith("1") for k in keys)
    # every bitmap run sits inside the label box
    for k in polys:
        for q in polys[k]["poly"].values():
            assert -1 <= q[2] <= 29


def test_beside_road_keeps_buildings_off_the_route():
    road = [[(0.0, 0.0), (200.0, 0.0)]]
    # a truck station planned right on the road end: moved beside it, front 4 m from the centre line
    it = {"k": "station", "x": 200.0, "y": 0.0, "h": 0.0, "len": 40.0, "wid": 25.0, "match": ["/stations/street/"]}
    ov._place(ov._Ctx(None), it, road, "road", toward=(200.0, 80.0), placed=[])
    assert it["y"] == 4.0 + 12.5 and it["access"][1] == 4.0 and it["access"][3] == 0.0
    # a depot next to it does not overlap the station
    placed = [(it["x"], it["y"], it["h"], it["len"], it["wid"])]
    dp = {"k": "depot", "x": 200.0, "y": 0.0, "h": 0.0, "len": 32.0, "wid": 26.0, "match": ["/depots/road/"]}
    ov._place(ov._Ctx(None), dp, road, "road", toward=(200.0, 80.0), placed=placed)
    assert not ov._overlaps(dp["x"], dp["y"], dp["h"], dp["len"], dp["wid"], placed[:1])
    # already off the road: kept in place, access stub drawn to the road
    far = {"k": "depot", "x": 100.0, "y": 60.0, "h": 0.0, "len": 32.0, "wid": 26.0, "match": ["/depots/road/"]}
    ov._place(ov._Ctx(None), far, road, "road")
    assert (far["x"], far["y"]) == (100.0, 60.0) and far["access"][3] == 0.0


def test_labels_face_the_camera():
    lua = make_lua()
    bp = blueprint(lua)
    bp.LABELS = True
    bp.open()
    assert zone_keys(lua, "tf3p:l:L1:s1:i1:o2:")              # no camera API: north up
    # the camera turns to look south: labels are redrawn rotated by 180 degrees
    lua.execute("""
      api.gui.camera.getEye = function() return { x = 0, y = 500, z = 300 } end
      api.gui.camera.getCenter = function() return { x = 0, y = 0, z = 0 } end
    """)
    assert bp.tick() is True
    assert zone_keys(lua, "tf3p:l:L1:s1:i1:o6:") and not zone_keys(lua, "tf3p:l:L1:s1:i1:o2:")
    assert bp.tick() is False                                  # same direction: nothing redrawn
    bp.close()


def test_mission_highlights_and_build_menu():
    lua = make_lua()
    lua.execute("""
      HL = {}; EV = nil
      api.gui.byId = { setHighlighted = function(id, on) HL[id] = on end }
      api.gui.HighlightStyle = { Circle = 1 }
      api.gui.fireReactEvent = function(name, p) EV = { name = name, res = p.resName } end
      api.res = api.res or {}
      api.res.constructionRep = { getAll = function() return { [7] = "station/rail/modular_station/modular_station.con",
        [8] = "depots/rail/rail_depot.con" } end }
    """)
    bp = blueprint(lua)
    # game API calls go through the deferrer (react.enqueueJoin in the game): nothing before the join
    lua.execute("JOINS = {}")
    bp.setDeferrer(lua.eval("function(fn) table.insert(JOINS, fn) end"))
    bp.open()
    bp.selectTask(5)                                                   # the station (after the track)
    m = bp.mission()
    assert m["task"]["n"] == 5 and m["task"]["menu"] == "rail" and m["task"]["canOpen"]
    assert not lua.globals().HL["menu.construction.rail"]
    lua.execute("for _, fn in ipairs(JOINS) do fn() end; JOINS = {}")
    assert lua.globals().HL["menu.construction.rail"] is True          # the rail build menu button glows
    bp.setDeferrer(None)
    assert bp.openMenu() is True
    ev = lua.globals().EV
    assert ev["name"] == "constructionMenuSelectTabForConstruction" and ev["res"].endswith("modular_station.con")
    # a step the game cannot check: confirm by hand, the bar moves on
    tasks = [m["tasks"][k] for k in sorted(m["tasks"])]
    manual = next((t for t in tasks if t["manual"]), None)
    if manual is not None:
        bp.selectTask(manual["n"])
        bp.toggleManual(manual["n"])
        assert bp.stepDone(bp.current()["id"], {"n": manual["n"]}) is True
    bp.close()
    assert lua.globals().HL["menu.construction.rail"] is None           # highlight removed when off


def test_loan_card_named_by_amount():
    lua = make_lua()
    lua.execute("""
      LOANSTATE = { availableLoans = {
        { amount = 13000000, duration = 10227000, percentage = 0.1 },
        { amount = 10000000, duration = 11688000, percentage = 0.05 } } }
      api.type = api.type or {}; api.type.ComponentType = api.type.ComponentType or {}
      api.type.ComponentType.GAME_SCRIPT = 77
      api.engine.system = api.engine.system or {}
      api.engine.system.gameScriptSystem = { getEntityForGameScript = function() return 5 end }
      local orig = api.engine.getComponent
      api.engine.getComponent = function(e, ct) if ct == 77 then return { state = LOANSTATE } end return orig(e, ct) end
    """)
    bp = blueprint(lua)
    text, amount = bp.loanCard(10000000)
    assert text == "Нажмите «Получить» на карточке $10 000 000 (96 мес., 5 %)" and amount == 10000000
    text, amount = bp.loanCard(12000000)
    assert "ближайшая" in text and "$13 000 000" in text and amount == 13000000
    assert bp.clean("A → B ↺") == "A → B (кольцо)"


def test_loan_card_wrapper_adds_id():
    lua = make_lua()
    lua.execute("""
      local function reg(name)          -- like react.lua: the recipe function holds `name` as an upvalue
        return function(p) local _ = name; return { builtin = "Card" } end
      end
      _react = { recipeReplace = {}, originalRecipeFn = { [41] = reg("LoanBoard"), [42] = reg("DefaultLoanCard") } }
    """)
    lua.execute((CONTENT / "gui" / "tf3_planner" / "tf3_planner_button.script.lua").read_text(encoding="utf-8"))
    lua.globals().data()
    rep = lua.globals()._react.recipeReplace[42]
    offer = rep(lua.eval("{ loan = { amount = 10000000 }, isObtained = false }"))
    assert offer["builtin"] == "Component" and offer["params"]["meta"]["id"] == "tf3p.loan.10000000"
    assert rep(lua.eval("{ loan = { amount = 5 }, isObtained = true }"))["builtin"] == "Card"   # obtained: as is


def test_bar_moves_on_when_steps_complete_quickly():
    lua = make_lua()
    lua.execute("""
      HL = {}
      api.gui.byId = { setHighlighted = function(id, on) if on == nil then HL[id] = nil else HL[id] = on end end }
      api.gui.HighlightStyle = { Circle = 1, Square = 2 }
    """)
    bp = blueprint(lua)
    bp.open()
    # two hand-confirmable steps done between two ticks: the bar must not stay on a done step
    cl = bp.checklist(bp.current()["id"])
    manual = [cl[k]["n"] for k in sorted(cl) if cl[k]["manual"]]
    first = bp.hud()["n"]
    st = bp._state()
    st.manual = lua.eval("{}")
    st.manual[bp.current()["id"] + "#" + str(int(first))] = True
    bp.tick()
    h = bp.hud()
    assert h["n"] != first and h["done"] is not True, (first, h["n"])
    bp.close()


def test_menu_targets_pick_the_cargo_template_of_this_year():
    lua = make_lua()
    lua.execute("""
      local function vec(t) return { size = function(self) return #t end, at = function(self, i) return t[i] end } end
      local function tpl(from, to, filter)
        return { availability = { yearFrom = from, yearTo = to },
                 menuCategory = { categories = { { category = "road_buildings", filterCategories = { filter } } } } }
      end
      api.res = api.res or {}
      api.res.constructionRep = {
        getAll = function() return { [5] = "stations/street/modular_street_station/modular_terminal.con" } end,
        find = function(n) return 5 end,
        get = function(id) return { constructionTemplates_native = vec({
          tpl(1850, 1920, "passenger"), tpl(1920, 0, "passenger"), tpl(1850, 1920, "cargo"), tpl(1920, 0, "cargo") }) } end,
      }
    """)
    bp = blueprint(lua)
    mt = bp.menuTargets("modular_terminal.con", "road", "cargo")
    tags = list(mt["tags"].keys())
    # 1900: the cargo template of era A, as the card's outer tag "<tab id>.<resName>@<template>"
    assert tags == ["menu.construction.road.road_buildings.stations/street/modular_street_station/modular_terminal.con@2"], tags
    tabs = sorted(mt["tabs"].keys())
    assert tabs == ["menu.construction.road.road_buildings.indicator"]          # the header, not the whole list


def test_build_street_terminal_matches_the_players_station():
    """«Построить»: params and transf the mod sends equal those of the terminal the player built
    by hand (export v9 capture, 2026-10-05): modules 20010001/20210001 cargo_platform +
    20015503 entrance_exit, platforms 1, specialization 1, tramTrack 1; local Y = n."""
    lua = make_lua()
    lua.execute("""
      SENT_CMDS = {}
      local function V(...) return { ... } end
      api.type = api.type or {}
      api.type.Vec2f = { new = function(x, y) return { x = x, y = y } end }
      api.type.Vec4f = { new = function(x, y, z, w) return { x, y, z, w } end }
      api.type.Mat4f = { new = function(a, b, c, d) return { cols = { a, b, c, d } } end }
      api.type.SimpleProposal = { new = function() return { constructionsToAdd = {} } end,
        ConstructionEntity = { new = function() return {} end } }
      api.engine.util.terrain = { getHeightAt = function(p) return 2.5 end }
      api.engine.util.proposal = { makeProposalData = function(sp, ctx)
        return { costs = 101648, errorState = { critical = false, messages = {} } } end }
      api.type.Context = { new = function() return {} end }
      api.engine.util.getPlayer = function() return 4242 end
      api.cmd = { makeWorldBuildProposalCmd = function(sp, ctx, ignore, player) return { sp = sp, ctx = ctx, ignore = ignore, player = player } end,
        sendCommand = function(cmd, cb) table.insert(SENT_CMDS, cmd); if cb then cb({}, true) end end }
      api.res = api.res or {}
      api.res.constructionRep = { getAll = function() return { [9] = "::/stations/street/modular_street_station/modular_terminal.con" } end }
      local it = OVERLAY.projects["L1:s1"].items[1]
      it.build = { kind = "street_terminal", res = "modular_terminal.con", platforms = 1, specialization = 1,
                   tramTrack = 1, cargo = true, t = { -12932.56, -3655.63 }, n = { 0.927184, -0.374607 } }
    """)
    bp = blueprint(lua)
    bp.open()
    n = next(k for k in range(1, 30) if bp.canBuild(k))
    bp.buildCheck(n)
    chk = bp._state().buildCheck
    assert chk["state"] == "ok" and chk["cost"] == 101648, chk["text"]
    bp.buildConfirm()
    cmd = lua.globals().SENT_CMDS[1]
    assert cmd["ignore"] is False and cmd["player"] is True            # like the build menu
    assert cmd["ctx"]["player"] == 4242                                 # the player pays (context.player)
    ce = cmd["sp"]["constructionsToAdd"][1]
    assert ce["fileName"] == "::/stations/street/modular_street_station/modular_terminal.con"
    p = ce["params"]
    mods = {int(k): (v["name"].rsplit("/", 1)[1], v["variant"]) for k, v in p["modules"].items()}
    assert mods == {20010001: ("cargo_platform.module", 0), 20210001: ("cargo_platform.module", 0),
                    20015503: ("entrance_exit.module", 0)}
    assert (p["platforms"], p["specialization"], p["tramTrack"], p["seed"]) == (1, 1, 1, 0)
    cols = [list(ce["transf"]["cols"][i].values()) for i in (1, 2, 4)]
    assert cols[0][:2] == [-0.374607, -0.927184]       # local X, as in the player's station
    assert cols[1][:2] == [0.927184, -0.374607]        # local Y = n (away from the road)
    assert cols[2][:3] == [-12932.56, -3655.63, 2.5]
    assert bp._state().buildCheck["state"] == "done"


def test_build_demolishes_town_houses_in_the_way():
    """A dry run that collides only with town houses: the proposal is re-checked with those houses
    in constructionsToRemove (paid demolition), the panel says how many; anything else blocks."""
    lua = make_lua()
    lua.execute("""
      SENT_CMDS = {}
      api.type = api.type or {}
      api.type.Vec2f = { new = function(x, y) return { x = x, y = y } end }
      api.type.Vec4f = { new = function(x, y, z, w) return { x, y, z, w } end }
      api.type.Mat4f = { new = function(a, b, c, d) return { cols = { a, b, c, d } } end }
      api.type.Context = { new = function() return {} end }
      api.type.SimpleProposal = { new = function() return { constructionsToAdd = {} } end,
        ConstructionEntity = { new = function() return {} end } }
      api.type.ComponentType.TOWN_BUILDING = 901
      api.engine.util.terrain = { getHeightAt = function(p) return 1 end }
      local orig = api.engine.getComponent
      api.engine.getComponent = function(e, ct)
        if ct == 901 and e == 7001 then return { construction = 7100 } end
        return orig(e, ct)
      end
      api.engine.util.proposal = { makeProposalData = function(sp, ctx)
        local removes = sp.constructionsToRemove or {}
        if #removes == 0 then
          return { costs = 100000, errorState = { critical = false, messages = {} },
                   collisionInfo = { collisionEntities = { { entity = 7001 } }, buildingEntities = {} } }
        end
        return { costs = 302000, errorState = { critical = false, messages = {} },
                 collisionInfo = { collisionEntities = {}, buildingEntities = {} } }
      end }
      api.cmd = { makeWorldBuildProposalCmd = function(sp, c, i, p) return { sp = sp } end,
        sendCommand = function(cmd, cb) table.insert(SENT_CMDS, cmd); if cb then cb({}, true) end end }
      api.res = api.res or {}
      api.res.constructionRep = { getAll = function() return { [9] = "::/stations/street/modular_street_station/modular_terminal.con" } end }
      OVERLAY.projects["L1:s1"].items[1].build = { kind = "street_terminal", res = "modular_terminal.con", platforms = 1,
        specialization = 1, tramTrack = 1, cargo = true, t = { 0, 0 }, n = { 0, 1 } }
    """)
    bp = blueprint(lua)
    bp.open()
    n = next(k for k in range(1, 30) if bp.canBuild(k))
    bp.buildCheck(n)
    chk = bp._state().buildCheck
    assert chk["state"] == "ok" and chk["cost"] == 302000 and "под снос 1 дом" in chk["text"], chk["text"]
    bp.buildConfirm()
    sp = lua.globals().SENT_CMDS[1]["sp"]
    assert list(sp["constructionsToRemove"].values()) == [7100]


def test_terminal_candidates_stay_on_the_route():
    """Candidates move along the route polyline, never past its end on a straight tangent: with the
    only place blocked near the end of an L-shaped route, the road point must still lie on the route."""
    route = [[(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]]
    it = {"k": "station", "x": 100.0, "y": 95.0, "h": 0.0, "len": 30.0, "wid": 40.0, "platforms": 1,
          "match": ["/stations/street/"]}
    blocked = [(100.0 + 34.0 + 5.0, 100.0 - 4.0, 0.0, 200.0, 200.0)]       # whole right side near the end
    ok = ov.place_street_terminal(ov._Ctx(None), it, route, toward=None, placed=list(blocked))
    assert ok
    ax, ay = it["access"][2], it["access"][3]
    assert ov.project_on(route[0], ax, ay)[1] < 1e-6, (ax, ay)


def test_road_segment_proposal():
    """«Построить» for a new road: edges along the line (≤ 80 m), the bridge part as BRIDGE edges of the
    planned bridge type, the start joined to an existing node nearby, and the road passing THROUGH the
    entrance end node of a station already built on it (no extra link edge)."""
    lua = make_lua()
    lua.execute("""
      api.type = api.type or {}
      api.type.Vec2f = { new = function(x, y) return { x = x, y = y } end }
      api.type.Vec3f = { new = function(x, y, z) return { x = x, y = y, z = z } end }
      api.type.enum = { BaseEdgeType = { NORMAL = 0, BRIDGE = 1, TUNNEL = 2 }, RoadType = { STREET = 1, TRACK = 0 } }
      api.type.SimpleProposal = { new = function() return { constructionsToAdd = {}, streetProposal = {} } end }
      api.type.NodeAndEntity = { new = function() return { comp = {} } end }
      api.type.SegmentAndEntity = { new = function() return { comp = {} } end }
      api.engine.util.terrain = { getHeightAt = function(p) return 10 end }
      -- existing edges; their end nodes are what the road snaps to (nodes themselves are not in the octree)
      EDGES = {
        [601] = { node0 = 501, node1 = 599, position0 = { x = 2, y = 1, z = 10 }, position1 = { x = -50, y = 1, z = 10 } },
        [602] = { node0 = 598, node1 = 502, position0 = { x = 500, y = -31, z = 10 }, position1 = { x = 500, y = -1, z = 10 } },
      }
      api.engine.util.octree = api.engine.util.octree or {}
      api.engine.util.octree.findEntitiesInCircle = function(c, r, ct)
        local out = {}
        if ct == "BASE_EDGE" then
          for e, be in pairs(EDGES) do
            for _, p in ipairs({ be.position0, be.position1 }) do
              if (p.x - c.x) ^ 2 + (p.y - c.y) ^ 2 <= r * r then out[#out + 1] = e break end
            end
          end
        end
        return out
      end
      local orig = api.engine.getComponent
      api.engine.getComponent = function(e, ct)
        if ct == "BASE_EDGE" and EDGES[e] then return EDGES[e] end
        return orig(e, ct)
      end
      api.res = api.res or {}
      api.res.streetTemplateRep = { getAll = function() return { [3] = "::/infrastructure/street/country/country_old_small.street_template" } end,
        find = function(n) return 3 end, get = function(i) return { laneConfigs = { "L" }, streetStyle = "style" } end }
      -- 7: rail-only trestle (as in the game), 8: the road trestle
      api.res.bridgeTypeRep = {
        getAll = function() return { [7] = "::/infrastructure/bridge/trestle.bridge", [8] = "::/infrastructure/bridge/trestle_s.bridge" } end,
        get = function(id) return id == 7 and { carriers = { 1 }, speedLimit = 16.7, availability = { yearFrom = 0, yearTo = 1970 } }
                                     or { carriers = { 0 }, speedLimit = 16.7, availability = { yearFrom = 0, yearTo = 1970 } } end }
      PR = { segments = { { t = "new", k = "road", way = "country_old_small", p = { 0, 0, 1000, 0 } } },
             spans = { { t = "bridge", seg = 1, d0 = 300, d1 = 420, key = "trestle.bridge" } },
             items = { { k = "station", access = { 500, -15, 500, 0 },
                         build = { kind = "street_terminal", t = { 500, -64 }, n = { 0, -1 }, gap = 4 } } } }
    """)
    bp = blueprint(lua)
    sp, why, info = bp.segmentProposal(lua.globals().PR, 1)
    assert sp is not None, why
    edges = [sp["streetProposal"]["edgesToAdd"][k] for k in sorted(sp["streetProposal"]["edgesToAdd"])]
    nodes = [sp["streetProposal"]["nodesToAdd"][k] for k in sorted(sp["streetProposal"]["nodesToAdd"])]
    main = edges
    assert all(abs(e["comp"]["position1"]["x"] - e["comp"]["position0"]["x"]) <= 80.0 + 1e-6 for e in main)
    assert main[0]["comp"]["node0"] == 501                         # start joined to the existing node
    br = [e for e in edges if e["comp"]["type"] == 1]
    assert br and all(e["comp"]["typeIndex"] == 8 for e in br)        # the plan's rail trestle -> road trestle
    assert all(e["comp"]["position0"]["z"] >= 3 for e in br)          # deck >= water level + 3
    assert min(e["comp"]["position0"]["x"] for e in br) == 300 and max(e["comp"]["position1"]["x"] for e in br) == 420
    normal = [e for e in edges if e["comp"]["type"] == 0]
    assert all(e["comp"]["typeIndex"] == -1 and e["comp"]["roadTemplate"].endswith("country_old_small.street_template") for e in normal)
    through = [e for e in edges if 502 in (e["comp"]["node0"], e["comp"]["node1"])]
    assert len(through) == 2                                        # the road passes through the station node
    assert "подключено въездов станций: 1" in info and "конец" in info
    assert all(n["entity"] < 0 for n in nodes)


def test_road_end_splits_an_existing_edge():
    """No node near the end, but an existing street passes 0 m away: that edge is replaced by two
    halves meeting in the new road's end node (positions/tangents of the same Hermite curve)."""
    lua = make_lua()
    src = (Path(__file__).read_text(encoding="utf-8"))
    i = src.index("def test_road_segment_proposal")
    block = src[i:]
    lua.execute(block[block.index('lua.execute("""') + len('lua.execute("""'): block.index('""")')])
    lua.execute("""
      add_edge(9001, 1000, -50, 1000, 50, false)
      local origGet = api.engine.getComponent
      api.engine.getComponent = function(e, ct)          -- the engine hands out a fresh copy per call
        if e == 9001 and ct == "BASE_EDGE" then
          return make_ud({ position0 = { x = 1000, y = -50, z = 0 }, position1 = { x = 1000, y = 50, z = 0 },
            tangent0 = { x = 0, y = 100, z = 0 }, tangent1 = { x = 0, y = 100, z = 0 }, node0 = 9101, node1 = 9102 }, "BaseEdge")
        end
        return origGet(e, ct)
      end
      local orig = api.engine.util.octree.findEntitiesInCircle
      api.engine.util.octree.findEntitiesInCircle = function(c, r, ct)
        if ct == "BASE_EDGE" then return (c.x - 1000) ^ 2 + c.y ^ 2 <= (r + 1) ^ 2 and { 9001 } or {} end
        return orig(c, r, ct)
      end
      PR.items = {}
    """)
    bp = blueprint(lua)
    sp, why, info = bp.segmentProposal(lua.globals().PR, 1)
    assert sp is not None, why
    assert list(sp["streetProposal"]["edgesToRemove"].values()) == [9001]
    edges = [sp["streetProposal"]["edgesToAdd"][k] for k in sorted(sp["streetProposal"]["edgesToAdd"])]
    # the halves are copies of the old edge's whole component (engine object), with new geometry
    halves = [e["comp"] for e in edges if isinstance(e["comp"], UD)]
    assert len(halves) == 2
    ys = sorted(round(h.position0["y"]) for h in halves)
    assert ys == [-50, 0]                                    # halves meet at the new road's end (1000, 0)
    assert "примыкание к существующей дороге: 1" in info and "конец" not in info


def test_hot_reload_swaps_the_module():
    """A new blueprint_hot version: the bar timer replaces the module; callers use the new code."""
    lua = make_lua()
    lua.execute("""
      USERDATA = USERDATA or {}
      USERDATA.blueprint_hot_ver = { v = 1 }
    """)
    script = (CONTENT / "gui" / "tf3_planner" / "tf3_planner_button.script.lua").read_text(encoding="utf-8")
    lua.execute(script)
    mods = lua.globals().data()
    mods["Tf3PlannerButton"]()
    g = lua.globals()
    timer = g.REACT.timers[len(list(g.REACT.timers.keys()))]
    lua.execute("""
      USERDATA.blueprint_hot_ver = { v = 2 }
      USERDATA.blueprint_hot = { v = 2, fn = function()
        local real = ug_require("tf3_planner_1::/tf3_planner/blueprint.lua")
        local m = setmetatable({ hotMarker = 42 }, { __index = real })
        return m
      end }
    """)
    lua.execute("PRINTED = {}; print = function(s) table.insert(PRINTED, tostring(s)) end")
    timer["fn"]()
    printed = list(lua.globals().PRINTED.values())
    assert any("hot reload v2 ok (fn)" in s for s in printed), printed
    timer["fn"]()                                         # same version: no second reload
    assert sum("ok (fn)" in s for s in list(lua.globals().PRINTED.values())) == 1


def test_build_road_depot_params():
    lua = make_lua()
    lua.execute("""
      api.type = api.type or {}
      api.type.Vec2f = { new = function(x, y) return { x = x, y = y } end }
      api.type.Vec4f = { new = function(x, y, z, w) return { x, y, z, w } end }
      api.type.Mat4f = { new = function(a, b, c, d) return { cols = { a, b, c, d } } end }
      api.type.SimpleProposal = { new = function() return { constructionsToAdd = {} } end,
        ConstructionEntity = { new = function() return {} end } }
      api.engine.util.terrain = { getHeightAt = function(p) return 3 end }
      api.res = api.res or {}
      api.res.constructionRep = { getAll = function() return { [4] = "::/depots/road/road_depot/road_depot.con" } end }
    """)
    bp = blueprint(lua)
    it = lua.eval("{ build = { kind = 'construction', res = 'road_depot.con', params = { seed = 0 }, t = { 10, 20 }, n = { 0, -1 } } }")
    sp = bp.itemProposal(it, 0)[0]
    ce = sp["constructionsToAdd"][1]
    assert ce["fileName"] == "::/depots/road/road_depot/road_depot.con"
    assert ce["params"]["year"] == 1900 and ce["params"]["seed"] == 0 and ce["params"]["modules"] is None
    cols = [list(ce["transf"]["cols"][i].values()) for i in (1, 2, 4)]
    assert cols[1][:2] == [0, -1] and cols[2][:3] == [10, 20, 3]


def test_road_profile_respects_max_grade():
    lua = make_lua()
    src = (Path(__file__).read_text(encoding="utf-8"))
    i = src.index("def test_road_segment_proposal")
    block = src[i:]
    lua.execute(block[block.index('lua.execute("""') + len('lua.execute("""'): block.index('""")')])
    lua.execute("""
      api.engine.util.terrain = { getHeightAt = function(p) return p.x * 0.5 end }   -- a 50 % hillside
      PR.items = {}; PR.spans = {}
    """)
    bp = blueprint(lua)
    sp, why, info = bp.segmentProposal(lua.globals().PR, 1)
    assert sp is not None, why
    edges = [sp["streetProposal"]["edgesToAdd"][k] for k in sorted(sp["streetProposal"]["edgesToAdd"])]
    for e in edges:
        a, b = e["comp"]["position0"], e["comp"]["position1"]
        L = ((b["x"] - a["x"]) ** 2 + (b["y"] - a["y"]) ** 2) ** 0.5
        assert abs(b["z"] - a["z"]) / L <= bp.ROAD_MAX_GRADE + 1e-6, (a, b)
    assert "макс. уклон 12 %" in info


def test_station_at_existing_road_is_built_in_two_steps():
    """Stub end on an existing road edge: check ignores that edge's collision; confirm sends the node
    on the road first and, after it is built, the station at exactly the checked place."""
    lua = make_lua()
    src = (Path(__file__).read_text(encoding="utf-8"))
    i = src.index("def test_build_street_terminal_matches_the_players_station")
    block = src[i:]
    lua.execute(block[block.index('lua.execute("""') + len('lua.execute("""'): block.index('""")')])
    lua.execute("""
      api.type.Vec3f = { new = function(x, y, z) return { x = x, y = y, z = z } end }
      api.type.NodeAndEntity = { new = function() return { comp = {} } end }
      api.type.SegmentAndEntity = { new = function() return { comp = {} } end }
      api.type.SimpleProposal = { new = function() return { constructionsToAdd = {}, streetProposal = {} } end,
        ConstructionEntity = { new = function() return {} end } }
      api.type.enum = { BaseEdgeType = { NORMAL = 0 }, RoadType = { STREET = 1 } }
      -- an existing street along x at y = -36 (the station's stub end is at t - 30 n)
      local EDGE = { position0 = { x = -13100, y = -3700, z = 0 }, position1 = { x = -12800, y = -3700, z = 0 },
        tangent0 = { x = 300, y = 0, z = 0 }, tangent1 = { x = 300, y = 0, z = 0 }, node0 = 7001, node1 = 7002,
        roadType = 1, type = 0 }
      api.engine.util.octree = { findEntitiesInCircle = function(c, r, ct)
        if ct == "BASE_EDGE" and math.abs(c.y + 3700) <= r + 1 then return { 7100 } end
        return {} end }
      local og = api.engine.getComponent
      api.engine.getComponent = function(e, ct) if e == 7100 and ct == "BASE_EDGE" then return EDGE end return og(e, ct) end
      api.engine.util.proposal = { makeProposalData = function(sp, ctx)
        if #sp.constructionsToAdd > 0 then
          return { costs = 101000, errorState = { critical = false, messages = {} },
                   collisionInfo = { collisionEntities = {}, buildingEntities = {} } }
        end
        return { costs = 500, errorState = { critical = false, messages = {} }, collisionInfo = { collisionEntities = {} } }
      end }
      local it = OVERLAY.projects["L1:s1"].items[1]
      it.build = { kind = "street_terminal", res = "modular_terminal.con", platforms = 1, specialization = 1,
                   tramTrack = 1, cargo = true, t = { -12950, -3672 }, n = { 0, 1 }, snap = 30 }
    """)
    bp = blueprint(lua)
    bp.open()
    n = next(k for k in range(1, 30) if bp.canBuild(k))
    bp.buildCheck(n)
    chk = bp._state().buildCheck
    assert chk["state"] == "ok", chk["text"]
    assert "в 3 шага" in chk["text"]
    bp.buildConfirm()
    cmds = [lua.globals().SENT_CMDS[k] for k in sorted(lua.globals().SENT_CMDS)]
    assert len(cmds) == 2                       # node on the road, then the station (the link needs real nodes)
    first, second = cmds[0]["sp"], cmds[1]["sp"]
    assert len(list(first["constructionsToAdd"].keys())) == 0 and first["streetProposal"]["edgesToRemove"][1] == 7100
    node = first["streetProposal"]["nodesToAdd"][1]["comp"]["position"]
    ce = second["constructionsToAdd"][1]
    t = list(ce["transf"]["cols"][4].values())
    # the stub end stays LINK_GAP m off the road node; the link edge is built afterwards
    assert abs(t[0] - node["x"]) < 1e-6 and abs(t[1] - 30 - bp.LINK_GAP - node["y"]) < 1e-6
    assert "подъезд" in bp._state().buildCheck["text"]
