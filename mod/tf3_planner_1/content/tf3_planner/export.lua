-- Collects the game state needed by the external optimizer.
-- TF3 has no io library for mods, so the result goes through app.saveUserdata to
-- <userdata>/tf3_planner/export.lua (function data() return {...} end).
-- Engine objects are userdata without pairs(); dump.of walks them using the field
-- schema generated from the game's *.d.tl. Every section runs under pcall and
-- failures are listed in result.errors.

local dump = ug_require "tf3_planner_1::/tf3_planner/dump.lua"
local C = ug_require "tf3_planner_1::/tf3_planner/common.lua"
local stats = ug_require "tf3_planner_1::/tf3_planner/stats.lua"
local netgraph = ug_require "tf3_planner_1::/tf3_planner/network.lua"
local townbuildings = ug_require "tf3_planner_1::/tf3_planner/townbuildings.lua"

local export = {}

local EXPORT_VERSION = 9
local HEIGHT_STEP = 32 -- metres between height samples
local OUT_DIR = "tf3_planner"

local log, CT, comp, vec = C.log, C.CT, C.comp, C.vec
local entityName, bboxCenter = C.entityName, C.bboxCenter

---------------------------------------------------------------------------
-- sections

local function probe()
	-- base/init.lua predeclares globals like app as false, so test truthiness, not ~= nil
	local p = {
		io = (io and io.open) and true or false,
		os = os and true or false,
		app = app and true or false,
		appSaveUserdata = (app and app.saveUserdata) and true or false,
		repGetAsTable = (api.res.modelRep.getAsTable) and true or false,
		lua = _VERSION,
	}
	if app and app.getUserDataFolder then
		local ok, f = pcall(app.getUserDataFolder)
		p.appUserDataFolder = ok and f or ("error: " .. tostring(f))
	end
	return p
end

local function meta()
	local m = {}
	local u = api.engine.util
	m.year = u.getYear()
	local player = u.getPlayer()
	m.player = player
	pcall(function() m.money = u.finance.getPlayersBalance(player) end)
	pcall(function() m.earnings = u.finance.calculateEarnings(player) end)
	pcall(function() m.account = dump.of(comp(player, "ACCOUNT"), "Engine.Component.Account") end)
	pcall(function()
		local gt = comp(u.getWorld(), "GAME_TIME")
		m.gameTime = gt.gameTime
		local d = u.getCalendarDate(gt.gameTime)
		m.date = { year = d.year, month = d.month, day = d.day }
	end)
	pcall(function() m.yearDurationMs = api.util.getDefaultYearDuration() end)
	-- calendar speed: millisPerDay is affected by the speed-up setting (0 when paused),
	-- so also record the duration of a year at the current calendar speed
	pcall(function()
		local gs = comp(u.getWorld(), "GAME_SPEED")
		m.gameSpeed = { speedup = gs.speedup, millisPerDay = gs.millisPerDay }
		if gs.millisPerDay and gs.millisPerDay > 0 and gs.speedup and gs.speedup > 0 then
			m.yearDurationAtSpeedMs = api.util.getYearDuration(gs.millisPerDay)
			m.millisPerDayAt1x = gs.millisPerDay * gs.speedup
		end
	end)
	pcall(function() m.monthDurationMs = api.util.getDefaultMonthDuration() end)
	pcall(function()
		local bb = api.engine.terrain.getBoundingBox()
		m.bbox = { min = vec(bb.min), max = vec(bb.max) }
	end)
	pcall(function()
		m.terrain = dump.of(comp(u.getWorld(), "TERRAIN"), "Engine.Component.Terrain")
		if m.terrain then m.terrain.dataMaps = nil end
	end)
	return m
end

local function baseConfig()
	local bc = dump.of(api.res.getBaseConfig(), "BaseConfig")
	-- drop purely visual parts to keep the export small
	if type(bc) == "table" then
		bc.animal, bc.particles, bc.water, bc.scaffold, bc.locations, bc.environment = nil, nil, nil, nil, nil, nil
	end
	return bc
end

local function heightmap(m)
	local bb = api.engine.terrain.getBoundingBox()
	local x0, y0, x1, y1 = bb.min.x, bb.min.y, bb.max.x, bb.max.y
	local nx = math.floor((x1 - x0) / HEIGHT_STEP) + 1
	local ny = math.floor((y1 - y0) / HEIGHT_STEP) + 1
	local h = {}
	local getH = api.engine.terrain.getBaseHeightAt or api.engine.terrain.getHeightAt
	local i = 0
	for iy = 0, ny - 1 do
		local y = y0 + iy * HEIGHT_STEP
		for ix = 0, nx - 1 do
			i = i + 1
			local z = getH(api.type.Vec2f.new(x0 + ix * HEIGHT_STEP, y))
			h[i] = math.floor(z * 10 + 0.5) / 10
		end
	end
	return {
		origin = { x = x0, y = y0 }, step = HEIGHT_STEP, nx = nx, ny = ny,
		waterLevel = m.terrain and m.terrain.waterLevel or nil,
		rowMajorY = true, -- h[iy*nx + ix + 1]
		h = h,
	}
end

local function towns(gameMap)
	local res = {}
	local seen = {}
	if gameMap and gameMap.towns then
		for _, t in ipairs(gameMap.towns) do
			local e = t.existing
			local entry = {
				id = e, name = t.name, pos = vec(t.pos),
				sizeFactors = dump.of(t.sizeFactors),
				landUse2CargoNeeds = dump.of(t.landUse2CargoNeeds),
			}
			res[#res + 1] = entry
			if e then seen[e] = entry end
		end
	end
	for _, e in ipairs(api.engine.getEntitiesWithComponent(CT("TOWN"))) do
		local entry = seen[e]
		if not entry then
			entry = { id = e, name = entityName(e) }
			res[#res + 1] = entry
		end
		entry.town = dump.of(comp(e, "TOWN"), "Engine.Component.Town")
		entry.bboxCenter = bboxCenter(e)
		entry.pos = entry.pos or entry.bboxCenter
		pcall(function() entry.capacityUsage = dump.of(api.engine.util.town.getTownCapacityUsage(e)) end)
		pcall(function() entry.supplyAndLimit = dump.of(api.engine.system.townBuildingSystem.getCargoSupplyAndLimit(e)) end)
		pcall(function() entry.landUse2CargoTypes = dump.of(api.engine.util.town.getLandUse2CargoTypes(e)) end)
		pcall(function() entry.stockCargo = dump.of(api.engine.util.town.getTownStockCargo(e)) end)
	end
	return res
end

-- v8: where an industry physically is (station siting must not build into it):
-- AABBs (minx, miny, maxx, maxy) of the industry entity, its construction and every
-- subconstruction, the construction transf, and fields found around it (octree query; FIELD
-- loops may be refused like BASE_EDGE).
local FIELD_RADIUS = 700

local function bb4(ent)
	local bv = comp(ent, "BOUNDING_VOLUME")
	if not bv then return nil end
	local a, b = bv.bbox.min, bv.bbox.max
	local r = function(v) return math.floor(v * 10 + 0.5) / 10 end
	return { r(a.x), r(a.y), r(b.x), r(b.y) }
end

local function industryFootprint(e, ic, pos)
	local fp = { ind = bb4(e) }
	local con = ic and ic.construction
	if type(con) == "number" and con >= 0 then
		fp.con = bb4(con)
		local cc = comp(con, "CONSTRUCTION")
		if cc then
			pcall(function()
				local t, m = cc.transf, {}
				for i = 1, 16 do
					local v = t[i]
					if type(v) ~= "number" then return end
					m[i] = v
				end
				fp.transf = m
			end)
			pcall(function()
				local parts = {}
				for _, sc in ipairs(C.list(cc.subconstructions)) do
					local b = bb4(sc)
					if b then for q = 1, 4 do parts[#parts + 1] = b[q] end end
				end
				if #parts > 0 then fp.parts = parts end
			end)
		end
	end
	if pos then
		local ok, err = pcall(function()
			local fl = {}
			local ents = api.engine.util.octree.findEntitiesInCircle(api.type.Vec2f.new(pos.x, pos.y), FIELD_RADIUS, CT("FIELD"))
			for _, f in ipairs(C.list(ents)) do
				local b = bb4(f)
				if b then for q = 1, 4 do fl[#fl + 1] = b[q] end end
			end
			if #fl > 0 then fp.fields = fl end
		end)
		if not ok then fp.fieldsError = tostring(err) end
	end
	return fp
end

local function industries(gameMap)
	local res = {}
	local byEntity = {}
	if gameMap and gameMap.industries then
		for _, ind in ipairs(gameMap.industries) do
			local entry = {
				id = ind.existing, fileName = ind.fileName, pos = vec(ind.pos),
				angle = ind.angle, onWater = ind.onWater, tag = ind.tag,
			}
			res[#res + 1] = entry
			if ind.existing then byEntity[ind.existing] = entry end
		end
	end
	for _, e in ipairs(api.engine.getEntitiesWithComponent(CT("INDUSTRY"))) do
		local entry = byEntity[e]
		if not entry then
			entry = { id = e }
			res[#res + 1] = entry
		end
		entry.name = entityName(e)
		local ic = comp(e, "INDUSTRY")
		if ic then
			entry.industry = dump.of(ic, "Engine.Component.Industry")
			local con = comp(ic.construction, "CONSTRUCTION")
			if con then
				entry.fileName = entry.fileName or con.fileName
			end
			entry.stockList = dump.of(comp(ic.stockList, "STOCK_LIST"), "Engine.Component.StockList")
			local sl = ic.stockList
			local st = api.engine.util.stock
			pcall(function() entry.inputsOutputs = dump.of(st.getInputsOutputsFromRules(sl)) end)
			pcall(function() entry.shippedPerYear = st.getCargoShippedPerYear(sl) end)
			-- per-cargo figures: the util functions take (stockListEntity, cargoTypeId).
			-- Stored as a list: app.saveUserdata drops sparse integer keys.
			entry.perCargo = {}
			local seen = {}
			local stocks = entry.stockList and entry.stockList.stocks or {}
			for si, s in ipairs(stocks) do
				local c = s.cargoType
				if type(c) == "number" and not seen[c] then
					seen[c] = true
					local pc = { cargo = c, stockType = s.type, stockCapacity = s.capacity }
					pcall(function() pc.producedPerYear = st.getCargoProducedPerYear(sl, c) end)
					pcall(function() pc.maxProductionPerYear = st.getCargoMaxProductionPerYear(sl, c) end)
					pcall(function() pc.maxConsumptionPerYear = st.getCargoMaxConsumptionPerYear(sl, c) end)
					pcall(function() pc.shippedPerYear = st.getCargoTypeShippedPerYear(sl, c) end)
					pcall(function() pc.stockCount = api.engine.system.simEntityAtStockSystem.getStockCount(e, si - 1) end)
					entry.perCargo[#entry.perCargo + 1] = pc
				end
			end
			pcall(function() entry.productivityInfo = dump.of(api.engine.util.industry.getIndustryProductivityInfo(e)) end)
			pcall(function()
				entry.personCapacity = dump.of(comp(e, "PERSON_CAPACITY"), "Engine.Component.PersonCapacity")
				if type(entry.personCapacity) == "table" then
					entry.personCapacity.personNodes, entry.personCapacity.vehicleNodes = nil, nil
				end
			end)
		end
		entry.pos = entry.pos or bboxCenter(e)
		local okF, fp = pcall(industryFootprint, e, comp(e, "INDUSTRY"), entry.pos)
		if okF then entry.footprint = fp else entry.footprintError = tostring(fp) end
	end
	return res
end

local function repDump(rep, typeName, post)
	local res = {}
	for id, name in pairs(rep.getAll(true)) do
		local entry = { id = id, name = name }
		pcall(function() entry.visible = rep.isVisible(id) end)
		local ok, err = pcall(function()
			entry.data = dump.of(rep.get(id), typeName)
			if post then post(entry) end
		end)
		if not ok then entry.error = tostring(err) end
		res[#res + 1] = entry
	end
	return res
end

local function cargoTypes()
	local pid = nil
	pcall(function() pid = api.res.cargoTypeRep.getPassengerCargoTypeId() end)
	return repDump(api.res.cargoTypeRep, "CargoType", function(entry)
		entry.isPassenger = (entry.id == pid)
	end)
end

local function streetTemplates()
	return repDump(api.res.streetTemplateRep, "StreetTemplate", function(entry)
		local d = entry.data
		if type(d) == "table" then
			-- keep it compact: textures/decorations are irrelevant for planning
			d.fillGroundTex, d.sidewalkFillGroundTex, d.borderGroundTex = nil, nil, nil
			d.defaultEdgeDecorations = nil
		end
	end)
end

local function vehicles()
	local rep = api.res.modelRep
	local res = {}
	rep.forEachModelWithMetadata("transportVehicle", function(name)
		local entry = { name = name }
		local ok, err = pcall(function()
			local id = rep.find(name)
			entry.id = id
			pcall(function() entry.visible = rep.isVisible(id) end)
			local model = rep.get(id)
			local md = dump.of(model.metadata)
			if type(md) == "table" then
				-- visual-only blocks
				md.soundConfig, md.particleSystem, md.lod, md.bulldozer = nil, nil, nil, nil
			end
			entry.metadata = md
		end)
		if not ok then entry.error = tostring(err) end
		res[#res + 1] = entry
	end)
	return res
end

local function industryTypes(inds)
	local res = {}
	local done = {}
	for _, ind in ipairs(inds) do
		local fn = ind.fileName
		if fn and not done[fn] then
			done[fn] = true
			local entry = { fileName = fn }
			local ok, err = pcall(function()
				local id = api.res.constructionRep.find(fn)
				local cd = api.res.constructionRep.get(id)
				entry.id = id
				entry.description = dump.of(cd.description, "ModelMetadata.Description")
				entry.availability = dump.of(cd.availability, "ModelMetadata.Availability")
				entry.placementTag = cd.placementTag
				pcall(function() entry.metadata = dump.of(cd.metadata) end)
				pcall(function() entry.params = dump.of(cd.params) end)
			end)
			if not ok then entry.error = tostring(err) end
			res[#res + 1] = entry
		end
	end
	return res
end

-- Catalogue of transport buildings: stations (modular: cost comes from modules), harbors,
-- depots, maintenance stations, warehouses, airports. Cost/maintenance live in
-- description.attributes (constructions/templates) and module cost/metadata.
local BUILDING_PATTERN = { "station", "depot", "harbor", "harbour", "warehouse", "airport", "dock", "pier",
	"stop", "terminal", "platform", "maint", "airfield", "heliport", "helipad", "cargo", "truck", "bus" }

local function isBuilding(name)
	local n = string.lower(name)
	if n:find("/industries/") or n:find("/town") or n:find("/assets/") then return false end
	for _, p in ipairs(BUILDING_PATTERN) do
		if n:find(p, 1, true) then return true end
	end
	return false
end

local function buildings()
	local res = { modules = {}, constructions = {}, metaConstructions = {} }
	local function collect(rep, typeName, target, fields)
		for id, name in pairs(rep.getAll(true)) do
			if isBuilding(name) then
				local entry = { id = id, name = name }
				local ok, err = pcall(function()
					local obj = rep.get(id)
					for _, f in ipairs(fields) do
						local okf, v = pcall(function() return obj[f] end)
						if okf and v ~= nil then
							entry[f] = dump.of(v, (f == "description" and "ModelMetadata.Description")
								or (f == "availability" and "ModelMetadata.Availability")
								or (f == "cost" and "ModelMetadata.Cost")
								or (f == "menuCategory" and "ModelMetadata.MenuCategory")
								or (f == "constructionTemplates" and nil) or nil)
						end
					end
					if type(entry.description) == "table" then
						local d = entry.description
						entry.description = { name = d.name, attributes = d.attributes, cargoTypeSet = d.cargoTypeSet }
					end
				end)
				if not ok then entry.error = tostring(err) end
				target[#target + 1] = entry
			end
		end
	end
	collect(api.res.moduleRep, "ModuleDesc", res.modules,
		{ "fileName", "type", "availability", "cost", "description", "metadata", "category" })
	collect(api.res.constructionRep, "ConstructionDesc", res.constructions,
		{ "fileName", "type", "availability", "description", "menuCategory", "constructionTemplates", "costsYearProgression", "metadata" })
	collect(api.res.metaConstructionRep, "MetaConstructionDesc", res.metaConstructions,
		{ "availability", "description", "menuCategory", "metadata", "relativePath" })
	return res
end

-- Loan offers and obtained loans live in the state of the loan game script
-- (game_mechanics/finance/loan.script.tl, LoanTable). Durations are in ms of game time.
local function loans()
	local e = api.engine.system.gameScriptSystem.getEntityForGameScript("::/game_mechanics/finance/loan.gs")
	local gs = comp(e, "GAME_SCRIPT")
	local r = {
		state = dump.of(gs.state),
		maxObtainable = 4,
		monthDurationMs = nil,
	}
	pcall(function() r.monthDurationMs = api.util.getDefaultMonthDuration() end)
	return r
end

local function network()
	local n = { lines = {}, stations = {}, vehicles = 0, depots = 0 }
	pcall(function()
		for _, e in ipairs(api.engine.getEntitiesWithComponent(CT("LINE"))) do
			n.lines[#n.lines + 1] = { id = e, name = entityName(e), line = dump.of(comp(e, "LINE"), "Engine.Component.Line") }
		end
	end)
	pcall(function()
		for _, e in ipairs(api.engine.getEntitiesWithComponent(CT("STATION"))) do
			n.stations[#n.stations + 1] = { id = e, name = entityName(e), pos = bboxCenter(e) }
		end
	end)
	pcall(function() n.vehicles = #api.engine.getEntitiesWithComponent(CT("TRANSPORT_VEHICLE")) end)
	pcall(function() n.depots = #api.engine.getEntitiesWithComponent(CT("VEHICLE_DEPOT")) end)
	return n
end

---------------------------------------------------------------------------

local function writeOutput(result)
	local folder = result.probe and result.probe.appUserDataFolder
	if app and app.saveUserdata then
		local ok, err = pcall(app.saveUserdata, OUT_DIR, "export", result)
		if ok then
			return { path = tostring(folder) .. OUT_DIR .. "/export.lua" }
		end
		log("saveUserdata failed: " .. tostring(err))
		return { error = tostring(err) }
	end
	return { error = "app.saveUserdata not available" }
end

function export.run()
	local t0 = os and os.clock and os.clock() or 0
	local result = { version = EXPORT_VERSION, errors = {}, timings = {} }
	local function section(name, fn, ...)
		local ts = os and os.clock and os.clock() or 0
		local ok, v = pcall(fn, ...)
		result.timings[name] = (os and os.clock and os.clock() or 0) - ts
		if ok then
			result[name] = v
		else
			result.errors[#result.errors + 1] = { section = name, error = tostring(v) }
			log("section " .. name .. " failed: " .. tostring(v))
		end
		return v
	end

	section("probe", probe)
	local m = section("meta", meta) or {}
	section("baseConfig", baseConfig)

	local okMap, gameMap = pcall(api.engine.terrain.makeMapFromGame, false, true, true, false)
	if not okMap then
		result.errors[#result.errors + 1] = { section = "makeMapFromGame", error = tostring(gameMap) }
		gameMap = nil
	end

	section("heightmap", heightmap, m)
	section("towns", towns, gameMap)
	local inds = section("industries", industries, gameMap) or {}
	section("industryTypes", industryTypes, inds)
	section("cargoTypes", cargoTypes)
	section("vehicles", vehicles)
	section("multipleUnits", repDump, api.res.multipleUnitRep, "MultipleUnit")
	section("streetTemplates", streetTemplates)
	section("bridgeTypes", repDump, api.res.bridgeTypeRep, "BridgeType")
	section("tunnelTypes", repDump, api.res.tunnelTypeRep, "BridgeType")
	section("network", network)
	-- v6: street/track graph, merged into result.network (nodeIds, nodePos, edge* arrays, templates)
	section("networkGraph", function()
		local g = netgraph.collect(m.player)
		result.network = result.network or {}
		for k, v in pairs(g) do result.network[k] = v end
		return nil
	end)
	-- v7: town buildings (demolition pricing); `buildings` is the transport-building catalogue
	section("townBuildings", townbuildings.collect, m.player)
	section("loans", loans)
	section("buildings", buildings)
	-- v9: the player's own constructions with exact params (reference for the build button)
	section("playerConstructions", function()
		return ug_require("tf3_planner_1::/tf3_planner/capture.lua").playerConstructions()
	end)

	-- v5: statistics for model calibration (lines must run before stations: it marks line stations)
	local t1 = C.gameTime()
	local ctx = {
		t1 = t1, t0 = math.max(0, t1 - C.yearMs()),
		lineStations = {},
		passengerId = 0,
	}
	pcall(function() ctx.passengerId = api.res.cargoTypeRep.getPassengerCargoTypeId() end)
	section("lines", stats.lines, ctx)
	section("stations", stats.stations, ctx)
	section("towns_stats", stats.towns, ctx)
	section("company", stats.company, ctx)
	section("industries_extra", stats.industriesExtra, ctx)
	section("finance", stats.finance, ctx)

	local status = writeOutput(result)
	local dt = (os and os.clock and os.clock() or 0) - t0
	local summary = string.format(
		"v%d towns=%d industries=%d vehicles=%d lines=%d nodes=%.0f edges=%.0f townBuildings=%.0f errors=%d time=%.1fs -> %s",
		EXPORT_VERSION,
		result.towns and #result.towns or -1,
		result.industries and #result.industries or -1,
		result.vehicles and #result.vehicles or -1,
		result.lines and #result.lines or -1,
		result.network and result.network.nodes or -1,
		result.network and result.network.edges or -1,
		result.townBuildings and result.townBuildings.count or -1,
		#result.errors, dt, tostring(status.path or status.error))
	log(summary)
	return status.path ~= nil and #result.errors == 0, summary
end

return export
