-- Flight points on the world map and the flight map at a flight master (Travel.lua). Loaded
-- after map.lua: adds a few zones to its maps, and the C_Map calls the world map uses to
-- find zones.

Enum.FlightPathFaction = { Neutral = 0, Horde = 1, Alliance = 2 }
Enum.FlightPathState = { Current = 0, Reachable = 1, Unreachable = 2 }
Enum.UIMapSystem = Enum.UIMapSystem or { World = 0, Taxi = 1, Adventure = 2, Minimap = 3 }

-- More zones, in map.lua's continent coordinates. Like the real ones, zone rectangles overlap:
-- Stormwind City lies inside Elwynn Forest's.
local zones = {
	[1413] = { name = "The Barrens", parentMapID = 1414, cont = 1, rect = { 350, 300, 550, 800 } },
	[1429] = { name = "Elwynn Forest", parentMapID = 1415, cont = 0, rect = { 380, 620, 560, 760 } },
	[1436] = { name = "Westfall", parentMapID = 1415, cont = 0, rect = { 300, 700, 420, 800 } },
}
local base = {
	GetMapInfo = C_Map.GetMapInfo,
	GetWorldPosFromMapPos = C_Map.GetWorldPosFromMapPos,
	GetMapPosFromWorldPos = C_Map.GetMapPosFromWorldPos,
}
function C_Map.GetMapInfo(id)
	local z = zones[id]
	if z then return { mapID = id, name = z.name, mapType = Enum.UIMapType.Zone, parentMapID = z.parentMapID } end
	return base.GetMapInfo(id)
end
function C_Map.GetWorldPosFromMapPos(id, pos)
	local z = zones[id]
	if not z then return base.GetWorldPosFromMapPos(id, pos) end
	local r = z.rect
	return z.cont, CreateVector2D(r[1] + pos.x * (r[3] - r[1]), r[2] + pos.y * (r[4] - r[2]))
end
function C_Map.GetMapPosFromWorldPos(cont, world, id)
	local z = zones[id]
	if not z then return base.GetMapPosFromWorldPos(cont, world, id) end
	if z.cont ~= cont then return end
	local r = z.rect
	return id, CreateVector2D((world.x - r[1]) / (r[3] - r[1]), (world.y - r[2]) / (r[4] - r[2]))
end

local MAX_MAP_ID = 3000

local function IsUnder(child, parentID, anyDepth)
	local info = C_Map.GetMapInfo(child)
	local parent = info and info.parentMapID
	while parent and parent ~= 0 do
		if parent == parentID then return true end
		if not anyDepth then return false end
		local up = C_Map.GetMapInfo(parent)
		parent = up and up.parentMapID
	end
	return false
end

function C_Map.GetMapChildrenInfo(id, mapType, allDescendants)
	local list = {}
	for child = 1, MAX_MAP_ID do
		local info = C_Map.GetMapInfo(child)
		if info and (mapType == nil or info.mapType == mapType) and IsUnder(child, id, allDescendants) then
			list[#list + 1] = info
		end
	end
	return list
end

-- Where a map position (0-1) is on another map, if it's on it.
local function OnMap(fromID, x, y, toID)
	local cont, world = C_Map.GetWorldPosFromMapPos(fromID, CreateVector2D(x, y))
	if not cont then return end
	local _, pos = C_Map.GetMapPosFromWorldPos(cont, world, toID)
	if pos and pos.x >= 0 and pos.x <= 1 and pos.y >= 0 and pos.y <= 1 then return pos end
end

local function Area(id)
	local _, a = C_Map.GetWorldPosFromMapPos(id, CreateVector2D(0, 0))
	local _, b = C_Map.GetWorldPosFromMapPos(id, CreateVector2D(1, 1))
	return a and b and math.abs((b.x - a.x) * (b.y - a.y)) or math.huge
end

-- The most specific map under id at that position (what the world map highlights), or id itself.
function C_Map.GetMapInfoAtPosition(id, x, y)
	local best, bestArea
	for _, info in ipairs(C_Map.GetMapChildrenInfo(id, nil, true)) do
		if OnMap(id, x, y, info.mapID) then
			local area = Area(info.mapID)
			if not best or area < bestArea then best, bestArea = info, area end
		end
	end
	return best or C_Map.GetMapInfo(id)
end

---------------------------------------------------------------------------
-- Taxi nodes, placed on a zone's map (0-1).
---------------------------------------------------------------------------

client.taxiNodes = {
	{ id = 23, name = "Orgrimmar, Durotar", faction = 1, map = 1454, x = 0.451, y = 0.639 },
	{ id = 25, name = "Crossroads, The Barrens", faction = 1, map = 1413, x = 0.516, y = 0.303 },
	{ id = 80, name = "Ratchet, The Barrens", faction = 0, map = 1413, x = 0.631, y = 0.371 },
	{ id = 2, name = "Stormwind, Elwynn", faction = 2, map = 1453, x = 0.663, y = 0.621 },
	{ id = 8, name = "Sentinel Hill, Westfall", faction = 2, map = 1436, x = 0.566, y = 0.526 },
}
-- known: discovered node IDs; flightMap: the open one ({ from = node ID }); unreachable: node
-- IDs with no route from there. worldMap = "zones only" makes continent maps list nothing,
-- "broken" makes it fail. oldFlightMap: only the older flight map API answers.
-- noDiscoveryFlag: like Forever's client, never mark a world-map node undiscovered.
client.taxi = { known = { [2] = true, [80] = true }, unreachable = {} }

local function NodesOn(mapID, filter)
	local list = {}
	for _, node in ipairs(client.taxiNodes) do
		local pos = OnMap(node.map, node.x, node.y, mapID)
		if pos and filter(node) then list[#list + 1] = { node = node, position = pos } end
	end
	return list
end

local function ContinentOf(mapID)
	for _ = 1, 10 do
		local info = C_Map.GetMapInfo(mapID)
		if not info then return nil end
		if info.mapType == Enum.UIMapType.Continent then return mapID end
		mapID = info.parentMapID
	end
end

-- The flight map's nodes: every node on the continent, in a fixed order.
local function FlightMapNodes()
	local open = client.taxi.flightMap
	if not open then return {} end
	local from
	for _, node in ipairs(client.taxiNodes) do
		if node.id == open.from then from = node end
	end
	return NodesOn(ContinentOf(from.map), function() return true end), from
end

local function State(node, from)
	if node.id == from.id then return Enum.FlightPathState.Current end
	return client.taxi.unreachable[node.id] and Enum.FlightPathState.Unreachable or Enum.FlightPathState.Reachable
end

C_TaxiMap = {
	GetTaxiNodesForMap = function(mapID)
		local mode = client.taxi.worldMap
		if mode == "broken" then error("GetTaxiNodesForMap: secret value") end
		local info = C_Map.GetMapInfo(mapID)
		if mode == "zones only" and info and info.mapType == Enum.UIMapType.Continent then return {} end
		local list = {}
		for i, entry in ipairs(NodesOn(mapID, function() return true end)) do
			local node = entry.node
			list[i] = {
				nodeID = node.id, name = node.name, position = entry.position, faction = node.faction,
				isUndiscovered = not client.taxi.noDiscoveryFlag and not client.taxi.known[node.id] or nil, atlasName = "TaxiNode_Neutral", textureKit = "",
			}
		end
		return list
	end,
	GetAllTaxiNodes = function(mapID)
		if client.taxi.oldFlightMap then return {} end
		local entries, from = FlightMapNodes()
		local list = {}
		for _, entry in ipairs(entries) do
			local node = entry.node
			if client.taxi.known[node.id] then
				local pos = OnMap(node.map, node.x, node.y, mapID)
				list[#list + 1] = {
					nodeID = node.id, name = node.name, position = pos, state = State(node, from), slotIndex = #list + 1,
					textureKit = "", useSpecialIcon = false, isMapLayerTransition = false,
				}
			end
		end
		return list
	end,
	ShouldMapShowTaxiNodes = function() return true end,
}

function GetTaxiMapID()
	local _, from = FlightMapNodes()
	return from and ContinentOf(from.map) or 0
end

-- The older API: a slot per node on the continent, "NONE" for undiscovered ones.
local function Slot(i)
	local entries, from = FlightMapNodes()
	local entry = entries[i]
	return entry and entry.node, from
end
function NumTaxiNodes() return #(FlightMapNodes()) end
function TaxiNodeName(i)
	local node = Slot(i)
	return node and node.name or "INVALID"
end
function TaxiNodeGetType(i)
	local node, from = Slot(i)
	if not node or not client.taxi.known[node.id] then return "NONE" end
	local state = State(node, from)
	if state == Enum.FlightPathState.Current then return "CURRENT" end
	return state == Enum.FlightPathState.Unreachable and "UNREACHABLE" or "REACHABLE"
end

function client.OpenFlightMap(fromNodeID)
	client.taxi.flightMap = { from = fromNodeID }
	client.Fire("TAXIMAP_OPENED", Enum.UIMapSystem.Taxi)
end

function client.CloseFlightMap()
	client.taxi.flightMap = nil
	client.Fire("TAXIMAP_CLOSED")
end
