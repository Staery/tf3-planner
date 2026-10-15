"""Offline test of the schema-driven dumper: Python objects appear in Lua as userdata
that supports field indexing but not pairs(), just like TF3 engine objects."""
from pathlib import Path
from lupa.lua52 import LuaRuntime, lua_type

ROOT = Path(__file__).resolve().parent.parent / "mod" / "tf3_planner_1" / "content"


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def make_lua():
    lua = LuaRuntime(unpack_returned_tuples=True, attribute_handlers=(lambda o, k: getattr(o, k, None), None))
    def ug_require(path):
        rel = path.split("::/", 1)[1]
        return lua.execute((ROOT / rel).read_text(encoding="utf-8"))
    lua.globals().ug_require = ug_require
    return lua


def to_py(o):
    if lua_type(o) != "table":
        return o
    return {k: to_py(v) for k, v in o.items()}


def test_cargo_type_and_street_template():
    lua = make_lua()
    dump = lua.eval('ug_require("tf3_planner_1::/tf3_planner/dump.lua")')
    vec = Obj(x=1.0, y=2.0, z=3.0)
    cargo = Obj(name="Coal", weight=1.2, color=vec, timeToDeliverInSeconds=3000,
                scriptRef=Obj(fileName="economy/cargotype.script", params=lua.table_from({"initialPrice": 4})),
                cargoClasses=lua.table_from(["BULK"]))
    r = to_py(dump.of(cargo, "CargoType"))
    assert r["name"] == "Coal" and r["weight"] == 1.2
    assert r["color"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert r["scriptRef"]["params"]["initialPrice"] == 4
    assert r["cargoClasses"] == {1: "BULK"}

    lane = Obj(speed=44.4, width=4.0)
    st = Obj(cost=180, maintenanceCost=30, roadType=Obj(), laneConfigs=lua.table_from([lane]),
             availability=Obj(yearFrom=1930, yearTo=0))
    r = to_py(dump.of(st, "StreetTemplate"))
    assert r["cost"] == 180 and r["availability"]["yearFrom"] == 1930
    assert r["laneConfigs"][1]["speed"] == 44.4
    assert isinstance(r["roadType"], str)  # enum -> tostring, no recursion


def test_untyped_table_guesses_by_key():
    lua = make_lua()
    dump = lua.eval('ug_require("tf3_planner_1::/tf3_planner/dump.lua")')
    cts = Obj(cargoClassesIncluded=lua.table_from(["PASSENGERS"]))
    md = lua.table_from({"cargoTypeSet": cts, "capacity": 60})
    r = to_py(dump.of(md))
    assert r["capacity"] == 60
    assert r["cargoTypeSet"]["cargoClassesIncluded"] == {1: "PASSENGERS"}


class MetadataMap(Obj):
    def __str__(self):
        return "MetadataMap: 0x1234"


def test_vehicle_metadata_map():
    lua = make_lua()
    dump = lua.eval('ug_require("tf3_planner_1::/tf3_planner/dump.lua")')
    md = MetadataMap(availability=Obj(yearFrom=1974, yearTo=2010),
                     cost=Obj(price=120000),
                     maintenance=Obj(lifespan=21915, runningCosts=9000),
                     transportVehicle=Obj(loadSpeed=3, compartments=lua.table_from([
                         Obj(loadConfigs=lua.table_from([Obj(cargoEntry=Obj(capacity=60))]))])))
    r = to_py(dump.of(md))
    assert r["availability"]["yearFrom"] == 1974
    assert r["cost"]["price"] == 120000 and r["maintenance"]["lifespan"] == 21915
    assert r["transportVehicle"]["compartments"][1]["loadConfigs"][1]["cargoEntry"]["capacity"] == 60


def test_sparse_int_keys_become_strings():
    lua = make_lua()
    dump = lua.eval('ug_require("tf3_planner_1::/tf3_planner/dump.lua")')
    r = to_py(dump.of(lua.eval("{[12] = 8, [3] = 1}")))
    assert r == {"12": 8, "3": 1}
    r = to_py(dump.of(lua.eval("{10, 20}")))
    assert r == {1: 10, 2: 20}


def test_all_lua_compiles():
    lua = LuaRuntime()
    chk = lua.eval("function(s, n) local f, e = load(s, n) return e end")
    for f in ROOT.parent.rglob("*.lua"):
        assert chk(f.read_text(encoding="utf-8"), str(f)) is None, f


# --------------------------------------------------------------------------- full export with a fake api

# Engine objects are emulated by real userdata (Python wrappers, no pairs()) like TF3's; fields
# that the engine returns as Lua tables (lists, maps) are plain tables.
class UD:
    def __init__(self, table, name):
        # snapshot: indexing a Lua table from inside lupa's attribute callback is not re-entrant
        self._d, self._name = dict(table.items()), name

    def __getattr__(self, k):
        if k.startswith("_"):
            raise AttributeError(k)
        return self._d.get(k)

    def __str__(self):
        return f"{self._name}: 0x0"

FAKE_API = r"""
local function ud(t, name) return make_ud(t, name or "userdata") end

local function logOf(n, base)
  local times, values = {}, {}
  for i = 1, n do times[i] = i * 1000; values[i] = base + i end
  return ud({ times = times, values = values }, "Log")
end

local CTN = { "NAME", "LINE", "LOG_BOOK", "STATION", "STATION_GROUP", "TOWN", "INDUSTRY", "STOCK_LIST",
  "TRANSPORT_VEHICLE", "VEHICLE_DEPOT", "BOUNDING_VOLUME", "CONSTRUCTION", "PERSON_CAPACITY", "PLAYER_OWNED",
  "ACCOUNT", "GAME_SCRIPT", "GAME_TIME", "GAME_SPEED", "TERRAIN", "BASE_EDGE", "BASE_NODE", "TOWN_BUILDING", "FIELD" }
local ComponentType = {}
for _, n in ipairs(CTN) do ComponentType[n] = n end

local P, WORLD, TOWN, IND, SL, LINE = 100, 1, 200, 300, 301, 400
local SG1, ST1, SG2, ST2, V1, V2 = 500, 501, 510, 511, 600, 601
local GS = {
  ["::/game_mechanics/finance/loan.gs"] = 900,
  ["::game_mechanics/company/company_progression.gs"] = 901,
  ["::/game_mechanics/company/company.gs"] = 902,
  ["::/game_mechanics/towns/town.gs"] = 903,
  ["::/game_mechanics/towns/town_cargo.gs"] = 904,
  ["::/industries/industry_workers.gs"] = 905,
  ["::/game_mechanics/industries/industries.gs"] = 906,
}
local function bbox(x, y) return ud({ bbox = ud({ min = ud({ x = x - 5, y = y - 5, z = 0 }), max = ud({ x = x + 5, y = y + 5, z = 2 }) }) }) end
local terminal = ud({ capacity = 10, cargoLoad = false, cargoUnload = false, passengersLoad = true,
  passengersUnload = true, comfortFactor = 0.1, tag = 0, vehicleNodeId = 7, transportModes = { [3] = true } }, "Terminal")
local function station(x, y)
  return ud({ catchmentAreas = { ud({ position = ud({ x = x, y = y, z = 0 }), radius = 150 }) },
    pool = ud({ capacity = 20 }), tag = 1, terminals = { terminal, terminal } }, "Station")
end
local function vehicle(model, t)
  return ud({
    carrier = 0, state = 2, userStopped = false, noPath = false, stopIndex = 1, line = LINE, daysAtTerminal = 1,
    lastLineStopDeparture = 60000, sectionTimes = { 40.5, 42.0 }, lineStopDepartures = { 50000, 70000 },
    config = ud({ capacities = { 30 }, loadSpeeds = { 1 } }),
    transportVehicleConfig = ud({ vehicles = { ud({ part = ud({ modelId = model }), purchaseTime = t, maintenanceState = 0.9 }) } }),
  }, "TransportVehicle")
end
local lineLogs = ud({ name2log = { itemsTransported = logOf(30, 0) },
  refEntity2name2log = { [TOWN] = { itemsTransported = logOf(3, 0) } } }, "LogBook")

local comps = {
  [WORLD] = { GAME_TIME = ud({ gameTime = 2000000 }), GAME_SPEED = ud({ speedup = 1, millisPerDay = 4000 }),
    TERRAIN = ud({ waterLevel = 0 }, "Engine.Component.Terrain") },
  [P] = { ACCOUNT = ud({ balance = 5000000, loan = 0 }) },
  [TOWN] = { TOWN = ud({ initialLandUseCapacities = { 100, 50, 40 } }), BOUNDING_VOLUME = bbox(0, 0),
    LOG_BOOK = ud({ name2log = { townResidentialCount = logOf(40, 100) }, refEntity2name2log = {} }) },
  [IND] = { INDUSTRY = ud({ construction = 302, stockList = SL, level = 2, maxLevel = 8, closureTimeStamp = 0, upgradeProgress = 0 }),
    PERSON_CAPACITY = ud({ capacity = 20, type = 2, personNodes = { 1, 2 }, vehicleNodes = {} }), BOUNDING_VOLUME = bbox(900, 0) },
  [SL] = { STOCK_LIST = ud({ stocks = {}, rules = {}, modifiers = ud({ productivity = 1.0 }) }) },
  [LINE] = { LINE = ud({ stops = {
      ud({ stationGroup = SG1, station = 0, terminal = 0, loadMode = 0, minWaitingTime = 0, maxWaitingTime = 180 }),
      ud({ stationGroup = SG2, station = 0, terminal = 1, loadMode = 0, minWaitingTime = 0, maxWaitingTime = 180 }) },
    vehicleInfo = ud({ transportModes = { [3] = true } }) }), LOG_BOOK = lineLogs, PLAYER_OWNED = ud({ player = P }) },
  [SG1] = { STATION_GROUP = ud({ stations = { ST1 } }), LOG_BOOK = ud({ name2log = { itemsLoaded = logOf(5, 0) }, refEntity2name2log = {} }) },
  [SG2] = { STATION_GROUP = ud({ stations = { ST2 } }) },
  [ST1] = { STATION = station(10, 0), BOUNDING_VOLUME = bbox(10, 0), PLAYER_OWNED = ud({ player = P }) },
  [ST2] = { STATION = station(3000, 0), BOUNDING_VOLUME = bbox(3000, 0) },
  [V1] = { TRANSPORT_VEHICLE = vehicle(7001, 1000000) },
  [V2] = { TRANSPORT_VEHICLE = vehicle(7001, 1200000) },
  [900] = { GAME_SCRIPT = ud({ state = { obtainedLoans = { [3] = { amount = 1000000 } }, availableLoans = {} } }) },
  [901] = { GAME_SCRIPT = ud({ state = { companyState = { [P] = { experience = 1234, level = 3, potentialLevel = 4, ticketPriceMultiplier = 1.1 } } } }) },
  [902] = { GAME_SCRIPT = ud({ state = { basePopulation = 5000, version = 2, companyStates = {} } }) },
  [903] = { GAME_SCRIPT = ud({ state = { version = 1, townStates = { {
      townEntity = { entity = TOWN, revision = 1 }, authorityScore = 0.6, deliveryRating = "Good",
      cachedRatings = { urban_care = { value = 0.7, critical = false } },
      eventFactors = { score = 0.5, marketingParams = { big = true } } } } } }) },
  [904] = { GAME_SCRIPT = ud({ state = { townState = { [TOWN] = { experience = 777, level = 2, initialLevel = 1,
      cargoDelivered = true, accumulatedExperience = { passenger = { [0] = { rawExperience = 5, multipliedExperience = 5 } } } } } } }) },
  [905] = { GAME_SCRIPT = ud({ state = { industriesState = { [IND] = { lastVisitors = {
      { time = 1500000, entity = { entity = 1, revision = 0 } }, { time = 1600000, entity = { entity = 2, revision = 0 } } },
      pendingVisitors = {} } } } }) },
  [906] = { GAME_SCRIPT = ud({ state = { toSpawn = 0, industryFailedExtensions = { [IND] = { tick = 1 } } } }) },
}
-- v6 street/track graph: N1 -(country road)- N2 =(player track bridge)= N3, plus an edge
-- whose node has to be read from BASE_NODE (no position0) and a broken edge
local N1, N2, N3, N4, NBAD = 1000, 1001, 1002, 1003, 1999
local function v3(x, y, z) return ud({ x = x, y = y, z = z }, "Vec3f") end
local function lanes(speed, n) local l = {}; for i = 1, n do l[i] = ud({ speed = speed, width = 3 }, "LaneConfig") end; return l end
local ROAD_T = "::/infrastructure/street/country/country_old_small.street_template"
local TRACK_T = "::/infrastructure/track/simple/simple.street_template"
local function edge(n0, n1, p0, p1, tmpl, rt, typ, len, sp, nl)
  return ud({ node0 = n0, node1 = n1, position0 = p0, position1 = p1, tangent0 = v3(len, 0, 0), tangent1 = v3(len, 0.04, 0),
    roadTemplate = tmpl, roadType = rt, type = typ, typeIndex = (typ == 1) and 2 or -1, distance = len + 0.04,
    laneConfigs = lanes(sp, nl) }, "Engine.Component.BaseEdge")
end
comps[1100] = { BASE_EDGE = edge(N1, N2, v3(0, 0, 10.04), v3(100, 0, 10), ROAD_T, 1, 0, 100, 16.67, 3) }
comps[1101] = { BASE_EDGE = edge(N2, N3, v3(100, 0, 10), v3(100, 100, 12), TRACK_T, 0, 1, 100, 27.777, 1), PLAYER_OWNED = ud({ player = P }) }
comps[1102] = { BASE_EDGE = edge(N3, N4, v3(100, 100, 12), nil, ROAD_T, 1, 0, 50, 16.67, 3) }
comps[1103] = { BASE_EDGE = edge(NBAD, N1, nil, v3(0, 0, 10), ROAD_T, 1, 0, 10, 16.67, 3) }
comps[N4] = { BASE_NODE = ud({ position = v3(150, 100, 13) }, "Engine.Component.BaseNode") }
local TEMPLATE_IDS = { [ROAD_T] = 29, [TRACK_T] = 86 }
-- v7 town buildings of TOWN: 800 (res, bbox on the building), 801 (com, bbox only on its
-- construction 811), 802 (ind, construction only via streetConnectorSystem), 803 (no position -> failed)
local TB_FILE = "::/buildings/a/r1/1x1_01.con"
local function tb(level, con, parcels) return ud({ town = TOWN, level = level, construction = con, depth = 2, height = 12.34,
  parcels = parcels, timeBuilt = 123456, personCapacity = -1 }, "Engine.Component.TownBuilding") end
local function pcap(t, c) return ud({ type = t, capacity = c }, "Engine.Component.PersonCapacity") end
comps[800] = { TOWN_BUILDING = tb(2, 810, { 1, 2 }), PERSON_CAPACITY = pcap(0, 12), BOUNDING_VOLUME = bbox(100, 200) }
comps[801] = { TOWN_BUILDING = tb(1, 811, { 3 }), PERSON_CAPACITY = pcap(1, 6) }
comps[802] = { TOWN_BUILDING = tb(3, nil, { 4, 5, 6 }), PERSON_CAPACITY = pcap(2, 30), BOUNDING_VOLUME = bbox(-50, 40) }
comps[803] = { TOWN_BUILDING = tb(1, nil, { 7 }), PERSON_CAPACITY = pcap(0, 2) }
comps[810] = { CONSTRUCTION = ud({ fileName = TB_FILE }) }
comps[811] = { CONSTRUCTION = ud({ fileName = "::/buildings/a/c1/1x1_01.con" }), BOUNDING_VOLUME = bbox(300, 300) }
comps[812] = { CONSTRUCTION = ud({ fileName = "::/buildings/a/i1/2x2_01.con" }) }
SENT_COMMANDS = 0
PROPOSALS = 0
-- v8 industry footprint: construction 302 with two subconstructions and a field nearby
local M16 = {}
for i = 1, 16 do M16[i] = (i == 1 or i == 6 or i == 11 or i == 16) and 1 or 0 end
M16[13], M16[14] = 900, 0
local function bb(x0, y0, x1, y1) return ud({ bbox = ud({ min = ud({ x = x0, y = y0, z = 0 }), max = ud({ x = x1, y = y1, z = 5 }) }) }) end
comps[302] = { CONSTRUCTION = ud({ fileName = "::/industries/forest/forest.con", transf = M16, subconstructions = { 303, 305 } }),
  BOUNDING_VOLUME = bb(840, -60, 960, 60) }
comps[303] = { BOUNDING_VOLUME = bb(850, -50, 900, 0) }
comps[305] = { BOUNDING_VOLUME = bb(900, 0, 950, 50) }
comps[304] = { FIELD = ud({}), BOUNDING_VOLUME = bb(1000, -100, 1200, 100) }
-- streetSystem maps: node -> {edges} of the given roadType (nil = all)
function node2edges(rt)
  local m = {}
  for e, cs in pairs(comps) do
    local be = cs.BASE_EDGE
    if be and (rt == nil or be.roadType == rt) then
      for _, n in ipairs({ be.node0, be.node1 }) do m[n] = m[n] or {}; table.insert(m[n], e) end
    end
  end
  return m
end

local withComp = {}
for e, cs in pairs(comps) do for n in pairs(cs) do withComp[n] = withComp[n] or {}; table.insert(withComp[n], e) end end
for _, l in pairs(withComp) do table.sort(l) end

local function chart() return ud({ series = { { { 1900, 1901 }, { 1000, 2000 } }, { { 1900, 1901 }, { -300, -600 } } },
  xRange = { 1900, 1901 }, numPotentialGroups = 2 }, "ChartResult") end
local function emptyRep() return { getAll = function() return {} end, get = function() return nil end, find = function() return -1 end,
  isVisible = function() return true end } end

local financeData = {
  header = { "1900", "1901" }, total = { 1, 2 }, balance = { 3, 4 }, loan = { 0, 0 }, interest = { 0, 0 },
  loanBorrowing = { 0, 0 }, loanRepayment = { 0, 0 },
}
function financeData:foreach_carrier(fn) fn(0, {}) ; fn(1, {}) end
function financeData:foreach_transport(fn, carrier) if carrier == 0 then fn(11, { 500, 700 }); fn(12, { -100, -200 }) end end
function financeData:foreach_investment(fn) fn(21, { -1000, 0 }) end
function financeData:foreach_other(fn) fn(0, { 5, 6 }) end
function financeData:unfoldKey(k) if k == 11 then return { 0, 0, 0 } end return { 1, 0, 0 } end

api = {
  type = {
    ComponentType = ComponentType,
    Vec2f = { new = function(x, y) return { x = x, y = y } end },
    ChartConfig = { new = function() return {} end },
    JournalEntry = { Type = { INCOME = 0, MAINTENANCE = 1 }, Maintenance = { VEHICLE = 0, INFRASTRUCTURE = 2 },
      Carrier = { ROAD = 0, RAIL = 1 } },
    enum = { Carrier = { ROAD = 0, RAIL = 1, TRAM = 2, AIR = 3, WATER = 4 },
      RoadType = { TRACK = 0, STREET = 1 }, BaseEdgeType = { NORMAL = 0, BRIDGE = 1, TUNNEL = 2 },
      LandUseType = { RESIDENTIAL = 0, COMMERCIAL = 1, INDUSTRIAL = 2 } },
  },
  util = { getDefaultYearDuration = function() return 1461000 end, getDefaultMonthDuration = function() return 121750 end,
    getYearDuration = function() return 1461000 end },
  res = {
    cargoTypeRep = { getAll = function() return { [0] = "passengers.lua" } end, get = function() return ud({ name = "Passengers" }, "CargoType") end,
      isVisible = function() return true end, getPassengerCargoTypeId = function() return 0 end },
    modelRep = { forEachModelWithMetadata = function(_, fn) end, find = function() return -1 end },
    streetTemplateRep = { getAll = function() return {} end, get = function() return nil end, isVisible = function() return true end,
      find = function(name) return TEMPLATE_IDS[name] or -1 end },
    bridgeTypeRep = emptyRep(), tunnelTypeRep = emptyRep(), multipleUnitRep = emptyRep(),
    moduleRep = emptyRep(), constructionRep = emptyRep(), metaConstructionRep = emptyRep(),
    getBaseConfig = function() return ud({ costs = {} }, "BaseConfig") end,
  },
  engine = {
    getComponent = function(e, ct) local c = comps[e]; return c and c[ct] or nil end,
    getEntitiesWithComponent = function(ct, filters)
      -- like TF3 build 40408: street graph components cannot be enumerated
      if ct == "BASE_EDGE" or ct == "BASE_NODE" or ct == "TOWN_BUILDING" then error("Cannot loop over this component type") end
      local l = withComp[ct] or {}; local r = {}
      for _, e in ipairs(l) do
        local po = comps[e].PLAYER_OWNED
        if not (filters and filters.requireOwnedByPlayer) or (po and po.player == filters.requireOwnedByPlayer) then r[#r + 1] = e end
      end
      return r
    end,
    terrain = {
      getBoundingBox = function() return ud({ min = ud({ x = 0, y = 0 }), max = ud({ x = 64, y = 64 }) }) end,
      getBaseHeightAt = function() return 1.0 end,
      makeMapFromGame = function() return { towns = {}, industries = {} } end,
    },
    util = {
      getYear = function() return 1901 end, getPlayer = function() return P end, getWorld = function() return WORLD end,
      getCalendarDate = function() return ud({ year = 1901, month = 5, day = 1 }) end,
      getEntityName = function(e) return "E" .. tostring(e) end,
      finance = {
        getPlayersBalance = function() return 5000000 end, calculateEarnings = function() return 1000 end,
        calculateBalance = function(ents, t0, t1, mi, mt)
          assert(type(ents) == "table" and t1 >= t0)
          if mt ~= nil then return -300 * #ents end
          return (mi and 700 or 400) * #ents
        end,
        getAccountChart = function(e, cfg) assert(cfg.count) return chart() end,
        computeFinanceTable = function() return financeData end,
        calcIncomeSince = function() return 1234 end, getFirstIncomeTime = function() return 10 end, getLastIncomeTime = function() return 20 end,
      },
      town = {
        getTownCapacityUsage = function() return {} end, getLandUse2CargoTypes = function() return {} end, getTownStockCargo = function() return {} end,
        getTownLineUsage = function() return 0.25 end,
        getTownDeliveriesStats = function(e, interval, perLine, perCargo)
          assert(interval > 0)
          if perLine then return { [LINE] = { [0] = { 40, 50 } } } end
          return { [-1] = { [0] = { 40, 50 } } }
        end,
        getTownHappinessStats = function() return ud({ byCar = { 1, 2 }, walking = { 0, 3 }, byLine = { { LINE, ud({ resident = { 1, 2 } }) } } }, "UtilTown.TownHappinessStats") end,
        getTownReachability = function() return ud({ commercialPrivate = 1, commercialPublic = 2, industrialPrivate = 3, industrialPublic = 4 }) end,
      },
      stock = { getInputsOutputsFromRules = function() return {} end, getCargoShippedPerYear = function() return 0 end },
      industry = {
        getIndustryProductivityInfo = function() return ud({ producing = true, boostFromRule = false, boostFromPersonCapacity = true }) end,
        getClosingIndustries = function() return { { IND, "::/industries/forest/forest.con" } } end,
      },
      line = {
        getLineCapacityUsages = function() return { [0] = ud({ used = 33, capacity = 60 }, "UtilLine.LineCapacityUsage") } end,
        getMaxFrequency = function() return 0.02 end, calcLineStationThroughput = function() return 99 end,
        getLineIssues = function() return {} end, getLineTransportModesUnion = function() return { [3] = true } end,
      },
      cargo = { getSummarizedCargoQualityDataForLine = function() return ud({ passengers = ud({ countBad = 1, countTotal = 10 }), cargo = ud({ countBad = 0, countTotal = 0 }) }) end },
      logbook = {
        getLogValuePerYear = function(e, name) return 120 end, getLogValueMostRecent = function() return 30 end,
        getChartDiff = function(e, names, cfg) assert(type(names) == "table") return chart() end,
      },
      station = {
        calculateStationTerminalUsage = function(st, i) return ud({ used = 2, capacity = 10 }) end,
        calculateStationUsage = function() return ud({ totalUsed = 4, overflow = 0, poolCapacity = 20, terminalCapacity = 20 }) end,
        isStationOfType = function(st, cargo) return not cargo end,
      },
      maintenance = { calcMaintenanceForStationGroup = function() return 21000 end },
      octree = {
        findEntitiesInCircle = function(c, r, ct)
          if ct ~= "FIELD" then return {} end
          OCTREE_FIELD = { c.x, c.y, r }
          return { 304 }
        end,
      },
      proposal = {
        -- like the game: a removal Proposal is refused, only a SimpleProposal is accepted
        createProposalRemove = function(con, ctx) return { kind = "Proposal", remove = con } end,
        makeProposalData = function(prop, ctx)
          if type(prop) ~= "table" or prop.kind ~= "SimpleProposal" then
            error("bad argument #2 to 'makeProposalData' (SimpleProposal expected, got Proposal)")
          end
          if MPD_ONE_ARG and ctx ~= nil then error("bad argument #3 to 'makeProposalData'") end
          if ctx ~= nil then assert(ctx.player == P) end
          PROPOSALS = PROPOSALS + 1
          local con = prop.constructionsToRemove[1]
          return ud({ costs = 150 * (con - 809), errorState = ud({ critical = false, messages = {}, warnings = { "w" } }),
            collisionInfo = ud({ buildingEntities = {} }) }, "ProposalData")
        end,
      },
      vehicle = { getRunningCost = function() return 9000 end, getDepreciatedValue = function() return 50000 end },
      headquarters = {
        getTransportedData = function() return ud({ passengersTransported = 240, cargoTransported = 0 }, "TransportationStats") end,
        getCompaniesValue = function() return ud({ totalScore = 5, numberOfLines = 1 }, "CompanyValue") end,
      },
    },
    system = {
      lineSystem = { getLinesForPlayer = function(p) assert(p == P) return { LINE } end,
        getLineStopsForStation = function(st) return { { LINE, st == ST1 and 0 or 1 } } end },
      transportVehicleSystem = { getLineVehicles = function(l) return { V1, V2 } end, getLine2VehicleMap = function() return { [LINE] = { V1, V2 } } end },
      stationGroupSystem = { getStationGroup = function(st) return st == ST1 and SG1 or SG2 end },
      stationSystem = { getTown = function() return TOWN end, getStations = function() return { ST1 } end },
      streetConnectorSystem = { getConstructionEntityForStation = function() return 700 end,
        getConstructionEntityForTownBuilding = function(e) if e == 802 then return 812 end return -1 end },
      streetSystem = {
        getNode2StreetEdgeMap = function() return node2edges(1) end,
        getNode2TrackEdgeMap = function() return node2edges(0) end,
        getNode2SegmentMap = function() return node2edges(nil) end,
      },
      catchmentAreaSystem = { getStationCatchables = function() return { 1, 2, 3 } end },
      simEntityAtTerminalSystem = { getLineStopSimEntitiesCount = function(l, i, c) assert(i >= 0) return 5 end },
      simEntityAtVehicleSystem = { getVehicleSimEntitiesCount = function() return 12 end },
      simEntityAtStockSystem = { getStockCount = function() return 0 end },
      simPersonSystem = { getSimPersonsForLine = function() return { 1, 2 } end, getCount = function() return 180 end,
        getSimPersonsForDestination = function() return { 1, 2, 3 } end },
      simPersonAtBuildingSystem = { getSimPersonAtBuilding = function() return { 1 } end },
      carriageListSystem = { getVehicleData = function() return ud({ topSpeed = 13.9, length = 9 }) end },
      townBuildingSystem = {
        getTown2BuildingMap = function() return { [TOWN] = { 801, 800, 802, 803 } } end,
        getPersonCapacity2townBuildingMap = function() return { [900800] = 800, [900801] = 801, [900802] = 802, [900803] = 803 } end,
        getParcel2BuildingMap = function() return { [1] = 800, [2] = 800, [3] = 801 } end,
        getCargoSupplyAndLimit = function(t, lu) return { [9] = { 1.5, 140, 48 } } end,
        getLandUsePersonCapacities = function() return { 100, 50, 40 } end,
        getTown2personCapacitiesMap = function() return { [TOWN] = { 100, 50, 40 } } end,
      },
      gameScriptSystem = { getEntityForGameScript = function(name) return GS[name] or -1 end },
    },
  },
}

-- base game modules the exporter may require; town_cargo_util is missing on purpose
FAKE_MODULES = {
  ["::/game_mechanics/company/company_progression_util.tl"] = {
    getMaxRank = function() return 15 end,
    getLevelAndFraction = function(base, exp) return 3, 0.5, 1000, 1500 end,
    getXpNeededForRank = function(base, rank) return base * rank end,
    getTicketPriceMultiplierForLevel = function(l) if l <= 1 then return nil end return 1 + (l - 1) * 0.05 end,
  },
  ["::/game_mechanics/towns/town_util.tl"] = {
    GetRatings = function() return { { key = "urban_care", getRating = function(e, ts) assert(ts.authorityScore) return 0.7 end },
      { key = "broken", getRating = function() error("boom") end } } end,
    getRatingDeliveriesPassenger = function() return 0.4 end,
    getRatingDeliveriesCargo = function() return 0.1 end,
    getXpFactor = function() return 1.2 end,
  },
  ["::/industries/industry_productivity_util.tl"] = {
    isIndustryWorkersBoosted = function(e, n) return (n or 0) >= 10 end,
    getPersonCapacityPotentialBoost = function() return 2.31 end,
    getIndustryProductivity = function() return 1.0 end,
  },
}

SAVED = nil
api.cmd = { sendCommand = function() SENT_COMMANDS = SENT_COMMANDS + 1 end }
api.type.Context = { new = function() return {} end }
api.type.SimpleProposal = { new = function() return { kind = "SimpleProposal" } end }
app = {
  saveUserdata = function(dir, name, data) SAVED = data end,
  getUserDataFolder = function() return "C:/fake/" end,
}
"""


def _fake_lua(missing_modules: bool = False):
    lua = make_lua()
    base_require = lua.globals().ug_require

    def ug_require(path):
        if path.startswith("tf3_planner_1::"):
            return base_require(path)
        mods = lua.globals().FAKE_MODULES
        m = None if (missing_modules or mods is None) else mods[path]
        if m is None:
            raise RuntimeError("module not found: " + path)
        return m

    lua.globals().ug_require = ug_require
    lua.globals().make_ud = UD
    lua.execute(FAKE_API)
    return lua


def _run_export(lua):
    export = lua.execute((ROOT / "tf3_planner" / "export.lua").read_text(encoding="utf-8"))
    ok, summary = export.run()
    return ok, summary, lua.globals().SAVED


def _assert_plain(o, path="result"):
    t = lua_type(o)
    if t == "table":
        for k, v in o.items():
            assert isinstance(k, (int, str)), f"{path}: key {k!r}"
            _assert_plain(v, f"{path}.{k}")
    else:
        assert t is None, f"{path}: lua {t}"   # numbers/strings/bools arrive as Python values


V5_SECTIONS = {"lines", "stations", "towns_stats", "company", "industries_extra", "finance"}


def test_export_v5_with_fake_api():
    ok, summary, saved = _run_export(_fake_lua())
    assert saved is not None, summary
    _assert_plain(saved)
    r = to_py(saved)
    assert r["version"] == 9
    errors = r.get("errors") or {}
    assert not [e for e in errors.values() if e["section"] in V5_SECTIONS], errors
    for s in V5_SECTIONS:
        assert s in r, s

    line = r["lines"][1]
    assert line["name"] == "E400"
    stops = line["stops"]
    assert stops[1]["stationEntity"] == 501 and stops[2]["stationEntity"] == 511
    assert stops[2]["pos"]["x"] == 3000 and stops[1]["waitingPassengers"] == 5
    assert line["carriers"] == {1: "ROAD"}
    v = line["vehicles"][1]
    assert v["parts"][1]["modelId"] == 7001 and v["purchaseTime"] == 1000000
    assert v["runningCost"] == 9000 and v["sectionTimes"] == {1: 40.5, 2: 42.0}
    assert line["firstPurchaseTime"] == 1000000
    assert line["money"]["line"]["maintIncome"] == 700 and line["money"]["vehicles"]["maintIncome"] == 1400
    assert line["money"]["line"]["byMaintenance"]["VEHICLE"] == -300
    assert line["money"]["lineChart"]["series"][1][2] == {1: 1000, 2: 2000}
    assert line["capacityUsage"][1] == {"cargo": 0, "used": 33, "capacity": 60}
    assert line["maxFrequency"] == 0.02 and line["transportedPerYear"] == 120
    assert line["quality"]["passengers"]["countTotal"] == 10
    log = line["logBook"]["logs"]["itemsTransported"]
    assert log["n"] == 30 and len(log["values"]) == 24 and log["values"][24] == 30

    st = r["stations"]
    s1 = next(s for s in st["stations"].values() if s["id"] == 501)
    assert s1["playerOwned"] is True and s1["onPlayerLine"] is True and s1["group"] == 500
    assert s1["terminals"][2]["used"] == 2 and s1["catchmentAreas"][1]["radius"] == 150
    assert s1["lineStops"][1] == {"line": 400, "stop": 0}
    g = next(g for g in st["groups"].values() if g["id"] == 500)
    assert g["maintenance"] == 21000 and g["logBook"]["logs"]["itemsLoaded"]["n"] == 5

    t = r["towns_stats"]["towns"][1]
    assert t["lineUsage"] == 0.25
    assert t["deliveriesPerLine"][1] == {"line": 400, "cargo": 0, "a": 40, "b": 50}
    assert t["supplyByLandUse"]["COMMERCIAL"][1]["limit"] == 140
    assert t["state"]["authorityScore"] == 0.6 and "marketingParams" not in t["state"]["eventFactors"]
    assert t["ratings"] == {1: {"key": "urban_care", "value": 0.7}}
    assert t["growth"]["experience"] == 777 and "levelCalc" not in t["growth"]
    assert len(t["logBook"]["logs"]["townResidentialCount"]["values"]) == 24

    c = r["company"]
    assert c["level"] == 3 and c["experience"] == 1234 and c["ticketPriceMultiplier"] == 1.1
    assert c["basePopulation"] == 5000 and c["worldPopulation"] == 100
    assert c["levelCalc"]["level"] == 3 and c["xpForRank"][15] == 75000
    assert c["transported"]["passengersTransported"] == 240

    ie = r["industries_extra"]
    assert ie["closing"][1]["id"] == 300
    ind = ie["industries"][1]
    assert ind["visitors"] == 2 and ind["visitorNewest"] == 1600000 and ind["workersBoosted"] is False
    assert ind["potentialBoost"] == 2.31 and ind["workersAssigned"] == 3
    assert ind["personCapacity"]["capacity"] == 20 and "personNodes" not in ind["personCapacity"]

    f = r["finance"]
    assert f["balance"] == 5000000 and f["loansObtained"] == {"count": 1, "amount": 1000000}
    road = f["table"]["transport"][1]
    assert road["carrier"] == "ROAD" and road["rows"][1]["type"] == "INCOME" and road["rows"][2]["type"] == "MAINTENANCE"
    assert f["linesBalance"]["maintIncome"] == 700 and f["vehiclesBalance"]["maintIncome"] == 1400
    assert "lines=1" in summary


def test_export_survives_missing_api():
    """Missing engine functions / base modules must not abort the v5 sections."""
    lua = _fake_lua(missing_modules=True)
    lua.execute("api.engine.system.lineSystem = nil; api.engine.util.town = nil; api.engine.util.finance = nil")
    _ok, _summary, saved = _run_export(lua)
    _assert_plain(saved)
    r = to_py(saved)
    assert r["version"] == 9
    assert r["lines"][1]["id"] == 400                    # fallback: LINE components owned by the player
    assert r["towns_stats"]["towns"][1]["id"] == 200
    assert "ratings" not in r["towns_stats"]["towns"][1]
    assert r["company"]["level"] == 3 and "levelCalc" not in r["company"]

def test_export_v6_network_graph():
    _ok, summary, saved = _run_export(_fake_lua())
    _assert_plain(saved)
    r = to_py(saved)
    assert r["version"] == 9
    errors = r.get("errors") or {}
    assert not [e for e in errors.values() if e["section"] in ("network", "networkGraph")], errors
    n = r["network"]
    assert n["stations"] and "lines" in n                      # v5 content kept
    assert n["edges"] == 3 and n["failedEdges"] == 1 and "firstError" in n
    assert n["ownedVia"] == "playerOwnedList" and n["enumeration"] == "node2street+track"
    assert n["roadTypeValues"] == {"STREET": 1, "TRACK": 0}
    assert n["structValues"] == {"NORMAL": 0, "BRIDGE": 1, "TUNNEL": 2}
    ids = [n["nodeIds"][k] for k in sorted(n["nodeIds"])]
    pos = [n["nodePos"][k] for k in sorted(n["nodePos"])]
    assert len(pos) == 3 * len(ids)
    p = {nid: tuple(pos[3 * k:3 * k + 3]) for k, nid in enumerate(ids)}
    assert p[1000] == (0, 0, 10.0) and p[1002] == (100, 100, 12) and p[1003] == (150, 100, 13)  # 0.1 m rounding, BASE_NODE
    en = [n["edgeNodes"][k] for k in sorted(n["edgeNodes"])]
    assert [ids[i] for i in en] == [1000, 1001, 1001, 1002, 1002, 1003]
    lst = lambda f: [n[f][k] for k in sorted(n[f])]
    assert lst("edgeIds") == [1100, 1101, 1102]
    assert lst("edgeOwned") == [0, 1, 0]
    assert lst("edgeStruct") == [0, 1, 0] and lst("edgeTypeIndex") == [-1, 2, -1]
    assert lst("edgeRoadType") == [1, 0, 1]
    assert lst("edgeLength") == [100.0, 100.0, 50.0]
    assert lst("edgeTangents")[:6] == [100, 0, 0, 100, 0.0, 0]
    tmpl = [n["templates"][k] for k in sorted(n["templates"])]
    assert lst("edgeTemplate") == [0, 1, 0]
    assert tmpl[0]["id"] == 29 and tmpl[0]["speed"] == 16.67 and tmpl[0]["lanes"] == 3
    assert tmpl[1]["name"].endswith("simple.street_template") and tmpl[1]["roadType"] == 0
    assert "edges=3" in summary


def test_export_v6_network_fallbacks():
    """PLAYER_OWNED not loopable -> ownership per edge; street/track maps missing -> node2segment map."""
    lua = _fake_lua()
    lua.execute("""
      local orig = api.engine.getEntitiesWithComponent
      api.engine.getEntitiesWithComponent = function(ct, f)
        if ct == "PLAYER_OWNED" then error("Cannot loop over this component type") end
        return orig(ct, f)
      end
      api.engine.system.streetSystem.getNode2StreetEdgeMap = nil
      api.engine.system.streetSystem.getNode2TrackEdgeMap = function() error("boom") end
      api.type.enum.RoadType = nil
    """)
    _ok, _summary, saved = _run_export(lua)
    r = to_py(saved)
    n = r["network"]
    assert n["ownedVia"] == "component" and n["enumeration"] == "node2segment"
    assert [n["edgeIds"][k] for k in sorted(n["edgeIds"])] == [1100, 1101, 1102]
    assert [n["edgeOwned"][k] for k in sorted(n["edgeOwned"])] == [0, 1, 0]
    assert "roadTypeValues" not in n
    assert r["lines"][1]["id"] == 400                  # unrelated sections unaffected


def test_export_v6_network_no_enumeration():
    """No street system at all: BASE_EDGE enumeration is refused -> section error, rest of export intact."""
    lua = _fake_lua()
    lua.execute("api.engine.system.streetSystem = nil")
    _ok, summary, saved = _run_export(lua)
    r = to_py(saved)
    errs = [e for e in (r.get("errors") or {}).values() if e["section"] == "networkGraph"]
    assert errs and "Cannot loop over this component type" in errs[0]["error"]
    assert r["network"]["stations"] and "edgeIds" not in r["network"]
    assert r["lines"][1]["id"] == 400


def test_export_v6_network_enum_objects():
    """Enums handed out as engine objects (not numbers) are mapped to codes, never nil holes."""
    lua = _fake_lua()
    lua.execute("""
      local TR, ST = make_ud({}, "RoadType"), make_ud({}, "RoadType")
      local NO, BR, TU = make_ud({}, "BaseEdgeType"), make_ud({}, "BaseEdgeType"), make_ud({}, "BaseEdgeType")
      api.type.enum.RoadType = { TRACK = TR, STREET = ST }
      api.type.enum.BaseEdgeType = { NORMAL = NO, BRIDGE = BR, TUNNEL = TU }
      local rt, ty = { [1100] = ST, [1101] = TR, [1102] = ST }, { [1100] = NO, [1101] = BR, [1102] = TU }
      local get = api.engine.getComponent
      api.engine.getComponent = function(e, ct)
        local c = get(e, ct)
        if ct == "BASE_EDGE" and rt[e] then
          return make_ud({ node0 = c.node0, node1 = c.node1, position0 = c.position0, position1 = c.position1,
            tangent0 = c.tangent0, tangent1 = c.tangent1, roadTemplate = c.roadTemplate, roadType = rt[e], type = ty[e],
            typeIndex = c.typeIndex, distance = c.distance, laneConfigs = c.laneConfigs }, "Engine.Component.BaseEdge")
        end
        return c
      end
    """)
    _ok, _summary, saved = _run_export(lua)
    _assert_plain(saved)
    n = to_py(saved)["network"]
    lst = lambda f: [n[f][k] for k in sorted(n[f])]
    assert lst("edgeRoadType") == [1, 0, 1]
    assert lst("edgeStruct") == [0, 1, 2]
    assert n["edges"] == 3


def _arr(t, f):
    return [t[f][k] for k in sorted(t[f])]


def test_export_v8_industry_footprint():
    lua = _fake_lua()
    _ok, _summary, saved = _run_export(lua)
    _assert_plain(saved)
    r = to_py(saved)
    ind = r["industries"][1]
    fp = ind["footprint"]
    assert fp["ind"] == {1: 895, 2: -5, 3: 905, 4: 5}
    assert fp["con"] == {1: 840, 2: -60, 3: 960, 4: 60}
    assert _arr(fp, "parts") == [850, -50, 900, 0, 900, 0, 950, 50]
    assert _arr(fp, "fields") == [1000, -100, 1200, 100]
    assert _arr(fp, "transf")[12:14] == [900, 0]
    assert list(lua.globals().OCTREE_FIELD.values()) == [900, 0, 700]
    # no octree / no construction -> still a footprint of the industry entity, no section error
    lua2 = _fake_lua()
    lua2.execute("""api.engine.util.octree = nil
      local g = api.engine.getComponent
      api.engine.getComponent = function(e, ct) if e == 302 then return nil end return g(e, ct) end""")
    _ok, _s, saved2 = _run_export(lua2)
    fp2 = to_py(saved2)["industries"][1]["footprint"]
    assert fp2["ind"] and "con" not in fp2 and "fieldsError" in fp2


def test_export_v7_town_buildings():
    lua = _fake_lua()
    _ok, summary, saved = _run_export(lua)
    _assert_plain(saved)
    r = to_py(saved)
    assert r["version"] == 9
    errors = r.get("errors") or {}
    assert not [e for e in errors.values() if e["section"] == "townBuildings"], errors
    assert "modules" in r["buildings"]                        # transport catalogue untouched
    tb = r["townBuildings"]
    assert tb["enumeration"] == "town2BuildingMap" and "enumerationErrors" not in tb
    assert tb["count"] == 3 and tb["failed"] == 1 and "no position" in tb["firstError"]
    assert _arr(tb, "ids") == [800, 801, 802]                  # sorted
    assert _arr(tb, "town") == [200, 200, 200]
    assert _arr(tb, "construction") == [810, 811, 812]         # 802 via streetConnectorSystem
    assert _arr(tb, "landUse") == [0, 1, 2] and _arr(tb, "capacity") == [12, 6, 30]
    assert tb["landUseValues"] == {"RESIDENTIAL": 0, "COMMERCIAL": 1, "INDUSTRIAL": 2}
    assert _arr(tb, "level") == [2, 1, 3] and _arr(tb, "parcels") == [2, 1, 3]
    assert _arr(tb, "depth") == [2, 2, 2] and _arr(tb, "height") == [12.3, 12.3, 12.3]
    assert _arr(tb, "timeBuilt") == [123456] * 3
    bb = _arr(tb, "bbox")
    assert bb[:6] == [95, 195, 0, 105, 205, 2] and bb[6:12] == [295, 295, 0, 305, 305, 2]  # 801: construction bbox
    assert _arr(tb, "pos")[:3] == [100, 200, 1]
    files = _arr(tb, "files")
    assert [files[i] for i in _arr(tb, "file")] == ["::/buildings/a/r1/1x1_01.con", "::/buildings/a/c1/1x1_01.con",
                                                    "::/buildings/a/i1/2x2_01.con"]
    samples = [tb["bulldozeSamples"][k] for k in sorted(tb["bulldozeSamples"])]
    assert len(samples) == 3 and [s_["costs"] for s_ in samples] == [150, 300, 450]
    assert samples[0]["landUse"] == 0 and samples[0]["critical"] is False and "error" not in samples[0]
    assert samples[0]["call"] == "proposal,context" and samples[0]["warnings"] == {1: "w"}
    assert samples[0]["collisionBuildings"] == 0
    assert lua.globals().SENT_COMMANDS == 0 and lua.globals().PROPOSALS == 3   # computed, never sent
    assert "townBuildings=3" in summary


def test_export_v7_bulldoze_probe_one_arg():
    """makeProposalData without a context is tried when the two-argument form is refused."""
    lua = _fake_lua()
    lua.execute("MPD_ONE_ARG = true")
    _ok, _summary, saved = _run_export(lua)
    tb = to_py(saved)["townBuildings"]
    samples = [tb["bulldozeSamples"][k] for k in sorted(tb["bulldozeSamples"])]
    assert [s_.get("call") for s_ in samples] == ["proposal"] * 3 and samples[2]["costs"] == 450
    assert lua.globals().SENT_COMMANDS == 0


def test_export_v7_town_buildings_fallbacks():
    """town2BuildingMap unavailable -> personCapacity map (town from TOWN_BUILDING); no proposal API."""
    lua = _fake_lua()
    lua.execute("""
      api.engine.system.townBuildingSystem.getTown2BuildingMap = function() error("Cannot loop over this component type") end
      api.engine.util.proposal = nil
    """)
    _ok, _summary, saved = _run_export(lua)
    tb = to_py(saved)["townBuildings"]
    assert tb["enumeration"] == "personCapacity2townBuildingMap"
    assert "town2BuildingMap" in tb["enumerationErrors"][1]
    assert _arr(tb, "ids") == [800, 801, 802] and _arr(tb, "town") == [200, 200, 200]
    s0 = tb["bulldozeSamples"][1]
    assert "error" in s0 and len(tb["bulldozeSamples"]) == 1      # stops after the first failure


def test_export_v7_town_buildings_refused_everywhere():
    """All maps gone and TOWN_BUILDING loops refused -> section error only, rest of the export intact."""
    lua = _fake_lua()
    lua.execute("""
      local s = api.engine.system.townBuildingSystem
      s.getTown2BuildingMap, s.getPersonCapacity2townBuildingMap, s.getParcel2BuildingMap = nil, nil, nil
    """)
    _ok, _summary, saved = _run_export(lua)
    r = to_py(saved)
    errs = [e for e in (r.get("errors") or {}).values() if e["section"] == "townBuildings"]
    assert errs and "Cannot loop over this component type" in errs[0]["error"]
    assert "townBuildings" not in r and r["network"]["edges"] == 3 and r["lines"][1]["id"] == 400


def test_loader_roundtrip_network():
    """saveUserdata-style file -> loader -> plain lists usable by tf3planner.network."""
    import sys
    sys.path.insert(0, str(ROOT.parent.parent.parent))
    from tf3planner.loader import _to_py
    _ok, _summary, saved = _run_export(_fake_lua())
    n = _to_py(saved)["network"]
    assert isinstance(n["nodePos"], list) and isinstance(n["edgeNodes"], list) and isinstance(n["templates"], list)
    assert len(n["edgeNodes"]) == 2 * n["edges"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
