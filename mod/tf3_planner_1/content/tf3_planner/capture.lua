-- Player constructions with their exact construction params (modules, template, platforms...) and
-- transformation. Nothing in the game builds a modular station from scratch in Lua: missions and
-- tools copy the params of an existing construction. TF3 Planner does the same ("by reference"):
-- this capture shows the real format, and the build button clones a reference of the same kind.

local C = ug_require "tf3_planner_1::/tf3_planner/common.lua"
local dump = ug_require "tf3_planner_1::/tf3_planner/dump.lua"

local M = {}

--- plain Lua copy of a params value (tables recursively, userdata through the schema dumper)
function M.plain(v, depth)
	depth = depth or 0
	if depth > 8 then return "<depth>" end
	local t = type(v)
	if t == "number" or t == "string" or t == "boolean" or t == "nil" then return v end
	if t == "table" then
		local out = {}
		for k, x in pairs(v) do
			local key = (type(k) == "number") and ("#" .. string.format("%.0f", k)) or tostring(k)
			out[key] = M.plain(x, depth + 1)
		end
		return out
	end
	if t == "userdata" then
		local ok, d = pcall(dump.of, v)
		if ok and d ~= nil then return d end
		return tostring(v)
	end
	return tostring(v)
end

--- 16 numbers of a Mat4f (column-major, translation at 13..15)
function M.transf16(m)
	local out = {}
	pcall(function()
		for i = 1, 16 do out[i] = m[i] end
	end)
	if #out == 16 then return out end
	out = {}
	pcall(function()
		local cols = { m:cols(0), m:cols(1), m:cols(2), m:cols(3) }
		for _, c in ipairs(cols) do
			out[#out + 1] = c.x; out[#out + 1] = c.y; out[#out + 1] = c.z; out[#out + 1] = c.w
		end
	end)
	return out
end

--- constructions owned by the player: {id, fileName, name, transf, params, frozen?}
function M.playerConstructions()
	local res = {}
	local player = nil
	pcall(function() player = api.engine.util.getPlayer() end)
	local ents = {}
	pcall(function() ents = C.list(api.engine.getEntitiesWithComponent(C.CT("CONSTRUCTION"))) end)
	for _, e in ipairs(ents) do
		pcall(function()
			local po = C.comp(e, "PLAYER_OWNED")
			if player ~= nil and (po == nil or po.player ~= player) then return end
			local con = C.comp(e, "CONSTRUCTION")
			if not con then return end
			local fn = tostring(con.fileName or "")
			if fn:find("/industries/", 1, true) or fn:find("/town", 1, true) then return end
			res[#res + 1] = {
				id = e,
				fileName = fn,
				name = C.entityName(e),
				transf = M.transf16(con.transf),
				params = M.plain(con.params),
				pos = C.bboxCenter(e),
			}
		end)
	end
	return res
end

return M
