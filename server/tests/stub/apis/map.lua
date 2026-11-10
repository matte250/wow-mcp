-- C_Map, waypoints and the world map canvas (Map.lua).

-- Two continents; every map is an axis-aligned rectangle in its continent's coordinates.
local maps = {
	[1414] = { name = "Kalimdor", mapType = 2, parentMapID = 947, cont = 1, rect = { 0, 0, 1000, 1000 } },
	[1411] = { name = "Durotar", mapType = 3, parentMapID = 1414, cont = 1, rect = { 500, 300, 700, 600 } },
	[1454] = { name = "Orgrimmar", mapType = 3, parentMapID = 1414, cont = 1, rect = { 520, 300, 600, 380 } },
	[1415] = { name = "Eastern Kingdoms", mapType = 2, parentMapID = 947, cont = 0, rect = { 0, 0, 1000, 1000 } },
	[1453] = { name = "Stormwind City", mapType = 3, parentMapID = 1415, cont = 0, rect = { 400, 600, 480, 680 } },
	[947] = { name = "Azeroth", mapType = 1, parentMapID = 0 },
}

client.waypoint = nil
C_Map = {
	GetMapInfo = function(id)
		local m = maps[id]
		return m and { mapID = id, name = m.name, mapType = m.mapType, parentMapID = m.parentMapID }
	end,
	GetBestMapForUnit = function() return client.player.map end,
	GetPlayerMapPosition = function(id) return id == client.player.map and CreateVector2D(client.player.x, client.player.y) or nil end,
	GetWorldPosFromMapPos = function(id, pos)
		local m = maps[id]
		if not m or not m.rect then return end
		local r = m.rect
		return m.cont, CreateVector2D(r[1] + pos.x * (r[3] - r[1]), r[2] + pos.y * (r[4] - r[2]))
	end,
	GetMapPosFromWorldPos = function(cont, world, id)
		local m = maps[id]
		if not m or not m.rect or m.cont ~= cont then return end
		local r = m.rect
		return id, CreateVector2D((world.x - r[1]) / (r[3] - r[1]), (world.y - r[2]) / (r[4] - r[2]))
	end,
	CanSetUserWaypointOnMap = function(id) return maps[id] ~= nil and maps[id].mapType == 3 end,
	SetUserWaypoint = function(point) client.waypoint = point return true end,
	HasUserWaypoint = function() return client.waypoint ~= nil end,
	GetUserWaypoint = function() return client.waypoint end,
	ClearUserWaypoint = function() client.waypoint = nil end,
}
C_SuperTrack = { SetSuperTrackedUserWaypoint = function(on) client.superTracked = on end }
UiMapPoint = { CreateFromCoordinates = function(id, x, y) return { uiMapID = id, position = CreateVector2D(x, y) } end }

-- World map canvas.
MapCanvasDataProviderMixin = {
	OnAdded = function(self, map) self.owningMap = map end,
	GetMap = function(self) return self.owningMap end,
}
MapCanvasPinMixin = {
	UseFrameLevelType = function() end,
	SetScalingLimits = function() end,
	SetPosition = function(self, x, y) self.x, self.y = x, y end,
}

local function FontString()
	return { SetFontObject = function() end, SetText = function(self, t) self.text = t end }
end

WorldMapFrame = { providers = {}, pins = {}, mapID = 1411, shown = false }
function WorldMapFrame:AddDataProvider(p) table.insert(self.providers, p) p:OnAdded(self) end
function WorldMapFrame:GetMapID() return self.mapID end
function WorldMapFrame:IsShown() return self.shown end
function WorldMapFrame:SetMapID(id)
	self.mapID = id
	for _, p in ipairs(self.providers) do p:RefreshAllData() end
end
function WorldMapFrame:RemoveAllPinsByTemplate() wipe(self.pins) end
function WorldMapFrame:AcquirePin(template, ...)
	assert(template == "WowMCPPinTemplate")
	local pin = CreateFromMixins(_G[template:gsub("Template$", "Mixin")])
	pin.Icon = { SetTexture = function(self, t) self.texture = t end }
	pin.Label = FontString()
	pin:OnLoad()
	pin:OnAcquired(...)
	table.insert(self.pins, pin)
	return pin
end
function OpenWorldMap(id)
	table.insert(client.opened, id)
	WorldMapFrame.shown = true
	WorldMapFrame:SetMapID(id)
end
