-- WowMCP travel: the flight paths the character knows (request kind TRAVEL).
--
-- The world map's own flight point data (C_TaxiMap.GetTaxiNodesForMap) says which flight masters
-- the character has discovered, without a flight master open. Every flight map the player opens
-- is also cached, per character and continent, in WowMCPDB.travel: it says which known flight
-- paths are reachable from there, and stands in if the world map data isn't readable.

local _, ns = ...

local FACTIONS = { [0] = "Neutral", [1] = "Horde", [2] = "Alliance" } -- Enum.FlightPathFaction
local STATES = { [0] = "current", [1] = "reachable", [2] = "unreachable" } -- Enum.FlightPathState
local OLD_STATES = { CURRENT = "current", REACHABLE = "reachable", DISTANT = "reachable", UNREACHABLE = "unreachable" }
local AZEROTH = 947 -- where to look when the player's map is unknown (loading screens)

local problem -- why the last flight map couldn't be cached

local function Percent(v)
	return math.floor(v * 1000 + 0.5) / 10
end

local function CharacterKey()
	return UnitName("player") .. "-" .. GetRealmName()
end

-- mapID's map info and its parents', up to the top.
local function Lineage(mapID)
	local list = {}
	for _ = 1, 10 do
		local info = mapID and mapID ~= 0 and C_Map.GetMapInfo(mapID)
		if not info then break end
		list[#list + 1] = info
		mapID = info.parentMapID
	end
	return list
end

-- The zone or city a point (0-1) on mapID lies in, with its 0-100 coordinates there.
local function InZone(mapID, x, y)
	local info = C_Map.GetMapInfoAtPosition(mapID, x, y)
	-- Caves and buildings count as the zone around them.
	for _ = 1, 5 do
		if not info or info.mapType <= Enum.UIMapType.Zone then break end
		info = C_Map.GetMapInfo(info.parentMapID)
	end
	if info and info.mapID ~= mapID then
		local continent, world = C_Map.GetWorldPosFromMapPos(mapID, CreateVector2D(x, y))
		local pos
		if continent then pos = select(2, C_Map.GetMapPosFromWorldPos(continent, world, info.mapID)) end
		if pos then
			local zx, zy = pos:GetXY()
			return info.name, Percent(zx), Percent(zy)
		end
	end
	info = C_Map.GetMapInfo(mapID)
	return info and info.name, Percent(x), Percent(y)
end

local function Entry(mapID, node)
	local entry = { name = node.name }
	local ok, zone, x, y = pcall(InZone, mapID, node.position:GetXY())
	if ok then entry.zone, entry.x, entry.y = zone, x, y end
	return entry
end

-- Every continent of the player's world.
local function Continents()
	local lineage = Lineage(C_Map.GetBestMapForUnit("player"))
	local top = lineage[#lineage]
	local list = top and C_Map.GetMapChildrenInfo(top.mapID, Enum.UIMapType.Continent, true)
	if not list or #list == 0 then list = C_Map.GetMapChildrenInfo(AZEROTH, Enum.UIMapType.Continent, true) end
	return list or {}
end

-- What the world map shows: every flight master, and (where the client says so) which ones the
-- character hasn't discovered. Continent maps list all of theirs; if one doesn't, its zones are
-- asked instead. Forever's client never marks a node undiscovered, so when none is, discovery
-- isn't known from here: return the player's faction's (and neutral) flight masters as
-- locations, and leave "known" to the flight maps seen at flight masters.
local function FromWorldMap()
	local faction = UnitFactionGroup("player")
	local nodes, seen, flagged = {}, {}, false
	local function Add(mapID)
		local list = C_TaxiMap.GetTaxiNodesForMap(mapID) or {}
		for _, node in ipairs(list) do
			if not seen[node.nodeID] then
				seen[node.nodeID] = true
				local entry = Entry(mapID, node)
				entry.faction = FACTIONS[node.faction]
				nodes[#nodes + 1] = { entry = entry, undiscovered = node.isUndiscovered == true }
				flagged = flagged or node.isUndiscovered == true
			end
		end
		return #list
	end
	for _, continent in ipairs(Continents()) do
		if Add(continent.mapID) == 0 then
			for _, zone in ipairs(C_Map.GetMapChildrenInfo(continent.mapID, Enum.UIMapType.Zone) or {}) do
				Add(zone.mapID)
			end
		end
	end
	local known, undiscovered, masters = {}, {}, {}
	for _, node in ipairs(nodes) do
		local ours = node.entry.faction == "Neutral" or node.entry.faction == faction
		if not flagged then
			if ours then masters[#masters + 1] = node.entry end
		elseif not node.undiscovered then
			known[#known + 1] = node.entry
		elseif ours then
			undiscovered[#undiscovered + 1] = node.entry
		end
	end
	if not flagged then return nil, nil, masters end
	return known, undiscovered
end

-- The flight map at a flight master: every flight path the character knows on that continent,
-- and whether it can be reached from there.
local function SeeFlightMap()
	local mapID = GetTaxiMapID and GetTaxiMapID()
	if not mapID or mapID == 0 then
		for _, info in ipairs(Lineage(C_Map.GetBestMapForUnit("player"))) do
			if info.mapType == Enum.UIMapType.Continent then mapID = info.mapID end
		end
	end
	if not mapID then return end
	local nodes, from = {}, nil
	for _, node in ipairs(C_TaxiMap.GetAllTaxiNodes(mapID) or {}) do
		if not node.isMapLayerTransition then
			local entry = Entry(mapID, node)
			entry.state = STATES[node.state]
			if entry.state == "current" then from = node.name end
			nodes[#nodes + 1] = entry
		end
	end
	if #nodes == 0 then
		-- The older flight map API: names and states, no positions.
		for i = 1, NumTaxiNodes() do
			local state = OLD_STATES[TaxiNodeGetType(i)]
			if state then
				nodes[#nodes + 1] = { name = TaxiNodeName(i), state = state }
				if state == "current" then from = TaxiNodeName(i) end
			end
		end
	end
	if #nodes == 0 then return end
	local info = C_Map.GetMapInfo(mapID)
	WowMCPDB.travel = WowMCPDB.travel or {}
	local mine = WowMCPDB.travel[CharacterKey()] or {}
	WowMCPDB.travel[CharacterKey()] = mine
	mine[mapID] = { map = info and info.name, from = from, at = time(), nodes = nodes }
end

local function FlightPaths()
	local reply = { flightMapProblem = problem }
	local ok, known, undiscovered, masters = pcall(FromWorldMap)
	if ok then
		reply.known, reply.undiscovered, reply.flightMasters = known, undiscovered, masters
	else
		reply.worldMapProblem = tostring(known)
	end
	local maps = {}
	for _, seen in pairs(WowMCPDB.travel and WowMCPDB.travel[CharacterKey()] or {}) do maps[#maps + 1] = seen end
	table.sort(maps, function(a, b) return a.at > b.at end)
	reply.flightMaps = maps
	return reply
end

ns.RegisterSection(ns.Kinds.TRAVEL, "flightPaths", FlightPaths)

local events = CreateFrame("Frame")
events:RegisterEvent("TAXIMAP_OPENED")
events:SetScript("OnEvent", function()
	local ok, err = pcall(SeeFlightMap)
	problem = not ok and tostring(err) or nil
end)
