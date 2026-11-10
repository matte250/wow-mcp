-- WowMCP reminders: reminders the AI sets (payload key "reminders", set_reminder tool) that fire
-- at a time or when the player reaches a zone, subzone or spot. Each fires once, with a
-- message on screen and in chat and an alarm sound; nothing is sent back to the server.

local _, ns = ...

local Reminders = {}
ns.Reminders = Reminders

local CHECK_INTERVAL = 2 -- seconds; zone changes are also checked on their events
local LATE = 120         -- a time reminder this late (e.g. due while the game was closed) says when it was due
local DEFAULT_YARDS = 30
local ALARM = { "ALARM_CLOCK_WARNING_2", 12867 } -- the game's alarm clock

local active = {} -- reminders from the payload that haven't fired, in the order sent

-- WowMCPDB.reminders: id -> { created, fired = unix time }. An entry means we've seen
-- (and announced) the reminder; ids the server no longer sends are forgotten.
local function Store()
	WowMCPDB.reminders = WowMCPDB.reminders or {}
	return WowMCPDB.reminders
end

local function Normalize(name)
	name = tostring(name or ""):lower():gsub("^the%s+", "")
	return (name:gsub("[^%w]", ""))
end

local function Same(a, b)
	return a ~= nil and b ~= nil and Normalize(a) ~= "" and Normalize(a) == Normalize(b)
end

local function Clock(at)
	return date(math.abs(at - time()) > 20 * 3600 and "%a %H:%M" or "%H:%M", at)
end

local function Describe(r)
	if r.at then return "at " .. Clock(r.at) end
	if r.near then
		return string.format("within %d yards of %s %.1f, %.1f", math.floor(r.yards + 0.5),
			ns.Escape(r.near.zone), tonumber(r.near.x) or 0, tonumber(r.near.y) or 0)
	end
	local place = r.subzone and r.zone and (r.subzone .. ", " .. r.zone) or r.subzone or r.zone
	return "in " .. ns.Escape(place)
end

---------------------------------------------------------------------------
-- Triggers
---------------------------------------------------------------------------

-- In the zone by name, or on its map or a map inside it (a city in a zone, a dungeon).
local function InZone(r)
	if Same(GetRealZoneText(), r.zone) or Same(GetZoneText(), r.zone) then return true end
	local mapID = r.mapID and C_Map.GetBestMapForUnit("player")
	for _ = 1, 10 do
		if not mapID or mapID == 0 then return false end
		if mapID == r.mapID then return true end
		local info = C_Map.GetMapInfo(mapID)
		mapID = info and info.parentMapID
	end
	return false
end

local function Due(r, now)
	if r.at then return now >= r.at end
	if r.near then
		local yards = r.loc and ns.Map.Distance(r.loc)
		return yards ~= nil and yards <= r.yards
	end
	if r.zone and not InZone(r) then return false end
	if r.subzone and not (Same(GetSubZoneText(), r.subzone) or Same(GetMinimapZoneText(), r.subzone)) then
		return false
	end
	return (r.zone or r.subzone) ~= nil
end

local function Fire(r, now)
	local entry = Store()[r.id]
	if entry then entry.fired = now end
	local text = "|cffffd100Reminder:|r " .. ns.Escape(r.text)
	if r.at and now - r.at > LATE then text = text .. " |cffaaaaaa(due " .. Clock(r.at) .. ")|r" end
	ns.ShowMessage(text, "both", "Claude", SOUNDKIT and SOUNDKIT[ALARM[1]] or ALARM[2])
end

function Reminders.Check()
	if #active == 0 then return end
	local now = time()
	local waiting = {}
	for _, r in ipairs(active) do
		-- Positions can be unavailable or hidden (instances, restricted content).
		local ok, due = pcall(Due, r, now)
		if ok and due then Fire(r, now) else table.insert(waiting, r) end
	end
	active = waiting
end

---------------------------------------------------------------------------
-- Payload
---------------------------------------------------------------------------

local function Parse(raw)
	if type(raw) ~= "table" or type(raw.id) ~= "number" or raw.text == nil then return nil end
	local r = {
		id = raw.id,
		created = raw.created,
		text = tostring(raw.text),
		at = tonumber(raw.at),
		zone = raw.zone and tostring(raw.zone),
		subzone = raw.subzone and tostring(raw.subzone),
	}
	local problem
	if r.at then
		r.zone, r.subzone = nil, nil
	elseif type(raw.near) == "table" then
		r.zone, r.subzone = nil, nil
		r.near = raw.near
		r.yards = tonumber(raw.near.yards) or DEFAULT_YARDS
		r.loc = ns.Map.Locate(raw.near.zone, tonumber(raw.near.x) or 0, tonumber(raw.near.y) or 0)
		if not r.loc then problem = raw.near.zone end
	elseif r.zone then
		local info = ns.Map.Resolve(r.zone)
		r.mapID = info and info.mapID
		if not r.mapID then problem = r.zone end
	elseif not r.subzone then
		return nil
	end
	return r, problem
end

function Reminders.Receive(list)
	local store = Store()
	local keep = {}
	active = {}
	for _, raw in ipairs(list) do
		local r, problem = Parse(raw)
		if r then
			local entry = store[r.id]
			-- Same id but another reminder: the server's state was reset and its ids started over.
			if entry and entry.created ~= r.created then entry = nil end
			if not entry then
				entry = { created = r.created }
				store[r.id] = entry
				ns.Print("Reminder set: " .. ns.Escape(r.text) .. " |cffaaaaaa(" .. Describe(r) .. ")|r")
				if problem then
					ns.Print("|cffff6060Couldn't find this zone on the map:|r " .. ns.Escape(problem)
						.. (r.near and ". This reminder can't fire." or ". It fires only if the zone name matches exactly."))
				end
			end
			keep[r.id] = true
			if not entry.fired then table.insert(active, r) end
		end
	end
	for id in pairs(store) do
		if not keep[id] then store[id] = nil end
	end
end

function Reminders.List()
	if #active == 0 then
		ns.Print("No reminders waiting.")
		return
	end
	for _, r in ipairs(active) do
		ns.Print(ns.Escape(r.text) .. " |cffaaaaaa(" .. Describe(r) .. ")|r")
	end
end

ns.RegisterReceiver("reminders", function(list)
	if type(list) == "table" then Reminders.Receive(list) end
end)

-- For list_reminders: which reminders the game has, and which of them have fired.
ns.RegisterSection(ns.Kinds.STATE, "reminders", function()
	local list = {}
	for id, entry in pairs(Store()) do
		list[#list + 1] = { id = id, fired = entry.fired }
	end
	table.sort(list, function(a, b) return a.id < b.id end)
	return list
end)

ns.RegisterCommand("reminders", "/mcp reminders - list the reminders waiting to fire", Reminders.List)

ns.OnLogin(function()
	C_Timer.NewTicker(CHECK_INTERVAL, Reminders.Check)
end)

local events = CreateFrame("Frame")
for _, event in ipairs({ "PLAYER_ENTERING_WORLD", "ZONE_CHANGED", "ZONE_CHANGED_INDOORS", "ZONE_CHANGED_NEW_AREA" }) do
	events:RegisterEvent(event)
end
events:SetScript("OnEvent", Reminders.Check)
