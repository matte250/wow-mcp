-- WowMCP requests: what the server can ask the game for. A REQUEST frame of the bell (Core.lua)
-- carries the kind and the request's id. Each feature registers the sections it contributes to a
-- kind, and the reply is { [section] = value, ... }, sent with Stream.lua under that id.

local _, ns = ...

-- Must match wow_mcp/kinds.py.
ns.Kinds = {
	STATE = 0,      -- Player.lua: location, character, professions; Stats.lua: money, XP, hearthstone
	QUESTS = 1,     -- Quests.lua
	ITEMS = 2,      -- Inventory.lua: bags, equipment, bank, mail
	REPUTATION = 3, -- Reputation.lua; Player.lua: spellbook, talents
	TRAVEL = 4,     -- Travel.lua: known flight paths
	KNOWLEDGE = 5,  -- Knowledge.lua: recipes, vendors
	STATUS = 6,     -- Status.lua: addon build, live channel, slots
	ITEM_DETAILS = 7, -- Inventory.lua: tooltip and stats of the items in the itemLookup payload
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
