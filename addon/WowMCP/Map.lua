-- WowMCP map: world map pins, the game's navigation waypoint, and chat output for
-- markers sent with the show_on_map tool.

local _, ns = ...

local Map = {}
ns.Map = Map

local PIN_TEMPLATE = "WowMCPPinTemplate"
local ICON_PATH = "Interface\\TargetingFrame\\UI-RaidTargetingIcon_%d"
local MAX_MAP_ID = 10000

local current      -- the map payload being shown
local markers = {} -- resolved markers: { index, mapID, x, y (0-1), label, note, zone, icon, continent, worldPos }
local provider
local openAfterCombat = false

local function Icon(m, size)
	return string.format("|T" .. ICON_PATH .. ":%d|t", m.icon, size or 0)
end

---------------------------------------------------------------------------
-- Zone names -> uiMapIDs
---------------------------------------------------------------------------

local function Normalize(name)
	name = tostring(name or ""):lower():gsub("^the%s+", "")
	return (name:gsub("[^%w]", ""))
end

local TYPE_RANK = {}
do
	local T = Enum and Enum.UIMapType or {}
	-- Cities are Zone maps too.
	for rank, t in ipairs({ T.Zone or 3, T.Continent or 2, T.Micro or 5, T.Dungeon or 4, T.Orphan or 6, T.World or 1, T.Cosmic or 0 }) do
		TYPE_RANK[t] = rank
	end
end

local nameIndex -- normalized name -> list of map infos

local function BuildIndex()
	nameIndex = {}
	for id = 1, MAX_MAP_ID do
		local info = C_Map.GetMapInfo(id)
		if info and info.name and info.name ~= "" then
			local key = Normalize(info.name)
			nameIndex[key] = nameIndex[key] or {}
			table.insert(nameIndex[key], info)
		end
	end
end

local function ContinentOf(mapID)
	local continentType = Enum and Enum.UIMapType and Enum.UIMapType.Continent or 2
	local guard = 0
	while mapID and mapID ~= 0 and guard < 20 do
		local info = C_Map.GetMapInfo(mapID)
		if not info then return nil end
		if info.mapType == continentType then return mapID end
		mapID = info.parentMapID
		guard = guard + 1
	end
end

local function Best(candidates)
	local playerMap = C_Map.GetBestMapForUnit("player")
	local playerContinent = playerMap and ContinentOf(playerMap)
	local best, bestKey
	for _, info in ipairs(candidates) do
		-- Lower is better: map type, then same continent as the player, then lowest id.
		local key = (TYPE_RANK[info.mapType] or 9) * 10 + ((ContinentOf(info.mapID) == playerContinent) and 0 or 1)
		if not best or key < bestKey or (key == bestKey and info.mapID < best.mapID) then
			best, bestKey = info, key
		end
	end
	return best
end

local function Resolve(zone, mapID)
	if mapID then
		local info = C_Map.GetMapInfo(mapID)
		-- Only trust an id the AI sent if it agrees with the zone name.
		if info and (not zone or Normalize(info.name) == Normalize(zone)) then return info end
	end
	local key = Normalize(zone)
	if key == "" then return nil end
	if not nameIndex then BuildIndex() end
	if nameIndex[key] then return Best(nameIndex[key]) end
	-- "Stormwind" -> "Stormwind City", "Thunder Bluff City" -> "Thunder Bluff"
	local partial = {}
	for name, infos in pairs(nameIndex) do
		if (#key >= 4 and name:sub(1, #key) == key) or (#name >= 4 and key:sub(1, #name) == name) then
			for _, info in ipairs(infos) do table.insert(partial, info) end
		end
	end
	return Best(partial)
end

---------------------------------------------------------------------------
-- Positions on other maps
---------------------------------------------------------------------------

local mapContinentCache = {}

local function MapContinent(mapID)
	if mapContinentCache[mapID] == nil then
		local ok, continent = pcall(C_Map.GetWorldPosFromMapPos, mapID, CreateVector2D(0.5, 0.5))
		mapContinentCache[mapID] = ok and continent or false
	end
	return mapContinentCache[mapID]
end

-- Where marker m sits on map mapID (0-1), if it's on that map at all.
local function Project(m, mapID)
	if m.mapID == mapID then return m.x, m.y end
	if not m.continent or MapContinent(mapID) ~= m.continent then return end
	local ok, _, pos = pcall(C_Map.GetMapPosFromWorldPos, m.continent, m.worldPos, mapID)
	if not ok or not pos then return end
	local x, y = pos.x, pos.y
	if x >= 0 and x <= 1 and y >= 0 and y <= 1 then return x, y end
end

-- Straight-line distance from the player in yards (world units), if on the same continent.
local function Distance(m)
	if not m.continent then return end
	local mapID = C_Map.GetBestMapForUnit("player")
	local pos = mapID and C_Map.GetPlayerMapPosition(mapID, "player")
	if not pos then return end
	local ok, continent, world = pcall(C_Map.GetWorldPosFromMapPos, mapID, pos)
	if not ok or continent ~= m.continent then return end
	local mx, my = m.worldPos.x, m.worldPos.y
	local px, py = world.x, world.y
	return math.sqrt((mx - px) ^ 2 + (my - py) ^ 2)
end

---------------------------------------------------------------------------
-- World map pins
---------------------------------------------------------------------------

-- Mixed into MapCanvasPinMixin once the world map code is loaded (see AttachToWorldMap).
local Pin = {}

function Pin:OnLoad()
	self:UseFrameLevelType("PIN_FRAME_LEVEL_WAYPOINT_LOCATION")
	-- For clients whose map canvas doesn't ask (see CheckMouseButtonPassthrough below).
	self:CheckMouseButtonPassthrough()
	self:SetScalingLimits(1, 1.0, 1.2)
	local font = GameFontNormalSmallOutline or GameFontNormalSmall
	if font then self.Label:SetFontObject(font) end
end

-- Right-clicks are for removing the marker, not for zooming the map out. The map canvas
-- asks this each time it hands out a pin (MapCanvasMixin:AcquirePin, after OnAcquired) and
-- by default lets right-clicks fall through to the map.
function Pin:ShouldMouseButtonBePassthrough()
	return false
end

-- Replaces MapCanvasPinMixin's version, which calls SetPassThroughButtons on every acquire:
-- that call is blocked for addons in combat, and markers can arrive in combat. Our pins never
-- pass a button through, so clearing is all there is to do, and only outside combat.
function Pin:CheckMouseButtonPassthrough()
	if self.SetPassThroughButtons and not InCombatLockdown() then pcall(self.SetPassThroughButtons, self) end
end

function Pin:OnAcquired(m, x, y, onOwnMap)
	self.marker = m
	self.Icon:SetTexture(ICON_PATH:format(m.icon))
	-- Labels only on the marker's own map, so continent maps stay readable.
	self.Label:SetText(onOwnMap and ns.ItemText(m.label) or "")
	self:SetPosition(x, y)
end

function Pin:OnMouseEnter()
	local m = self.marker
	GameTooltip:SetOwner(self, "ANCHOR_RIGHT")
	GameTooltip:SetText(Icon(m) .. " " .. ns.ItemText(m.label), 1, 0.82, 0)
	if m.note then GameTooltip:AddLine(ns.ItemText(m.note), 1, 1, 1, true) end
	GameTooltip:AddLine(string.format("%s  %.1f, %.1f", ns.Escape(m.zone), m.x * 100, m.y * 100), 0.7, 0.7, 0.7)
	local yards = Distance(m)
	if yards then GameTooltip:AddLine(string.format("%d yards away", math.floor(yards + 0.5)), 0.7, 0.7, 0.7) end
	GameTooltip:AddLine("Click to navigate here", 0.4, 0.8, 1)
	GameTooltip:AddLine("Right-click or shift-click to remove it", 0.4, 0.8, 1)
	GameTooltip:Show()
end

function Pin:OnMouseLeave()
	GameTooltip:Hide()
end

function Pin:OnMouseClickAction(button)
	if button == "RightButton" or (button == "LeftButton" and IsShiftKeyDown and IsShiftKeyDown()) then
		Map.Remove(self.marker.index)
	elseif button == "LeftButton" then
		Map.Navigate(self.marker.index)
	end
end

local function CreateProvider()
	local p = CreateFromMixins(MapCanvasDataProviderMixin)

	function p:RemoveAllData()
		self:GetMap():RemoveAllPinsByTemplate(PIN_TEMPLATE)
	end

	function p:RefreshAllData()
		self:RemoveAllData()
		local map = self:GetMap()
		local mapID = map:GetMapID()
		if not mapID then return end
		for _, m in ipairs(markers) do
			local x, y = Project(m, mapID)
			if x then map:AcquirePin(PIN_TEMPLATE, m, x, y, m.mapID == mapID) end
		end
	end

	return p
end

local function RefreshPins()
	if provider and provider:GetMap() then provider:RefreshAllData() end
end

local function AttachToWorldMap()
	if provider or not WorldMapFrame then return end
	WowMCPPinMixin = CreateFromMixins(MapCanvasPinMixin, Pin) -- referenced by Map.xml
	provider = CreateProvider()
	WorldMapFrame:AddDataProvider(provider)
end

---------------------------------------------------------------------------
-- Waypoint, map, chat
---------------------------------------------------------------------------

-- Set the game's own waypoint (map pin, minimap edge arrow, on-screen marker with
-- distance). Some maps don't allow one, so walk up to a parent map that does.
local function SetWaypoint(m)
	if not (C_Map.CanSetUserWaypointOnMap and C_Map.SetUserWaypoint and UiMapPoint) then return false end
	local mapID, x, y = m.mapID, m.x, m.y
	for _ = 1, 5 do
		if C_Map.CanSetUserWaypointOnMap(mapID) then
			local ok = pcall(C_Map.SetUserWaypoint, UiMapPoint.CreateFromCoordinates(mapID, x, y))
			if not ok then return false end
			if C_SuperTrack and C_SuperTrack.SetSuperTrackedUserWaypoint then
				pcall(C_SuperTrack.SetSuperTrackedUserWaypoint, true)
			end
			WowMCPDB.waypoint = { mapID = mapID, x = x, y = y }
			return true
		end
		local info = C_Map.GetMapInfo(mapID)
		local parent = info and info.parentMapID
		if not parent or parent == 0 then return false end
		x, y = Project(m, parent)
		if not x then return false end
		mapID = parent
	end
	return false
end

-- Remove the game waypoint, but only if it's still the one we set. Returns whether it did:
-- a route's waypoint, or one the player placed since, stays.
local function ClearOurWaypoint()
	local ours = WowMCPDB.waypoint
	WowMCPDB.waypoint = nil
	if not (ours and C_Map.HasUserWaypoint and C_Map.HasUserWaypoint()) then return false end
	local point = C_Map.GetUserWaypoint()
	if not point or point.uiMapID ~= ours.mapID then return false end
	local x, y = point.position.x, point.position.y
	if math.abs(x - ours.x) < 0.001 and math.abs(y - ours.y) < 0.001 then
		C_Map.ClearUserWaypoint()
		return true
	end
	return false
end

local function OpenOn(mapID)
	if InCombatLockdown() then
		openAfterCombat = true
		return
	end
	if WorldMapFrame and WorldMapFrame:IsShown() then
		WorldMapFrame:SetMapID(mapID)
	elseif OpenWorldMap then
		OpenWorldMap(mapID)
	end
end

-- Markers keep their number when another is removed, so look one up by it.
local function ByIndex(index)
	for _, m in ipairs(markers) do
		if m.index == index then return m end
	end
end

-- The game's waypoint is on this marker (for removing it with the marker).
local function WaypointOn(m)
	if WowMCPDB.waypoint and current then WowMCPDB.waypoint.markers, WowMCPDB.waypoint.marker = current.id, m.order end
end

local function MarkerLink(m)
	return string.format("|Haddon:WowMCP:nav:%d|h|cffffd100[%s]|r|h", m.index, ns.Escape(m.label))
end

local function MarkerLine(m)
	local line = string.format("%s %s %s %.1f, %.1f", Icon(m), MarkerLink(m), ns.Escape(m.zone), m.x * 100, m.y * 100)
	local yards = Distance(m)
	if yards then line = line .. string.format(" |cffaaaaaa(%d yd)|r", math.floor(yards + 0.5)) end
	return line
end

local function Resolved(raw, n)
	if type(raw) ~= "table" or type(raw.x) ~= "number" or type(raw.y) ~= "number" then return nil end
	local info = Resolve(raw.zone, raw.mapID)
	if not info then return nil end
	local m = {
		mapID = info.mapID,
		zone = info.name,
		x = math.min(math.max(raw.x / 100, 0), 1),
		y = math.min(math.max(raw.y / 100, 0), 1),
		label = tostring(raw.label or ("Marker " .. n)),
		note = raw.note and tostring(raw.note) or nil,
	}
	local ok, continent, worldPos = pcall(C_Map.GetWorldPosFromMapPos, m.mapID, CreateVector2D(m.x, m.y))
	if ok and continent then m.continent, m.worldPos = continent, worldPos end
	return m
end

-- Markers we've already announced (e.g. restored by a /reload) are shown quietly.
function Map.Receive(payload)
	local isNew = payload.id ~= WowMCPDB.mapId
	current = payload
	WowMCPDB.mapId = payload.id
	wipe(markers)

	local raw = type(payload.markers) == "table" and payload.markers or {}
	local missing = {}
	for i, r in ipairs(raw) do
		local m = Resolved(r, i)
		if m then
			m.order = i
			table.insert(markers, m)
		else
			table.insert(missing, ns.Escape(type(r) == "table" and r.zone or "?"))
		end
	end
	if payload.nearestFirst then
		-- Markers on another continent (no distance) go last, in the order sent.
		for _, m in ipairs(markers) do m.sortDistance = Distance(m) or math.huge end
		table.sort(markers, function(a, b)
			if a.sortDistance ~= b.sortDistance then return a.sortDistance < b.sortDistance end
			return a.order < b.order
		end)
	end
	for i, m in ipairs(markers) do
		m.index = i
		m.icon = ((i - 1) % 8) + 1
	end
	if WowMCPDB.hiddenMapId == payload.id then wipe(markers) end
	-- The ones the player removed stay removed (WowMCPDB.removedMarkers: { id, [order] = true }).
	local gone = WowMCPDB.removedMarkers
	if gone and gone.id == payload.id then
		for i = #markers, 1, -1 do
			if gone[markers[i].order] then table.remove(markers, i) end
		end
	else
		WowMCPDB.removedMarkers = nil
	end
	RefreshPins()

	if not isNew then return end

	if #raw == 0 then
		ClearOurWaypoint()
		if payload.allWaypoints and C_Map.HasUserWaypoint and C_Map.HasUserWaypoint() then
			C_Map.ClearUserWaypoint()
		end
		ns.Print(payload.allWaypoints and "Markers and waypoints cleared." or "Markers cleared.")
		return
	end
	ns.Print(payload.title and ("|cffffffff" .. ns.Escape(payload.title) .. "|r") or "New map markers:")
	for _, m in ipairs(markers) do ns.Print(MarkerLine(m)) end
	if #missing > 0 then
		ns.Print("|cffff6060Couldn't find these zones on the map:|r " .. table.concat(missing, ", "))
	end
	local first = markers[1]
	if not first then return end
	if payload.setWaypoint ~= false then
		if SetWaypoint(first) then
			WaypointOn(first)
		else
			ns.Print("Couldn't set a waypoint on " .. ns.Escape(first.zone) .. ".")
		end
	end
	if payload.openMap ~= false then OpenOn(first.mapID) end
end

function Map.Navigate(index)
	local m = ByIndex(index)
	if not m then
		ns.Print("No marker " .. tostring(index) .. ". Type /mcp to list them.")
		return
	end
	if SetWaypoint(m) then
		WaypointOn(m)
		ns.Print("Navigating to " .. MarkerLine(m))
	else
		ns.Print("Can't set a waypoint there, opening the map instead.")
	end
	OpenOn(m.mapID)
end

function Map.List()
	if #markers == 0 then
		ns.Print("No markers. Ask your AI where something is!")
		return
	end
	if current and current.title then ns.Print("|cffffffff" .. ns.Escape(current.title) .. "|r") end
	for _, m in ipairs(markers) do ns.Print(MarkerLine(m)) end
end

function Map.Open()
	if markers[1] then OpenOn(markers[1].mapID) else Map.List() end
end

function Map.Clear()
	if current then WowMCPDB.hiddenMapId = current.id end
	wipe(markers)
	ClearOurWaypoint()
	RefreshPins()
	ns.Print("Markers hidden until your AI sends new ones.")
end

-- Removes one marker (its pin, and the waypoint if it's on it) until the AI sends new ones.
function Map.Remove(index)
	index = tonumber(strtrim(tostring(index or "")))
	local m = index and ByIndex(index)
	if not m then
		ns.Print(index and ("No marker " .. index .. ". Type /mcp to list them.") or "/mcp remove <n> removes marker n; /mcp lists them.")
		return
	end
	for i, other in ipairs(markers) do
		if other == m then
			table.remove(markers, i)
			break
		end
	end
	local id = current and current.id
	local gone = WowMCPDB.removedMarkers
	if not gone or gone.id ~= id then
		gone = { id = id }
		WowMCPDB.removedMarkers = gone
	end
	gone[m.order] = true
	local ours = WowMCPDB.waypoint
	local navigating = ours and ours.markers == id and ours.marker == m.order
	-- The record can outlive the waypoint (a route step or the player replaced it), so say
	-- "and its waypoint" only when the game's waypoint really went.
	local cleared = navigating and ClearOurWaypoint()
	RefreshPins()
	ns.Print(string.format("Removed marker %s %s%s.", Icon(m), ns.Escape(m.label), cleared and " and its waypoint" or ""))
end

function Map.OnCombatEnded()
	if openAfterCombat then
		openAfterCombat = false
		if markers[1] then OpenOn(markers[1].mapID) end
	end
end

function Map.Init()
	if WorldMapFrame then
		AttachToWorldMap()
	elseif EventUtil and EventUtil.ContinueOnAddOnLoaded then
		EventUtil.ContinueOnAddOnLoaded("Blizzard_WorldMap", AttachToWorldMap)
	end
	-- Chat links: |Haddon:WowMCP:nav:<n>|h
	if EventRegistry then
		EventRegistry:RegisterCallback("SetItemRef", function(_, link)
			local index = type(link) == "string" and link:match("^addon:WowMCP:nav:(%d+)")
			if index then Map.Navigate(tonumber(index)) end
		end, Map)
	end
end

-- For other features (routes, areas, reminders): where a zone + 0-100 coordinates is, on
-- this and other maps. A location is { mapID, zone, x, y (0-1), continent, worldPos }.
function Map.Locate(zone, x, y, mapID)
	return Resolved({ zone = zone, x = x, y = y, mapID = mapID }, 1)
end
Map.Resolve = Resolve     -- zone name (+ optional uiMapID) -> map info, or nil
Map.Project = Project     -- (location, mapID) -> x, y (0-1) on that map, or nil
Map.Distance = Distance   -- location -> yards from the player, or nil (other continent, no position)
Map.SetWaypoint = SetWaypoint -- location -> true if the game's waypoint was set

ns.RegisterReceiver("map", function(payload)
	if type(payload) == "table" then Map.Receive(payload) end
end)
ns.OnLogin(Map.Init)
ns.OnItemsLoaded(RefreshPins) -- labels naming items that were still loading

ns.RegisterCommand("", "/mcp - list the current markers", Map.List)
ns.RegisterCommand("list", nil, Map.List)
ns.RegisterCommand("show", "/mcp show - open the map on the markers", Map.Open)
ns.RegisterCommand("#", "/mcp <n> - navigate to marker n", Map.Navigate)
ns.RegisterCommand("clear", "/mcp clear - hide the markers", Map.Clear)
ns.RegisterCommand("remove", "/mcp remove <n> - remove marker n (or right-click it on the map)", Map.Remove)

local combat = CreateFrame("Frame")
combat:RegisterEvent("PLAYER_REGEN_ENABLED")
combat:SetScript("OnEvent", Map.OnCombatEnded)
