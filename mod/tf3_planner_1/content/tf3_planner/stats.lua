-- Export v5: statistics for calibrating the planner model (player lines, stations, town and
-- company progression, industry workers, finances). Every engine call is guarded; each
-- section returns plain Lua data (common.plain) so app.saveUserdata never sees userdata.

local dump = ug_require "tf3_planner_1::/tf3_planner/dump.lua"
local C = ug_require "tf3_planner_1::/tf3_planner/common.lua"

local comp, list, scalar, plain = C.comp, C.list, C.scalar, C.plain
local LOG_N = C.LOG_N

local stats = {}

local CARRIER_NAMES = { "ROAD", "RAIL", "TRAM", "AIR", "WATER" }
local LOAD_MODES = { "LOAD_IF_AVAILABLE", "FULL_LOAD_ANY", "FULL_LOAD_ALL", "LEGACY_UNLOAD_ONLY" }

local function carrierName(v)
	local ok, E = pcall(function() return api.type.enum.Carrier end)
	return C.enumName(v, ok and E or nil, CARRIER_NAMES)
end

local function loadModeName(v)
	local ok, E = pcall(function() return api.type.enum.LineLoadMode end)
	if not ok or not E then
		ok, E = pcall(function() return api.type.Line.LoadMode end)
	end
	return C.enumName(v, ok and E or nil, LOAD_MODES)
end

local function passengerCargoId()
	local ok, id = pcall(api.res.cargoTypeRep.getPassengerCargoTypeId)
	return ok and id or 0
end

local function player()
	local ok, p = pcall(api.engine.util.getPlayer)
	return ok and p or nil
end

--- {key -> {used, capacity}} map from the engine -> list of rows
local function usageRows(m)
	local rows = {}
	pcall(function()
		for k, u in pairs(m) do
			local row = { cargo = scalar(k) }
			pcall(function() row.used = u.used end)
			pcall(function() row.capacity = u.capacity end)
			rows[#rows + 1] = row
		end
	end)
	return rows
end

---------------------------------------------------------------------------
-- lines

local function stationOfStop(stop)
	local sg = stop.stationGroup
	local idx = stop.station
	local sgc = comp(sg, "STATION_GROUP")
	local sts = sgc and list(sgc.stations) or {}
	local st = nil
	if type(idx) == "number" then
		-- Line.Stop.station is a 0-based index into StationGroup.stations (TF2 convention)
		st = sts[idx + 1] or sts[idx]
	end
	return st, sg, #sts
end

local function vehicleInfo(v, t0, t1)
	local r = { id = v, name = C.entityName(v) }
	local tv = comp(v, "TRANSPORT_VEHICLE")
	if tv then
		pcall(function() r.carrier = carrierName(tv.carrier) end)
		pcall(function() r.state = scalar(tv.state) end)
		pcall(function() r.userStopped = tv.userStopped end)
		pcall(function() r.noPath = tv.noPath end)
		pcall(function() r.stopIndex = tv.stopIndex end)
		pcall(function() r.line = tv.line end)
		pcall(function() r.daysAtTerminal = tv.daysAtTerminal end)
		pcall(function() r.lastLineStopDeparture = tv.lastLineStopDeparture end)
		pcall(function() r.sectionTimes = list(tv.sectionTimes) end)
		pcall(function() r.lineStopDepartures = list(tv.lineStopDepartures) end)
		pcall(function()
			local cfg = tv.config
			r.capacities = list(cfg.capacities)
			r.loadSpeeds = list(cfg.loadSpeeds)
		end)
		pcall(function()
			local parts = {}
			local minPurchase, minMaint, sumMaint, n = nil, nil, 0, 0
			for _, p in ipairs(list(tv.transportVehicleConfig.vehicles)) do
				local e = {}
				pcall(function() e.modelId = p.part.modelId end)
				pcall(function() e.purchaseTime = p.purchaseTime end)
				pcall(function() e.maintenanceState = p.maintenanceState end)
				parts[#parts + 1] = e
				if type(e.purchaseTime) == "number" and (minPurchase == nil or e.purchaseTime < minPurchase) then
					minPurchase = e.purchaseTime
				end
				if type(e.maintenanceState) == "number" then
					n = n + 1
					sumMaint = sumMaint + e.maintenanceState
					if minMaint == nil or e.maintenanceState < minMaint then minMaint = e.maintenanceState end
				end
			end
			r.parts = parts
			r.purchaseTime = minPurchase
			r.maintenanceMin = minMaint
			r.maintenanceAvg = n > 0 and sumMaint / n or nil
		end)
	end
	local uv = api.engine.util.vehicle
	pcall(function() r.runningCost = uv.getRunningCost(v) end)
	pcall(function() r.depreciatedValue = uv.getDepreciatedValue(v) end)
	pcall(function() r.load = api.engine.system.simEntityAtVehicleSystem.getVehicleSimEntitiesCount(v) end)
	pcall(function()
		local vd = api.engine.system.carriageListSystem.getVehicleData(v)
		r.topSpeed = vd.topSpeed
		r.length = vd.length
	end)
	r.balance = C.balances({ v }, t0, t1)
	r.chart = C.accountChart(v, 2)
	return r
end

local function lineInfo(e, ctx)
	local r = { id = e, name = C.entityName(e) }
	local lc = comp(e, "LINE")
	local t0, t1 = ctx.t0, ctx.t1
	-- stops
	r.stops = {}
	if lc then
		pcall(function() r.transportModes = dump.of(lc.vehicleInfo.transportModes) end)
		for i, s in ipairs(list(lc.stops)) do
			local stop = {}
			pcall(function()
				stop.stationGroup = s.stationGroup
				stop.station = s.station
				stop.terminal = s.terminal
			end)
			pcall(function() stop.loadMode = loadModeName(s.loadMode) end)
			pcall(function() stop.minWaitingTime = s.minWaitingTime end)
			pcall(function() stop.maxWaitingTime = s.maxWaitingTime end)
			pcall(function()
				local st, sg, nst = stationOfStop(s)
				stop.stationEntity = st
				stop.groupStations = nst
				stop.name = C.entityName(st) or C.entityName(sg)
				stop.pos = C.bboxCenter(st) or C.bboxCenter(sg)
				if st then ctx.lineStations[st] = true end
			end)
			pcall(function()
				stop.waitingPassengers = api.engine.system.simEntityAtTerminalSystem
					.getLineStopSimEntitiesCount(e, i - 1, ctx.passengerId)
			end)
			r.stops[#r.stops + 1] = stop
		end
	end
	pcall(function() r.modesUnion = dump.of(api.engine.util.line.getLineTransportModesUnion(e)) end)
	-- vehicles
	local vs = {}
	local ok = pcall(function() vs = list(api.engine.system.transportVehicleSystem.getLineVehicles(e)) end)
	if (not ok or #vs == 0) and ctx.line2vehicles then
		vs = list(ctx.line2vehicles[e])
	end
	r.vehicles = {}
	local carriers = {}
	local firstPurchase = nil
	for _, v in ipairs(vs) do
		local vi = vehicleInfo(v, t0, t1)
		r.vehicles[#r.vehicles + 1] = vi
		if vi.carrier then carriers[vi.carrier] = true end
		if vi.purchaseTime and (firstPurchase == nil or vi.purchaseTime < firstPurchase) then
			firstPurchase = vi.purchaseTime
		end
	end
	r.carriers = {}
	for c in pairs(carriers) do r.carriers[#r.carriers + 1] = c end
	r.firstPurchaseTime = firstPurchase
	-- money over the last 12 months (calculateBalance true = income + running costs only, as the
	-- line window does); the account chart splits income and running costs per year
	r.money = {
		t0 = t0, t1 = t1,
		line = C.balances({ e }, t0, t1),
		vehicles = C.balances(vs, t0, t1),
		lineChart = C.accountChart(e, 3),
	}
	-- usage / frequency / quality
	local ul = api.engine.util.line
	pcall(function() r.capacityUsage = usageRows(ul.getLineCapacityUsages(e, false)) end)
	pcall(function() r.capacityUsageAll = usageRows(ul.getLineCapacityUsages(e, true)) end)
	pcall(function() r.maxFrequency = ul.getMaxFrequency(e) end)
	pcall(function() r.stationThroughput = ul.calcLineStationThroughput(e) end)
	pcall(function()
		r.quality = dump.of(api.engine.util.cargo.getSummarizedCargoQualityDataForLine(e),
			"UtilCargo.SummarizedCargoQualityData")
	end)
	pcall(function() r.issues = dump.of(ul.getLineIssues(e, true)) end)
	-- transported
	pcall(function() r.transportedPerYear = api.engine.util.logbook.getLogValuePerYear(e, "itemsTransported") end)
	pcall(function() r.transportedMostRecent = api.engine.util.logbook.getLogValueMostRecent(e, "itemsTransported") end)
	r.transportedChart = C.logChartDiff(e, { "itemsTransported" }, 3)
	r.logBook = C.logBook(e, LOG_N, 30, { "itemsTransported" })
	pcall(function() r.simPersons = #list(api.engine.system.simPersonSystem.getSimPersonsForLine(e)) end)
	return r
end

function stats.lines(ctx)
	local p = player()
	local es = {}
	local ok = pcall(function() es = list(api.engine.system.lineSystem.getLinesForPlayer(p)) end)
	if not ok then
		for _, e in ipairs(list(api.engine.getEntitiesWithComponent(C.CT("LINE")))) do
			if C.isPlayerOwned(e, p) ~= false then es[#es + 1] = e end
		end
	end
	pcall(function() ctx.line2vehicles = api.engine.system.transportVehicleSystem.getLine2VehicleMap() end)
	local res = {}
	for _, e in ipairs(es) do
		local okL, r = pcall(lineInfo, e, ctx)
		if not okL then r = { id = e, error = tostring(r) } end
		res[#res + 1] = r
	end
	return plain(res)
end

---------------------------------------------------------------------------
-- stations

local function terminalsInfo(st, sc)
	local res = {}
	local terms = {}
	pcall(function() terms = list(sc.terminals) end)
	local us = api.engine.util.station
	for i, t in ipairs(terms) do
		local tr = { index = i - 1 }
		for _, f in ipairs({ "capacity", "cargoLoad", "cargoUnload", "passengersLoad", "passengersUnload",
			"comfortFactor", "tag", "vehicleNodeId" }) do
			pcall(function() tr[f] = scalar(t[f]) end)
		end
		pcall(function() tr.transportModes = dump.of(t.transportModes) end)
		pcall(function()
			local u = us.calculateStationTerminalUsage(st, i - 1)
			tr.used, tr.usageCapacity = u.used, u.capacity
		end)
		res[#res + 1] = tr
	end
	return res
end

local function stationInfo(st, ctx, groups)
	local r = { id = st, name = C.entityName(st), pos = C.bboxCenter(st) }
	local p = ctx.player
	local sys = api.engine.system
	pcall(function() r.group = sys.stationGroupSystem.getStationGroup(st) end)
	pcall(function() r.construction = sys.streetConnectorSystem.getConstructionEntityForStation(st) end)
	r.playerOwned = C.isPlayerOwned(st, p)
	if r.playerOwned == nil and r.group then r.playerOwned = C.isPlayerOwned(r.group, p) end
	if r.playerOwned == nil and r.construction then r.playerOwned = C.isPlayerOwned(r.construction, p) end
	r.onPlayerLine = ctx.lineStations[st] or false
	pcall(function() r.town = sys.stationSystem.getTown(st) end)
	pcall(function() r.cargo = api.engine.util.station.isStationOfType(st, true) end)
	pcall(function() r.passengers = api.engine.util.station.isStationOfType(st, false) end)
	local sc = comp(st, "STATION")
	if sc then
		pcall(function() r.tag = scalar(sc.tag) end)
		pcall(function() r.poolCapacity = sc.pool.capacity end)
		pcall(function()
			r.catchmentAreas = {}
			for _, ca in ipairs(list(sc.catchmentAreas)) do
				r.catchmentAreas[#r.catchmentAreas + 1] = { pos = C.vec(ca.position), radius = ca.radius }
			end
		end)
		r.terminals = terminalsInfo(st, sc)
	end
	pcall(function()
		local u = api.engine.util.station.calculateStationTerminalUsage(st, -1)
		r.poolUsed, r.poolUsageCapacity = u.used, u.capacity
	end)
	pcall(function() r.usage = dump.of(api.engine.util.station.calculateStationUsage(st), "UtilStation.StationUsage") end)
	pcall(function() r.catchablesPassenger = #list(sys.catchmentAreaSystem.getStationCatchables(st, false)) end)
	pcall(function() r.catchablesCargo = #list(sys.catchmentAreaSystem.getStationCatchables(st, true)) end)
	pcall(function()
		r.lineStops = {}
		for _, ls in ipairs(list(sys.lineSystem.getLineStopsForStation(st))) do
			r.lineStops[#r.lineStops + 1] = { line = ls[1], stop = ls[2] }
		end
	end)
	local g = r.group
	if g and not groups[g] then
		local gr = { id = g, name = C.entityName(g) }
		pcall(function() gr.maintenance = api.engine.util.maintenance.calcMaintenanceForStationGroup(g) end)
		pcall(function() gr.loadedPerYear = api.engine.util.logbook.getLogValuePerYear(g, "itemsLoaded") end)
		pcall(function() gr.unloadedPerYear = api.engine.util.logbook.getLogValuePerYear(g, "itemsUnloaded") end)
		gr.logBook = C.logBook(g, LOG_N, 0, { "itemsLoaded", "itemsUnloaded" })
		pcall(function()
			local sgc = comp(g, "STATION_GROUP")
			gr.stations = list(sgc.stations)
		end)
		groups[g] = gr
	end
	return r
end

function stats.stations(ctx)
	ctx.player = ctx.player or player()
	local res = { stations = {}, groups = {} }
	local groups = {}
	for _, st in ipairs(list(api.engine.getEntitiesWithComponent(C.CT("STATION")))) do
		local ok, r = pcall(stationInfo, st, ctx, groups)
		if not ok then r = { id = st, error = tostring(r) } end
		res.stations[#res.stations + 1] = r
	end
	for _, g in pairs(groups) do res.groups[#res.groups + 1] = g end
	return plain(res)
end

---------------------------------------------------------------------------
-- towns

local LAND_USES = { "RESIDENTIAL", "COMMERCIAL", "INDUSTRIAL" }

--- nested {k1 : {k2 : {a, b}}} -> rows {k1, k2, a, b}
local function nestedRows(m, n1, n2)
	local rows = {}
	pcall(function()
		for k1, inner in pairs(m) do
			for k2, v in pairs(inner) do
				local row = { [n1] = scalar(k1), [n2] = scalar(k2) }
				pcall(function() row.a, row.b = v[1], v[2] end)
				rows[#rows + 1] = row
			end
		end
	end)
	return rows
end

local function townStateFor(ts, e)
	if type(ts) ~= "table" or type(ts.townStates) ~= "table" then return nil end
	for _, s in ipairs(ts.townStates) do
		local te = type(s) == "table" and s.townEntity
		local id = type(te) == "table" and te.entity or te
		if id == e then return s end
	end
	return nil
end

function stats.towns(ctx)
	local ut = api.engine.util.town
	local halfYear = math.floor(C.yearMs() / 2)
	local townsState = C.gameScriptState("::/game_mechanics/towns/town.gs")
	local growthState = C.gameScriptState("::/game_mechanics/towns/town_cargo.gs")
	local townUtil = C.tryRequire("::/game_mechanics/towns/town_util.tl")
	local townCargoUtil = C.tryRequire("::/game_mechanics/towns/town_cargo_util.tl")
	local LU = nil
	pcall(function() LU = api.type.enum.LandUseType end)
	local res = { towns = {} }
	pcall(function() res.town2personCapacities = plain(api.engine.system.townBuildingSystem.getTown2personCapacitiesMap()) end)
	for _, e in ipairs(list(api.engine.getEntitiesWithComponent(C.CT("TOWN")))) do
		local r = { id = e, name = C.entityName(e) }
		local ok, err = pcall(function()
			pcall(function() r.lineUsage = ut.getTownLineUsage(e) end)
			pcall(function() r.deliveriesPerLine = nestedRows(ut.getTownDeliveriesStats(e, halfYear, true, true), "line", "cargo") end)
			pcall(function() r.deliveries = nestedRows(ut.getTownDeliveriesStats(e, halfYear, false, true), "key", "cargo") end)
			r.deliveriesIntervalMs = halfYear
			pcall(function() r.happiness = dump.of(ut.getTownHappinessStats(e, 10), "UtilTown.TownHappinessStats") end)
			pcall(function() r.reachability = dump.of(ut.getTownReachability(e), "UtilTown.TownReachability") end)
			pcall(function() r.personCapacities = list(api.engine.system.townBuildingSystem.getLandUsePersonCapacities(e)) end)
			r.supplyByLandUse = {}
			for i, name in ipairs(LAND_USES) do
				pcall(function()
					local lu = (LU and LU[name] ~= nil) and LU[name] or (i - 1)
					local m = api.engine.system.townBuildingSystem.getCargoSupplyAndLimit(e, lu)
					local rows = {}
					for cargo, v in pairs(m) do
						rows[#rows + 1] = { cargo = scalar(cargo), supply = v[1], limit = v[2], n = v[3] }
					end
					r.supplyByLandUse[name] = rows
				end)
			end
			pcall(function() r.stations = list(api.engine.system.stationSystem.getStations(e)) end)
			r.logBook = C.logBook(e, LOG_N, 0)
			-- ratings / authority (towns game script)
			local ts = townStateFor(townsState, e)
			if ts then
				r.state = dump.of(ts, "TownState")
				if type(r.state) == "table" and type(r.state.eventFactors) == "table" then
					r.state.eventFactors.marketingParams = nil
				end
			end
			if townUtil and ts then
				pcall(function()
					r.ratings = {}
					for _, rating in ipairs(list(townUtil.GetRatings())) do
						pcall(function()
							r.ratings[#r.ratings + 1] = { key = rating.key, value = rating.getRating(e, ts) }
						end)
					end
				end)
				pcall(function() r.ratingPassenger = townUtil.getRatingDeliveriesPassenger(e) end)
				pcall(function() r.ratingCargo = townUtil.getRatingDeliveriesCargo(e) end)
				pcall(function() r.xpFactor = townUtil.getXpFactor(e) end)
			end
			-- level / XP (town cargo growth script)
			pcall(function()
				local td = growthState and growthState.townState and growthState.townState[e]
				if td then
					r.growth = {
						experience = td.experience, level = td.level, initialLevel = td.initialLevel,
						cargoDelivered = td.cargoDelivered,
						accumulatedExperience = plain(td.accumulatedExperience),
					}
					if townCargoUtil and td.experience then
						local lvl, frac, cur, nxt = townCargoUtil.getLevelAndFraction(td.experience)
						r.growth.levelCalc = { level = lvl, fraction = frac, expCurrent = cur, expNext = nxt }
					end
				end
			end)
		end)
		if not ok then r.error = tostring(err) end
		res.towns[#res.towns + 1] = r
	end
	return plain(res)
end

---------------------------------------------------------------------------
-- company

function stats.company(ctx)
	local p = player()
	local r = { player = p }
	local progression = C.gameScriptState("::game_mechanics/company/company_progression.gs")
		or C.gameScriptState("::/game_mechanics/company/company_progression.gs")
	pcall(function()
		local cd = progression.companyState[p]
		r.experience = cd.experience
		r.level = cd.level
		r.potentialLevel = cd.potentialLevel
		r.ticketPriceMultiplier = cd.ticketPriceMultiplier
	end)
	pcall(function() r.progressionState = dump.of(progression, "CompaniesGrowthState") end)
	local companies = C.gameScriptState("::/game_mechanics/company/company.gs")
	pcall(function()
		r.basePopulation = companies.basePopulation
		r.companiesStateVersion = companies.version
	end)
	-- world population as the game computes it (company_util.computeWorldPopulation)
	pcall(function()
		local total = 0
		for _, caps in pairs(api.engine.system.townBuildingSystem.getTown2personCapacitiesMap()) do
			total = total + (list(caps)[1] or 0)
		end
		r.worldPopulation = total
	end)
	pcall(function() r.simPersons = api.engine.system.simPersonSystem.getCount() end)
	local cpu = C.tryRequire("::/game_mechanics/company/company_progression_util.tl")
	if cpu then
		pcall(function() r.maxRank = cpu.getMaxRank() end)
		pcall(function()
			if r.basePopulation and r.experience then
				local lvl, frac, cur, nxt = cpu.getLevelAndFraction(r.basePopulation, r.experience)
				r.levelCalc = { level = lvl, fraction = frac, expCurrent = cur, expNext = nxt }
			end
		end)
		pcall(function()
			r.xpForRank = {}
			for rank = 1, (r.maxRank or 15) do
				r.xpForRank[rank] = cpu.getXpNeededForRank(r.basePopulation, rank)
			end
		end)
		pcall(function()
			r.ticketMultiplierForLevel = {}
			for lvl = 1, (r.maxRank or 15) do
				r.ticketMultiplierForLevel[lvl] = cpu.getTicketPriceMultiplierForLevel(lvl) or 1
			end
		end)
	end
	pcall(function() r.companyValue = dump.of(api.engine.util.headquarters.getCompaniesValue(), "CompanyValue") end)
	pcall(function() r.transported = dump.of(api.engine.util.headquarters.getTransportedData(), "TransportationStats") end)
	return plain(r)
end

---------------------------------------------------------------------------
-- industries (extra)

function stats.industriesExtra(ctx)
	local r = { closing = {}, industries = {} }
	pcall(function()
		for _, c in ipairs(list(api.engine.util.industry.getClosingIndustries())) do
			r.closing[#r.closing + 1] = { id = c[1], fileName = scalar(c[2]), name = C.entityName(c[1]) }
		end
	end)
	local workers = C.gameScriptState("::/industries/industry_workers.gs")
	local ipu = C.tryRequire("::/industries/industry_productivity_util.tl")
	pcall(function()
		local st = C.gameScriptState("::/game_mechanics/industries/industries.gs")
		if type(st) == "table" then
			local n = 0
			for _ in pairs(st.industryFailedExtensions or {}) do n = n + 1 end
			r.industriesScript = { toSpawn = st.toSpawn, failedExtensions = n }
		end
	end)
	local now = C.gameTime()
	for _, e in ipairs(list(api.engine.getEntitiesWithComponent(C.CT("INDUSTRY")))) do
		local ir = { id = e }
		pcall(function()
			local ws = workers and workers.industriesState and workers.industriesState[e]
			if ws then
				local last = list(ws.lastVisitors)
				ir.visitors = #last
				ir.pendingVisitors = #list(ws.pendingVisitors)
				local newest, oldest = nil, nil
				for _, v in ipairs(last) do
					if type(v.time) == "number" then
						if newest == nil or v.time > newest then newest = v.time end
						if oldest == nil or v.time < oldest then oldest = v.time end
					end
				end
				ir.visitorNewest, ir.visitorOldest = newest, oldest
			end
		end)
		if ipu then
			pcall(function() ir.workersBoosted = ipu.isIndustryWorkersBoosted(e, ir.visitors) end)
			pcall(function() ir.potentialBoost = ipu.getPersonCapacityPotentialBoost(e) end)
			pcall(function() ir.productivity = ipu.getIndustryProductivity(e) end)
		end
		pcall(function() ir.personCapacity = dump.of(comp(e, "PERSON_CAPACITY"), "Engine.Component.PersonCapacity") end)
		if type(ir.personCapacity) == "table" then
			ir.personCapacity.personNodes, ir.personCapacity.vehicleNodes = nil, nil
		end
		pcall(function() ir.workersAssigned = #list(api.engine.system.simPersonSystem.getSimPersonsForDestination(e)) end)
		pcall(function() ir.atBuilding = #list(api.engine.system.simPersonAtBuildingSystem.getSimPersonAtBuilding(e)) end)
		pcall(function() ir.productivityInfo = dump.of(api.engine.util.industry.getIndustryProductivityInfo(e)) end)
		pcall(function()
			local ic = comp(e, "INDUSTRY")
			ir.closureTimeStamp = ic.closureTimeStamp
			ir.level = ic.level
			ir.upgradeProgress = ic.upgradeProgress
			local sl = ic.stockList
			ir.logBook = C.logBook(e, LOG_N, 0, { "itemsProduced", "itemsConsumed", "itemsDestroyed", "itemsShipped" })
				or C.logBook(sl, LOG_N, 0, { "itemsProduced", "itemsConsumed", "itemsDestroyed", "itemsShipped" })
			pcall(function() ir.modifiers = dump.of(comp(sl, "STOCK_LIST").modifiers) end)
		end)
		r.industries[#r.industries + 1] = ir
	end
	r.now = now
	return plain(r)
end

---------------------------------------------------------------------------
-- finance

local JT = { "INCOME", "MAINTENANCE", "ACQUISITION", "CONSTRUCTION", "SUBSIDY", "OTHER" }
local JM = { "VEHICLE", "VEHICLE_MAINTENANCE", "INFRASTRUCTURE", "OTHER" }

local function financeTable(p, years)
	local cfg = api.type.ChartConfig.new()
	cfg.count = years
	local fd = api.engine.util.finance.computeFinanceTable(p, cfg)
	local r = {}
	for _, f in ipairs({ "header", "total", "balance", "loan", "interest", "loanBorrowing", "loanRepayment" }) do
		pcall(function() r[f] = dump.of(fd[f]) end)
	end
	local JE = nil
	pcall(function() JE = api.type.JournalEntry end)
	local function keyInfo(key)
		local k = { key = key }
		pcall(function()
			local u = fd:unfoldKey(key)
			k.type = C.enumName(u[1], JE and JE.Type, JT)
			k.maintenance = C.enumName(u[2], JE and JE.Maintenance, JM)
			k.construction = scalar(u[3])
		end)
		return k
	end
	pcall(function()
		r.transport = {}
		fd:foreach_carrier(function(carrier)
			local rows = {}
			pcall(function()
				fd:foreach_transport(function(key, values)
					local k = keyInfo(key)
					k.values = dump.of(values)
					rows[#rows + 1] = k
				end, carrier)
			end)
			r.transport[#r.transport + 1] = {
				carrier = C.enumName(carrier, JE and JE.Carrier, { "ROAD", "RAIL", "TRAM", "WATER", "AIR", "OTHER" }),
				rows = rows,
			}
		end)
	end)
	pcall(function()
		r.investment = {}
		fd:foreach_investment(function(key, values)
			local k = keyInfo(key)
			k.values = dump.of(values)
			r.investment[#r.investment + 1] = k
		end)
	end)
	pcall(function()
		r.other = {}
		fd:foreach_other(function(key, values)
			r.other[#r.other + 1] = { key = scalar(key), values = dump.of(values) }
		end)
	end)
	return r
end

function stats.finance(ctx)
	local p = player()
	local fin = api.engine.util.finance
	local t1 = C.gameTime()
	local t0 = math.max(0, t1 - C.yearMs())
	local r = { t0 = t0, t1 = t1 }
	pcall(function() r.balance = fin.getPlayersBalance(p) end)
	pcall(function() r.account = dump.of(comp(p, "ACCOUNT"), "Engine.Component.Account") end)
	pcall(function() r.earningsThisYear = fin.calculateEarnings(p) end)
	pcall(function() r.incomeSinceT0 = fin.calcIncomeSince(t0, p) end)
	pcall(function() r.firstIncomeTime = fin.getFirstIncomeTime(p) end)
	pcall(function() r.lastIncomeTime = fin.getLastIncomeTime(p) end)
	pcall(function() r.transported = dump.of(api.engine.util.headquarters.getTransportedData(), "TransportationStats") end)
	r.chart = C.accountChart(p, 3)
	local okT, ft = pcall(financeTable, p, 4)
	r.table = okT and ft or nil
	if not okT then r.tableError = tostring(ft) end
	-- balance over all player lines and vehicles
	pcall(function()
		local lines = list(api.engine.system.lineSystem.getLinesForPlayer(p))
		local vehicles = {}
		for _, l in ipairs(lines) do
			for _, v in ipairs(list(api.engine.system.transportVehicleSystem.getLineVehicles(l))) do
				vehicles[#vehicles + 1] = v
			end
		end
		r.linesBalance = C.balances(lines, t0, t1)
		r.vehiclesBalance = C.balances(vehicles, t0, t1)
		r.playerBalance = C.balances({ p }, t0, t1)
	end)
	pcall(function()
		local st = C.gameScriptState("::/game_mechanics/finance/loan.gs")
		local n, total = 0, 0
		for _, l in pairs(st.obtainedLoans or {}) do
			n = n + 1
			if type(l) == "table" and type(l.amount) == "number" then total = total + l.amount end
		end
		r.loansObtained = { count = n, amount = total }
	end)
	return plain(r)
end

return stats
