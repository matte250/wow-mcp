-- WowMCP requests: what the server can ask the game for. Request n asks for kind n % 8
-- (Core.lua's req/ doorbell). Each feature registers the sections it contributes to a kind,
-- and the reply is { [section] = value, ... }, sent with Stream.lua.

local _, ns = ...

-- Must match wow_mcp/kinds.py.
ns.Kinds = {
	STATE = 0,      -- Player.lua: location, character, professions; Stats.lua: money, XP, hearthstone
	QUESTS = 1,     -- Quests.lua
	ITEMS = 2,      -- Inventory.lua: bags, equipment, bank
	REPUTATION = 3, -- Reputation.lua
	TRAVEL = 4,     -- Travel.lua: known flight paths
	KNOWLEDGE = 5,  -- Knowledge.lua: recipes, vendors
	STATUS = 6,     -- Status.lua: addon build, live channel, slots
	-- 7 is free.
}

local sections = {} -- kind -> { [name] = collect }

-- collect() returns any JSON-serializable value. If it fails, the reply carries the error
-- under errors[name] instead (an API missing, or a secret value in restricted content).
function ns.RegisterSection(kind, name, collect)
	sections[kind] = sections[kind] or {}
	sections[kind][name] = collect
end

function ns.Answer(id, kind)
	local reply = {}
	for name, collect in pairs(sections[kind] or {}) do
		local ok, value = pcall(collect)
		if ok then
			reply[name] = value
		else
			reply.errors = reply.errors or {}
			reply.errors[name] = tostring(value)
		end
	end
	ns.Stream.Send(id, reply)
end
