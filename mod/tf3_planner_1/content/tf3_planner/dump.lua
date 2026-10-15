-- Schema-driven dumper: turns TF3 userdata (which does not support pairs) into plain
-- Lua tables, using field lists generated from the game's *.d.tl by tools/gen_schema.py.
-- Type codes: "P" primitive, "E" enum, "R:Name" record, "L:<code>" list, "M:<code>" map, "?" unknown.

local schema = ug_require "tf3_planner_1::/tf3_planner/schema.lua"

local dump = {}

local MAX_DEPTH = 14

-- short names that are ambiguous in the schema: pick the first record ending in ".Name"
local suffixCache = {}
local function bySuffix(n)
	if suffixCache[n] == nil then
		suffixCache[n] = false
		local suffix = "." .. n
		for rec in pairs(schema.records) do
			if rec:sub(-#suffix) == suffix then
				suffixCache[n] = rec
				break
			end
		end
	end
	return suffixCache[n] or nil
end

local function recordOf(v, code)
	if code and code:sub(1, 2) == "R:" then
		local r = code:sub(3)
		if schema.records[r] then return r end
	end
	-- tostring of some bound types yields "TypeName: 0x..."
	local n = tostring(v):match("^([%w_%.]+):")
	if n and n ~= "userdata" then
		if schema.records[n] then return n end
		return schema.short[n] or bySuffix(n)
	end
	return nil
end

local function elemCode(code)
	if code and (code:sub(1, 2) == "L:" or code:sub(1, 2) == "M:") then
		return code:sub(3)
	end
	return nil
end

local walk

local function walkRecord(v, rec, depth)
	local r = {}
	for field, fcode in pairs(schema.records[rec]) do
		local ok, fv = pcall(function() return v[field] end)
		if ok and fv ~= nil and type(fv) ~= "function" then
			r[field] = walk(fv, fcode, depth + 1)
		end
	end
	return r
end

local function walkIterable(v, code, depth)
	local ec = elemCode(code)
	local ok, r = pcall(function()
		local t = {}
		for k, val in pairs(v) do
			local key = k
			if type(k) ~= "number" and type(k) ~= "string" then key = tostring(k) end
			if type(val) ~= "function" then
				local c = ec
				-- untyped table holding userdata (e.g. model metadata .cargoTypeSet):
				-- guess the record from the key name, "cargoTypeSet" -> "CargoTypeSet"
				if c == nil and type(val) == "userdata" and type(k) == "string" then
					local name = k:sub(1, 1):upper() .. k:sub(2)
					local guess = schema.records[name] and name or schema.short[name]
					if guess then c = "R:" .. guess end
				end
				t[key] = walk(val, c, depth + 1)
			end
		end
		-- app.saveUserdata keeps only the 1..n array part of integer-keyed tables, so maps
		-- like {[12] = 8} (cargo id -> amount) would vanish; give them string keys instead
		local n, maxk, sparse = 0, 0, false
		for k in pairs(t) do
			n = n + 1
			if type(k) == "number" then
				if k > maxk then maxk = k end
				if k < 1 or k ~= math.floor(k) then sparse = true end
			end
		end
		if sparse or (maxk > 0 and maxk ~= n) then
			local s = {}
			for k, val in pairs(t) do s[tostring(k)] = val end
			return s
		end
		return t
	end)
	if ok then return r end
	return nil
end

walk = function(v, code, depth)
	local tv = type(v)
	if tv == "number" or tv == "string" or tv == "boolean" then return v end
	if tv == "function" or tv == "thread" or tv == "nil" then return nil end
	if depth > MAX_DEPTH then return "<depth>" end
	if code == "E" then
		return tv == "userdata" and tostring(v) or v
	end
	if tv == "table" then
		return walkIterable(v, code, depth)
	end
	-- userdata
	local rec = recordOf(v, code)
	if rec then
		return walkRecord(v, rec, depth)
	end
	local it = walkIterable(v, code, depth)
	if it and next(it) ~= nil then return it end
	return tostring(v)
end

--- Dump any value. `typeName` is a schema record name (full or short) or nil.
function dump.of(v, typeName)
	local code = nil
	if typeName then
		local full = schema.records[typeName] and typeName or schema.short[typeName]
		code = full and ("R:" .. full) or nil
	end
	return walk(v, code, 0)
end

function dump.hasType(typeName)
	return schema.records[typeName] ~= nil or schema.short[typeName] ~= nil
end

return dump
