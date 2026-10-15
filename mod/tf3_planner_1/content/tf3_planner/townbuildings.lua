-- v7: town buildings (for pricing demolition along planned routes). Section `townBuildings`
-- (the key `buildings` is the transport-building catalogue). Flat arrays, b = 1..M:
--
--   ids[b]                  town building entity
--   town[b]                 town entity (-1 unknown)
--   construction[b]         construction entity (-1 unknown)
--   file[b]                 0-based index into files (construction fileName)
--   landUse[b]              0 residential, 1 commercial, 2 industrial, -1 unknown (see landUseValues)
--   capacity[b]             PERSON_CAPACITY.capacity (-1 unknown)
--   level[b], parcels[b], depth[b]   TownBuilding level (1-4), #parcels, depth in parcels
--   height[b]               building height, m (0.1)
--   bbox[6b-5 .. 6b]        min.xyz, max.xyz of BOUNDING_VOLUME (building, else its construction), 0.1 m;
--                           all 0 when unknown (then pos holds the construction origin)
--   pos[3b-2 .. 3b]         bbox centre (or construction transf translation), 0.1 m
--   timeBuilt[b]            game time (ms) the building was built (-1 unknown)
--   bulldozeSamples         removal cost probes: api.engine.util.proposal.makeProposalData on a
--                           SimpleProposal with constructionsToRemove = {construction}; computed
--                           only, NEVER sent as a command. Up to BULLDOZE_PER_CLASS buildings per
--                           (land use, level, parcels) class; `call` = the call form that worked.
--
-- Enumeration (TOWN_BUILDING loops may be refused like BASE_EDGE): townBuildingSystem
-- getTown2BuildingMap -> getPersonCapacity2townBuildingMap -> getParcel2BuildingMap ->
-- getEntitiesWithComponent(TOWN_BUILDING); `enumeration` records which one worked.

local C = ug_require "tf3_planner_1::/tf3_planner/common.lua"

local M = {}

local BULLDOZE_PROBE = true      -- set false to skip the proposal-based cost probe
local BULLDOZE_MAX_SAMPLES = 48
local BULLDOZE_MAX_SECONDS = 3.0
local BULLDOZE_PER_CLASS = 2

local function r1(v)
	return math.floor(v * 10 + 0.5) / 10
end

local function clockFn()
	if os and os.clock then return os.clock end
	return function() return 0 end
end

local LU_NAMES = { { "RESIDENTIAL", 0 }, { "COMMERCIAL", 1 }, { "INDUSTRIAL", 2 } }

--- list of {building, town|nil}; sets g.enumeration / g.enumerationErrors
local function enumerate(g)
	local tbs = nil
	pcall(function() tbs = api.engine.system.townBuildingSystem end)
	local errs = {}
	local out, seen = {}, {}
	local function add(b, t)
		if b ~= nil and not seen[b] then
			seen[b] = true
			out[#out + 1] = { b, t }
		end
	end
	local methods = {
		{ "town2BuildingMap", function()
			for town, list in pairs(tbs.getTown2BuildingMap()) do
				for _, b in ipairs(C.list(list)) do add(b, town) end
			end
		end },
		{ "personCapacity2townBuildingMap", function()
			for _, b in pairs(tbs.getPersonCapacity2townBuildingMap()) do add(b, nil) end
		end },
		{ "parcel2BuildingMap", function()
			for _, b in pairs(tbs.getParcel2BuildingMap()) do add(b, nil) end
		end },
		{ "entitiesWithComponent", function()
			for _, b in ipairs(C.list(api.engine.getEntitiesWithComponent(C.CT("TOWN_BUILDING")))) do add(b, nil) end
		end },
	}
	for _, m in ipairs(methods) do
		local ok, err = pcall(m[2])
		if ok and #out > 0 then
			g.enumeration = m[1]
			break
		end
		errs[#errs + 1] = m[1] .. ": " .. (ok and "empty" or tostring(err))
		out, seen = {}, {}
	end
	if #errs > 0 then g.enumerationErrors = errs end
	if g.enumeration == nil then
		error("no way to enumerate town buildings: " .. table.concat(errs, " | "))
	end
	table.sort(out, function(a, b) return a[1] < b[1] end)
	return out
end

local function bboxOf(e)
	local bv = C.comp(e, "BOUNDING_VOLUME")
	if not bv then return nil end
	local a, b = bv.bbox.min, bv.bbox.max
	local r = { r1(a.x), r1(a.y), r1(a.z), r1(b.x), r1(b.y), r1(b.z) }
	if r[4] < r[1] or r[5] < r[2] then return nil end
	return r
end

local function transfPos(con)
	local ok, p = pcall(function()
		local t = con.transf
		local x, y, z = t[13], t[14], t[15]
		if type(x) ~= "number" then
			local c = t:cols(3)
			x, y, z = c.x, c.y, c.z
		end
		return { r1(x), r1(y), r1(z) }
	end)
	return ok and p or nil
end

local function bulldozeProbe(g, player, classes)
	local clock = clockFn()
	local t0 = clock()
	local samples = {}
	local util = api.engine.util.proposal
	for _, s in ipairs(classes) do
		if #samples >= BULLDOZE_MAX_SAMPLES or clock() - t0 > BULLDOZE_MAX_SECONDS then break end
		local entry = { building = s.building, construction = s.construction, landUse = s.landUse,
			level = s.level, parcels = s.parcels, capacity = s.capacity }
		local ok, err = pcall(function()
			local sp = api.type.SimpleProposal.new()
			sp.constructionsToRemove = { s.construction }
			local ctx = nil
			pcall(function()
				ctx = api.type.Context.new()
				if player ~= nil then ctx.player = player end
			end)
			local pd, errs = nil, {}
			local calls = {
				{ "proposal,context", function() return util.makeProposalData(sp, ctx) end },
				{ "proposal", function() return util.makeProposalData(sp) end },
			}
			for _, c in ipairs(calls) do
				if c[1] ~= "proposal,context" or ctx ~= nil then
					local okc, r = pcall(c[2])
					if okc and r ~= nil then
						pd = r
						entry.call = c[1]
						break
					end
					errs[#errs + 1] = c[1] .. ": " .. tostring(r)
				end
			end
			if pd == nil then error(table.concat(errs, " | ")) end
			pcall(function() entry.costs = C.scalar(pd.costs) end)
			pcall(function() entry.bulldozeCost = C.scalar(pd.bulldozeCost) end)
			pcall(function()
				local es = pd.errorState
				entry.critical = es.critical
				local msgs = {}
				for _, m in ipairs(C.list(es.messages)) do msgs[#msgs + 1] = tostring(m) end
				if #msgs > 0 then entry.messages = msgs end
				local ws = {}
				for _, m in ipairs(C.list(es.warnings)) do ws[#ws + 1] = tostring(m) end
				if #ws > 0 then entry.warnings = ws end
			end)
			pcall(function() entry.collisionBuildings = #C.list(pd.collisionInfo.buildingEntities) end)
		end)
		if not ok then entry.error = tostring(err) end
		samples[#samples + 1] = entry
		-- the API is missing or refuses: do not hammer it
		if not ok and #samples == 1 then break end
	end
	g.bulldozeSeconds = clock() - t0
	return samples
end

function M.collect(player)
	local clock = clockFn()
	local t0 = clock()
	local g = {
		version = 1,
		ids = {}, town = {}, construction = {}, file = {}, landUse = {}, capacity = {}, level = {},
		parcels = {}, depth = {}, height = {}, bbox = {}, pos = {}, timeBuilt = {},
		files = {}, failed = 0,
	}
	local LU = {}
	pcall(function() LU = api.type.enum.LandUseType or {} end)
	local luValues = {}
	for _, nc in ipairs(LU_NAMES) do
		pcall(function() if LU[nc[1]] ~= nil then luValues[nc[1]] = C.scalar(LU[nc[1]]) end end)
	end
	if next(luValues) then g.landUseValues = luValues end
	local function landUseCode(v)
		if v == nil then return -1 end
		for _, nc in ipairs(LU_NAMES) do
			local ok, eq = pcall(function() return LU[nc[1]] ~= nil and LU[nc[1]] == v end)
			if ok and eq then return nc[2] end
		end
		if type(v) == "number" then return v end
		return -1
	end

	local list = enumerate(g)
	g.timeList = clock() - t0

	local getComponent = api.engine.getComponent
	local CT_TB, CT_PC, CT_CON = C.CT("TOWN_BUILDING"), C.CT("PERSON_CAPACITY"), C.CT("CONSTRUCTION")
	local scc = nil
	pcall(function() scc = api.engine.system.streetConnectorSystem end)
	local fileIdx, nFiles = {}, 0
	local n = 0
	local classes, classSeen = {}, {}

	local function num(v, d)
		if type(v) == "number" then return v end
		return d
	end

	local function one(item)
		local e, town = item[1], item[2]
		local tb = getComponent(e, CT_TB)
		local con = num(tb and tb.construction, -1)
		if con < 0 and scc then
			local ok, c = pcall(scc.getConstructionEntityForTownBuilding, e)
			if ok then con = num(c, -1) end
		end
		if town == nil and tb then town = tb.town end
		town = num(town, -1)
		local pc = getComponent(e, CT_PC)
		if pc == nil and tb and tb.personCapacity then
			pcall(function() pc = getComponent(tb.personCapacity, CT_PC) end)
		end
		local lu, cap = -1, -1
		if pc then
			lu = landUseCode(pc.type)
			cap = num(pc.capacity, -1)
		end
		local level, parcels, depth, height, built = -1, -1, -1, -1, -1
		if tb then
			level = num(tb.level, -1)
			depth = num(tb.depth, -1)
			height = num(tb.height, -1)
			if height >= 0 then height = r1(height) end
			built = num(tb.timeBuilt, -1)
			pcall(function() parcels = #C.list(tb.parcels) end)
		end
		local fname = nil
		local box = bboxOf(e)
		local cc = (con >= 0) and getComponent(con, CT_CON) or nil
		if cc then
			pcall(function() fname = cc.fileName end)
		end
		if box == nil and con >= 0 then box = bboxOf(con) end
		local pos
		if box then
			pos = { r1((box[1] + box[4]) / 2), r1((box[2] + box[5]) / 2), r1((box[3] + box[6]) / 2) }
		else
			pos = cc and transfPos(cc) or nil
			if pos == nil then error("no position") end
			box = { 0, 0, 0, 0, 0, 0 }
		end
		local key = fname and tostring(fname) or "?"
		local fi = fileIdx[key]
		if fi == nil then
			fi = nFiles
			nFiles = nFiles + 1
			fileIdx[key] = fi
			g.files[nFiles] = key
		end
		-- append (all reads done)
		n = n + 1
		g.ids[n], g.town[n], g.construction[n], g.file[n] = e, town, con, fi
		g.landUse[n], g.capacity[n], g.level[n] = lu, cap, level
		g.parcels[n], g.depth[n], g.height[n], g.timeBuilt[n] = parcels, depth, height, built
		local b6, b3 = 6 * (n - 1), 3 * (n - 1)
		for q = 1, 6 do g.bbox[b6 + q] = box[q] end
		for q = 1, 3 do g.pos[b3 + q] = pos[q] end
		local ck = lu .. ":" .. level .. ":" .. parcels
		local seenN = classSeen[ck] or 0
		if con >= 0 and seenN < BULLDOZE_PER_CLASS then
			classSeen[ck] = seenN + 1
			classes[#classes + 1] = { building = e, construction = con, landUse = lu, level = level,
				parcels = parcels, capacity = cap, rep = seenN }
		end
	end

	for _, item in ipairs(list) do
		local ok, err = pcall(one, item)
		if not ok then
			g.failed = g.failed + 1
			if g.firstError == nil then g.firstError = tostring(err) end
		end
	end
	g.count = n
	g.timeCollect = clock() - t0

	-- first one building of every class, then the second ones (the sample cap cuts the repeats)
	table.sort(classes, function(a, b)
		if a.rep ~= b.rep then return a.rep < b.rep end
		if a.landUse ~= b.landUse then return a.landUse < b.landUse end
		if a.level ~= b.level then return a.level < b.level end
		if a.parcels ~= b.parcels then return a.parcels < b.parcels end
		return a.building < b.building
	end)
	if BULLDOZE_PROBE and #classes > 0 then
		local ok, s = pcall(bulldozeProbe, g, player, classes)
		if ok then g.bulldozeSamples = s else g.bulldozeError = tostring(s) end
	end
	g.seconds = clock() - t0
	return g
end

return M
