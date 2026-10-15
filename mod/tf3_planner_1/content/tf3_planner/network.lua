-- v6: the street/track graph of the whole map (all BASE_EDGE entities: town and country
-- roads, highways, player roads and tracks, construction-internal edges of stations, depots
-- and industries). Exported compactly as flat arrays; Python side: tf3planner/network.py.
--
--   nodeIds[k]                   node entity, k = 1..N (the 0-based index k-1 is used below)
--   nodePos[3k-2 .. 3k]          x, y, z of node k (0.1 m)
--   edgeIds[j]                   edge entity, j = 1..M
--   edgeNodes[2j-1], [2j]        0-based node indices of node0 / node1
--   edgeTemplate[j]              0-based index into templates (roadTemplate resource names)
--   edgeRoadType[j]              raw BaseEdge.roadType (see roadTypeValues: STREET / TRACK)
--   edgeStruct[j], edgeTypeIndex[j]  BaseEdge.type (see structValues: NORMAL/BRIDGE/TUNNEL) and
--                                its bridge/tunnel type index
--   edgeLength[j]                BaseEdge.distance (curve length, 0.1 m)
--   edgeOwned[j]                 1 = owned by the player, 0 = town / country / nobody
--   edgeTangents[6j-5 .. 6j]     tangent0.xyz, tangent1.xyz (0.1)
--   templates[t]                 { name, id (streetTemplateRep), roadType, speed (max lane speed
--                                seen on the first edge of that template), lanes }
-- Every engine access is defensive: a failing edge is skipped and counted.
--
-- Enumeration: the engine refuses getEntitiesWithComponent / forEachEntityWithComponent for
-- BASE_EDGE ("Cannot loop over this component type"), so edges come from the street system's
-- node -> edges maps (getNode2StreetEdgeMap + getNode2TrackEdgeMap, else getNode2SegmentMap);
-- components are then read per entity with getComponent. Ownership: PLAYER_OWNED per edge
-- (or the PLAYER_OWNED entity list if the engine allows looping over it).

local C = ug_require "tf3_planner_1::/tf3_planner/common.lua"

local M = {}

local function r1(v)
	return math.floor(v * 10 + 0.5) / 10
end

local function clockFn()
	if os and os.clock then return os.clock end
	return function() return 0 end
end

local function enumValues(getTable, names)
	local r = {}
	pcall(function()
		local t = getTable()
		for _, n in ipairs(names) do
			pcall(function()
				local v = t[n]
				if v ~= nil then r[n] = C.scalar(v) end
			end)
		end
	end)
	return next(r) and r or nil
end

--- Set of entities owned by `player` from the PLAYER_OWNED entity list (nil if the engine does
--- not allow looping over that component; the caller then reads PLAYER_OWNED per edge).
local function ownedSet(player)
	if player == nil then return nil end
	local ok, list = pcall(function()
		return C.list(api.engine.getEntitiesWithComponent(C.CT("PLAYER_OWNED")))
	end)
	if not ok or type(list) ~= "table" then return nil end
	local s = {}
	for _, e in ipairs(list) do s[e] = true end
	return s
end

--- Sorted list of all edge entities (union over the street system node -> edges maps).
local function edgeList(g)
	local seen, list = {}, {}
	local function addMap(getter)
		local ok, err = pcall(function()
			local map = getter()
			if map == nil then error("nil map") end
			for _, edges in pairs(map) do
				for _, e in ipairs(C.list(edges)) do
					if not seen[e] then
						seen[e] = true
						list[#list + 1] = e
					end
				end
			end
		end)
		return ok, err
	end
	local ss = api.engine.system.streetSystem
	local okS, errS = addMap(function() return ss.getNode2StreetEdgeMap() end)
	local okT, errT = addMap(function() return ss.getNode2TrackEdgeMap() end)
	if okS or okT then
		g.enumeration = "node2street+track"
		if not okS then g.enumerationError = "street map: " .. tostring(errS) end
		if not okT then g.enumerationError = "track map: " .. tostring(errT) end
	else
		local okA, errA = addMap(function() return ss.getNode2SegmentMap() end)
		if okA then
			g.enumeration = "node2segment"
		else
			-- last resort (refused by the engine for BASE_EDGE in TF3 build 40408)
			local okE, errE = pcall(function()
				for _, e in ipairs(C.list(api.engine.getEntitiesWithComponent(C.CT("BASE_EDGE")))) do
					if not seen[e] then
						seen[e] = true
						list[#list + 1] = e
					end
				end
			end)
			if not okE then
				error("no way to enumerate edges: " .. tostring(errS) .. " | " .. tostring(errA) .. " | " .. tostring(errE))
			end
			g.enumeration = "entitiesWithComponent"
		end
	end
	pcall(table.sort, list)
	return list
end

function M.collect(player)
	local clock = clockFn()
	local t0 = clock()
	local g = {
		graphVersion = 1,
		nodeIds = {}, nodePos = {},
		edgeIds = {}, edgeNodes = {}, edgeTemplate = {}, edgeRoadType = {}, edgeStruct = {},
		edgeTypeIndex = {}, edgeLength = {}, edgeOwned = {}, edgeTangents = {},
		templates = {},
		failedEdges = 0,
	}
	g.roadTypeValues = enumValues(function() return api.type.enum.RoadType end, { "STREET", "TRACK" })
	g.structValues = enumValues(function() return api.type.enum.BaseEdgeType end, { "NORMAL", "BRIDGE", "TUNNEL" })

	local getComponent = api.engine.getComponent
	local CT_EDGE, CT_NODE, CT_OWNED = C.CT("BASE_EDGE"), C.CT("BASE_NODE"), C.CT("PLAYER_OWNED")
	local edges = edgeList(g)
	g.timeList = clock() - t0

	local owned = ownedSet(player)
	g.ownedVia = owned and "playerOwnedList" or "component"
	g.timeOwned = clock() - t0

	local nodeIdx, nNodes = {}, 0
	local tmplIdx, nTmpl = {}, 0
	local nodeIds, nodePos = g.nodeIds, g.nodePos
	local edgeIds, edgeNodes, edgeTemplate, edgeRoadType = g.edgeIds, g.edgeNodes, g.edgeTemplate, g.edgeRoadType
	local edgeStruct, edgeTypeIndex, edgeLength, edgeOwned, edgeTangents =
		g.edgeStruct, g.edgeTypeIndex, g.edgeLength, g.edgeOwned, g.edgeTangents
	local scalar = C.scalar
	-- raw enum constants: values are written as numbers; should the engine hand out non-number
	-- enum objects, they are mapped to the codes of the "...Values" tables (STREET 1 / TRACK 0,
	-- NORMAL 0 / BRIDGE 1 / TUNNEL 2) and -1 when unknown (flat arrays must not contain nil)
	local RT, BT = {}, {}
	pcall(function() RT = api.type.enum.RoadType or {} end)
	pcall(function() BT = api.type.enum.BaseEdgeType or {} end)
	local function code(v, E, names)
		if type(v) == "number" then return v end
		if v ~= nil then
			for _, nc in ipairs(names) do
				local okc, eq = pcall(function() return E[nc[1]] ~= nil and E[nc[1]] == v end)
				if okc and eq then return nc[2] end
			end
		end
		return -1
	end
	local RT_NAMES = { { "TRACK", 0 }, { "STREET", 1 } }
	local BT_NAMES = { { "NORMAL", 0 }, { "BRIDGE", 1 }, { "TUNNEL", 2 } }
	local nEdges = 0

	local function node(n, pos)
		if n == nil then error("edge without node") end
		local k = nodeIdx[n]
		if k then return k end
		if pos == nil then
			local bn = getComponent(n, CT_NODE)
			pos = bn and bn.position
		end
		-- read and round first: a failure must not leave a half-registered node
		local x, y, z = r1(pos.x), r1(pos.y), r1(pos.z)
		k = nNodes
		nNodes = nNodes + 1
		nodeIdx[n] = k
		nodeIds[nNodes] = n
		local b = 3 * k
		nodePos[b + 1], nodePos[b + 2], nodePos[b + 3] = x, y, z
		return k
	end

	local function template(be)
		local name = be.roadTemplate
		local key = name or "?"
		local k = tmplIdx[key]
		if k then return k end
		k = nTmpl
		nTmpl = nTmpl + 1
		tmplIdx[key] = k
		local t = { name = name and tostring(name) or nil, roadType = code(be.roadType, RT, RT_NAMES) }
		pcall(function()
			local id = api.res.streetTemplateRep.find(name)
			if id and id >= 0 then t.id = id end
		end)
		pcall(function()
			local best, n = 0, 0
			for _, lc in ipairs(C.list(be.laneConfigs)) do
				n = n + 1
				local s = lc.speed
				if type(s) == "number" and s > best then best = s end
			end
			t.speed, t.lanes = best, n
		end)
		g.templates[k + 1] = t
		return k
	end

	local function one(e)
		local be = getComponent(e, CT_EDGE)
		if not be then return end
		local n0, n1 = be.node0, be.node1
		local i0 = node(n0, nodeIdx[n0] == nil and be.position0 or nil)
		local i1 = node(n1, nodeIdx[n1] == nil and be.position1 or nil)
		local ti = template(be)
		local tg = { 0, 0, 0, 0, 0, 0 }
		local t0v, t1v = be.tangent0, be.tangent1
		if t0v and t1v then
			tg = { r1(t0v.x), r1(t0v.y), r1(t0v.z), r1(t1v.x), r1(t1v.y), r1(t1v.z) }
		end
		local isOwned
		if owned then
			isOwned = owned[e] and 1 or 0
		else
			local po = getComponent(e, CT_OWNED)
			isOwned = (po and po.player == player) and 1 or 0
		end
		local length = be.distance
		local roadType, struct = code(be.roadType, RT, RT_NAMES), code(be.type, BT, BT_NAMES)
		local typeIndex = be.typeIndex
		if type(typeIndex) ~= "number" then typeIndex = -1 end
		-- all reads done: append (a failure above leaves the arrays consistent)
		nEdges = nEdges + 1
		local j = nEdges
		edgeIds[j] = e
		edgeNodes[2 * j - 1], edgeNodes[2 * j] = i0, i1
		edgeTemplate[j] = ti
		edgeRoadType[j] = roadType
		edgeStruct[j] = struct
		edgeTypeIndex[j] = typeIndex
		edgeLength[j] = type(length) == "number" and r1(length) or -1
		edgeOwned[j] = isOwned
		local b = 6 * (j - 1)
		for q = 1, 6 do edgeTangents[b + q] = tg[q] end
	end

	for _, e in ipairs(edges) do
		local ok, err = pcall(one, e)
		if not ok then
			g.failedEdges = g.failedEdges + 1
			if g.firstError == nil then g.firstError = tostring(err) end
		end
	end
	g.nodes, g.edges = nNodes, nEdges
	g.seconds = clock() - t0
	return g
end

return M
