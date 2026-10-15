-- Small helpers shared by export.lua and stats.lua. Everything that touches the engine is
-- wrapped in pcall: a missing function or a changed signature must not abort a section.

local dump = ug_require "tf3_planner_1::/tf3_planner/dump.lua"

local common = {}

common.LOG_N = 24 -- keep only the last N entries of every logbook series

function common.log(msg)
	pcall(print, "[TF3P] " .. tostring(msg))
end

function common.CT(name)
	return api.type.ComponentType[name]
end

function common.comp(entity, name)
	if entity == nil then return nil end
	local ok, c = pcall(api.engine.getComponent, entity, api.type.ComponentType[name])
	if ok then return c end
	return nil
end

function common.vec(v)
	if v == nil then return nil end
	local r = {}
	for _, k in ipairs({ "x", "y", "z" }) do
		local ok, c = pcall(function() return v[k] end)
		if ok and type(c) == "number" then r[k] = c end
	end
	return next(r) and r or nil
end

function common.entityName(e)
	if e == nil then return nil end
	local ok, n = pcall(api.engine.util.getEntityName, e)
	if ok and n then return n end
	local c = common.comp(e, "NAME")
	return c and c.name or nil
end

function common.bboxCenter(e)
	local bv = common.comp(e, "BOUNDING_VOLUME")
	if not bv then return nil end
	local ok, r = pcall(function()
		local a, b = bv.bbox.min, bv.bbox.max
		return { x = (a.x + b.x) / 2, y = (a.y + b.y) / 2, z = (a.z + b.z) / 2 }
	end)
	return ok and r or nil
end

--- Engine arrays usually arrive as Lua tables; tolerate userdata vectors too.
function common.list(x)
	if type(x) == "table" then return x end
	if x == nil then return {} end
	local r = {}
	local ok = pcall(function()
		for i = 1, #x do r[i] = x[i] end
	end)
	if ok and #r > 0 then return r end
	r = {}
	pcall(function()
		for _, v in ipairs(x) do r[#r + 1] = v end
	end)
	return r
end

--- Plain value for the export: numbers/strings/booleans as is, anything else via tostring.
function common.scalar(v)
	local t = type(v)
	if t == "number" or t == "string" or t == "boolean" or t == "nil" then return v end
	return tostring(v)
end

--- Name of an enum value: compares against api.type.<path>[name] for the given names.
function common.enumName(v, enumTable, names)
	if v == nil then return nil end
	if enumTable then
		for _, n in ipairs(names) do
			local ok, eq = pcall(function() return enumTable[n] ~= nil and enumTable[n] == v end)
			if ok and eq then return n end
		end
	end
	return common.scalar(v)
end

--- Last n elements of a list (Lua table or userdata vector) and the full length.
function common.tail(t, n)
	local l = common.list(t)
	local len = #l
	local r = {}
	for i = math.max(1, len - n + 1), len do r[#r + 1] = l[i] end
	return r, len
end

function common.gameTime()
	local ok, t = pcall(function()
		return common.comp(api.engine.util.getWorld(), "GAME_TIME").gameTime
	end)
	return ok and t or 0
end

function common.yearMs()
	local ok, t = pcall(function() return api.util.getDefaultYearDuration() end)
	return (ok and t) or 1461000
end

--- Compact LOG_BOOK dump: last `n` values of every log, ref-entity logs capped at maxRefs.
-- `names` (optional) are probed directly in case name2log cannot be iterated.
function common.logBook(e, n, maxRefs, names)
	local lb = common.comp(e, "LOG_BOOK")
	if not lb then return nil end
	n = n or common.LOG_N
	local function one(log)
		local ok, r = pcall(function()
			local times, total = common.tail(log.times, n)
			local values = common.tail(log.values, n)
			return { times = times, values = values, n = total }
		end)
		return ok and r or nil
	end
	local r = { logs = {}, refs = {} }
	local okIter = pcall(function()
		for name, log in pairs(lb.name2log) do
			r.logs[tostring(name)] = one(log)
		end
	end)
	if not okIter and names then
		for _, name in ipairs(names) do
			pcall(function()
				local log = lb.name2log[name]
				if log then r.logs[name] = one(log) end
			end)
		end
	end
	if (maxRefs or 0) > 0 then
		pcall(function()
			local cnt = 0
			for ref, m in pairs(lb.refEntity2name2log) do
				cnt = cnt + 1
				if cnt > maxRefs then
					r.refsTruncated = true
					break
				end
				local logs = {}
				for name, log in pairs(m) do logs[tostring(name)] = one(log) end
				r.refs[#r.refs + 1] = { ref = ref, logs = logs }
			end
		end)
	end
	return r
end

--- State table of a game script (the GAME_SCRIPT component of its entity), or nil.
function common.gameScriptState(name)
	local ok, st = pcall(function()
		local e = api.engine.system.gameScriptSystem.getEntityForGameScript(name)
		local gs = common.comp(e, "GAME_SCRIPT")
		return gs and gs.state
	end)
	if ok then return st end
	return nil
end

--- pcall(ug_require, path) returning the module or nil (plus the error).
function common.tryRequire(path)
	local ok, m = pcall(ug_require, path)
	if ok and m then return m end
	return nil, tostring(m)
end

--- Sum of balances over entities (maintenanceIncomeOnly / per maintenance type).
-- calculateBalance returns a signed sum; the account chart gives income and costs separately.
local MAINT_TYPES = { "VEHICLE", "VEHICLE_MAINTENANCE", "INFRASTRUCTURE", "OTHER" }

function common.balances(ents, t0, t1)
	local fin = api.engine.util.finance
	local r = {}
	if #ents == 0 then return r end
	pcall(function() r.all = fin.calculateBalance(ents, t0, t1, false) end)
	pcall(function() r.maintIncome = fin.calculateBalance(ents, t0, t1, true) end)
	pcall(function()
		local M = api.type.JournalEntry.Maintenance
		local bm = {}
		for _, k in ipairs(MAINT_TYPES) do
			pcall(function()
				if M[k] ~= nil then bm[k] = fin.calculateBalance(ents, t0, t1, true, M[k]) end
			end)
		end
		if next(bm) then r.byMaintenance = bm end
	end)
	return r
end

--- Account chart (series[1] = income, series[2] = costs; series[i] = {xValues, yValues}).
function common.accountChart(e, years)
	local ok, r = pcall(function()
		local cfg = api.type.ChartConfig.new()
		cfg.count = years or 2
		local ch = api.engine.util.finance.getAccountChart(e, cfg)
		return { series = dump.of(ch.series), xRange = dump.of(ch.xRange), numPotentialGroups = ch.numPotentialGroups }
	end)
	return ok and r or nil
end

--- Logbook diff chart (per-interval increments of cumulative logs such as itemsTransported).
function common.logChartDiff(e, names, years)
	local ok, r = pcall(function()
		local cfg = api.type.ChartConfig.new()
		cfg.count = years or 2
		local ch = api.engine.util.logbook.getChartDiff(e, names, cfg)
		return { series = dump.of(ch.series), xRange = dump.of(ch.xRange), numPotentialGroups = ch.numPotentialGroups }
	end)
	return ok and r or nil
end

function common.isPlayerOwned(e, player)
	local po = common.comp(e, "PLAYER_OWNED")
	if not po then return nil end
	local ok, p = pcall(function() return po.player end)
	if not ok then return nil end
	return p == player
end

--- Deep copy into plain Lua data (userdata -> dump/tostring, functions dropped), so that
-- app.saveUserdata never sees engine objects. Integer keys that would be dropped by
-- saveUserdata (sparse / non-positive) become strings.
function common.plain(v, depth)
	depth = depth or 0
	local t = type(v)
	if t == "number" or t == "string" or t == "boolean" or t == "nil" then return v end
	if t == "function" or t == "thread" then return nil end
	if depth > 16 then return "<depth>" end
	if t == "userdata" then return dump.of(v) end
	local r, n, maxk, sparse = {}, 0, 0, false
	for k, x in pairs(v) do
		local key = k
		if type(k) ~= "number" and type(k) ~= "string" then key = tostring(k) end
		local pv = common.plain(x, depth + 1)
		if pv ~= nil then
			r[key] = pv
			n = n + 1
			if type(key) == "number" then
				if key > maxk then maxk = key end
				if key < 1 or key ~= math.floor(key) then sparse = true end
			end
		end
	end
	if sparse or (maxk > 0 and maxk ~= n) then
		local s = {}
		for k, x in pairs(r) do s[tostring(k)] = x end
		return s
	end
	return r
end

return common
